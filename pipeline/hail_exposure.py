"""Build hail exposure report data (ZIP codes and counties) from a finished hail exposure study.

Usage:
  .venv/bin/python pipeline/hail_exposure.py --state IL --edition 2026-10
  .venv/bin/python pipeline/hail_exposure.py --state IL --edition 2026-10 --publish

Reads the study's ranking tables from pipeline/inputs/<st>-hail-exposure/<edition>/:
  county_ranking.csv  every county: rank, ground reports, radar days, owner homes, hail index, score
  zip_ranking.csv     every ZIP code (ZCTA) in the top 15 counties, with its tier
and writes attrs.json and meta.json to pipeline/out/<st>/hail-exposure/<edition>/ for review, or to
src/data/<st>/hail-exposure/<edition>/ with --publish (never over an existing folder). Map shapes come
from pipeline/geo.py; county names are matched to FIPS codes with the same Census county file.

The Illinois October 2026 tables were transcribed from "Illinois hail exposure, 2021 to 2026"
(October 7, 2026), Tables 3.1 and 4.1. Its method and sources are summarized in pipeline/sources.md.
"""
import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sources
from common import ROOT, write_json
from geo import read_records

# Hail index bins for the map (Illinois average = 1.0)
HAIL_BINS = [1.0, 2.0, 4.0]
SOURCE = {"R": "ground report", "M": "radar", "R+M": "ground report and radar"}


def hail_tier(index):
    return sum(index >= b for b in HAIL_BINS)


def storm(value):
    """'2026-07-27 R+M' -> 'Jul 27, 2026 (ground report and radar)'; 'none' -> 'None since 2021'."""
    if value == "none":
        return "None since 2021"
    day, src = value.split(" ")
    d = date.fromisoformat(day)
    return f"{d:%b} {d.day}, {d.year} ({SOURCE[src]})"


def key(name):
    return re.sub(r"[^a-z]", "", name.lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--edition", required=True)
    ap.add_argument("--publish", action="store_true")
    args = ap.parse_args()
    st = args.state.lower()
    src = ROOT / f"pipeline/inputs/{st}-hail-exposure/{args.edition}"
    counties = list(csv.DictReader(open(src / "county_ranking.csv")))
    zips = list(csv.DictReader(open(src / "zip_ranking.csv")))

    fips = sources.STATES[args.state.upper()][0]
    by_name = {key(rec["NAME"]): rec["GEOID"] for rec in read_records(sources.cb_county(2020)) if rec["STATEFP"] == fips}
    missing = [c["county"] for c in counties if key(c["county"]) not in by_name]
    if missing:
        raise SystemExit(f"No Census county for: {missing}")

    by_index = sorted(counties, key=lambda c: -float(c["hail_index"]))
    index_rank = {c["county"]: i + 1 for i, c in enumerate(by_index)}
    county_rows = []
    for c in counties:
        rank = int(c["rank"])
        county_rows.append({
            "id": by_name[key(c["county"])], "name": c["county"], "rank": rank, "top15": int(rank <= 15),
            "hailIndex": c["hail_index"], "hailTier": hail_tier(float(c["hail_index"])), "hailRank": index_rank[c["county"]],
            "score": c["score"], "ownerHomes": int(c["owner_homes"]),
            "reports": f"{c['reports_1in']} / {c['reports_175in']}",
            "maxHail": "None" if c["max_in"] == "none" else f"{c['max_in']} in",
            "radarDays": c["radar_days"].replace("/", " / "), "lastStorm": storm(c["last_175_storm"]),
            "altRank": int(c["alt_rank"]),
        })

    zip_rows = []
    for z in zips:
        zip_rows.append({
            "id": z["zip"], "name": z["city"], "county": z["county"], "rank": int(z["rank"]), "tier": int(z["tier"]),
            "hailIndex": z["hail_index"], "hailTier": hail_tier(float(z["hail_index"])), "score": z["score"],
            "ownerHomes": int(z["owner_homes"]),
            "yearBuilt": {"n/p": "Not published", "pre-1940": "Before 1940"}.get(z["median_year_built"], z["median_year_built"]),
            "reportDays": z["report_days"].replace("/", " / "), "radarDays": z["radar_days"].replace("/", " / "),
            "lastStorm": storm(z["last_sig_storm"]),
        })

    tier = lambda t: [z for z in zip_rows if z["tier"] == t]
    homes = lambda rows: sum(z["ownerHomes"] for z in rows)
    top = sorted(county_rows, key=lambda c: -float(c["hailIndex"]))[0]
    stats = {
        "rankedZips": len(zip_rows), "counties": len(county_rows),
        "tier1Zips": len(tier(1)), "tier1Homes": homes(tier(1)),
        "tier2Zips": len(tier(2)), "tier2Homes": homes(tier(2)), "tier3Zips": len(tier(3)),
        "stateHomes": sum(c["ownerHomes"] for c in county_rows),
        "topIndexCounty": top["name"], "topIndex": top["hailIndex"],
        "topZip": zip_rows[0]["id"], "topZipName": zip_rows[0]["name"],
    }
    meta = {"stats": stats, "regions": {}, "report": f"{st}-hail-exposure", "edition": args.edition,
            "built": date.today().isoformat(), "source": f"pipeline/inputs/{st}-hail-exposure/{args.edition}"}
    attrs = {"zip": sorted(zip_rows, key=lambda z: (z["rank"], z["id"])), "county": sorted(county_rows, key=lambda c: c["rank"])}

    out = ROOT / (f"src/data/{st}/hail-exposure/{args.edition}" if args.publish else f"pipeline/out/{st}/hail-exposure/{args.edition}")
    if args.publish and out.exists():
        raise SystemExit(f"{out.relative_to(ROOT)} exists: editions are never overwritten")
    write_json(out / "attrs.json", attrs)
    write_json(out / "meta.json", meta)
    print(f"wrote {out.relative_to(ROOT)}: {len(zip_rows)} ZIPs, {len(county_rows)} counties; {stats}")


if __name__ == "__main__":
    main()
