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


def encode(src: Path, out: Path, start: float, end: float, bitrate: str, normalize: bool,
           tags: dict, cover: Path | None):
    length = end - start
    filters = ["highpass=f=70"]
    if normalize:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    filters += ["afade=t=in:d=0.5", f"afade=t=out:st={max(length - 2, 0):.2f}:d=2"]
    tmp = out.with_suffix(".tmp.mp3")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(src),
         "-vn", "-af", ",".join(filters), "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame",
         "-b:a", bitrate, "-id3v2_version", "3", str(tmp)])
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
