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

# --- automation: watch the archive for new recordings, report + approve over Discord ---
WATCH_ENABLED = env("WATCH_ENABLED", "false").lower() in ("1", "true", "yes")
WATCH_INTERVAL = int(env("WATCH_INTERVAL", "300"))
WATCH_EXT = {e.strip().lower() for e in env("WATCH_EXT", ".wav").split(",") if e.strip()}
DISCORD_TOKEN = env("DISCORD_TOKEN")
DISCORD_USER_ID = int(env("DISCORD_USER_ID", "0") or 0)
DISCORD_CHANNEL_ID = int(env("DISCORD_CHANNEL_ID", "0") or 0)
PUBLIC_URL = env("PUBLIC_URL").rstrip("/")  # address on the home network
PUBLIC_URL_REMOTE = env("PUBLIC_URL_REMOTE").rstrip("/")
# used when the preacher's voice isn't recognized with confidence (one of the WordPress People)
DEFAULT_PREACHER = env("DEFAULT_PREACHER")
# where the sermon ends: "after_prayer" (after the closing prayer's amen) or "before_prayer" (at "Let's pray")
END_STYLE = env("END_STYLE", "after_prayer")

# --- Life Group discussion guides ---
# folder (e.g. NAS/Documents/Life Group) with a few of your own past guides (.docx/.md/.txt): the newest two
# are shown to the AI as examples of your format and tone. Blank = a generic built-in format.
GUIDE_EXAMPLES_PATH = env("GUIDE_EXAMPLES_PATH")
# when a sermon is published, also write its guide and post the Word file in the Discord channel
GUIDE_DISCORD = env("GUIDE_DISCORD", "false").lower() in ("1", "true", "yes", "on")

# --- settings the web UI can change (Settings tab / first-run setup) ---------------------------
# Stored in data/settings.json under "config"; they override the values from .env above.
EDITABLE = {
    "ARCHIVE_ROOT": str, "START_PATH": str, "FILE_PREFIX": str,
    "WP_URL": str, "WP_USER": str, "WP_PASSWORD": str,
    "DISCORD_TOKEN": str, "DISCORD_USER_ID": int, "DISCORD_CHANNEL_ID": int, "PUBLIC_URL": str,
    "PUBLIC_URL_REMOTE": str, "DEFAULT_PREACHER": str, "END_STYLE": str, "GUIDE_EXAMPLES_PATH": str, "GUIDE_DISCORD": bool,
}
SECRET = {"WP_PASSWORD", "DISCORD_TOKEN"}
_ENV = {k: globals()[k] for k in EDITABLE}


def overrides():
    import json
    p = DATA / "settings.json"
    return json.loads(p.read_text()).get("config", {}) if p.exists() else {}


def apply():
    """(Re)load the editable settings: UI overrides first, then .env."""
    over = overrides()
    for key, typ in EDITABLE.items():
        v = over.get(key, _ENV[key])
        if typ is int:
            v = int(v or 0)
        elif typ is bool:
            v = v if isinstance(v, bool) else str(v or "").strip().lower() in ("1", "true", "yes", "on")
        else:
            v = str(v or "").strip()
            if key in ("WP_URL", "PUBLIC_URL", "PUBLIC_URL_REMOTE"):
                v = v.rstrip("/")
        globals()[key] = v


apply()

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".aif", ".aiff", ".ogg", ".mp4", ".mov"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
