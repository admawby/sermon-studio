"""One job per source recording, persisted under /data/work/<id>/state.json."""
import hashlib
import json
import queue
import re
import threading
import time
import traceback
from datetime import date
from pathlib import Path

from . import audio, config, detect, transcribe

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_queue: "queue.Queue[str]" = queue.Queue()


def job_id(src: Path):
    st = src.stat()
    return hashlib.sha1(f"{src}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]


def workdir(jid):
    d = config.WORK / jid
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(job):
    p = workdir(job["id"]) / "state.json"
    p.with_suffix(".tmp").write_text(json.dumps(job))
    p.with_suffix(".tmp").replace(p)


def get(jid):
    with _lock:
        if jid not in _jobs:
            p = config.WORK / jid / "state.json"
            if not p.exists():
                return None
            job = json.loads(p.read_text())
            # fields added after the job was created
            job.setdefault("archive", {"status": "none", "folder": None, "message": ""})
            job.setdefault("cover", (p.parent / "cover.jpg").exists())
            _jobs[jid] = job
        return _jobs[jid]


def update(job, **kw):
    with _lock:
        job.update(kw)
        job["updated"] = time.time()
    save(job)


def recording_date(src: Path):
    for text in (src.name, str(src.parent)):
        m = re.search(r"(20\d\d)[-_]?(\d\d)[-_]?(\d\d)", text)
        if m:
            try:
                return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
            except ValueError:
                pass
    return date.fromtimestamp(src.stat().st_mtime).isoformat()


def create(src: Path, virtual: str):
    jid = job_id(src)
    job = get(jid)
    if job and job["status"] != "error":
        return job
    job = {"id": jid, "source": str(src), "virtual": virtual, "name": src.name, "status": "queued",
           "message": "Waiting in line…", "progress": 0, "created": time.time(),
           "date": recording_date(src), "duration": None, "proposal": None, "suggest": {},
           "output": None, "published": None, "cover": False,
           "archive": {"status": "none", "folder": None, "message": ""}}
    with _lock:
        _jobs[jid] = job
    save(job)
    _queue.put(jid)
    return job


def describe(job, start, end):
    """Re-run the title/scripture/description suggestion for the given cut."""
    update(job, describing=True)

    def go():
        try:
            update(job, suggest=detect.suggest(transcript(job["id"]), start, end), describing=False)
        except Exception as e:
            update(job, describing=False, message=f"Description failed: {e}")
    threading.Thread(target=go, daemon=True).start()


def redetect(job):
    update(job, status="queued", message="Waiting in line…", progress=0)
    (workdir(job["id"]) / "proposal.json").unlink(missing_ok=True)
    _queue.put(job["id"])


def recent(limit=15):
    out = []
    for p in sorted(config.WORK.glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        j = get(p.parent.name)
        if j:
            out.append({k: j.get(k) for k in ("id", "name", "virtual", "status", "date", "published")})
    return out


def transcript(jid):
    p = config.WORK / jid / "transcript.json"
    return json.loads(p.read_text()) if p.exists() else []


def _analyze(job):
    src, work = Path(job["source"]), workdir(job["id"])
    update(job, status="preparing", message="Converting audio…", progress=0)
    total = audio.duration(src)
    audio.prepare(src, work)
    update(job, duration=total)

    tpath = work / "transcript.json"
    if tpath.exists():
        segments = json.loads(tpath.read_text())
    else:
        info = transcribe.model_info()
        update(job, status="transcribing", message=f"Transcribing with {info['model']} on {info['device'].upper()}…")
        segments = transcribe.transcribe(work / "speech.wav", total,
                                         lambda f: update(job, progress=round(f, 3)) if f - job["progress"] > 0.005 else None)
        tpath.write_text(json.dumps(segments))

    update(job, status="detecting", message="Finding the sermon…", progress=1)
    status = lambda m: update(job, message=m)
    proposal = None
    if detect.ensure_llm(status):
        try:
            proposal = detect.llm(segments, total, status)
        except Exception as e:
            proposal = None
            update(job, message=f"AI detection failed ({e}); using heuristic.")
    proposal = proposal or detect.heuristic(segments, total)
    update(job, status="describing", message="Writing the title and description…")
    sug = detect.suggest(segments, proposal["start"], proposal["end"])
    (work / "proposal.json").write_text(json.dumps(proposal))
    update(job, status="ready", message="Ready for review", proposal=proposal, suggest=sug)


def _worker():
    while True:
        jid = _queue.get()
        job = get(jid)
        try:
            _analyze(job)
        except Exception as e:
            traceback.print_exc()
            update(job, status="error", message=str(e))


def _resume():
    """Requeue jobs that were mid-analysis when the container stopped."""
    for p in sorted(config.WORK.glob("*/state.json"), key=lambda p: p.stat().st_mtime):
        job = get(p.parent.name)
        if job and job["status"] not in ("ready", "error"):
            update(job, status="queued", message="Waiting in line…", progress=0)
            _queue.put(job["id"])
        elif job and (job.get("encoding") or job.get("publishing")):
            update(job, encoding=False, publishing=False, message="Interrupted by a restart; try again.")


_resume()
threading.Thread(target=_worker, daemon=True).start()
