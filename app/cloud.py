"""Word Cloud tab: transcribe the chosen sermons (once; transcripts are cached) and draw a word cloud
of everything that was said in them.

Sermons come from the sermon MP3s in the archive, plus WordPress episodes whose MP3 isn't on the NAS
(those are downloaded while they're transcribed). Transcripts live in data/transcripts/<id>.json;
the state of the tab (choices, progress, last cloud) in data/cloud.json.
"""
import hashlib
import json
import logging
import re
import tempfile
import threading
import time
import urllib.request
from datetime import date
from pathlib import Path

from . import audio, batch, config, settings, transcribe
from .wordpress import wp

log = logging.getLogger("sermon.cloud")
STATE = config.DATA / "cloud.json"
TRANSCRIPTS = config.DATA / "transcripts"
OUT = config.DATA / "cloud"
for d in (TRANSCRIPTS, OUT):
    d.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()
_thread = None
_list = None  # cached sermon list
lister = None  # set by main.py: builds the sermon list (needed when resuming after a restart)

DEFAULT_OPTIONS = {"max_words": 150, "phrases": True, "filler": True, "exclude": "", "colors": "church",
                   "shape": "wide", "font": "sans"}

# things everybody says while talking that would otherwise fill the cloud
FILLER = set("""um umm uh uhh hmm mm mhm oh ah yeah yep okay ok alright gonna wanna gotta just like really right well
so now know going go goes get gets got getting say says said saying thing things one way kind sort lot little mean
actually maybe something anything everything even us let see also come comes came want ever still back good much many
make makes made take every may might day today able can will would could should us thats that's there's here's it's
yet look basically put rather already done okay
don't didn't doesn't i'm i've you're we're they're we've can't won't isn't""".split())

SHAPES = {"wide": (3000, 1700), "square": (2200, 2200), "tall": (1440, 2560)}
FONTS = {"sans": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "serif": "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"}
PALETTES = {  # (background, word colours)
    "church": ("white", ["#1f2d3d", "#2c5687", "#3b6ea8", "#6b8fb8", "#b08d3c", "#8a6d2b"]),
    "warm": ("white", ["#7a1f1f", "#b4442f", "#d9822b", "#b08d3c", "#5c3d2e"]),
    "night": ("#121a24", ["#ffffff", "#c9d6e6", "#8fb3dd", "#f2c14e", "#e8a33d"]),
    "rainbow": ("white", ["#b4442f", "#d9822b", "#c9a227", "#2f855a", "#3b6ea8", "#6b4fa0"]),
}


# --- state ---------------------------------------------------------------------------
def _load():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


_state = None


def state():
    global _state
    if _state is None:
        _state = {"run": {"status": "idle"}, "selected": None, "options": dict(DEFAULT_OPTIONS), "result": None, **_load()}
    return _state


def _save():
    with _lock:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state()))
        tmp.replace(STATE)


def _run_set(**kw):
    state()["run"].update(kw)
    _save()


def view():
    st = state()
    return {"run": st["run"], "selected": st["selected"], "options": {**DEFAULT_OPTIONS, **st["options"]},
            "result": st["result"]}


# --- which sermons there are ---------------------------------------------------------------------
def _sid(source: str):
    return hashlib.sha1(source.encode()).hexdigest()[:12]


def _date_of(p: Path):
    m = re.search(r"(20\d\d)(\d\d)(\d\d)", p.name) or re.search(r"(20\d\d)[_-](\d\d)[_-](\d\d)", str(p.parent))
    try:
        return date(int(m[1]), int(m[2]), int(m[3])) if m else date.fromtimestamp(p.stat().st_mtime)
    except ValueError:
        return date.fromtimestamp(p.stat().st_mtime)


def _transcript(sid):
    p = TRANSCRIPTS / f"{sid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def sermons(root: Path, virtual_of, refresh=False):
    """Every sermon we can get audio for, newest first: the sermon MP3s in the archive, then
    WordPress episodes whose MP3 isn't on the NAS."""
    global _list
    if _list is not None and not refresh:
        return _with_status(_list)
    posts = batch.wp_posts() if wp.configured else []
    out, used = [], set()
    mp3s = [p for p in root.rglob("*.mp3") if "sermon" in p.name.lower() and not p.name.startswith(".")
            and "@eaDir" not in p.parts and "(" not in p.name and ".original" not in p.name]
    for p in sorted(mp3s, key=_date_of, reverse=True):
        d = _date_of(p)
        post = batch._match_post(d, posts, used)
        if post:
            used.add(post["id"])
        v = virtual_of(p)
        out.append({"id": _sid(v), "date": d.isoformat(), "source": "nas", "path": str(p), "name": p.name,
                    "title": (post or {}).get("title") or "", "speaker": (post or {}).get("speaker") or "",
                    "series": (post or {}).get("series") or "", "size": p.stat().st_size})
    for post in posts:
        if post["id"] in used or not post.get("mp3"):
            continue
        out.append({"id": _sid(post["mp3"]), "date": post.get("service_date") or post["date"], "source": "wordpress",
                    "url": post["mp3"], "name": Path(post["mp3"]).name, "title": post["title"],
                    "speaker": post.get("speaker") or "", "series": post.get("series") or "", "size": None})
    out.sort(key=lambda x: x["date"], reverse=True)
    _list = out
    return _with_status(out)


_meta = {}  # id -> (mtime, {words, duration}) so the list doesn't re-read every transcript


def _summary(sid):
    p = TRANSCRIPTS / f"{sid}.json"
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None
    if sid not in _meta or _meta[sid][0] != mt:
        t = json.loads(p.read_text())
        _meta[sid] = (mt, {"words": t["words"], "duration": t["duration"]})
    return _meta[sid][1]


def _with_status(items):
    out = []
    for it in items:
        t = _summary(it["id"])
        out.append({**it, "transcribed": bool(t), "words": t["words"] if t else None,
                    "minutes": round(t["duration"] / 60) if t else None,
                    "speaker": it["speaker"] if it["speaker"] != "— No tags" else ""})
    return out


def find(ids):
    if _list is None and lister:
        lister()
    by = {x["id"]: x for x in (_list or [])}
    return [by[i] for i in ids if i in by]


# --- transcribing ------------------------------------------------------------------------------
def start(ids: list, options: dict):
    """Remember the choices, then transcribe whatever isn't yet and draw the cloud (in the background)."""
    global _thread
    st = state()
    if st["run"].get("status") == "running":
        raise RuntimeError("Already working on a word cloud")
    st["selected"] = ids
    st["options"] = {**DEFAULT_OPTIONS, **(options or {})}
    todo = [x["id"] for x in find(ids) if not _summary(x["id"])]
    if not todo:  # everything's transcribed: drawing only takes a few seconds
        _save()
        render()
        return
    st["run"] = {"status": "running", "todo": todo, "done": 0, "total": len(todo), "current": None, "fraction": 0,
                 "errors": [], "started": time.time(), "audio_done": 0, "stop": False}
    _save()
    _thread = threading.Thread(target=_run, daemon=True, name="cloud")
    _thread.start()


def stop():
    if state()["run"].get("status") == "running":
        _run_set(stop=True)


def resume_after_restart():
    global _thread
    if state()["run"].get("status") == "running":
        _run_set(stop=False, current=None, fraction=0)
        _thread = threading.Thread(target=_run, daemon=True, name="cloud")
        _thread.start()


def _run():
    run = state()["run"]
    try:
        for sid in list(run["todo"]):
            if run.get("stop"):
                break
            if _summary(sid):
                run["done"] += 1
                continue
            item = (find([sid]) or [None])[0]
            if not item:
                run["errors"].append({"id": sid, "error": "no longer in the list"})
                continue
            _run_set(current={"id": sid, "date": item["date"], "title": item["title"] or item["name"]}, fraction=0)
            try:
                minutes = _transcribe(item)
                run["audio_done"] += minutes
            except Exception as e:
                log.exception("transcribing %s failed", item["name"])
                run["errors"].append({"id": sid, "date": item["date"], "error": str(e)[:300]})
            run["done"] += 1
            _run_set()
        stopped = run.get("stop")
        _run_set(status="stopped" if stopped else "drawing", current=None)
        if not stopped:
            render()
            _run_set(status="done", finished=time.time())
    except Exception as e:
        log.exception("word cloud failed")
        _run_set(status="error", error=str(e)[:300], current=None)


def _transcribe(item):
    """Transcribe one sermon and cache it. Returns the length in minutes."""
    with tempfile.TemporaryDirectory(dir=config.WORK) as d:
        src = Path(item["path"]) if item["source"] == "nas" else Path(d) / "sermon.mp3"
        if item["source"] == "wordpress":
            urllib.request.urlretrieve(item["url"], src)
        wav = Path(d) / "speech.wav"
        audio.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
        length = audio.duration(wav)
        if length < 60:
            raise RuntimeError("the recording is under a minute long")
        segs = transcribe.transcribe(wav, length, lambda f: state()["run"].update(fraction=round(f, 3)))
    text = " ".join(s["text"] for s in segs)
    t = {"id": item["id"], "source": item.get("path") or item.get("url"), "date": item["date"], "title": item["title"],
         "speaker": item["speaker"], "duration": length, "model": transcribe.model_info()["model"],
         "words": len(text.split()), "made": time.time(), "text": text, "segments": segs}
    tmp = TRANSCRIPTS / f".{item['id']}.tmp"
    tmp.write_text(json.dumps(t))
    tmp.replace(TRANSCRIPTS / f"{item['id']}.json")
    return length / 60


# --- drawing ---------------------------------------------------------------------------------
def _stopwords(opts):
    from wordcloud import STOPWORDS
    words = set(STOPWORDS)
    if opts.get("filler"):
        words |= FILLER
    words |= {w.strip().lower() for w in re.split(r"[,\n]", opts.get("exclude") or "") if w.strip()}
    return words


def render():
    """Draw the cloud for the current choices. Only uses sermons that have a transcript."""
    import random
    import zlib

    from wordcloud import WordCloud
    st = state()
    opts = {**DEFAULT_OPTIONS, **st["options"]}
    ts = [t for t in (_transcript(i) for i in st["selected"] or []) if t]
    if not ts:
        raise RuntimeError("None of the chosen sermons has a transcript yet")
    bg, colors = PALETTES.get(opts["colors"], PALETTES["church"])
    w, h = SHAPES.get(opts["shape"], SHAPES["wide"])
    wc = WordCloud(width=w, height=h, background_color=bg, max_words=int(opts["max_words"]),
                   stopwords=_stopwords(opts), collocations=bool(opts["phrases"]), collocation_threshold=20,
                   font_path=FONTS.get(opts["font"]) if Path(FONTS.get(opts["font"], "")).exists() else None,
                   prefer_horizontal=0.9, min_font_size=10, margin=6, random_state=7,
                   color_func=lambda *a, random_state=None, **k: random.Random(zlib.crc32(a[0].encode())).choice(colors))
    freqs = wc.process_text(" ".join(t["text"] for t in ts))
    wc.generate_from_frequencies(freqs)
    made = int(time.time())
    for old in OUT.glob("cloud-*.png"):
        old.unlink()
    wc.to_file(str(OUT / f"cloud-{made}.png"))
    top = sorted(freqs.items(), key=lambda kv: -kv[1])[:60]
    st["result"] = {"made": made, "image": f"cloud-{made}.png", "sermons": len(ts),
                    "words": sum(t["words"] for t in ts), "hours": round(sum(t["duration"] for t in ts) / 3600, 1),
                    "first": min(t["date"] for t in ts), "last": max(t["date"] for t in ts),
                    "top": [[k, v] for k, v in top], "options": opts}
    _save()
    return st["result"]
