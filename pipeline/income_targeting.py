"""Build income targeting report data (ZIP codes and counties) for one state from public sources.

Usage:
  .venv/bin/python pipeline/income_targeting.py --state TX --edition 2026-10
  .venv/bin/python pipeline/income_targeting.py --state TX --edition 2026-10 --publish

Writes attrs.json and meta.json to pipeline/out/<st>/income-targeting/<edition>/ for review, or to
src/data/<st>/income-targeting/<edition>/ with --publish (never over an existing folder). Map shapes come
from pipeline/geo.py, which keeps the same ZIP codes (every ZCTA with land in one of the state's counties).

Method (see pipeline/sources.md):
- Income: ACS 5-year median household income (B19013) for every ZCTA and county, with its margin of error.
- Quartiles: the state's ZIP codes with a published median, each ZIP counted once, cut at the 25th, 50th
  and 75th percentiles (linear interpolation). The top quartile is the target list. Counties get their own
  quartiles across the state's counties.
- Core: a target ZIP with at least 500 households and a margin of error under 25% of the estimate
  (top-coded medians, which have no published margin, pass on households alone).
- Context: households (B11001), owner occupancy (B25003), median home value (B25077), the county holding
  the largest share of the ZIP's land, and the county's metro area (OMB delineation, metropolitan only).
"""
import argparse
import csv
import io
import re
import statistics
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sources
from common import ROOT, pct1, write_json
from home_risk import acs_values, county_names, place_names, zcta_land

# Settings for each edition. A new edition adds an entry; old entries stay so editions can be rebuilt.
EDITIONS = {
    "2026-10": {"acs_year": 2024, "gazetteer_year": 2024, "cbsa_year": 2023},
}

CORE_MIN_HOUSEHOLDS = 500
CORE_MAX_MOE = 25.0          # margin of error, percent of the estimate
INCOME_TOP_CODE = 250001     # ACS writes "$250,000 or more" as 250001
HOME_VALUE_TOP_CODE = 2000001
TOP_TABLE = 20               # core target ZIPs ranked by households for the report table
INTERNAL_FIELDS = ("income", "incomeMoe", "ownerHomes", "countyFips", "core")
TOP_METROS = 5               # metros shown by name in the metro chart; the rest are grouped

# Short metro names. By default a metro is named for its first principal city.
METRO_LABELS = {"19100": "Dallas-Fort Worth", "33460": "Minneapolis-St. Paul", "41860": "San Francisco-Oakland"}


# ---------- statistics ----------

def quartile_breaks(values):
    """25th, 50th and 75th percentiles with linear interpolation (numpy's default, Excel's QUARTILE.INC)."""
    return statistics.quantiles(values, n=4, method="inclusive")


def quartile(value, breaks):
    if value is None:
        return -1
    return sum(value >= b for b in breaks)


def half_up(x):
    return int(x + 0.5)


def correlation(pairs):
    xs, ys = zip(*pairs)
    return statistics.correlation(xs, ys)


def money(n, top=None):
    if n is None:
        return "Not published"
    return f"${n - 1:,}+" if top and n >= top else f"${n:,}"


# ---------- sources ----------

def cbsa_counties(fips, year):
    """County FIPS -> (CBSA code, title) for metropolitan areas, from the OMB delineation file (xlsx)."""
    url = (f"{sources.CENSUS}/programs-surveys/metro-micro/geographies/reference-files/{year}/"
           f"delineation-files/list1_{year}.xlsx")
    z = zipfile.ZipFile(sources.download(url, f"census/list1_{year}.xlsx"))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    shared = [("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
              for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns)]
    rows = []
    for row in ElementTree.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(f"{{{ns['m']}}}row"):
        cells = {}
        for c in row.findall("m:c", ns):
            col = re.match(r"[A-Z]+", c.get("r")).group(0)
            v = c.find("m:v", ns)
            text = c.find("m:is/m:t", ns)
            if v is not None:
                cells[col] = shared[int(v.text)] if c.get("t") == "s" else v.text
            elif text is not None:
                cells[col] = text.text
        rows.append(cells)
    head = next(i for i, r in enumerate(rows) if r.get("A") == "CBSA Code")
    cols = {name: col for col, name in rows[head].items()}
    out = {}
    for r in rows[head + 1:]:
        get = lambda name: (r.get(cols[name]) or "").strip()
        if get("Metropolitan/Micropolitan Statistical Area") != "Metropolitan Statistical Area":
            continue
        if get("FIPS State Code").zfill(2) != fips:
            continue
        county = get("FIPS State Code").zfill(2) + get("FIPS County Code").zfill(3)
        out[county] = (get("CBSA Code"), get("CBSA Title"))
    return out


def metro_label(code, title):
    return METRO_LABELS.get(code) or title.split(",")[0].split("-")[0].strip()


def gather(state, edition):
    """Raw ZIP and county rows for one state, from the ACS summary files and Census relationship files."""
    settings = EDITIONS[edition]
    fips = sources.STATES[state][0]
    land, _ = zcta_land(fips)
    counties = county_names(fips, settings["gazetteer_year"])
    metros = cbsa_counties(fips, settings["cbsa_year"])
    zips = sorted(land)

    year = settings["acs_year"]
    geo_ids = ({f"860Z200US{z}" for z in zips} | {f"0500000US{c}" for c in counties}
               | {f"0400000US{fips}", "0100000US"})
    acs = defaultdict(dict)
    for table in ("b19013", "b11001", "b25003", "b25077"):
        for g, rec in acs_values(sources.acs_table(table, year), geo_ids).items():
            acs[g].update(rec)

    def values(g):
        a = acs.get(g, {})
        income, moe = a.get("B19013_E001"), a.get("B19013_M001")
        occupied, owned = a.get("B25003_E001"), a.get("B25003_E002")
        return {"income": income, "incomeMoe": round(moe / income * 100, 1) if income and moe is not None else None,
                "households": a.get("B11001_E001") or 0, "occupied": occupied or 0, "ownerHomes": owned or 0,
                "homeValue": a.get("B25077_E001")}

    names = place_names(set(zips))
    zip_rows = []
    for z in zips:
        county = max(land[z], key=lambda c: (land[z][c], c))
        code, title = metros.get(county, (None, None))
        zip_rows.append({"id": z, "name": names.get(z), "county": counties[county]["name"], "countyFips": county,
                         "metro": metro_label(code, title) if code else "Non-metro", **values(f"860Z200US{z}")})
    county_rows = []
    for c in sorted(counties):
        code, title = metros.get(c, (None, None))
        county_rows.append({"id": c, "name": counties[c]["name"],
                            "metro": metro_label(code, title) if code else "Non-metro", **values(f"0500000US{c}")})
    return zip_rows, county_rows, values(f"0400000US{fips}"), values("0100000US")


# ---------- the report ----------

def build(zip_rows, county_rows, state_vals, us_vals):
    """attrs.json and meta.json from raw rows. Pure: no downloads, so it can be checked on any input."""
    priced = [r for r in zip_rows if r["income"] is not None]
    zb = quartile_breaks([r["income"] for r in priced])
    cb = quartile_breaks([r["income"] for r in county_rows if r["income"] is not None])
    state_hh = sum(r["households"] for r in zip_rows)

    zips = []
    for r in zip_rows:
        q = quartile(r["income"], zb)
        # Top-coded medians carry no margin of error; they count as solid on size alone.
        core = int(r["households"] >= CORE_MIN_HOUSEHOLDS and (r["incomeMoe"] or 0) < CORE_MAX_MOE)
        status = -1 if q < 0 else 0 if q < 3 else 2 if core else 1
        zips.append({
            "id": r["id"], "name": r["name"], "county": r["county"], "countyFips": r["countyFips"],
            "metro": r["metro"], "income": r["income"], "incomeText": money(r["income"], INCOME_TOP_CODE),
            "incomeMoe": r["incomeMoe"], "moeText": "No margin published" if r["incomeMoe"] is None else f"±{r['incomeMoe']}%",
            "households": r["households"], "ownerHomes": r["ownerHomes"],
            "ownerRate": round(r["ownerHomes"] / r["occupied"] * 100, 1) if r["occupied"] else None,
            "homeValueText": money(r["homeValue"], HOME_VALUE_TOP_CODE),
            "quartile": q, "status": status, "core": core, "coreRank": None,
        })
    targets = [z for z in zips if z["status"] > 0]
    cores = [z for z in targets if z["status"] == 2]
    for i, z in enumerate(sorted(cores, key=lambda z: (-z["households"], z["id"])), 1):
        z["coreRank"] = i

    counties = []
    for r in county_rows:
        q = quartile(r["income"], cb)
        mine = [z for z in zips if z["countyFips"] == r["id"]]
        counties.append({
            "id": r["id"], "name": r["name"], "metro": r["metro"], "income": r["income"],
            "incomeText": money(r["income"], INCOME_TOP_CODE), "incomeMoe": r["incomeMoe"],
            "households": r["households"], "ownerHomes": r["ownerHomes"],
            "ownerRate": round(r["ownerHomes"] / r["occupied"] * 100, 1) if r["occupied"] else None,
            "homeValueText": money(r["homeValue"], HOME_VALUE_TOP_CODE),
            "quartile": q, "top": int(q == 3), "zips": len(mine),
            "targetZips": sum(1 for z in mine if z["status"] > 0),
            "coreZips": sum(1 for z in mine if z["status"] == 2),
            "bottomZips": sum(1 for z in mine if z["quartile"] == 0),
            "targetHouseholds": sum(z["households"] for z in mine if z["status"] > 0),
            "countyRank": None,
        })
    for i, c in enumerate(sorted((c for c in counties if c["top"]), key=lambda c: (-c["households"], c["id"])), 1):
        c["countyRank"] = i

    held = lambda rows: sum(z["households"] for z in rows)
    target_hh = held(targets)

    # Metros by target households: the largest by name, the rest grouped.
    by_metro = defaultdict(list)
    for z in targets:
        by_metro[z["metro"]].append(z)
    ranked = sorted(((m, rows) for m, rows in by_metro.items() if m != "Non-metro"), key=lambda kv: -held(kv[1]))
    shown, rest = ranked[:TOP_METROS], ranked[TOP_METROS:]
    metro_groups = [{"label": m, "sub": f"{len(rows)} ZIPs", "values": [pct1(held(rows), target_hh)]} for m, rows in shown]
    if rest:
        rows = [z for _, r in rest for z in r]
        metro_groups.append({"label": "Other metros", "sub": f"{len(rest)} metros, {len(rows)} ZIPs",
                             "values": [pct1(held(rows), target_hh)]})
    if by_metro.get("Non-metro"):
        rows = by_metro["Non-metro"]
        metro_groups.append({"label": "Outside metros", "sub": f"{len(rows)} ZIPs", "values": [pct1(held(rows), target_hh)]})

    labels = ["Bottom 25%", "Second 25%", "Third 25%", "Top 25%"]
    quartile_groups = [{"label": labels[q], "sub": f"{sum(1 for z in zips if z['quartile'] == q)} ZIPs",
                        "values": [pct1(held([z for z in zips if z["quartile"] == q]), state_hh)]} for q in range(4)]

    # Stricter reading: the ZIPs holding the top quarter of households, ranked by ZIP median.
    run, strict, quarter = 0, [], 0.25 * held(z for z in zips if z["income"] is not None)
    for z in sorted((z for z in zips if z["income"] is not None), key=lambda z: -z["income"]):
        if run >= quarter:
            break
        strict.append(z)
        run += z["households"]

    solid = [z for z in zips if z["income"] is not None and z["households"] >= CORE_MIN_HOUSEHOLDS]
    moes = [z["incomeMoe"] for z in zips if z["incomeMoe"] is not None]
    renter_targets = [z for z in targets if z["ownerRate"] is not None and z["ownerRate"] < 50]
    top_counties = [c for c in counties if c["top"]]
    metro4 = ranked[:4]
    occupied_t = sum(r["occupied"] for r in zip_rows if r["id"] in {z["id"] for z in targets})
    stats = {
        "stateMedian": state_vals["income"], "usMedian": us_vals["income"],
        "stateHouseholds": state_hh, "zips": len(zips), "zipsPriced": len(priced),
        "zipsSuppressed": len(zips) - len(priced),
        "suppressedShare": pct1(held(z for z in zips if z["income"] is None), state_hh),
        "q1": half_up(zb[0]), "q2": half_up(zb[1]), "q3": half_up(zb[2]),
        "targetZips": len(targets), "coreZips": len(cores), "lowConfZips": len(targets) - len(cores),
        "targetHouseholds": target_hh, "targetShare": pct1(target_hh, state_hh),
        "coreHouseholds": held(cores), "coreShare": pct1(held(cores), state_hh),
        "lowConfHouseholds": target_hh - held(cores),
        "targetMedian": half_up(statistics.median(z["income"] for z in targets)),
        "targetOwnerRate": pct1(sum(z["ownerHomes"] for z in targets), occupied_t),
        "stateOwnerRate": pct1(state_vals["ownerHomes"], state_vals["occupied"]),
        "topCoded": sum(1 for z in zips if (z["income"] or 0) >= INCOME_TOP_CODE),
        "metro4Names": ", ".join(m for m, _ in metro4[:-1]) + f" and {metro4[-1][0]}" if len(metro4) > 1 else "",
        "metro4Zips": sum(len(r) for _, r in metro4), "metro4Share": pct1(sum(held(r) for _, r in metro4), target_hh),
        "topMetro": ranked[0][0] if ranked else "", "topMetroShare": pct1(held(ranked[0][1]), target_hh) if ranked else 0,
        "nonMetroTargets": len(by_metro.get("Non-metro", [])),
        "nonMetroCore": sum(1 for z in by_metro.get("Non-metro", []) if z["status"] == 2),
        "thirdShare": quartile_groups[2]["values"][0],
        "counties": len(counties), "cq1": half_up(cb[0]), "cq2": half_up(cb[1]), "countyLine": half_up(cb[2]),
        "topCounties": len(top_counties),
        "topCountyShare": pct1(sum(c["households"] for c in top_counties), sum(c["households"] for c in counties)),
        "bluntZips": sum(c["bottomZips"] for c in top_counties),
        "strictFloor": strict[-1]["income"] if strict else None, "strictZips": len(strict),
        "strictHouseholds": held(strict), "strictShare": pct1(held(strict), state_hh),
        "renterTargets": len(renter_targets), "renterTargetHouseholds": held(renter_targets),
        "incomeOwnerCorr": f"{correlation([(z['income'], z['ownerRate']) for z in solid if z['ownerRate'] is not None]):.2f}",
        "medianMoe": round(statistics.median(moes), 1), "highMoeZips": sum(1 for m in moes if m >= CORE_MAX_MOE),
        "coreMinHouseholds": CORE_MIN_HOUSEHOLDS, "coreMaxMoe": int(CORE_MAX_MOE),
    }
    meta = {"stats": stats, "charts": {"metros": metro_groups, "quartiles": quartile_groups}, "regions": {}}
    # The page reads only display fields; working fields stay out of attrs.json.
    for rec in zips + counties:
        for k in INTERNAL_FIELDS:
            rec.pop(k, None)
    return {"zip": zips, "county": counties}, meta


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", required=True, choices=sorted(sources.STATES))
    ap.add_argument("--edition", required=True, choices=sorted(EDITIONS))
    ap.add_argument("--publish", action="store_true", help="write to src/data (new editions only)")
    args = ap.parse_args()
    st = args.state.lower()
    attrs, meta = build(*gather(args.state, args.edition))
    meta.update({"report": f"{st}-income-targeting", "edition": args.edition, "source": "pipeline/income_targeting.py"})
    out = (ROOT / "src/data" if args.publish else ROOT / "pipeline/out") / st / "income-targeting" / args.edition
    if args.publish and out.exists():
        raise SystemExit(f"{out.relative_to(ROOT)} exists. Published editions are frozen; build a new edition instead.")
    for p in (write_json(out / "attrs.json", attrs), write_json(out / "meta.json", meta)):
        print(f"wrote {p.relative_to(ROOT)} ({p.stat().st_size:,} bytes)")
    print("key figures:", meta["stats"])


if __name__ == "__main__":
    main()
