"""Pick (and fill) the NAS folder a service is archived in: <root>/YYYY/MM - Month/YYYY_MM_DD/.

The folder layout is learned from what's already in the archive, so it follows whatever naming
the existing folders use."""
import calendar
import re
import shutil
from collections import Counter
from datetime import date
from pathlib import Path

from . import config

DAY_FORMATS = {
    "underscore": (re.compile(r"^\d{4}_\d{2}_\d{2}$"), "{y:04d}_{m:02d}_{d:02d}"),
    "dash": (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "{y:04d}-{m:02d}-{d:02d}"),
    "compact": (re.compile(r"^\d{8}$"), "{y:04d}{m:02d}{d:02d}"),
}


def _dirs(p: Path):
    try:
        return [c for c in p.iterdir() if c.is_dir() and not c.name.startswith((".", "@", "#"))]
    except OSError:
        return []


def _month_dir(year_dir: Path, m: int, examples):
    name = calendar.month_name[m]
    for c in _dirs(year_dir):
        if re.match(rf"^0?{m}\b", c.name) or name.lower() in c.name.lower():
            return c.name
    # copy the style of other years' month folders, e.g. "10 - October" or "10-October"
    for ex in examples:
        mm = re.match(r"^(\d{1,2})(\s*-\s*|\s+|_)([A-Za-z]+)$", ex)
        if mm:
            return f"{m:0{len(mm.group(1))}d}{mm.group(2)}{name}"
    return f"{m:02d} - {name}"


def recommend(d: date):
    """Return the virtual path (e.g. 'NAS/Sermons/2026/10 - October/2026_10_11') and whether it exists."""
    root_virtual = config.ARCHIVE_ROOT.strip("/")
    parts = root_virtual.split("/")
    root = config.ROOTS[parts[0]].joinpath(*parts[1:])
    years = sorted((c for c in _dirs(root) if re.fullmatch(r"\d{4}", c.name)), key=lambda c: c.name, reverse=True)
    month_examples = [c.name for y in years[:2] for c in _dirs(y)]
    day_names = [c.name for y in years[:2] for mdir in _dirs(y) for c in _dirs(mdir)]

    year_dir = root / f"{d.year}"
    month = _month_dir(year_dir, d.month, month_examples)
    month_dir = year_dir / month
    # an existing folder for this date in any style wins
    for c in _dirs(month_dir):
        for rx, fmt in DAY_FORMATS.values():
            if c.name == fmt.format(y=d.year, m=d.month, d=d.day):
                return {"folder": f"{root_virtual}/{d.year}/{month}/{c.name}", "exists": True}
    counts = Counter(k for n in day_names for k, (rx, _) in DAY_FORMATS.items() if rx.match(n))
    style = counts.most_common(1)[0][0] if counts else "underscore"
    day = DAY_FORMATS[style][1].format(y=d.year, m=d.month, d=d.day)
    return {"folder": f"{root_virtual}/{d.year}/{month}/{day}", "exists": False}


def copy_into(src: Path, folder: Path, ours=(), name: str | None = None):
    """Copy a file into the archive folder. Never overwrites a file this app didn't write itself
    (e.g. a hand-made MP3 already in the folder); picks "name (2).mp3" instead."""
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / (name or src.name)
    if dest.exists() and src.resolve() == dest.resolve():
        return dest
    n = 2
    while dest.exists() and dest.name not in ours:
        if dest.stat().st_size == src.stat().st_size:
            return dest  # already archived
        dest = folder / f"{Path(name or src.name).stem} ({n}){Path(name or src.name).suffix}"
        n += 1
    tmp = folder / f".{dest.name}.part"
    shutil.copyfile(src, tmp)
    tmp.replace(dest)
    return dest
