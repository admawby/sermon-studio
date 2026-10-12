"""Small persistent key/value store (data/settings.json) for things changed from the UI."""
import json
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
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(s))
        tmp.replace(PATH)
