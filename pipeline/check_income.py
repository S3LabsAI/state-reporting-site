"""Compare an income targeting build with a published edition and print a Markdown summary.

Usage: python3 -I pipeline/check_income.py pipeline/out/tx/income-targeting/2026-10 src/data/tx/income-targeting/2026-10

Prints the headline figures side by side and how many ZIP codes changed quartile or target status. With no
published edition (a new state), prints the build's figures alone. Always exits 0: this is a report, and the
person publishing decides.
"""
import json
import sys
from pathlib import Path

KEYS = ["zips", "zipsPriced", "q1", "q2", "q3", "targetZips", "coreZips", "lowConfZips", "targetHouseholds",
        "targetShare", "coreHouseholds", "coreShare", "stateMedian", "topCounties", "countyLine", "metro4Share"]


def load(folder):
    folder = Path(folder)
    if not (folder / "meta.json").exists():
        return None, None
    return (json.loads((folder / "meta.json").read_text()), json.loads((folder / "attrs.json").read_text()))


def main():
    built, ref = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""
    meta, attrs = load(built)
    if meta is None:
        raise SystemExit(f"No build found in {built}")
    rmeta, rattrs = load(ref) if ref else (None, None)
    s = meta["stats"]
    out = ["| Figure | This build | Published |" if rmeta else "| Figure | This build |",
           "|---|---:|---:|" if rmeta else "|---|---:|"]
    for k in KEYS:
        if rmeta:
            r = rmeta["stats"].get(k)
            mark = "" if r == s.get(k) else " ⚠"
            out.append(f"| {k} | {s.get(k)}{mark} | {r} |")
        else:
            out.append(f"| {k} | {s.get(k)} |")
    if rattrs:
        mine = {z["id"]: z for z in attrs["zip"]}
        theirs = {z["id"]: z for z in rattrs["zip"]}
        both = mine.keys() & theirs.keys()
        q = sum(1 for z in both if mine[z]["quartile"] != theirs[z]["quartile"])
        st = sum(1 for z in both if mine[z]["status"] != theirs[z]["status"])
        out += ["", f"ZIP codes in both: {len(both)} (only in this build: {len(mine.keys() - theirs.keys())}, "
                    f"only in published: {len(theirs.keys() - mine.keys())}).",
                f"Quartile changed: {q}. Target status changed: {st}."]
    print("\n".join(out))


if __name__ == "__main__":
    main()
