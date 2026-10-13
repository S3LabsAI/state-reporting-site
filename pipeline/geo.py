"""Build a state's map shapes (src/data/<st>/geo-2020.json) from Census 2020 cartographic boundaries.

Usage:
  .venv/bin/python pipeline/geo.py --state IL --zips pipeline/inputs/il-hail-exposure/2026-10/zip_ranking.csv \
      --places "Chicago:-87.63,41.88" "Rockford:-89.09,42.27" ...

Writes w, h, outline (the state), county (every county, keyed by 5-digit FIPS), zip (ZCTAs, keyed by ZIP:
those listed in --zips, else every ZCTA that overlaps the state) and places (label points). Shapes are projected with a transverse
Mercator centered on the state, fitted so the longer side is 1000 units, simplified, and written as absolute
M/L/Z paths with one decimal, the form the map script and the static map images read.
"""
import argparse
import csv
import io
import math
import sys
import zipfile
from pathlib import Path

import shapefile
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sources
from common import ROOT, write_json

SIZE = 1000        # the longer side of the canvas, in map units
PAD = 4            # margin around the state, in map units
TOLERANCE = 0.12   # simplification tolerance, in map units (the map zooms to 12x)


def read_records(zpath):
    """The attribute records (dicts) of a Census cartographic boundary zip."""
    z = zipfile.ZipFile(zpath)
    base = [n for n in z.namelist() if n.endswith(".dbf")][0]
    r = shapefile.Reader(dbf=io.BytesIO(z.read(base)))
    fields = [f[0] for f in r.fields[1:]]
    return [dict(zip(fields, rec)) for rec in r.records()]


def read_shapes(zpath, key, keep):
    """{key value: [ring, ...]} of lon/lat rings for records where keep(record) is true."""
    z = zipfile.ZipFile(zpath)
    base = [n for n in z.namelist() if n.endswith(".shp")][0][:-4]
    r = shapefile.Reader(shp=io.BytesIO(z.read(base + ".shp")), dbf=io.BytesIO(z.read(base + ".dbf")),
                         shx=io.BytesIO(z.read(base + ".shx")))
    fields = [f[0] for f in r.fields[1:]]
    out = {}
    for sr in r.iterShapeRecords():
        rec = dict(zip(fields, sr.record))
        if not keep(rec):
            continue
        pts, parts = sr.shape.points, list(sr.shape.parts) + [len(sr.shape.points)]
        out[rec[key]] = [pts[a:b] for a, b in zip(parts, parts[1:])]
    return out


def simplify(ring, tol):
    """Douglas-Peucker on a closed ring; keeps the first point and at least a triangle."""
    if len(ring) < 5:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = ring[a], ring[b]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy)
        best, idx = -1.0, None
        for i in range(a + 1, b):
            x0, y0 = ring[i]
            d = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / norm if norm else math.hypot(x0 - x1, y0 - y1)
            if d > best:
                best, idx = d, i
        if idx is not None and best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    out = [p for p, k in zip(ring, keep) if k]
    return out if len(out) >= 4 else ring


def path(rings):
    parts = []
    for ring in rings:
        pts = []
        for x, y in ring[:-1]:
            p = f"{x:.1f} {y:.1f}"
            if not pts or pts[-1] != p:
                pts.append(p)
        if len(pts) >= 3:
            parts.append("M" + "L".join(pts) + "Z")
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--zips", help="CSV with a `zip` column: only these ZCTAs are kept")
    ap.add_argument("--places", nargs="*", default=[], help='label points as "Name:lon,lat"')
    ap.add_argument("--out", help="output file (default src/data/<st>/geo-2020.json)")
    args = ap.parse_args()
    st = args.state.upper()
    fips = sources.STATES[st][0]
    wanted = {r["zip"] for r in csv.DictReader(open(args.zips))} if args.zips else None

    state = read_shapes(sources.cb_state(2020), "STATEFP", lambda r: r["STATEFP"] == fips)[fips]
    counties = read_shapes(sources.cb_county(2020), "GEOID", lambda r: r["STATEFP"] == fips)
    xs = [x for ring in state for x, _ in ring]
    ys = [y for ring in state for _, y in ring]
    lon0, lat0 = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    if wanted is None:
        # Every ZCTA that overlaps one of the state's counties.
        with open(sources.zcta_county(), encoding="utf-8-sig") as f:
            wanted = {r["GEOID_ZCTA5_20"] for r in csv.DictReader(f, delimiter="|")
                      if r["GEOID_ZCTA5_20"] and r["GEOID_COUNTY_20"][:2] == fips}
    zctas = read_shapes(sources.cb_zcta(), "ZCTA5CE20", lambda r: r["ZCTA5CE20"] in wanted)
    if set(zctas) != wanted:
        print(f"  {len(wanted - set(zctas))} ZIPs have no ZCTA shape: {sorted(wanted - set(zctas))[:10]}")

    tf = Transformer.from_crs("EPSG:4269", f"+proj=tmerc +lat_0={lat0:.3f} +lon_0={lon0:.3f} +datum=NAD83 +units=m",
                              always_xy=True)
    proj = lambda rings: [list(zip(*tf.transform(*zip(*ring)))) for ring in rings]
    state_p = proj(state)
    px = [x for ring in state_p for x, _ in ring]
    py = [y for ring in state_p for _, y in ring]
    x0, x1, y0, y1 = min(px), max(px), min(py), max(py)
    k = (SIZE - 2 * PAD) / max(x1 - x0, y1 - y0)
    w, h = round((x1 - x0) * k + 2 * PAD, 1), round((y1 - y0) * k + 2 * PAD, 1)
    fit = lambda rings: [[((x - x0) * k + PAD, (y1 - y) * k + PAD) for x, y in ring] for ring in rings]
    shape = lambda rings: path([simplify(r, TOLERANCE) for r in fit(proj(rings))])

    places = []
    for spec in args.places:
        name, ll = spec.split(":")
        lon, lat = map(float, ll.split(","))
        x, y = fit([[tf.transform(lon, lat)]])[0][0]
        places.append({"name": name, "x": round(x, 1), "y": round(y, 1)})

    geo = {
        "w": w, "h": h,
        "outline": path([simplify(r, TOLERANCE) for r in fit(state_p)]),
        "places": places,
        "zip": {z: shape(r) for z, r in sorted(zctas.items())},
        "county": {c: shape(r) for c, r in sorted(counties.items())},
    }
    out = Path(args.out) if args.out else ROOT / f"src/data/{st.lower()}/geo-2020.json"
    write_json(out, geo)
    print(f"wrote {out.relative_to(ROOT)}: {w} x {h}, {len(geo['zip'])} ZIPs, {len(geo['county'])} counties, "
          f"{out.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
