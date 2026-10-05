"""Snapshot the 3 owner databases (BigQuery issac_final) by Texas county into manual.json.

Run locally after the tables change (needs `gcloud auth login` as info@evolv.one):
    python3 scripts/issac/dashboard/build_territory.py
Aggregates only: the dashboard repo is public, so no names, emails or addresses leave BigQuery.
"""
import csv, io, json, subprocess, datetime, pathlib

HERE = pathlib.Path(__file__).parent
T = "`evolv-issac-data.issac_final.{}`"
# Comptroller county codes are 001-254 in alphabetical order of county name.
QUERIES = {
    "business_owners": f"SELECT primary_outlet_county_code k, COUNT(*) n FROM {T.format('business_owners')} GROUP BY 1",
    "property_owners": f"SELECT county k, COUNT(*) n FROM {T.format('property_owners')} GROUP BY 1",
    "both_owners": f"SELECT primary_outlet_county_code k, COUNT(*) n FROM {T.format('both_owners')} GROUP BY 1",
}


def bq(sql):
    out = subprocess.run(["bq", "query", "--use_legacy_sql=false", "--format=csv", "--max_rows=1000", sql],
                         capture_output=True, text=True, check=True).stdout
    return list(csv.DictReader(io.StringIO(out[out.index("k,n"):])))


def main():
    manual = json.loads((HERE / "manual.json").read_text())
    names = sorted(manual["list"]["registry"]["by_county"])  # all 254 Texas counties
    by_code = {f"{i + 1:03d}": n for i, n in enumerate(names)}
    dbs = {}
    for key, sql in QUERIES.items():
        counts, unknown = {}, 0
        for r in bq(sql):
            k = (r["k"] or "").strip()
            name = by_code.get(k.zfill(3)) if k.isdigit() else next((n for n in names if n.lower() == k.lower()), None)
            if name: counts[name] = counts.get(name, 0) + int(r["n"])
            else: unknown += int(r["n"])
        dbs[key] = {"total": sum(counts.values()) + unknown, "unknown_county": unknown,
                    "by_county": dict(sorted(counts.items(), key=lambda x: -x[1]))}
        print(key, dbs[key]["total"], "counties", len(counts), "unknown", unknown)
    L = manual["list"]
    L["databases"] = dbs
    L["universe"] = {k: v["total"] for k, v in dbs.items()}
    L["databases_updated"] = datetime.date.today().isoformat()
    (HERE / "manual.json").write_text(json.dumps(manual, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
