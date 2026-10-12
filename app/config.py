import os
from pathlib import Path


def env(name, default=""):
    return os.environ.get(name, default).strip()


DATA = Path(env("DATA_DIR", "/data"))
WORK = DATA / "work"
UPLOADS = DATA / "uploads"
OUTPUT = DATA / "output"
for d in (WORK, UPLOADS, OUTPUT):
    d.mkdir(parents=True, exist_ok=True)

ROOTS = {}
for part in env("BROWSE_ROOTS", "Uploads=/data/uploads").split(","):
    if "=" in part:
        name, path = part.split("=", 1)
        ROOTS[name.strip()] = Path(path.strip())
ROOTS.setdefault("Uploads", UPLOADS)

START_PATH = env("START_PATH", "")
# where service recordings + finished MP3s are archived (a virtual path under one of the roots)
ARCHIVE_ROOT = env("ARCHIVE_ROOT", "NAS")
WHISPER_MODEL = env("WHISPER_MODEL", "auto")
LLM_MODEL = env("LLM_MODEL", "")
OLLAMA_URL = env("OLLAMA_URL", "http://ollama:11434").rstrip("/")
MP3_BITRATE = env("MP3_BITRATE", "64k")
FILE_PREFIX = env("FILE_PREFIX", "Sermon_")

WP_URL = env("WP_URL").rstrip("/")
WP_USER = env("WP_USER")
WP_PASSWORD = env("WP_PASSWORD")
WP_POST_TYPE = env("WP_POST_TYPE", "podcasts")
WP_ALWAYS_CATEGORY = env("WP_ALWAYS_CATEGORY", "All Sermons")
WP_DEFAULT_STATUS = env("WP_DEFAULT_STATUS", "draft")

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".aif", ".aiff", ".ogg", ".mp4", ".mov"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
