"""Find where the sermon starts and ends in a transcribed service."""
import http.client
import json
import re
import socket
import threading
import urllib.parse

import requests

from . import config

BOOKS = ("Genesis|Exodus|Leviticus|Numbers|Deuteronomy|Joshua|Judges|Ruth|1 Samuel|2 Samuel|1 Kings|2 Kings|"
         "1 Chronicles|2 Chronicles|Ezra|Nehemiah|Esther|Job|Psalms?|Proverbs|Ecclesiastes|Song of Solomon|"
         "Isaiah|Jeremiah|Lamentations|Ezekiel|Daniel|Hosea|Joel|Amos|Obadiah|Jonah|Micah|Nahum|Habakkuk|"
         "Zephaniah|Haggai|Zechariah|Malachi|Matthew|Mark|Luke|John|Acts|Romans|1 Corinthians|2 Corinthians|"
         "Galatians|Ephesians|Philippians|Colossians|1 Thessalonians|2 Thessalonians|1 Timothy|2 Timothy|Titus|"
         "Philemon|Hebrews|James|1 Peter|2 Peter|1 John|2 John|3 John|Jude|Revelation")
SCRIPTURE_RE = re.compile(rf"\b({BOOKS})\s+(?:chapter\s+)?(\d{{1,3}})(?:[:, ]+(?:verses?\s+)?(\d{{1,3}}))?", re.I)
NUM_WORDS = {"first": "1", "second": "2", "third": "3", "one": "1", "two": "2", "three": "3"}


def fmt(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}"


def heuristic(segments, total):
    """Sermon = longest stretch of steady speech (speaking runs ~140 wpm, sung lyrics much slower)."""
    if not segments:
        return {"start": 0, "end": total, "source": "none", "reason": "No speech found."}
    minutes = int(total // 60) + 1
    words = [0] * minutes
    for s in segments:
        words[int(s["start"] // 60)] += len(s["text"].split())
    talky = [w >= 90 for w in words]
    best, i = (0, 0), 0
    while i < minutes:
        if not talky[i]:
            i += 1
            continue
        j = i
        # allow a single slow minute (a pause, a reading) inside a run
        while j + 1 < minutes and (talky[j + 1] or (j + 2 < minutes and talky[j + 2])):
            j += 1
        if j + 1 - i > best[1] - best[0]:
            best = (i, j + 1)
        i = j + 1
    lo, hi = best[0] * 60, best[1] * 60
    inside = [s for s in segments if s["end"] > lo and s["start"] < hi]
    start = inside[0]["start"] if inside else lo
    end = inside[-1]["end"] if inside else hi
    return {"start": max(start - 1, 0), "end": min(end + 2, total), "source": "heuristic",
            "reason": f"Longest run of steady speech ({(end - start) / 60:.0f} min)."}


def find_scripture(text):
    text = re.sub(r"\b(first|second|third|one|two|three)\s+(?=(Samuel|Kings|Chronicles|Corinthians|Thessalonians|Timothy|Peter|John)\b)",
                  lambda m: NUM_WORDS[m.group(1).lower()] + " ", text, flags=re.I)
    counts = {}
    for m in SCRIPTURE_RE.finditer(text):
        ref = f"{m.group(1).title()} {m.group(2)}"
        counts[ref] = counts.get(ref, 0) + 1
    return max(counts, key=counts.get) if counts else ""


class Cancelled(Exception):
    pass


should_stop = lambda: False  # set by jobs.py to the worker's cancel flag


def _ollama(prompt, num_ctx=16384):
    """POST /api/generate, but abortable: closing the socket makes Ollama stop generating."""
    u = urllib.parse.urlsplit(config.OLLAMA_URL)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=3600)
    conn.request("POST", "/api/generate", json.dumps({
        "model": config.LLM_MODEL, "prompt": prompt, "format": "json", "stream": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }), {"Content-Type": "application/json"})
    box = {}

    def wait():
        try:
            r = conn.getresponse()
            box["status"], box["body"] = r.status, r.read()
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=wait, daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.5)
        if should_stop():
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            conn.close()
            raise Cancelled()
    conn.close()
    if "error" in box:
        raise box["error"]
    if box["status"] != 200:
        raise RuntimeError(f"ollama HTTP {box['status']}: {box['body'][:200]!r}")
    return json.loads(json.loads(box["body"])["response"])


def ensure_llm(status):
    """Pull the LLM into the ollama container on first use. Returns False if unavailable."""
    if not config.LLM_MODEL:
        return False
    try:
        tags = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=10).json()
        names = {m["name"] for m in tags.get("models", [])}
        if config.LLM_MODEL not in names and f"{config.LLM_MODEL}:latest" not in names:
            status(f"Downloading {config.LLM_MODEL} (first run only)…")
            r = requests.post(f"{config.OLLAMA_URL}/api/pull", json={"model": config.LLM_MODEL, "stream": False},
                              timeout=7200)
            r.raise_for_status()
        return True
    except Exception:
        return False


def _lines(segments, lo, hi, bucket):
    lines, cur, cur_t = [], [], None
    for s in segments:
        if s["end"] < lo or s["start"] > hi:
            continue
        if cur_t is None or s["start"] - cur_t >= bucket:
            if cur:
                lines.append(f"[{fmt(cur_t)}] {' '.join(cur)[:160 if bucket > 5 else 400]}")
            cur, cur_t = [], s["start"]
        cur.append(s["text"])
    if cur:
        lines.append(f"[{fmt(cur_t)}] {' '.join(cur)[:160 if bucket > 5 else 400]}")
    return "\n".join(lines)


def _secs(v):
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(p) for p in str(v).strip().split(":")]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def llm(segments, total, status):
    """Two passes: coarse pass over the whole service, then pin each edge with full-resolution text."""
    status("Asking the AI where the sermon is…")
    coarse = _ollama(
        "Below is a timestamped transcript of a church worship service (songs, announcements, prayers, "
        "offering, scripture reading, sermon, response song). Identify the SERMON: the main preached message. "
        "It starts at the preacher's first words to the congregation (a greeting, 'open your Bibles', a "
        "personal story or introduction), which often comes several minutes BEFORE his opening prayer. "
        "It does not include songs, announcements, children's dismissal or a scripture reading by someone else. "
        "It ends after the preacher's closing prayer, before any response song, offering or benediction.\n"
        'Reply as JSON: {"start": "H:MM:SS", "end": "H:MM:SS", "reason": "<one sentence>"}\n\n'
        + _lines(segments, 0, total, 30))
    start, end = _secs(coarse["start"]), _secs(coarse["end"])
    status("Fine-tuning the start and end points…")
    edges = {}
    for name, t, what in (("start", start, "the preacher's FIRST words of the sermon: the greeting / 'open your Bibles' / introduction "
                           "right after the kids are dismissed or the scripture is read. The sermon introduction usually "
                           "comes several minutes BEFORE the preacher says 'let's pray', so do not pick the prayer"),
                          ("end", end, "the end of the sermon (after the preacher's closing prayer / final 'amen' of the message)")):
        res = _ollama(f"This is part of a church service transcript. Give the timestamp of {what}.\n"
                      'Reply as JSON: {"time": "H:MM:SS"}\n\n' + _lines(segments, t - 240, t + 240, 0), 8192)
        edges[name] = _secs(res["time"])
    start, end = edges["start"], edges["end"]
    if end <= start:
        raise ValueError("model returned an empty range")
    # snap to segment edges
    start = max([s["start"] for s in segments if s["start"] <= start + 1] or [start])
    ends_after = [s["end"] for s in segments if s["end"] >= end - 1]
    end = min(ends_after or [end])
    return {"start": max(start - 1, 0), "end": min(end + 2, total), "source": "ai", "reason": coarse.get("reason", "")}


def suggest(segments, start, end):
    """Title, main scripture and a short description of the cut."""
    text = " ".join(s["text"] for s in segments if start <= s["start"] <= end)
    out = {"title": "", "scripture": find_scripture(text[:6000]) or find_scripture(text), "description": ""}
    if config.LLM_MODEL and text:
        words = text.split()
        sample = " ".join(words[:2500]) + (" … " + " ".join(words[-700:]) if len(words) > 3200 else "")
        try:
            res = _ollama(
                "Here is the transcript of a sermon. Write:\n"
                "- title: a short sermon title, 2-7 words, Title Case, like 'Grace Upon Grace' or 'Faith in the Storm'\n"
                "- scripture: the main passage preached, as a reference like 'Romans 8:28-39' (book chapter:verses)\n"
                "- description: 2-3 sentences for the church website inviting people to listen. Say what the passage "
                "is about and the main point of the message. Plain, warm language; no hype, no quotes, don't name "
                "the preacher, don't start with 'In this sermon'.\n"
                'Reply as JSON: {"title": "...", "scripture": "...", "description": "..."}\n\n' + sample, 8192)
            out["title"] = res.get("title", "").strip().strip('"')
            out["scripture"] = res.get("scripture", "").strip() or out["scripture"]
            out["description"] = res.get("description", "").strip()
        except Cancelled:
            raise
        except Exception:
            pass
    return out
