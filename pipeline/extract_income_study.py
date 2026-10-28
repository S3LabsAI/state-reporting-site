"""Phase 1 only: lift a state's income targeting data out of the original single-file income study.

Writes
  src/data/<st>/geo-2020.json                            shapes, state outline, city label points
  src/data/<st>/income-targeting/<edition>/attrs.json    ZIP and county values
  src/data/<st>/income-targeting/<edition>/meta.json     headline figures and chart values

Usage: python3 -I pipeline/extract_income_study.py --state TX --edition 2026-10 path/to/original.html

The study embeds Census ACS 2020-2024 values for every ZCTA and county in `const D = {...};`, already
projected shapes (relative SVG paths) and city label points. This keeps the Census values and drops
everything else in the file. The report figures are then computed by income_targeting.build(), the same
code the full pipeline (income_targeting.py) runs on raw downloads, so the two paths agree by construction.
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, load_config, load_original, write_json
from income_targeting import build

# Statewide values quoted in the study's data section (ACS 2020-2024): owner-occupied share of occupied homes.
STATE_OWNER_RATE = {"TX": 62.6}
STATE_MEDIAN = {"TX": 78476}
US_MEDIAN = 80734


def absolute(d):
    """Relative `M x y l dx dy ... z` paths to the absolute M/L/Z form the map script reads."""
    out, x, y, start = [], 0.0, 0.0, (0.0, 0.0)
    for cmd, args in re.findall(r"([MmLlZz])([^MmLlZz]*)", d):
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", args)]
        if cmd in "Zz":
            out.append("Z")
            x, y = start
            continue
        pts = []
        for i in range(0, len(nums), 2):
            if cmd in "ml":
                x, y = x + nums[i], y + nums[i + 1]
            else:
                x, y = nums[i], nums[i + 1]
            if i == 0 and cmd in "Mm":
                start = (x, y)
            pts.append(f"{x:.1f} {y:.1f}")
        out.append(("M" if cmd in "Mm" else "L") + pts[0] + "".join("L" + p for p in pts[1:]))
    return "".join(out)


def stitch(d, tol=0.25):
    """Join the study's open state-border segments into closed rings (the site needs a filled outline)."""
    lines = []
    for part in absolute(d).split("M")[1:]:
        pts = [tuple(map(float, p.split())) for p in part.replace("Z", "").split("L")]
        lines.append(pts)
    near = lambda a, b: abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol
    rings = []
    while lines:
        ring = lines.pop(0)
        grown = True
        while grown and not near(ring[0], ring[-1]):
            grown = False
            for i, seg in enumerate(lines):
                if near(ring[-1], seg[0]):
                    ring += seg[1:]
                elif near(ring[-1], seg[-1]):
                    ring += seg[::-1][1:]
                elif near(ring[0], seg[-1]):
                    ring[:0] = seg[:-1]
                elif near(ring[0], seg[0]):
                    ring[:0] = seg[::-1][:-1]
                else:
                    continue
                lines.pop(i)
                grown = True
                break
        rings.append(ring)
    closed = [r for r in rings if len(r) > 3 and near(r[0], r[-1])]
    if len(closed) != len(rings):
        raise SystemExit(f"State border did not close: {len(rings) - len(closed)} open pieces")
    return "".join("M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in r[:-1]) + "Z" for r in closed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--edition", required=True)
    ap.add_argument("source", help="the original single-file study (kept outside the repo)")
    args = ap.parse_args()
    st = args.state.upper()
    D = load_original(args.source)
    report = f"{st.lower()}-income-targeting"
    places = load_config(report)["map"]["places"]

    # ZIP rows: zip, place name, county, metro, median income, MOE %, households, owner-occupied %,
    # median home value, (study-only columns), county FIPS.
    zip_rows = [{"id": z[0], "name": z[1], "county": z[2], "countyFips": z[11], "metro": z[3], "income": z[4],
                 "incomeMoe": z[5], "households": z[6], "occupied": z[6],
                 "ownerHomes": round((z[7] or 0) * z[6] / 100), "homeValue": z[8]} for z in D["z"]]
    # County rows: FIPS, name, median income, MOE $, (cross-check), (cross-check), households, owner-occupied %, metro.
    county_rows = [{"id": c[0], "name": c[1], "metro": c[8], "income": c[2],
                    "incomeMoe": round(c[3] / c[2] * 100, 1) if c[2] else None, "households": c[6],
                    "occupied": c[6], "ownerHomes": round(c[7] * c[6] / 100), "homeValue": None} for c in D["c"]]
    state_vals = {"income": STATE_MEDIAN[st], "ownerHomes": STATE_OWNER_RATE[st] * 10, "occupied": 1000}
    attrs, meta = build(zip_rows, county_rows, state_vals, {"income": US_MEDIAN})
    meta.update({"report": report, "edition": args.edition, "built": date.today().isoformat(),
                 "source": "Census ACS 2020-2024 values extracted from the original income study (Phase 1)"})

    w, h = D["vb"]
    geo = {"w": float(w), "h": float(h), "outline": stitch(D["sp"]),
           "places": [{"name": n, "x": x, "y": y} for n, x, y in D["labels"] if n in places],
           "zip": {k: absolute(v) for k, v in sorted(D["zp"].items())},
           "county": {k: absolute(v) for k, v in sorted(D["cp"].items())}}

    out = ROOT / "src/data" / st.lower()
    for path in (write_json(out / "geo-2020.json", geo),
                 write_json(out / "income-targeting" / args.edition / "attrs.json", attrs),
                 write_json(out / "income-targeting" / args.edition / "meta.json", meta)):
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")
    print("key figures:", meta["stats"])


if __name__ == "__main__":
    main()
