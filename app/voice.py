"""Voice recognition: find the sermon by the preacher's voice, and recognise who preached.

1. Speech in the service is cut into ~3 s windows and each gets a "voiceprint" (a speaker embedding
   from a small local model, CAM++ trained on VoxCeleb, run with sherpa-onnx).
2. The longest stretch of continuous speech is almost always the sermon; the preacher's voice is
   learned from its middle.
3. Start = the point where the voice changes most from "someone else" to the preacher.
   End = where speech stops (the sermon is followed by music) after the preacher's last words.
4. The preacher's voiceprint is compared with profiles learned from previously published sermons
   (data/voices.json) to fill in who preached.
"""
import json
import logging
import threading
import urllib.request
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger("sermon.voice")
SR = 16000
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             "wespeaker_en_voxceleb_CAM++.onnx")
MODEL = config.DATA / "models" / "voice" / "wespeaker_en_voxceleb_CAM++.onnx"
PROFILES = config.DATA / "voices.json"
MIN_SERMON = 20 * 60   # a stretch shorter than this isn't treated as a sermon
STEP_SPAN = 90         # seconds compared on each side of a candidate start
END_GAP = 45           # the sermon ends where speech stops for this long

_lock = threading.Lock()
_ext = None


def _extractor():
    global _ext
    with _lock:
        if _ext is None:
            import sherpa_onnx
            if not MODEL.exists():
                MODEL.parent.mkdir(parents=True, exist_ok=True)
                log.info("downloading voice model")
                tmp = MODEL.with_suffix(".part")
                urllib.request.urlretrieve(MODEL_URL, tmp)
                tmp.replace(MODEL)
            _ext = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL), num_threads=4))
        return _ext


def available():
    try:
        import sherpa_onnx  # noqa: F401
        return True
    except ImportError:
        return False


def _windows(audio, win=3.0, min_len=1.0):
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    out = []
    for c in get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300, speech_pad_ms=100)):
        s, e = c["start"], c["end"]
        while e - s >= SR * min_len:
            w = min(e, s + int(SR * win))
            if w - s >= SR * min_len:
                out.append((s, w))
            s = w
    return out


def embed_windows(audio, wins, cancelled=lambda: False):
    ext = _extractor()
    E = []
    for i, (s, e) in enumerate(wins):
        if i % 50 == 0 and cancelled():
            raise InterruptedError
        st = ext.create_stream()
        st.accept_waveform(SR, audio[s:e])
        st.input_finished()
        E.append(ext.compute(st))
    E = np.array(E, dtype=np.float32)
    return E / np.linalg.norm(E, axis=1, keepdims=True)


def analyze(audio, cache: Path, cancelled=lambda: False):
    """Voiceprints for the whole service (cached). Returns (W [n,2] seconds, E [n,d])."""
    if cache.exists():
        d = np.load(cache)
        return d["W"], d["E"]
    wins = _windows(audio)
    E = embed_windows(audio, wins, cancelled)
    W = np.array([(s / SR, e / SR) for s, e in wins])
    np.savez(cache, W=W, E=E)
    return W, E


def _unit(v):
    return v / np.linalg.norm(v)


def find_sermon(W, E):
    """Returns dict(start, end, voice [embedding], sim [per window], thr) or None."""
    if len(W) < 50:
        return None
    # 1) core: longest stretch of speech without a gap over a minute
    best, s = (0, 0), 0
    for i in range(1, len(W) + 1):
        if i == len(W) or W[i, 0] - W[i - 1, 1] > 60:
            if W[i - 1, 1] - W[s, 0] > W[best[1], 1] - W[best[0], 0]:
                best = (s, i - 1)
            s = i
    a, b = best
    if W[b, 1] - W[a, 0] < MIN_SERMON:
        return None
    # 2) the preacher's voice, from the middle of the core
    mid = slice(a + (b - a) // 4, b - (b - a) // 4)
    sim = E @ _unit(E[mid].mean(0))
    core_med = np.median(sim[mid])
    thr = (core_med + np.median(sim[:a])) / 2 if a > 10 else core_med - 0.15
    # 3) rough start: walk back while at least half of the last ~60 s sounds like the preacher
    i = a + (b - a) // 4
    while i > 0 and np.mean(sim[max(0, i - 20):i] >= thr) >= 0.5:
        i -= 1
    while i < b and sim[i] < thr:
        i += 1
    # 4) end: forward until speech stops, then drop trailing words in another voice
    j = i
    while j + 1 < len(W) and W[j + 1, 0] - W[j, 1] <= END_GAP:
        j += 1
    while j > i and np.mean(sim[max(0, j - 2):j + 1]) < thr - 0.05:
        j -= 1
    start, end = W[i, 0], W[j, 1]
    # 5) re-learn the voice from the rough sermon, then put the start where the voice changes most
    L = end - start
    sel = (W[:, 0] > start + .1 * L) & (W[:, 1] < end - .1 * L)
    voice = _unit(E[sel].mean(0))
    sim = E @ voice
    thr = (np.median(sim[sel]) + np.median(sim[~sel])) / 2
    best_score, best_t = -9, start
    for k in range(len(W)):
        t = W[k, 0]
        if abs(t - start) > 600:
            continue
        after = sim[(W[:, 0] >= t) & (W[:, 0] < t + STEP_SPAN)]
        before = sim[(W[:, 0] < t) & (W[:, 0] >= t - STEP_SPAN)]
        if len(after) >= 10 and len(before) >= 3 and after.mean() - before.mean() > best_score:
            best_score, best_t = after.mean() - before.mean(), t
    return {"start": float(best_t), "end": float(end), "voice": voice, "sim": sim, "thr": float(thr)}


def label_segments(segments, W, sim, thr):
    """Mark each transcript segment "P" (preacher's voice) or "-" (anyone/anything else)."""
    for s in segments:
        m = sim[(W[:, 1] > s["start"]) & (W[:, 0] < s["end"])]
        s["voice"] = "P" if len(m) and m.mean() >= thr else "-"
    return segments


# --- who preached ------------------------------------------------------------------------
# Profiles are learned from the raw service recordings of sermons you publish (same mixer and
# room every week), not from the published MP3s: those were processed differently over the years,
# which swamps the difference between voices. Every recording shares a lot (room, PA, the model's
# own bias), so voices are compared after subtracting the average of all analyzed services.
def profiles():
    p = json.loads(PROFILES.read_text()) if PROFILES.exists() else {}
    p.setdefault("background", {"vec": None, "n": 0, "sources": []})
    p.setdefault("people", {})
    return p


def _save(p):
    tmp = PROFILES.with_suffix(".tmp")
    tmp.write_text(json.dumps(p))
    tmp.replace(PROFILES)


def _running(entry, v, source):
    if source and source in entry.get("sources", []):
        return False
    n = entry.get("n", 0)
    entry["vec"] = (v if entry.get("vec") is None else (np.array(entry["vec"]) * n + v) / (n + 1)).tolist()
    entry["n"] = n + 1
    if source:
        entry["sources"] = (entry.get("sources", []) + [source])[-500:]
    return True


def add_background(E, source):
    """Fold one service's voiceprints (everyone and everything in it) into the shared average."""
    with _lock:
        p = profiles()
        if _running(p["background"], np.asarray(E, dtype=np.float64).mean(0), source):
            _save(p)


def learn(name, sermon_vec, source=None):
    """Add a confirmed sermon's (raw) voiceprint to a preacher's profile."""
    if not name or sermon_vec is None:
        return
    with _lock:
        p = profiles()
        entry = p["people"].setdefault(name, {"vec": None, "n": 0, "sources": []})
        if _running(entry, np.asarray(sermon_vec, dtype=np.float64), source):
            _save(p)
            log.info("learned %s's voice (%d sermons)", name, entry["n"])


def identify(sermon_vec, service_mean=None):
    """Compare a sermon's voice with the known preachers.
    Returns {"name": str|None, "score": float, "margin": float, "known": [names]}."""
    p = profiles()
    people = {n: v for n, v in p["people"].items() if v.get("vec") is not None}
    if sermon_vec is None or not people:
        return {"name": None, "score": 0.0, "margin": 0.0, "known": list(people)}
    bg = p["background"]["vec"]
    bg = np.array(bg) if bg is not None else np.asarray(service_mean)
    v = _unit(np.asarray(sermon_vec) - bg)
    scores = sorted(((float(v @ _unit(np.array(e["vec"]) - bg)), n) for n, e in people.items()), reverse=True)
    top, name = scores[0]
    margin = top - scores[1][0] if len(scores) > 1 else top
    sure = top >= IDENTIFY_MIN and (len(scores) == 1 or margin >= IDENTIFY_MARGIN)
    return {"name": name if sure else None, "best": name, "score": round(top, 3), "margin": round(margin, 3),
            "known": [n for _, n in scores]}


# Tuned on five services: the same preacher scored +0.01..+0.82 across services, a different one
# -0.46..-0.77. A profile averages several sermons, so it matches more strongly than single pairs.
IDENTIFY_MIN = 0.15
IDENTIFY_MARGIN = 0.25
