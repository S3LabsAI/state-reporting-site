"""Build Homeowner Market Brief data (ZIP codes and counties) for one state from public Census tables.

Usage:
  .venv/bin/python pipeline/homeowner_market.py --state MD --edition 2026-11
  .venv/bin/python pipeline/homeowner_market.py --state MD --edition 2026-11 --publish

Writes attrs.json, meta.json and zips.csv (the ZIP list for geo.py) to pipeline/out/<st>/homeowner-market/<edition>/
for review, or attrs.json and meta.json to src/data/<st>/homeowner-market/<edition>/ with --publish (never over
an existing folder). Region figures use the `regions` in src/_data/reports/<st>-homeowner-market.json.

Method (see pipeline/sources.md):
- Which ZIP codes, and each ZIP code's county: as in home_risk.py (ZCTAs with more than half their land in the
  state; the county with the most 2020 housing units).
- Homes and income: ACS 5-year tables B25002 (housing units), B25003 (tenure), B25004 (seasonal vacancy),
  B11001 (households), B19013 (median household income), B25077 (median home value), B25075 (home value bands).
- Value band: the share of owner-occupied homes in the state's band (BANDS), summed from B25075's fixed bands.
- Prime target: owner-occupied rate 70%+ of occupied homes, median household income at or above the state
  median (B19013 for the state), and 1,000+ owner-occupied homes; ranked by owner-occupied homes.
- Seasonal market: 20%+ of all housing units vacant for seasonal, recreational or occasional use, in ZIP codes
  with 100 or more housing units.
"""
import argparse
import csv
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sources
from common import ROOT, load_config, pct1, write_json
from home_risk import acs_values, county_names, half_up, housing_weights, place_names, zcta_land

# Settings for each edition. A new edition adds an entry; old entries stay so editions can be rebuilt.
EDITIONS = {
    "2026-11": {"acs_year": 2024, "gazetteer_year": 2024},
}

# B25075 columns are fixed bands: (first column, last column) of each band a state can use.
BAND_DEFS = {
    "under-400k": {"cols": (2, 21), "label": "under $400,000", "short": "Under $400K"},
    "300k-750k": {"cols": (21, 23), "label": "$300,000 to $749,999", "short": "$300K-$750K"},
}
BANDS = {"MD": "under-400k", "GA": "300k-750k"}

OWN_CUTS = (0.40, 0.70, 0.90)                       # owner-occupied rate tiers: under 40, 40-69, 70-89, 90+
INCOME_CUTS = (60_000, 85_000, 110_000, 150_000)    # bands as in the California brief
SHARE_CUTS = (25, 50, 75)                            # value band share: under 25, 25-49, 50-74, 75+
PRIME = {"rate": 0.70, "homes": 1000}
SEASONAL = {"share": 0.20, "units": 100}            # 20%+ of homes seasonal, in ZIPs with 100+ housing units
INCOME_RANGE = {"counties": 5, "households": 1000}  # largest counties; ZIPs with 1,000+ households


def band(value, cuts):
    return -1 if value is None else sum(value >= c for c in cuts)


def county_label(name):
    """Gazetteer names drop " County"; independent cities keep " city" (Baltimore city)."""
    return (name[:-5] + " City", name[:-5] + " City") if name.endswith(" city") else (name, name + " County")


def build(state, edition):
    settings = EDITIONS[edition]
    fips = sources.STATES[state][0]
    bdef = BAND_DEFS[BANDS[state]]
    by_zip, _ = housing_weights(fips)
    land, land_total = zcta_land(fips)
    counties = county_names(fips, settings["gazetteer_year"])
    zips = sorted(z for z in land if land_total[z] and sum(land[z].values()) / land_total[z] > 0.5)

    def home_county(z):
        hu = Counter()
        for tr, w in by_zip.get(z, {}).items():
            hu[tr[:5]] += w
        if sum(hu.values()):
            return max(hu, key=lambda c: (hu[c], c))
        return max(land[z], key=land[z].get)

    year = settings["acs_year"]
    geo_ids = {f"860Z200US{z}" for z in zips} | {f"0500000US{c}" for c in counties} | {f"0400000US{fips}"}
    acs = defaultdict(dict)
    for table in ("b25002", "b25003", "b25004", "b11001", "b19013", "b25075", "b25077"):
        for g, rec in acs_values(sources.acs_table(table, year), geo_ids).items():
            acs[g].update(rec)
    st = acs[f"0400000US{fips}"]
    state_median = st["B19013_E001"]
    lo, hi = bdef["cols"]

    def homes(g):
        a = acs.get(g, {})
        units, occupied, owned = a.get("B25002_E001"), a.get("B25003_E001"), a.get("B25003_E002")
        rate = owned / occupied if occupied else None
        in_band = sum(a.get(f"B25075_E{i:03d}") or 0 for i in range(lo, hi + 1)) if owned else None
        seasonal = a.get("B25004_E006") / units if units and a.get("B25004_E006") is not None else None
        income = a.get("B19013_E001")
        return {
            "ownerHomes": owned, "occupied": occupied, "housingUnits": units, "households": a.get("B11001_E001"),
            "ownerRate": half_up(rate * 100) if rate is not None else None, "ownTier": band(rate, OWN_CUTS),
            "ownAllShare": half_up(owned / units * 100) if units else None,
            "income": income, "incomeBand": band(income, INCOME_CUTS),
            "homeValue": a.get("B25077_E001"),
            "bandHomes": in_band, "bandShare": half_up(in_band / owned * 100) if owned else None,
            "valueBand": band(in_band / owned * 100, SHARE_CUTS) if owned else -1,
            "seasonalShare": half_up(seasonal * 100) if seasonal is not None else None,
            "_rate": rate, "_seasonal": seasonal,
        }

    try:
        regions = load_config(f"{state.lower()}-homeowner-market").get("regions", [])
    except FileNotFoundError:
        regions = []
    region_of = {c: r for r in regions for c in r["counties"]}

    names = place_names(set(zips))
    zip_recs = []
    for z in zips:
        h = homes(f"860Z200US{z}")
        c = home_county(z)
        cname, clabel = county_label(counties[c]["name"])
        notes = []
        if h["income"] is None and h["households"]:
            notes.append("ACS median household income not published (sample too small)")
        if h["income"] == 250001:
            notes.append("Census top-codes median household income at $250,000 or more")
        if h["homeValue"] == 2000001:
            notes.append("Census top-codes median home value at $2,000,000 or more")
        prime = (h["_rate"] is not None and h["_rate"] >= PRIME["rate"] and h["income"] is not None
                 and h["income"] >= state_median and (h["ownerHomes"] or 0) >= PRIME["homes"])
        rec = {"id": z, "name": names.get(z) or clabel, "county": cname, "countyLabel": clabel, "countyFips": c}
        rec.update({k: v for k, v in h.items() if not k.startswith("_")})
        rec.update({"prime": int(prime), "primeRank": None,
                    "seasonal": int(h["_seasonal"] is not None and h["_seasonal"] >= SEASONAL["share"]
                                    and h["housingUnits"] >= SEASONAL["units"]), "seasonalRank": None,
                    "note": "; ".join(notes) or None, "_rate": h["_rate"]})
        zip_recs.append(rec)
    for i, r in enumerate(sorted((r for r in zip_recs if r["prime"]), key=lambda r: -r["ownerHomes"]), 1):
        r["primeRank"] = i
    for i, r in enumerate(sorted((r for r in zip_recs if r["seasonal"]), key=lambda r: -r["seasonalShare"]), 1):
        r["seasonalRank"] = i

    county_recs = []
    for c in sorted(counties):
        h = homes(f"0500000US{c}")
        cname, clabel = county_label(counties[c]["name"])
        mine = [r for r in zip_recs if r["countyFips"] == c]
        rec = {"id": c, "name": cname, "label": clabel,
               "region": region_of[cname]["label"] if cname in region_of else sources.STATES[state][1].title()}
        rec.update({k: v for k, v in h.items() if not k.startswith("_")})
        rec.update({"primeOwnerHomes": sum(r["ownerHomes"] or 0 for r in mine if r["prime"]),
                    "primeZips": sum(1 for r in mine if r["prime"]), "bandRank": None, "sizeRank": None})
        ranged = sorted((r for r in mine if r["income"] is not None and (r["households"] or 0) >= INCOME_RANGE["households"]),
                        key=lambda r: r["income"])
        if ranged:
            rec.update({"incLow": ranged[0]["income"], "incLowZip": ranged[0]["id"], "incLowName": ranged[0]["name"],
                        "incMed": round(statistics.median(r["income"] for r in ranged)),
                        "incHigh": ranged[-1]["income"], "incHighZip": ranged[-1]["id"], "incHighName": ranged[-1]["name"],
                        "incZips": len(ranged)})
        county_recs.append(rec)
    for i, r in enumerate(sorted(county_recs, key=lambda r: -(r["bandHomes"] or 0)), 1):
        r["bandRank"] = i
    for i, r in enumerate(sorted(county_recs, key=lambda r: -(r["households"] or 0)), 1):
        r["sizeRank"] = i if i <= INCOME_RANGE["counties"] else None

    # ---------- headline figures ----------
    owner_total = sum(r["ownerHomes"] or 0 for r in zip_recs)
    prime = [r for r in zip_recs if r["prime"]]
    seasonal = sorted((r for r in zip_recs if r["seasonal"]), key=lambda r: -(r["housingUnits"] or 0))
    st_band = sum(st.get(f"B25075_E{i:03d}") or 0 for i in range(lo, hi + 1))
    stats = {
        "zips": len(zip_recs), "counties": len(county_recs),
        "primeZips": len(prime), "primeHomes": sum(r["ownerHomes"] for r in prime),
        "primeShare": pct1(sum(r["ownerHomes"] for r in prime), owner_total),
        "stateIncome": state_median,
        "ownRate": pct1(st["B25003_E002"], st["B25003_E001"]), "ownerHomes": st["B25003_E002"],
        "ownAllShare": pct1(st["B25003_E002"], st["B25002_E001"]),
        "stateHomeValue": st["B25077_E001"],
        "bandLabel": bdef["label"], "bandShort": bdef["short"],
        "bandHomes": st_band, "bandShare": pct1(st_band, st["B25003_E002"]),
        "incomeZips": sum(1 for r in zip_recs if r["income"] is not None),
        "seasonalZips": len(seasonal),
        "seasonalTop": {k: seasonal[0][k] for k in ("id", "name", "ownerRate", "ownAllShare", "seasonalShare")} if seasonal else None,
        # For the open decision on whether the value band belongs in the prime screen.
        "primeInBand50": sum(1 for r in prime if (r["bandShare"] or 0) >= 50),
    }

    def region_figures(members):
        occ = sum(r["occupied"] or 0 for r in members)
        own = sum(r["ownerHomes"] or 0 for r in members)
        inc = [r for r in members if r["income"] is not None and r["households"]]
        hh = sum(r["households"] for r in inc)
        return {"zips": len(members), "primeZips": sum(r["prime"] for r in members), "ownerHomes": own,
                "households": sum(r["households"] or 0 for r in members),
                "ownRate": pct1(own, occ) if occ else None,
                "income": round(sum(r["income"] * r["households"] for r in inc) / hh) if hh else None}

    region_stats = {r["id"]: region_figures([z for z in zip_recs if z["county"] in set(r["counties"])]) for r in regions}
    by_rate = sorted(regions, key=lambda r: -(region_stats[r["id"]]["ownRate"] or 0))
    by_income = sorted(regions, key=lambda r: -(region_stats[r["id"]]["income"] or 0))
    charts = {
        "regionOwnRate": [{"label": r["short"], "sub": f"{region_stats[r['id']]['ownerHomes']:,} homes",
                           "values": [region_stats[r["id"]]["ownRate"]]} for r in by_rate],
        "regionIncome": [{"label": r["short"], "sub": f"{region_stats[r['id']]['households']:,} households",
                          "values": [region_stats[r["id"]]["income"]]} for r in by_income],
    }
    for r in zip_recs:
        del r["_rate"]
    return {"zip": zip_recs, "county": county_recs}, {"stats": stats, "regions": region_stats, "charts": charts}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", required=True, choices=sorted(BANDS))
    ap.add_argument("--edition", required=True, choices=sorted(EDITIONS))
    ap.add_argument("--publish", action="store_true", help="write to src/data (new editions only)")
    args = ap.parse_args()
    st = args.state.lower()
    attrs, meta = build(args.state, args.edition)
    meta.update({"report": f"{st}-homeowner-market", "edition": args.edition, "source": "pipeline/homeowner_market.py"})
    out = (ROOT / "src/data" if args.publish else ROOT / "pipeline/out") / st / "homeowner-market" / args.edition
    if args.publish and out.exists():
        raise SystemExit(f"{out.relative_to(ROOT)} exists. Published editions are frozen; build a new edition instead.")
    for p in (write_json(out / "attrs.json", attrs), write_json(out / "meta.json", meta)):
        print(f"wrote {p.relative_to(ROOT)} ({p.stat().st_size:,} bytes)")
    if not args.publish:
        with open(out / "zips.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["zip"])
            w.writerows([r["id"]] for r in attrs["zip"])
    print("key figures:", meta["stats"])


if __name__ == "__main__":
    main()
