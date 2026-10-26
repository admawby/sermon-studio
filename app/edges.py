"""Pick the exact first and last line of the sermon.

The voice step (voice.find_sermon) finds roughly where the preacher's long talk is. Here every
transcript line near that region is a candidate start or end, scored on several clues at once:

start  switch      the voice changes from someone else to the preacher here (4 min after vs 2.5 min before)
       v10         the next 10 minutes are mostly the preacher (a scripture reading or prayer by someone
                   with a similar voice lasts minutes; the sermon keeps going)
       greet       the line is a greeting / "open your Bibles"
       last_cue    ...and no other greeting follows within 10 minutes (the reader greets first, the
                   preacher last)
       amen_greet  an "amen" right before the greeting (the children's prayer ends, the preacher starts)
       reading     penalty: someone announces a scripture reading ("we're going to read…")
       long_gap    a long pause before the line (e.g. after a video)
       dist        penalty: distance from the voice-based estimate
end    switch      the preacher's voice stops here
       amen        the line ends with "amen" (his closing prayer)
       after_cue   followed by "please stand" / "let's sing"
       gap         a pause after the line
       lyrics_after  a song starts: the next 3 minutes repeat themselves (choruses), the last 3 didn't
       dist        penalty: distance from the voice-based estimate

The weights were tuned on the hand-made cuts of 104 past services (5-fold, scored on services the tuning
didn't see): starts within 30 s 84% (was 66%), off by more than 5 minutes 1 (was 14); ends within 30 s
94% (was 76%). See README "Finding the sermon".
"""
import json
import re

import numpy as np

from . import config

WEIGHTS = {
    "start": {"switch": 0.625, "v10": 3.2, "greet": 0.5, "last_cue": 2.0, "amen_greet": 2.0, "dist": 1.0,
              "reading": 4.0, "long_gap": 2.0},
    "end": {"switch": 2.0, "amen": 8.0, "after_cue": 0.0, "gap": 0.0, "dist": 4.0, "lyrics_after": 4.0},
}
START_BACK = 1200   # look this far before the voice-based start (it's sometimes late)
START_FWD = 600
END_BACK, END_FWD = 600, 360
PENALTY = {"dist", "reading"}

GREET = re.compile(r"\b(good morning|morning,? (church|everyone|everybody|y'?all)|good to see (you|everyone)|"
                   r"welcome (back|everyone|this morning)|open (up )?your bibles|turn (with me )?(in|to) your bibles|"
                   r"grab your bibles|if you have your bibles)", re.I)
READING = re.compile(r"\b((we'?re|i'?m) (going to|gonna) (read|be reading)|let'?s read|our (scripture )?reading|"
                     r"(scripture )?reading (this morning|today) (is|comes|will be)|read (our|the) scripture|"
                     r"going to read (from|out of))", re.I)
AFTER_END = re.compile(r"\b(please stand|let'?s stand|stand with me|let'?s sing|as we sing|we'?re going to sing|"
                       r"come on up|worship team|band)", re.I)
PRAY = re.compile(r"\blet'?s pray\b|\blet us pray\b|\bpray with me\b|\bbow (your|our) heads\b", re.I)


def weights():
    """Built-in weights, or data/edge_weights.json if it exists (from a later re-tune)."""
    p = config.DATA / "edge_weights.json"
    if p.exists():
        try:
            w = json.loads(p.read_text())
            return {"start": {**WEIGHTS["start"], **w.get("start", {})}, "end": {**WEIGHTS["end"], **w.get("end", {})}}
        except ValueError:
            pass
    return WEIGHTS


def _norm(t):
    return re.sub(r"[^a-z ]", "", t.lower()).strip()


def _lyrics(segs, lo, hi):
    """How song-like [lo, hi) is: the share of lines whose words come back within a few minutes
    (choruses repeat; a sermon hardly ever repeats a whole line)."""
    lines = [_norm(z["text"]) for z in segs if lo <= z["start"] < hi]
    lines = [l for l in lines if len(l.split()) >= 3]
    if len(lines) < 4:
        return 0.0
    near = [_norm(z["text"]) for z in segs if lo - 60 <= z["start"] < hi + 180]
    rep = 0
    for l in lines:
        words = set(l.split())
        if sum(1 for n in near if n != l and len(words & set(n.split())) >= max(3, int(0.7 * len(words)))) >= 1 \
                or near.count(l) > 1:
            rep += 1
    return rep / len(lines)


def _features(segs, W, P, vs, ve):
    def frac(lo, hi):
        sel = (W[:, 0] >= lo) & (W[:, 0] < hi)
        return P[sel].mean() if sel.sum() >= 3 else 0.0

    starts, ends = [], []
    for i, s in enumerate(segs):
        t = s["start"]
        if vs - START_BACK <= t <= vs + START_FWD:
            prev = segs[i - 1] if i else None
            nxt = " ".join(z["text"] for z in segs[i:i + 2] if z["start"] - t < 5)
            soon = " ".join(z["text"] for z in segs[i:i + 8] if z["start"] - t < 45)
            here = " ".join(z["text"] for z in segs[max(0, i - 1):i + 2] if abs(z["start"] - t) < 20)
            cue = bool(GREET.search(nxt))
            # a greeting by someone else later on (the same person going on within 90 s doesn't count)
            later = any(GREET.search(z["text"]) for z in segs[i + 2:i + 200] if 90 < z["start"] - t < 600)
            starts.append((t, {
                "switch": frac(t, t + 240) - frac(t - 150, t), "v10": frac(t, t + 600),
                "greet": float(cue), "last_cue": float(cue and not later),
                "amen_greet": float(cue and bool(re.search(r"amen", here, re.I))),
                "reading": float(bool(READING.search(soon))),
                "long_gap": float(bool(prev and t - prev["end"] > 45)),
                "dist": abs(t - vs) / 600}))
        # end candidates: the end of each line, plus the moment of an "amen" in the middle of a line
        # (the transcript often joins "…in Jesus' name. Amen. Please stand." into one line)
        text = s["text"].strip()
        cands = [(s["end"], bool(re.search(r"amen\.?\s*$", text, re.I)), "")]
        for m in re.finditer(r"\bamen\b\.?", text, re.I):
            if m.end() < len(text) - 2:
                frac_pos = m.end() / max(len(text), 1)
                cands.append((s["start"] + (s["end"] - s["start"]) * frac_pos, True, text[m.end():]))
        for t, amen, rest in cands:
            if not ve - END_BACK <= t <= ve + END_FWD:
                continue
            nxt = segs[i + 1] if i + 1 < len(segs) else None
            follow = rest + " " + " ".join(z["text"] for z in segs[i + 1:i + 4] if z["start"] - t < 40)
            ends.append((t, {
                "switch": frac(t - 240, t) - frac(t, t + 150),
                "amen": float(amen),
                "after_cue": float(bool(AFTER_END.search(follow))),
                "lyrics_after": _lyrics(segs, t + 5, t + 185) - _lyrics(segs, t - 185, t - 5),
                "gap": 0.0 if rest.strip() else min((nxt["start"] - t) if nxt else 30, 20) / 20,
                "dist": abs(t - ve) / 600}))
    return starts, ends


def _best(cands, w):
    score = lambda f: sum(w.get(k, 0) * (-v if k in PENALTY else v) for k, v in f.items())
    return max(cands, key=lambda c: score(c[1]))[0] if cands else None


def find(segs, W, sim, thr, vs, ve):
    """(start, end) of the sermon in seconds, or None where there's nothing to choose from."""
    P = (sim >= thr - 0.05).astype(float)
    starts, ends = _features(segs, W, P, vs, ve)
    w = weights()
    start, end = _best(starts, w["start"]), _best(ends, w["end"])
    if end is not None and config.END_STYLE == "before_prayer":
        # house style: stop where the closing prayer begins ("Let's pray") instead of after its amen
        pray = [s for s in segs if PRAY.search(s["text"]) and end - 240 <= s["start"] < end]
        if pray:
            end = pray[-1]["end"]  # just after "Let's pray."
    return start, end


def options(segs, W, sim, thr, vs, ve, k=5, spread=15):
    """The best k candidate starts and ends, best first, at least `spread` s apart (so the list isn't
    five versions of the same sentence). Each: {"t", "score", "text"}."""
    P = (sim >= thr - 0.05).astype(float)
    starts, ends = _features(segs, W, P, vs, ve)
    w = weights()
    out = {}
    for side, cands in (("start", starts), ("end", ends)):
        ww = w[side]
        scored = sorted(((sum(ww.get(key, 0) * (-v if key in PENALTY else v) for key, v in f.items()), t)
                         for t, f in cands), reverse=True)
        picked = []
        for score, t in scored:
            if all(abs(t - p["t"]) >= spread for p in picked):
                picked.append({"t": round(float(t), 2), "score": round(float(score), 3), "text": _snippet(segs, t, side)})
            if len(picked) == k:
                break
        out[side] = picked
    if config.END_STYLE == "before_prayer":  # keep the list consistent with find()
        s, e = find(segs, W, sim, thr, vs, ve)
        if out["end"] and e is not None and all(abs(o["t"] - e) > 1 for o in out["end"]):
            out["end"].insert(0, {"t": round(float(e), 2), "score": None, "text": _snippet(segs, e, "end")})
            out["end"] = out["end"][:k]
    return out


def _snippet(segs, t, side, n=110):
    """The words at a candidate edge: what's said from a start onwards, or leading up to an end."""
    if side == "start":
        text = " ".join(s["text"] for s in segs if t - 0.5 <= s["start"] < t + 25)
        return (text[:n] + "…") if len(text) > n else text
    words = []
    for s in segs:
        if s["start"] >= t + 0.5 or s["end"] < t - 30:
            continue
        txt = s["text"]
        if s["end"] > t + 0.5:  # the edge falls inside this line (e.g. an "amen" mid-line): cut the text there
            m = list(re.finditer(r"\bamen\b\.?", txt, re.I))
            txt = txt[:m[0].end()] if m else txt
        words.append(txt)
    text = " ".join(words)
    return ("…" + text[-n:]) if len(text) > n else text
