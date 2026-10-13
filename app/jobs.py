"""One job per source recording, persisted under /data/work/<id>/state.json."""
import hashlib
import logging
import json
import queue
import shutil
import re
import threading
import time
import traceback
from datetime import date
from pathlib import Path

from . import audio, config, detect, transcribe, voice

log = logging.getLogger("sermon.jobs")
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_queue: "queue.Queue[str]" = queue.Queue()
_abort = threading.Event()  # set to stop the analysis that's running now
_current = {"id": None}
detect.should_stop = _abort.is_set
TERMINAL = ("ready", "error", "cancelled")
listeners = []  # fn(job, event) with event "ready" | "error"; called from the worker thread


def _notify(job, event):
    for fn in listeners:
        try:
            fn(job, event)
        except Exception:
            traceback.print_exc()


def job_id(src: Path):
    st = src.stat()
    return hashlib.sha1(f"{src}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]


def workdir(jid):
    d = config.WORK / jid
    d.mkdir(parents=True, exist_ok=True)
    return d


_save_lock = threading.Lock()


def save(job):
    """Write state.json atomically. Several threads (worker, watcher, web requests) save the same job,
    so writes are serialized and each uses its own temp file."""
    with _save_lock:
        p = workdir(job["id"]) / "state.json"
        tmp = p.with_name(f".state.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(job))
        tmp.replace(p)


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
            update(job, suggest={**(job.get("suggest") or {}), **detect.suggest(transcript(job["id"]), start, end)},
                   describing=False)
        except Exception as e:
            update(job, describing=False, message=f"Description failed: {e}")
    threading.Thread(target=go, daemon=True).start()


def cancel(job):
    """Stop a queued or running analysis. A re-run keeps the previous result."""
    if _current["id"] == job["id"]:
        _abort.set()  # the worker notices within a second and calls _stopped()
    elif job["status"] not in TERMINAL:
        _stopped(job)


def _stopped(job):
    if job.get("proposal"):
        update(job, status="ready", message="Stopped. Kept the previous result.", progress=1)
    else:
        update(job, status="cancelled", message="Analysis stopped.", progress=0)


def delete(job):
    """Forget a job and remove its cached files (data/work/<id>). Returns False if it's busy."""
    busy = job.get("encoding") or job.get("publishing") or (job.get("archive") or {}).get("status") == "saving"
    if _current["id"] == job["id"] or busy:
        return False
    with _lock:
        _jobs.pop(job["id"], None)
    job["status"] = "deleted"  # a queued copy in the worker is skipped
    shutil.rmtree(config.WORK / job["id"], ignore_errors=True)
    return True


def redetect(job):
    update(job, status="queued", message="Waiting in line…", progress=0, cut=None, discord=None)
    (workdir(job["id"]) / "proposal.json").unlink(missing_ok=True)
    _queue.put(job["id"])


def recent(limit=15):
    out = []
    for p in sorted(config.WORK.glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        j = get(p.parent.name)
        if j:
            out.append({k: j.get(k) for k in ("id", "name", "virtual", "status", "date", "published")})
    return out


def active():
    """Jobs being analyzed or waiting to be, oldest first."""
    out = []
    for p in sorted(config.WORK.glob("*/state.json"), key=lambda p: p.stat().st_mtime):
        j = get(p.parent.name)
        if j and j["status"] not in TERMINAL:
            out.append({k: j.get(k) for k in ("id", "name", "date", "status", "progress", "message", "auto")})
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

        def progress(f):
            if _abort.is_set():
                raise detect.Cancelled()
            if f - job["progress"] > 0.005:
                update(job, progress=round(f, 3))
        segments = transcribe.transcribe(work / "speech.wav", total, progress)
        tpath.write_text(json.dumps(segments))

    update(job, status="detecting", message="Recognizing the preacher's voice…", progress=1)
    status = lambda m: update(job, message=m)
    has_llm = detect.ensure_llm(status)
    proposal, vres, voice_info = None, None, None
    if voice.available():
        try:
            W, E = voice.analyze(transcribe._load_wav(work / "speech.wav"), work / "voice.npz", _abort.is_set)
            vres = voice.find_sermon(W, E)
            voice.add_background(E, job["id"])
            if vres:
                voice.label_segments(segments, W, vres["sim"], vres["thr"])
                proposal = detect.refine(segments, vres["start"], vres["end"], total, status, has_llm)
                sel = (W[:, 0] >= proposal["start"]) & (W[:, 1] <= proposal["end"])
                voice_info = {"vec": E[sel].mean(0).tolist(), "service_mean": E.mean(0).tolist()}
        except InterruptedError:
            raise detect.Cancelled()
        except detect.Cancelled:
            raise
        except Exception as e:
            log.exception("voice detection failed")
            update(job, message=f"Voice detection failed ({e}); using the transcript only.")
    if not proposal and has_llm:
        try:
            proposal = detect.llm(segments, total, status)
        except detect.Cancelled:
            raise
        except Exception as e:
            update(job, message=f"AI detection failed ({e}); using heuristic.")
    proposal = proposal or detect.heuristic(segments, total)
    update(job, status="describing", message="Writing the title and description…")
    sug = detect.suggest(segments, proposal["start"], proposal["end"])
    update(job, message="Working out who preached…")
    sug.update(_who_preached(segments, proposal, voice_info))
    (work / "proposal.json").write_text(json.dumps(proposal))
    update(job, status="ready", message="Ready for review", proposal=proposal, suggest=sug, voice=voice_info)
    _notify(job, "ready")


people_list = lambda: []  # set by main.py: names from WordPress "People"


def _who_preached(segments, proposal, voice_info):
    """speaker + how we know: "voice" (matches a learned profile), "transcript" (a clue in what was
    said), or nothing (Review falls back to the last sermon's speaker)."""
    if voice_info:
        m = voice.identify(voice_info["vec"], voice_info["service_mean"])
        if m["name"]:
            return {"speaker": m["name"], "speaker_source": "voice", "speaker_detail": m}
    else:
        m = None
    name, evidence = detect.speaker_from_transcript(segments, proposal["start"], proposal["end"], people_list())
    if name:
        return {"speaker": name, "speaker_source": "transcript", "speaker_detail": {"evidence": evidence, "voice": m}}
    return {"speaker": "", "speaker_source": "", "speaker_detail": {"voice": m}}


def _worker():
    while True:
        jid = _queue.get()
        job = get(jid)
        if not job or job["status"] != "queued":  # cancelled while waiting
            continue
        _abort.clear()
        _current["id"] = jid
        try:
            _analyze(job)
        except detect.Cancelled:
            _stopped(job)
        except Exception as e:
            traceback.print_exc()
            update(job, status="error", message=str(e))
            _notify(job, "error")
        finally:
            _current["id"] = None


def _resume():
    """Requeue jobs that were mid-analysis when the container stopped."""
    for p in sorted(config.WORK.glob("*/state.json"), key=lambda p: p.stat().st_mtime):
        job = get(p.parent.name)
        if job and job["status"] not in TERMINAL:
            update(job, status="queued", message="Waiting in line…", progress=0)
            _queue.put(job["id"])
        elif job and (job.get("encoding") or job.get("publishing")):
            update(job, encoding=False, publishing=False, message="Interrupted by a restart; try again.")


_resume()
threading.Thread(target=_worker, daemon=True).start()
