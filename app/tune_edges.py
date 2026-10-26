"""Re-tune the sermon start/end scorer (app/edges.py) on hand-made cuts.

Uses the services in the current batch (data/batch.json) whose hand-made MP3 was located in the
recording ("Your cut" in the Batch review). Run a batch over past recordings with every option off
first, then:

    docker exec -w /srv sermon-studio python -m app.tune_edges          # report only
    docker exec -w /srv sermon-studio python -m app.tune_edges --save   # also write data/edge_weights.json

Accuracy is measured with 5-fold cross-validation: the weights are tuned on 4/5 of the services and
scored on the other 1/5, so the numbers reflect services the tuning didn't see. Ends are only judged
on cuts that end after the closing prayer's "amen" (the default END_STYLE).
"""
import json
import random
import re
import sys

import numpy as np

from . import config, edges, voice

f = lambda x: "%d:%02d" % (x // 60, x % 60)


def load():
    st = json.loads((config.DATA / "batch.json").read_text())
    out = []
    for it in st["items"]:
        h = it.get("hand")
        if it.get("state") != "done" or not h or not it.get("job") or h["start"] < 30:
            continue
        if not 15 * 60 <= h["end"] - h["start"] <= 80 * 60:  # not a sermon cut
            continue
        try:
            d = np.load(config.WORK / it["job"] / "voice.npz")
            segs = json.loads((config.WORK / it["job"] / "transcript.json").read_text())
        except FileNotFoundError:
            continue
        r = voice.find_sermon(d["W"], d["E"])
        if not r:
            continue
        P = (r["sim"] >= r["thr"] - 0.05).astype(float)
        starts, ends = edges._features(segs, d["W"], P, r["start"], r["end"])
        if not starts or not ends:
            continue
        out.append({"date": it["date"], "ts": h["start"], "te": h["end"], "ai": it.get("ai"),
                    "S": (np.array([[c[1][k] for k in edges.WEIGHTS["start"]] for c in starts]), np.array([c[0] for c in starts])),
                    "E": (np.array([[c[1][k] for k in edges.WEIGHTS["end"]] for c in ends]), np.array([c[0] for c in ends])),
                    "after_amen": any(re.search(r"amen", s["text"], re.I) and abs(s["end"] - h["end"]) <= 15 for s in segs)})
    return out


def pick(x, w, side):
    M, T = x["S"] if side == "start" else x["E"]
    keys = list(edges.WEIGHTS[side])
    return T[int(np.argmax(M @ np.array([w[k] * (-1 if k in edges.PENALTY else 1) for k in keys])))]


def fit(group, side, seed=0, n=1500):
    keys, key = list(edges.WEIGHTS[side]), "ts" if side == "start" else "te"
    rng, truth = np.random.default_rng(seed), np.array([x[key] for x in group])
    loss = lambda w: np.minimum(np.abs(np.array([pick(x, w, side) for x in group]) - truth), 300).mean()
    best, bl = None, None
    for _ in range(n):
        w = dict(zip(keys, rng.choice([0, 0.25, 0.5, 1, 2, 4, 8], len(keys))))
        if w["switch"] and (bl is None or loss(w) < bl):
            best, bl = w, loss(w)
    for _ in range(3):
        for k in keys:
            for m in (0, 0.5, 0.8, 1.25, 2):
                w = dict(best)
                w[k] = best[k] * m if best[k] else (0.25 if m else 0)
                if w["switch"] and loss(w) < bl:
                    best, bl = w, loss(w)
    return {k: round(float(v), 3) for k, v in best.items()}


def report(name, errs):
    a = np.abs(np.array(errs))
    return (f"{name:28s} n={len(a):3d}  within 10s {np.mean(a <= 10):4.0%}  30s {np.mean(a <= 30):4.0%}  "
            f"60s {np.mean(a <= 60):4.0%}  over 5 min {int((a > 300).sum()):2d}  median {np.median(a):4.0f}s")


def main():
    items = load()
    if len(items) < 10:
        sys.exit(f"Only {len(items)} services with a located hand cut; run a batch over past recordings first.")
    ends = [x for x in items if x["after_amen"]]
    cur = edges.weights()
    print(report("start, current weights", [pick(x, cur["start"], "start") - x["ts"] for x in items]))
    print(report("end, current weights", [pick(x, cur["end"], "end") - x["te"] for x in ends]))
    for side, group, key in (("start", items, "ts"), ("end", ends, "te")):
        random.seed(1)
        order = group[:]
        random.shuffle(order)
        errs = []
        for k in range(5):
            w = fit([x for i, x in enumerate(order) if i % 5 != k], side, seed=k)
            errs += [pick(x, w, side) - x[key] for x in order[k::5]]
        print(report(f"{side}, re-tuned (held-out)", errs))
    new = {"start": fit(items, "start"), "end": fit(ends, "end")}
    print("re-tuned weights:", json.dumps(new))
    if "--save" in sys.argv:
        (config.DATA / "edge_weights.json").write_text(json.dumps(new, indent=1))
        print("saved to data/edge_weights.json (used from the next analysis on)")


if __name__ == "__main__":
    main()
