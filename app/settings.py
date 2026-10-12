"""Small persistent key/value store (data/settings.json) for things changed from the UI."""
import json
import os
import threading

from . import config

PATH = config.DATA / "settings.json"
_lock = threading.Lock()


def all():
    return json.loads(PATH.read_text()) if PATH.exists() else {}


def get(key, default=None):
    return all().get(key, default)


def set(**kw):
    with _lock:
        s = all()
        s.update(kw)
        _write(s)


def set_config(updates: dict):
    """Save UI-edited settings (see config.EDITABLE) and reload them."""
    with _lock:
        s = all()
        s.setdefault("config", {}).update(updates)
        _write(s)
    config.apply()


def _write(s):
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    os.chmod(tmp, 0o600)  # holds the WordPress password and Discord token
    tmp.replace(PATH)
