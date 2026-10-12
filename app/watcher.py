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

from . import config

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


def start(root: Path, on_new):
    def loop():
        pending = {}
        while True:
            try:
                scan(root, pending, on_new)
            except Exception:
                log.exception("watch: scan failed")
            time.sleep(config.WATCH_INTERVAL)
    threading.Thread(target=loop, daemon=True, name="watcher").start()
