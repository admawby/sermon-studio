"""Polls the archive folder for new service recordings.

On the very first run every recording already there is marked as seen, so only recordings that
appear afterwards are picked up. A file counts as "new" once its size has stopped changing between
two scans (so a recording that's still being copied isn't analyzed half-finished)."""
import json
import logging
import os
import threading
import time
from pathlib import Path

from . import config, settings

log = logging.getLogger("sermon.watch")
STATE = config.DATA / "watch.json"
_lock = threading.Lock()


def _load():
    return json.loads(STATE.read_text()) if STATE.exists() else {"initialized": False, "seen": {}}


def _save(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


def mark_seen(path: Path):
    """Called for files the app copies into the archive itself, so they don't look like new recordings."""
    with _lock:
        state = _load()
        state["seen"][str(path)] = path.stat().st_size
        _save(state)


def _recordings(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "@", "#"))]
        for f in filenames:
            if not f.startswith(".") and Path(f).suffix.lower() in config.WATCH_EXT:
                yield Path(dirpath) / f


def scan(root: Path, pending: dict, on_new):
    with _lock:
        state = _load()
        first = not state["initialized"]
        found = []
        for p in _recordings(root):
            key = str(p)
            if key in state["seen"]:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if first:
                state["seen"][key] = st.st_size
            elif pending.get(key) == st.st_size and time.time() - st.st_mtime > 120:
                state["seen"][key] = st.st_size
                pending.pop(key)
                found.append(p)
            else:
                pending[key] = st.st_size  # check again next scan
        if first:
            log.info("watch: baseline of %d existing recordings", len(state["seen"]))
        state["initialized"] = True
        _save(state)
    for p in found:
        log.info("watch: new recording %s", p)
        try:
            on_new(p)
        except Exception:
            log.exception("watch: handling %s failed", p)


status = {"last_scan": None, "last_error": None, "last_found": _load().get("last_found"), "pending": 0}
_wake = threading.Event()


def enabled():
    """The Settings page wins; WATCH_ENABLED in .env is only the default."""
    return bool(settings.get("watch_enabled", config.WATCH_ENABLED))


def counts():
    st = _load()
    return {"seen": len(st["seen"]), "initialized": st["initialized"]}


def rebaseline(root: Path):
    """Mark everything currently in the archive as seen (used when monitoring is switched back on)."""
    with _lock:
        state = _load()
        for p in _recordings(root):
            try:
                st = p.stat()
            except OSError:
                continue
            if time.time() - st.st_mtime > 600:  # leave files that may still be copying for the next scan
                state["seen"].setdefault(str(p), st.st_size)
        state["initialized"] = True
        _save(state)


def scan_now():
    _wake.set()


def start(root_fn, on_new):
    """root_fn() returns the folder to watch (it can change in Settings)."""
    def loop():
        pending = {}
        while True:
            if enabled():
                try:
                    found = []
                    scan(root_fn(), pending, lambda p: (found.append(p), on_new(p)))
                    status.update(last_scan=time.time(), last_error=None, pending=len(pending))
                    if found:
                        status["last_found"] = {"path": str(found[-1]), "at": time.time()}
                        with _lock:  # remember it across restarts
                            st = _load()
                            st["last_found"] = status["last_found"]
                            _save(st)
                except Exception as e:
                    log.exception("watch: scan failed")
                    status.update(last_scan=time.time(), last_error=str(e))
            _wake.wait(config.WATCH_INTERVAL)
            _wake.clear()
    threading.Thread(target=loop, daemon=True, name="watcher").start()
