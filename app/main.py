import json
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

from . import archive, audio, config, jobs, transcribe
from .wordpress import WPError, wp

app = FastAPI(title="Sermon Studio")
STATIC = Path(__file__).parent / "static"
SETTINGS = config.DATA / "settings.json"


def settings():
    return json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}


def remember(**kw):
    s = settings()
    s.update({k: v for k, v in kw.items() if v})
    SETTINGS.write_text(json.dumps(s))


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
        saved = [archive.copy_into(Path(job["source"]), folder, ours).name]
        if job.get("output"):
            saved.append(archive.copy_into(Path(job["output"]["path"]), folder, ours).name)
        jobs.update(job, archive={**job["archive"], "status": "saved", "written": sorted(ours | set(saved)),
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
    out = config.OUTPUT / f"{config.FILE_PREFIX}{cut.date.replace('-', '')}.mp3"
    cover = jobs.workdir(jid) / "cover.jpg"
    jobs.update(job, encoding=True, message="Encoding MP3…", cut=cut.model_dump())

    def go():
        try:
            audio.encode(Path(job["source"]), out, cut.start, cut.end, cut.bitrate or config.MP3_BITRATE,
                         cut.normalize, {"title": cut.title, "speaker": cut.speaker, "series": cut.series_name,
                                         "date": cut.date, "scripture": cut.scripture}, cover)
            jobs.update(job, encoding=False, message="Encoded", output={
                "path": str(out), "name": out.name, "size": out.stat().st_size, "duration": cut.end - cut.start})
            remember(speaker=cut.speaker, bitrate=cut.bitrate)
            if job["archive"]["status"] == "saved":
                _archive_files(job)
        except Exception as e:
            jobs.update(job, encoding=False, message=f"Encoding failed: {e}")

    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


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
    cover = jobs.workdir(jid) / "cover.jpg"
    jobs.update(job, publishing=True, message="Uploading to WordPress…")

    def go():
        try:
            res = wp.publish(Path(job["output"]["path"]), cover if cover.exists() else None, job["cut"],
                             body.status, lambda m: jobs.update(job, message=m))
            _meta_cache["at"] = 0  # the new post is now the "last edited" one
            jobs.update(job, publishing=False, published=res,
                        message="Published" if res["status"] == "publish" else "Saved as draft")
        except Exception as e:
            jobs.update(job, publishing=False, message=f"Upload failed: {e}")

    threading.Thread(target=go, daemon=True).start()
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
