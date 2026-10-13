import json
import subprocess
from pathlib import Path


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr.strip()[-800:]}")
    return p.stdout


def duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    return float(json.loads(out)["format"]["duration"])


def prepare(src: Path, work: Path):
    """Make the 16 kHz WAV for whisper, a seekable preview MP3 and a waveform image."""
    if not (work / "speech.wav").exists():
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
             str(work / "speech.tmp.wav")])
        (work / "speech.tmp.wav").rename(work / "speech.wav")
    if not (work / "preview.mp3").exists():
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "22050",
             "-b:a", "48k", str(work / "preview.tmp.mp3")])
        (work / "preview.tmp.mp3").rename(work / "preview.mp3")
    if not (work / "waveform.png").exists():
        run(["ffmpeg", "-y", "-v", "error", "-i", str(work / "speech.wav"), "-filter_complex",
             "compand,showwavespic=s=2400x160:colors=#3b6ea8", "-frames:v", "1",
             str(work / "waveform.png")])


def run_progress(cmd, length, progress):
    """Run ffmpeg with -progress on stdout, calling progress(fraction 0..1) as it goes."""
    p = subprocess.Popen(cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in p.stdout:
        if line.startswith("out_time_us=") and line.strip() != "out_time_us=N/A":
            progress(min(int(line.split("=", 1)[1]) / 1e6 / length, 1.0))
    err = p.stderr.read()
    if p.wait() != 0:
        raise RuntimeError(f"ffmpeg failed: {err.strip()[-800:]}")


def encode(src: Path, out: Path, start: float, end: float, bitrate: str, normalize: bool,
           tags: dict, cover: Path | None, progress=lambda f: None):
    length = end - start
    filters = ["highpass=f=70"]
    if normalize:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    filters += ["afade=t=in:d=0.5", f"afade=t=out:st={max(length - 2, 0):.2f}:d=2"]
    tmp = out.with_suffix(".tmp.mp3")
    run_progress(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(src),
                  "-vn", "-af", ",".join(filters), "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame",
                  "-b:a", bitrate, "-id3v2_version", "3", str(tmp)], length, progress)
    write_tags(tmp, tags, cover)
    tmp.rename(out)


def write_tags(path: Path, tags: dict, cover: Path | None):
    from mutagen.id3 import APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1

    id3 = ID3()
    id3.add(TIT2(encoding=3, text=tags.get("title", "")))
    id3.add(TPE1(encoding=3, text=tags.get("speaker", "")))
    if tags.get("series"):
        id3.add(TALB(encoding=3, text=tags["series"]))
    if tags.get("date"):
        id3.add(TDRC(encoding=3, text=tags["date"]))
    if tags.get("scripture"):
        id3.add(COMM(encoding=3, lang="eng", desc="", text=tags["scripture"]))
    id3.add(TCON(encoding=3, text="Podcast"))
    if cover and cover.exists():
        mime = "image/png" if cover.suffix.lower() == ".png" else "image/jpeg"
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover.read_bytes()))
    id3.save(path, v2_version=3)


def clip(src: Path, out: Path, start: float, length: float):
    """Short low-bitrate MP3 preview (for Discord)."""
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(start, 0):.2f}", "-t", f"{length:.2f}", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "44100", "-b:a", "64k", str(out)])
    return out


def _pcm(path, sr):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-af", "highpass=f=100",
                          "-f", "s16le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32)


def locate(full: Path, cut: Path, sr=2000):
    """Find where an edited MP3 (e.g. a hand-made sermon cut) sits inside the full recording, by
    cross-correlating two 30 s snippets. Returns {"start", "end"} in seconds, or None if unsure."""
    import numpy as np
    F, C = _pcm(full, sr), _pcm(cut, sr)
    if len(C) < sr * 180 or len(F) < len(C):
        return None
    n = 1 << int(np.ceil(np.log2(len(F) + sr * 30)))
    FF = np.fft.rfft(F, n)

    def offset(at):
        snip = C[int(sr * at):int(sr * (at + 30))]
        c = np.fft.irfft(FF * np.conj(np.fft.rfft(snip, n)), n)[:len(F) - len(snip)]
        k = int(c.argmax())
        score = c[k] / (np.linalg.norm(snip) * np.linalg.norm(F[k:k + len(snip)]) + 1e-9)
        return k / sr - at, score
    L = len(C) / sr
    (o1, s1), (o2, s2) = offset(60), offset(L - 90)
    if abs(o1 - o2) > 1.5 or min(s1, s2) < 0.5:
        return None
    return {"start": round(o1, 2), "end": round(o1 + L, 2)}
