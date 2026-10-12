import threading

from . import config

_lock = threading.Lock()
_model = None
_model_info = {}


def device():
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def model_info():
    dev = device()
    name = config.WHISPER_MODEL
    if name == "auto":
        name = "large-v3-turbo" if dev == "cuda" else "small.en"
    return {"device": dev, "model": name}


def _load():
    global _model, _model_info
    info = model_info()
    if _model is None or info != _model_info:
        from faster_whisper import WhisperModel
        compute = "float16" if info["device"] == "cuda" else "int8"
        _model = WhisperModel(info["model"], device=info["device"], compute_type=compute,
                              download_root=str(config.DATA / "models" / "whisper"))
        _model_info = info
    return _model


def _load_wav(path):
    import wave

    import numpy as np
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def transcribe(wav_path, total_seconds, progress):
    """Return [{start, end, text}] for the whole service; progress(fraction) as it goes."""
    with _lock:
        model = _load()
        on_gpu = _model_info["device"] == "cuda"
        segments, _ = model.transcribe(
            _load_wav(wav_path), language="en", vad_filter=True, beam_size=5 if on_gpu else 1,
            condition_on_previous_text=False,
        )
        out = []
        for s in segments:
            out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()})
            progress(min(s.end / total_seconds, 1.0))
        return out
