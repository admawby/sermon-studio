import json
import shutil
import threading
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, config, jobs, transcribe
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


def resolve(virtual: str) -> Path:
    """'NAS/Audio/2026' -> /media/nas/Audio/2026, refusing anything outside the configured roots."""
    parts = [p for p in virtual.strip("/").split("/") if p]
    if not parts or parts[0] not in config.ROOTS:
        raise HTTPException(404, "Unknown location")
    root = config.ROOTS[parts[0]].resolve()
    path = root.joinpath(*parts[1:]).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(403, "Outside allowed folders")
    if not path.exists():
        raise HTTPException(404, "Not found")
    return path


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
            "default_status": config.WP_DEFAULT_STATUS, "settings": settings()}


@app.get("/api/browse")
def browse(path: str = "", kind: str = "audio"):
    if not path.strip("/"):
        return {"path": "", "entries": [{"name": r, "dir": True} for r in config.ROOTS]}
    p = resolve(path)
    exts = config.AUDIO_EXT if kind == "audio" else config.IMAGE_EXT
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


@app.post("/api/upload-source")
def upload_source(file: UploadFile = File(...)):
    name = Path(file.filename).name
    if Path(name).suffix.lower() not in config.AUDIO_EXT:
        raise HTTPException(400, "Not an audio file")
    dest = config.UPLOADS / name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f, 1 << 20)
    return {"virtual": f"Uploads/{name}"}


# --- jobs -----------------------------------------------------------------------
class NewJob(BaseModel):
    path: str


@app.post("/api/jobs")
def new_job(body: NewJob):
    src = resolve(body.path)
    if not src.is_file() or src.suffix.lower() not in config.AUDIO_EXT:
        raise HTTPException(400, "Pick an audio file")
    return jobs.create(src, body.path)


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
        except Exception as e:
            jobs.update(job, encoding=False, message=f"Encoding failed: {e}")

    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


# --- WordPress ------------------------------------------------------------------
@app.get("/api/wp/meta")
def wp_meta():
    if not wp.configured:
        return {"configured": False, "series": []}
    try:
        return {"configured": True, **wp.meta()}
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
            jobs.update(job, publishing=False, published=res,
                        message="Published" if res["status"] == "publish" else "Saved as draft")
        except Exception as e:
            jobs.update(job, publishing=False, message=f"Upload failed: {e}")

    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
