import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import archive, audio, config, discord_bot, jobs, transcribe, watcher
from . import settings as store
from .wordpress import WPError, wp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.ERROR)  # "voice will NOT be supported": we don't use voice
app = FastAPI(title="Sermon Studio")
STATIC = Path(__file__).parent / "static"


@app.middleware("http")
async def revalidate_page(request, call_next):
    """Make browsers check for a new version of the page instead of running a stale copy."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
def settings():
    return store.all()


def remember(**kw):
    store.set(**{k: v for k, v in kw.items() if v})


def resolve(virtual: str, must_exist=True) -> Path:
    """'NAS/Audio/2026' -> /media/nas/Audio/2026, refusing anything outside the configured roots."""
    parts = [p for p in virtual.strip("/").split("/") if p]
    if not parts or parts[0] not in config.ROOTS or any(p in (".", "..") for p in parts):
        raise HTTPException(404, "Unknown location")
    root = config.ROOTS[parts[0]].resolve()
    path = root.joinpath(*parts[1:]).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(403, "Outside allowed folders")
    if must_exist and not path.exists():
        raise HTTPException(404, "Not found")
    return path


def virtual_of(path: Path):
    """Inverse of resolve(), or None if the path isn't under a root."""
    for name, root in config.ROOTS.items():
        root = root.resolve()
        if path == root or root in path.parents:
            return "/".join([name, *path.relative_to(root).parts])
    return None


def must_job(jid):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "No such job")
    return job


# --- browsing -----------------------------------------------------------------
@app.get("/api/status")
def status():
    return {"roots": list(config.ROOTS), "start_path": config.START_PATH, "ai": transcribe.model_info(),
            "llm": config.LLM_MODEL or None, "wp_configured": wp.configured, "bitrate": config.MP3_BITRATE,
            "default_status": config.WP_DEFAULT_STATUS, "archive_root": config.ARCHIVE_ROOT,
            "settings": settings()}


@app.get("/api/browse")
def browse(path: str = "", kind: str = "audio"):
    if not path.strip("/"):
        return {"path": "", "entries": [{"name": r, "dir": True} for r in config.ROOTS]}
    p = resolve(path)
    exts = {"audio": config.AUDIO_EXT, "image": config.IMAGE_EXT}.get(kind, set())
    entries = []
    try:
        for c in p.iterdir():
            if c.name.startswith(".") or c.name.startswith("@") or c.name == "#recycle":
                continue
            try:
                if c.is_dir():
                    entries.append({"name": c.name, "dir": True})
                elif c.suffix.lower() in exts:
                    st = c.stat()
                    entries.append({"name": c.name, "dir": False, "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"path": path.strip("/"), "entries": entries}


def _receive(file: UploadFile, dest: Path, last_modified: float | None):
    tmp = dest.with_name(f".{dest.name}.part")
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f, 1 << 20)
    tmp.replace(dest)
    if last_modified:  # keep the original timestamp so re-uploading the same file reuses its analysis
        os.utime(dest, (last_modified / 1000, last_modified / 1000))


@app.post("/api/upload/audio")
def upload_audio(file: UploadFile = File(...), last_modified: float = Form(0)):
    name = Path(file.filename).name
    if Path(name).suffix.lower() not in config.AUDIO_EXT:
        raise HTTPException(400, "That isn't an audio file")
    _receive(file, config.UPLOADS / name, last_modified)
    return {"virtual": f"Uploads/{name}", "name": name}


@app.post("/api/upload/image")
def upload_image(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in config.IMAGE_EXT:
        raise HTTPException(400, "Use a JPG, PNG or WebP image")
    d = config.UPLOADS / "images"
    d.mkdir(exist_ok=True)
    name = f"{uuid.uuid4().hex[:12]}{ext}"
    _receive(file, d / name, None)
    return {"virtual": f"Uploads/images/{name}", "name": Path(file.filename).name}


# --- jobs -----------------------------------------------------------------------
class NewJob(BaseModel):
    path: str
    cover: str = ""


@app.post("/api/jobs")
def new_job(body: NewJob):
    src = resolve(body.path)
    if not src.is_file() or src.suffix.lower() not in config.AUDIO_EXT:
        raise HTTPException(400, "Pick an audio file")
    job = jobs.create(src, body.path)
    _detect_archived(job)
    if body.cover:
        _save_cover(job, resolve(body.cover))
    return job


@app.get("/api/jobs")
def list_jobs():
    return jobs.recent()


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    return must_job(jid)


@app.get("/api/jobs/{jid}/transcript")
def get_transcript(jid: str):
    must_job(jid)
    return jobs.transcript(jid)


@app.post("/api/jobs/{jid}/cancel")
def cancel(jid: str):
    jobs.cancel(must_job(jid))
    return {"ok": True}


@app.post("/api/jobs/{jid}/redetect")
def redetect(jid: str):
    jobs.redetect(must_job(jid))
    return {"ok": True}


def _detect_archived(job):
    """A recording picked from inside the archive is already saved where it belongs."""
    src = Path(job["source"])
    if job["archive"]["status"] == "none" and resolve(config.ARCHIVE_ROOT) in src.parents:
        jobs.update(job, archive={"status": "saved", "folder": virtual_of(src.parent), "message": "Already on the NAS"})


@app.get("/api/jobs/{jid}/archive")
def archive_recommend(jid: str):
    job = must_job(jid)
    _detect_archived(job)
    return {**archive.recommend(date.fromisoformat(job["date"])), "state": job["archive"]}


class ArchiveTo(BaseModel):
    folder: str


def _archive_files(job):
    """Copy the recording (and the MP3, once it exists) into the job's archive folder."""
    folder = resolve(job["archive"]["folder"], must_exist=False)
    jobs.update(job, archive={**job["archive"], "status": "saving", "message": "Copying the recording to the NAS…"})
    try:
        ours = set(job["archive"].get("written", []))
        wav = archive.copy_into(Path(job["source"]), folder, ours)
        watcher.mark_seen(wav)  # our own copy isn't a "new recording"
        saved = [wav.name]
        if job.get("output"):
            saved.append(archive.copy_into(Path(job["output"]["path"]), folder, ours).name)
        # only files the app created count as "ours" (never the original recording itself)
        made = {n for n in saved if not (folder / n).resolve() == Path(job["source"]).resolve()}
        jobs.update(job, archive={**job["archive"], "status": "saved", "written": sorted(ours | made),
                                  "message": "Saved " + ", ".join(saved)})
    except Exception as e:
        jobs.update(job, archive={**job["archive"], "status": "error", "message": f"Couldn't save to the NAS: {e}"})


@app.post("/api/jobs/{jid}/archive")
def archive_save(jid: str, body: ArchiveTo):
    job = must_job(jid)
    dest = resolve(body.folder, must_exist=False)
    if resolve(config.ARCHIVE_ROOT) not in [dest, *dest.parents]:
        raise HTTPException(400, f"Pick a folder inside {config.ARCHIVE_ROOT}")
    jobs.update(job, archive={"status": "saving", "folder": virtual_of(dest), "message": ""})
    threading.Thread(target=_archive_files, args=(job,), daemon=True).start()
    return {"ok": True}


class Span(BaseModel):
    start: float
    end: float


@app.post("/api/jobs/{jid}/describe")
def describe(jid: str, body: Span):
    jobs.describe(must_job(jid), body.start, body.end)
    return {"ok": True}


def _save_cover(job, src: Path):
    dest = jobs.workdir(job["id"]) / "cover.jpg"
    audio.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", "scale='min(3000,iw)':-2",
               "-q:v", "2", "-frames:v", "1", str(dest)])
    jobs.update(job, cover=True)
    return {"ok": True}


@app.post("/api/jobs/{jid}/cover")
def upload_cover(jid: str, file: UploadFile = File(...)):
    job = must_job(jid)
    tmp = jobs.workdir(jid) / ("cover-src" + Path(file.filename).suffix.lower())
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        return _save_cover(job, tmp)
    except RuntimeError:
        raise HTTPException(400, "Couldn't read that image")
    finally:
        tmp.unlink(missing_ok=True)


class CoverPath(BaseModel):
    path: str


@app.post("/api/jobs/{jid}/cover-path")
def cover_from_path(jid: str, body: CoverPath):
    return _save_cover(must_job(jid), resolve(body.path))


class Cut(BaseModel):
    start: float
    end: float
    title: str
    speaker: str = ""
    series: str = ""        # term id
    series_name: str = ""
    scripture: str = ""
    description: str = ""
    date: str
    bitrate: str = ""
    normalize: bool = True


@app.post("/api/jobs/{jid}/encode")
def encode(jid: str, cut: Cut):
    job = must_job(jid)
    if cut.end - cut.start < 60:
        raise HTTPException(400, "Sermon must be at least a minute long")
    jobs.update(job, encoding=True, encode_progress=0, message="Making the MP3…", cut=cut.model_dump())
    threading.Thread(target=do_encode, args=(job,), daemon=True).start()
    return {"ok": True}


def do_encode(job):
    """Make the MP3 for job["cut"] (and archive it next to the recording). Returns True on success."""
    cut = job["cut"]
    out = config.OUTPUT / f"{config.FILE_PREFIX}{cut['date'].replace('-', '')}.mp3"
    jobs.update(job, encoding=True, encode_progress=0, message="Making the MP3…")
    last = [0.0]

    def progress(f):
        if f - last[0] >= 0.01 or f >= 1:
            last[0] = f
            jobs.update(job, encode_progress=round(f, 3))
    try:
        audio.encode(Path(job["source"]), out, cut["start"], cut["end"], cut.get("bitrate") or config.MP3_BITRATE,
                     cut.get("normalize", True), {"title": cut["title"], "speaker": cut.get("speaker", ""),
                                                  "series": cut.get("series_name", ""), "date": cut["date"],
                                                  "scripture": cut.get("scripture", "")},
                     jobs.workdir(job["id"]) / "cover.jpg", progress)
        jobs.update(job, encode_progress=1, output={
            "path": str(out), "name": out.name, "size": out.stat().st_size, "duration": cut["end"] - cut["start"]})
        remember(speaker=cut.get("speaker"), bitrate=cut.get("bitrate"))
        if job["archive"]["status"] == "saved":
            jobs.update(job, message="Copying the MP3 to the NAS…")
            _archive_files(job)
        jobs.update(job, encoding=False, message="Encoded")
        return True
    except Exception as e:
        jobs.update(job, encoding=False, message=f"Encoding failed: {e}")
        return False


# --- WordPress ------------------------------------------------------------------
_meta_cache = {"at": 0, "data": None}


@app.get("/api/wp/meta")
def wp_meta(fresh: bool = False):
    if not wp.configured:
        return {"configured": False, "series": []}
    if not fresh and _meta_cache["data"] and time.time() - _meta_cache["at"] < 300:
        return _meta_cache["data"]
    try:
        data = {"configured": True, **wp.meta()}
        _meta_cache.update(at=time.time(), data=data)
        return data
    except WPError as e:
        return {"configured": True, "series": [], "error": str(e)}


@app.get("/api/wp/discover")
def wp_discover():
    try:
        return wp.discover()
    except WPError as e:
        raise HTTPException(502, str(e))


class Publish(BaseModel):
    status: str = "draft"


@app.post("/api/jobs/{jid}/publish")
def publish(jid: str, body: Publish):
    job = must_job(jid)
    if not job.get("output") or not job.get("cut"):
        raise HTTPException(400, "Encode the MP3 first")
    if body.status not in ("draft", "publish"):
        raise HTTPException(400, "Bad status")
    jobs.update(job, publishing=True, message="Uploading to WordPress…")
    threading.Thread(target=do_publish, args=(job, body.status), daemon=True).start()
    return {"ok": True}


def do_publish(job, status):
    """Upload the encoded MP3 + cover and create the post. Returns the result dict or None."""
    cover = jobs.workdir(job["id"]) / "cover.jpg"
    jobs.update(job, publishing=True, message="Uploading to WordPress…")
    try:
        res = wp.publish(Path(job["output"]["path"]), cover if cover.exists() else None, job["cut"],
                         status, lambda m: jobs.update(job, message=m))
        _meta_cache["at"] = 0  # the new post is now the "last edited" one
        jobs.update(job, publishing=False, published=res,
                    message="Published" if res["status"] == "publish" else "Saved as draft")
        return res
    except Exception as e:
        jobs.update(job, publishing=False, message=f"Upload failed: {e}")
        return None


# --- automation: new recordings on the NAS -> analysis -> Discord approval ------------------
def _auto_cut(job):
    """The cut + details the app proposes on its own (same defaults the Review step would show)."""
    meta = wp_meta() if wp.configured else {}
    sug, prop = job.get("suggest") or {}, job["proposal"]
    series = meta.get("default_series")
    return {
        "start": prop["start"], "end": prop["end"], "title": sug.get("title") or f"Sermon {job['date']}",
        "speaker": meta.get("default_speaker") or settings().get("speaker", ""),
        "series": str(series or ""),
        "series_name": next((s["name"] for s in meta.get("series", []) if s["id"] == series), ""),
        "scripture": sug.get("scripture", ""), "description": sug.get("description", ""), "date": job["date"],
        "bitrate": settings().get("bitrate") or config.MP3_BITRATE, "normalize": True,
    }


def _on_new_recording(path: Path):
    job = jobs.create(path, virtual_of(path))
    if job.get("published"):
        return
    jobs.update(job, auto=True)
    _detect_archived(job)
    if discord_bot.enabled():
        mins = audio.duration(path) / 60
        on_gpu = transcribe.model_info()["device"] == "cuda"
        est = round(mins * 0.03 + 2) if on_gpu else round(mins * 0.25 + 12)
        discord_bot.notify_analyzing(job, virtual_of(path.parent), est)


def _on_job_event(job, event):
    if not job.get("auto") or not discord_bot.enabled():
        return
    if event == "error":
        discord_bot.notify_error(job)
        return
    cut = job.get("cut") or _auto_cut(job)
    jobs.update(job, cut=cut)
    work, src = jobs.workdir(job["id"]), Path(job["source"])
    clips = (audio.clip(src, work / "clip-start.mp3", cut["start"], 20),
             audio.clip(src, work / "clip-end.mp3", cut["end"] - 20, 20))
    discord_bot.notify_ready(job, cut, clips)


def _approve(jid, status):
    job = jobs.get(jid)
    if not job:
        return "That sermon isn't in Sermon Studio any more.", True
    if job.get("published"):
        p = job["published"]
        return f"Already posted ({p['status']}): {p.get('link') or p['edit']}", True
    if job["status"] != "ready":
        return "It's still being analyzed. Try again when it's done.", False
    if not job.get("cover"):
        return "📎 Send this week's sermon slide first (reply to the message with the image).", False
    jobs.update(job, cut=job.get("cut") or _auto_cut(job))
    if not do_encode(job):
        return f"⚠️ {job['message']}", False
    res = do_publish(job, status)
    if not res:
        return f"⚠️ {job['message']}", False
    if res["status"] == "publish":
        return f"🎉 **Published:** {res.get('link') or res['edit']}", True
    return f"📝 **Saved as a draft.** Check it here: {res['edit']}", True


def _latest_auto_job():
    for j in jobs.recent(30):
        job = jobs.get(j["id"])
        if job.get("auto") and not job.get("published"):
            return job
    return None


def _job_for_message(mid):
    for j in jobs.recent(30):
        job = jobs.get(j["id"])
        if (job.get("discord") or {}).get("message") == mid:
            return job
    return None


jobs.listeners.append(_on_job_event)
discord_bot.start({
    "latest_job": _latest_auto_job, "job_for_message": _job_for_message, "job": jobs.get,
    "set_cover": _save_cover, "approve": _approve, "update": jobs.update,
})
watcher.start(resolve(config.ARCHIVE_ROOT), _on_new_recording)  # idles while monitoring is off


# --- Settings page --------------------------------------------------------------------
@app.get("/api/settings")
def get_settings():
    return {
        "watch": {"enabled": watcher.enabled(), "folder": config.ARCHIVE_ROOT, "interval": config.WATCH_INTERVAL,
                  "types": sorted(config.WATCH_EXT), **watcher.status, **watcher.counts()},
        "discord": {"configured": discord_bot.enabled(), "connected": discord_bot.connected(),
                    "name": discord_bot.name(), "channel": bool(config.DISCORD_CHANNEL_ID)},
        "wordpress": {"configured": wp.configured, "url": config.WP_URL},
        "ai": {**transcribe.model_info(), "llm": config.LLM_MODEL or None},
    }


class WatchSetting(BaseModel):
    enabled: bool
    catch_up: bool = False  # when turning on: also process recordings that arrived while it was off


@app.post("/api/settings/watch")
def set_watch(body: WatchSetting):
    was = watcher.enabled()
    if body.enabled and not was and not body.catch_up:
        watcher.rebaseline(resolve(config.ARCHIVE_ROOT))
    store.set(watch_enabled=body.enabled)
    if body.enabled:
        watcher.scan_now()
    return get_settings()


@app.post("/api/settings/watch/scan")
def watch_scan_now():
    if not watcher.enabled():
        raise HTTPException(400, "Monitoring is off")
    watcher.scan_now()
    return {"ok": True}


# keep last: matches any /api/jobs/<id>/<name>
@app.get("/api/jobs/{jid}/{name}")
def job_file(jid: str, name: str):
    must_job(jid)
    if name not in ("preview.mp3", "waveform.png", "cover.jpg", "output.mp3"):
        raise HTTPException(404)
    p = config.WORK / jid / name
    if name == "output.mp3":
        out = jobs.get(jid).get("output")
        p = Path(out["path"]) if out else p
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
