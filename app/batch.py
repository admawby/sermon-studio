"""Batch processing: re-run past service recordings through the normal pipeline, review the results,
then apply them (NAS / WordPress / voice profiles) in one go.

State lives in data/batch.json. Recordings are processed one at a time through the normal job queue,
so a new Sunday recording found by the watcher simply goes in between two batch items.
"""
import html
import json
import logging
import re
import shutil
import tempfile
import threading
import time
import urllib.request
import wave
from datetime import date, timedelta
from pathlib import Path

from . import archive, audio, config, jobs, voice
from .wordpress import wp

log = logging.getLogger("sermon.batch")
STATE = config.DATA / "batch.json"
POSTS = config.DATA / "wp_posts.json"
_lock = threading.Lock()
_thread = None
actions = {}  # set by main.py: save_cover, encode, publish, archive_files, meta, notify

DEFAULT_OPTIONS = {
    "cut": "hand",            # "hand": keep my hand-made cut where there is one | "ai"
    "teach": True,            # learn voices from approved results
    "nas_mp3": True,          # save MP3s next to recordings
    "existing": "keep",       # hand-made MP3 already there: "keep" (save as (2)) | "replace" | "skip"
    "nas_slide": True,
    "wp_speaker": False, "wp_description": False, "wp_audio": False, "wp_drafts": False,
    "order": "newest",
    "discord": True,
    "free_space": True,
}


# --- state ---------------------------------------------------------------------------
def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {"status": "idle", "items": [], "options": DEFAULT_OPTIONS}


def save(st):
    with _lock:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(STATE)


_state = None


def state():
    global _state
    if _state is None:
        _state = load()
    return _state


def _set(**kw):
    state().update(kw)
    save(state())


def _item(i, **kw):
    state()["items"][i].update(kw)
    save(state())


# --- WordPress posts (cached) --------------------------------------------------------------
def wp_posts(refresh=False):
    """All published sermon posts: id, title, date, speaker, series, link, mp3, image, service date."""
    if POSTS.exists() and not refresh:
        return json.loads(POSTS.read_text())
    posts, page = [], 1
    while True:
        p = wp.admin("GET", f"edit.php?post_type={config.WP_POST_TYPE}&post_status=publish&orderby=date&order=desc&paged={page}").text
        rows = re.findall(r'<tr id="post-(\d+)"(.*?)</tr>', p, re.S)
        if not rows:
            break
        for pid, row in rows:
            cell = lambda name: (lambda m: re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", " ", m.group(1)))).strip() if m else "")(
                re.search(rf"<td class=['\"][^'\"]*{name}[^'\"]*['\"][^>]*>(.*?)</td>", row, re.S))
            title = re.search(r'class="row-title[^"]*"[^>]*>(.*?)</a>', row, re.S)
            d = re.search(r"(\d{4})/(\d{2})/(\d{2})", cell("column-date"))
            link = re.search(r'href="(https?://[^"]+/episode/[^"]+)"', row)
            series = [s.strip() for s in cell("taxonomy-podcast-category").split(",")
                      if s.strip() and s.strip().lower() != config.WP_ALWAYS_CATEGORY.lower()]
            posts.append({"id": int(pid), "title": html.unescape(re.sub("<[^>]+>", "", title.group(1))).strip() if title else "",
                          "date": f"{d[1]}-{d[2]}-{d[3]}" if d else None, "speaker": cell("taxonomy-people"),
                          "series": series[0] if series else "", "link": link.group(1) if link else None})
        page += 1
        if page > 100:
            break
    def episode(p):  # the episode page has the MP3 (its file name holds the service date) and the slide
        p["mp3"] = p["image"] = None
        if p["link"]:
            try:
                h = urllib.request.urlopen(p["link"], timeout=30).read().decode("utf-8", "ignore")
                m = re.search(r'https://[^"\'\s<>]+\.mp3', h)
                p["mp3"] = m.group(0) if m else None
                m = re.search(r'<meta property="og:image" content="([^"]+)"', h)
                p["image"] = m.group(1) if m else None
            except Exception:
                pass
        m = re.search(r"(20\d\d)(\d\d)(\d\d)", Path(p["mp3"] or "").name)
        p["service_date"] = f"{m[1]}-{m[2]}-{m[3]}" if m else None
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(episode, posts))
    POSTS.write_text(json.dumps(posts))
    return posts


# --- what's on the NAS ---------------------------------------------------------------------
def _duration(path: Path):
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return None


def _match_post(d: date, posts, used):
    exact = [p for p in posts if p.get("service_date") == d.isoformat() and p["id"] not in used]
    if exact:
        return exact[0]
    near = [p for p in posts if p["date"] and p["id"] not in used and not p.get("service_date")
            and 0 <= (date.fromisoformat(p["date"]) - d).days <= 7]
    return min(near, key=lambda p: date.fromisoformat(p["date"]), default=None)


def scan(root: Path, root_virtual: str, virtual_of):
    """Every service recording in the archive, newest first, with its MP3 and WordPress post."""
    posts = wp_posts() if wp.configured else []
    out, used = [], set()
    wavs = sorted((p for p in root.rglob("*") if p.suffix.lower() in config.WATCH_EXT and not p.name.startswith(".")),
                  key=lambda p: jobs.recording_date(p), reverse=True)
    for w in wavs:
        d = date.fromisoformat(jobs.recording_date(w))
        mp3s = [m for m in w.parent.iterdir() if m.suffix.lower() == ".mp3" and not m.name.startswith(".")]
        hand = [m for m in mp3s if "(" not in m.name and ".original" not in m.name]
        post = _match_post(d, posts, used)
        if post:
            used.add(post["id"])
        jid = jobs.job_id(w)
        out.append({"date": d.isoformat(), "path": str(w), "virtual": virtual_of(w), "name": w.name,
                    "duration": _duration(w), "size": w.stat().st_size,
                    "mp3": (max(hand, key=lambda m: m.stat().st_size).name if hand else None),
                    "post": post, "job": jid if (config.WORK / jid / "state.json").exists() else None})
    return out


# --- run -------------------------------------------------------------------------------------
def start(selected: list, options: dict):
    global _thread
    if state().get("status") in ("running", "paused"):
        raise RuntimeError("A batch is already running")
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    items = sorted(selected, key=lambda x: x["date"], reverse=opts["order"] == "newest")
    for it in items:
        it.update(state="queued", error=None, approved=False, flags=[], applied=None)
    _set(status="running", options=opts, items=items, started=time.time(), finished=None, current=None,
         apply=None, stop=False, pause=False)
    _thread = threading.Thread(target=_run, daemon=True, name="batch")
    _thread.start()


def control(action):
    st = state()
    if action == "pause" and st["status"] == "running":
        _set(status="paused", pause=True)
    elif action == "resume" and st["status"] == "paused":
        _set(status="running", pause=False)
    elif action == "stop" and st["status"] in ("running", "paused"):
        _set(stop=True, pause=False, status="stopping")
        cur = st.get("current")
        if cur is not None and st["items"][cur].get("job"):
            j = jobs.get(st["items"][cur]["job"])
            if j:
                jobs.cancel(j)


def resume_after_restart():
    """Continue a batch that was running when the container stopped."""
    global _thread
    st = state()
    if st.get("status") in ("running", "stopping"):
        for it in st["items"]:
            if it["state"] == "running":
                it["state"] = "queued"
        _set(status="running" if st["status"] == "running" else "done", stop=False)
        if st["status"] == "running":
            _thread = threading.Thread(target=_run, daemon=True, name="batch")
            _thread.start()


def _run():
    st = state()
    try:
        for i, it in enumerate(st["items"]):
            if it["state"] != "queued":
                continue
            while state().get("pause") and not state().get("stop"):
                time.sleep(2)
            if state().get("stop"):
                break
            _set(current=i)
            _item(i, state="running", started=time.time())
            try:
                _process(i)
                _item(i, state="done", finished=time.time())
            except Exception as e:
                log.exception("batch item %s failed", it["name"])
                _item(i, state="error", error=str(e), finished=time.time())
        for it in st["items"]:
            if it["state"] in ("queued", "running"):
                it["state"] = "skipped"
        _set(status="done", current=None, finished=time.time())
    except Exception:
        log.exception("batch crashed")
        _set(status="done", current=None, finished=time.time())
    if st["options"].get("discord"):
        try:
            done = sum(1 for x in st["items"] if x["state"] == "done")
            look = sum(1 for x in st["items"] if x["state"] == "done" and x["flags"])
            err = sum(1 for x in st["items"] if x["state"] == "error")
            actions["notify"](f"⚡ **Batch finished:** {done} sermons processed"
                              + (f", {look} need a look" if look else "") + (f", {err} failed" if err else "")
                              + ". Review them in Sermon Studio → Batch.")
        except Exception:
            log.exception("batch: discord summary failed")


def _process(i):
    it = state()["items"][i]
    src = Path(it["path"])
    job = jobs.create(src, it["virtual"])
    jobs.update(job, batch=True)
    _item(i, job=job["id"])
    while True:  # wait for the analysis (the normal queue: a new Sunday recording can go first)
        job = jobs.get(it["job"])
        if job["status"] in jobs.TERMINAL:
            break
        if state().get("stop"):
            jobs.cancel(job)
        time.sleep(2)
    if job["status"] != "ready":
        raise RuntimeError(job.get("message") or job["status"])
    work = jobs.workdir(job["id"])
    # where your hand-made MP3 sits in the recording
    hand = None
    if it.get("mp3"):
        try:
            hand = audio.locate(work / "speech.wav", src.parent / it["mp3"])
        except Exception:
            log.exception("couldn't locate %s", it["mp3"])
    # the post's slide becomes the cover
    post = it.get("post") or {}
    if post.get("image") and not job.get("cover"):
        try:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / ("slide" + (Path(post["image"].split("?")[0]).suffix or ".png"))
                urllib.request.urlretrieve(post["image"], p)
                actions["save_cover"](job, p)
        except Exception:
            log.exception("couldn't fetch the slide for %s", it["name"])
    ai = {"start": job["proposal"]["start"], "end": job["proposal"]["end"]}
    use = hand if (hand and state()["options"]["cut"] == "hand") else ai
    cut = build_cut(job, post, use)
    jobs.update(job, cut=cut)
    flags = _flags(job, post, hand, ai, it)
    if state()["options"].get("free_space"):
        (work / "speech.wav").unlink(missing_ok=True)
    _item(i, hand=hand, ai=ai, flags=flags, approved=not flags,
          diff=({"start": ai["start"] - hand["start"], "end": ai["end"] - hand["end"]} if hand else None))


def build_cut(job, post, span):
    meta = actions["meta"]()
    sug = job.get("suggest") or {}
    series_id, series_name = meta.get("default_series"), ""
    if post.get("series"):
        m = next((s for s in meta.get("series", []) if s["name"].lower() == post["series"].lower()), None)
        series_id = m["id"] if m else series_id
    series_name = next((s["name"] for s in meta.get("series", []) if s["id"] == series_id), "")
    return {
        "start": span["start"], "end": span["end"],
        "title": post.get("title") or sug.get("title") or f"Sermon {job['date']}",
        "speaker": sug.get("speaker") or post.get("speaker") or meta.get("default_speaker", ""),
        "series": str(series_id or ""), "series_name": series_name,
        "scripture": sug.get("scripture", ""), "description": sug.get("description", ""),
        "date": job["date"], "bitrate": config.MP3_BITRATE, "normalize": True,
    }


def _flags(job, post, hand, ai, it):
    f = []
    sug = job.get("suggest") or {}
    if not post:
        f.append("no_post")
    if not sug.get("speaker"):
        f.append("unknown_voice")
    elif post.get("speaker") and sug["speaker"] != post["speaker"]:
        f.append("speaker_differs")
    if it.get("mp3") and not hand:
        f.append("hand_not_found")
    if hand and (abs(ai["start"] - hand["start"]) > 30 or abs(ai["end"] - hand["end"]) > 30):
        f.append("edges_differ")
    return f


def on_job_event(job, event):
    """A batch item's analysis was re-run from the review: refresh its proposal, cut and flags."""
    if event != "ready" or not job.get("batch"):
        return
    for i, it in enumerate(state()["items"]):
        if it.get("job") == job["id"] and it["state"] in ("done", "error"):
            ai = {"start": job["proposal"]["start"], "end": job["proposal"]["end"]}
            hand = it.get("hand")
            use = hand if (hand and state()["options"]["cut"] == "hand") else ai
            jobs.update(job, cut=build_cut(job, it.get("post") or {}, use))
            _item(i, state="done", error=None, ai=ai, flags=_flags(job, it.get("post") or {}, hand, ai, it),
                  diff=({"start": ai["start"] - hand["start"], "end": ai["end"] - hand["end"]} if hand else None))


def view():
    """State plus what the review table needs from each item's job."""
    st = state()
    out = {k: v for k, v in st.items() if k != "items"}
    items = []
    for i, it in enumerate(st["items"]):
        x = {k: it.get(k) for k in ("date", "name", "virtual", "duration", "mp3", "post", "job", "state", "error",
                                    "flags", "approved", "hand", "ai", "diff", "applied", "edited")}
        x["i"] = i
        j = jobs.get(it["job"]) if it.get("job") else None
        if j:
            s = j.get("suggest") or {}
            x["job_status"], x["progress"], x["message"] = j["status"], j.get("progress"), j.get("message")
            x["cut"], x["cover"] = j.get("cut"), j.get("cover")
            x["speaker_source"], x["speaker_detail"] = s.get("speaker_source"), s.get("speaker_detail")
            x["suggest_speaker"] = s.get("speaker")
        items.append(x)
    out["items"] = items
    return out


def clear():
    if state().get("status") in ("running", "paused", "stopping"):
        raise RuntimeError("Stop the batch first")
    _set(status="idle", items=[], apply=None, current=None)


# --- review ---------------------------------------------------------------------------------
def update_item(i, fields: dict, approved=None):
    it = state()["items"][i]
    job = jobs.get(it["job"])
    cut = {**(job.get("cut") or {}), **{k: v for k, v in fields.items() if k in (
        "start", "end", "title", "speaker", "series", "series_name", "scripture", "description", "date")}}
    jobs.update(job, cut=cut)
    if approved is not None:
        _item(i, approved=approved, edited=True)


def reset_item(i):
    it = state()["items"][i]
    job = jobs.get(it["job"])
    use = it["hand"] if (it.get("hand") and state()["options"]["cut"] == "hand") else it["ai"]
    jobs.update(job, cut=build_cut(job, it.get("post") or {}, use))


def approve(indices, value=True):
    for i in indices:
        state()["items"][i]["approved"] = value
    save(state())


# --- apply ----------------------------------------------------------------------------------
def plan():
    st, o = state(), state()["options"]
    ok = [it for it in st["items"] if it["state"] == "done" and it.get("approved")]
    speaker_changes, desc, drafts, mp3s, slides, kept = [], 0, 0, 0, 0, 0
    learn = {}
    for it in ok:
        job = jobs.get(it["job"]) or {}
        cut, post = job.get("cut") or {}, it.get("post") or {}
        if o["nas_mp3"] and not (it.get("mp3") and o["existing"] == "skip"):
            mp3s += 1
            kept += 1 if it.get("mp3") and o["existing"] == "keep" else 0
        if o["nas_slide"] and job.get("cover"):
            slides += 1
        if post and o["wp_speaker"] and cut.get("speaker") and cut["speaker"] != post.get("speaker"):
            speaker_changes.append({"date": it["date"], "title": post.get("title"), "now": post.get("speaker"),
                                    "new": cut["speaker"], "why": _why(job)})
        if post and o["wp_description"] and cut.get("description"):
            desc += 1  # (only posts that turn out to have none are changed)
        if not post and o["wp_drafts"]:
            drafts += 1
        if o["teach"] and job.get("voice") and cut.get("speaker"):
            learn[cut["speaker"]] = learn.get(cut["speaker"], 0) + 1
    return {"approved": len(ok), "mp3s": mp3s, "kept": kept, "slides": slides, "speaker_changes": speaker_changes,
            "descriptions": desc, "drafts": drafts, "audio": sum(1 for it in ok if it.get("post")) if o["wp_audio"] else 0,
            "learn": learn, "options": o}


def _why(job):
    s = job.get("suggest") or {}
    d = s.get("speaker_detail") or {}
    if s.get("speaker_source") == "voice":
        return f"voice match {d.get('score')}"
    if s.get("speaker_source") == "transcript":
        return f"transcript: “{d.get('evidence', '')}”"
    return ""


def apply(nas_only=False):
    threading.Thread(target=_apply, args=(nas_only,), daemon=True, name="batch-apply").start()


def _apply(nas_only):
    st, o = state(), state()["options"]
    todo = [i for i, it in enumerate(st["items"]) if it["state"] == "done" and it.get("approved")
            and not (it.get("applied") or {}).get("ok")]  # failed ones can be retried
    _set(apply={"status": "running", "done": 0, "total": len(todo), "log": [], "nas_only": nas_only})
    say = lambda m: (state()["apply"]["log"].append(m), save(state()))
    for n, i in enumerate(todo):
        it = st["items"][i]
        job = jobs.get(it["job"])
        cut, post = job["cut"], it.get("post") or {}
        done = []
        try:
            folder = Path(it["path"]).parent
            want_mp3 = o["nas_mp3"] and not (it.get("mp3") and o["existing"] == "skip")
            if o["existing"] == "replace" and it.get("mp3") and want_mp3:
                old = folder / it["mp3"]
                if old.exists() and old.name == f"{config.FILE_PREFIX}{cut['date'].replace('-', '')}.mp3":
                    old.rename(old.with_name(old.stem + ".original.mp3"))
            jobs.update(job, archive={**job["archive"], "status": "saved", "folder": it["virtual"].rsplit("/", 1)[0],
                                      "files": {"recording": False, "slide": bool(o["nas_slide"]), "mp3": bool(want_mp3)}})
            if not actions["encode"](job):
                raise RuntimeError(job.get("message"))
            done.append("MP3 made")
            if job["archive"].get("message", "").startswith("Saved"):
                done.append(job["archive"]["message"].replace("Saved", "NAS:"))
            if not nas_only and wp.configured:
                if post:
                    changes = {}
                    if o["wp_speaker"] and cut.get("speaker") and cut["speaker"] != post.get("speaker"):
                        changes["people"] = cut["speaker"]
                    if o["wp_description"] and cut.get("description"):
                        changes["content_if_empty"] = cut["description"]
                    if o["wp_audio"]:
                        changes["mp3"] = Path(job["output"]["path"])
                    if changes:
                        res = wp.update_post(post["id"], **changes)
                        done.append("WordPress: " + ", ".join(res) if res else "WordPress: nothing to change")
                elif o["wp_drafts"]:
                    res = actions["publish"](job, "draft")
                    done.append(f"draft created (post {res['id']})" if res else f"draft failed: {job.get('message')}")
            if o["teach"] and job.get("voice") and cut.get("speaker"):
                voice.learn(cut["speaker"], job["voice"]["vec"], job["id"])
                done.append(f"voice learned ({cut['speaker']})")
            if o.get("free_space") and job.get("output"):
                Path(job["output"]["path"]).unlink(missing_ok=True)  # the NAS has it now
            _item(i, applied={"ok": True, "done": done})
            say(f"✓ {it['date']}: " + "; ".join(done))
        except Exception as e:
            log.exception("batch apply %s", it["name"])
            _item(i, applied={"ok": False, "error": str(e)})
            say(f"✗ {it['date']}: {e}")
        state()["apply"]["done"] = n + 1
        save(state())
    state()["apply"]["status"] = "done"
    save(state())
