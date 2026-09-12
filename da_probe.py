import os, requests

ua = os.environ["TRELLIS_USER_AGENT"]
CHAIN = ("DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
         "DepreciationAndAmortization", "Depreciation")

for cik, name in [(1551152,"ABBV"), (200406,"JNJ"), (14272,"BMY")]:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    facts = requests.get(url, headers={"User-Agent": ua}).json()["facts"]["us-gaap"]
    print(f"=== {name} ({cik}) ===")
    cands = sorted(t for t in facts
                   if ("epreciation" in t or "mortization" in t) and "Method" not in t)
    for tag in cands:
        units = facts[tag].get("units", {}).get("USD", [])
        fy = [u for u in units if u.get("form") == "10-K" and u.get("fp") == "FY"
              and u.get("start") and u.get("end","")[:4] >= "2025"]
        if not fy:
            continue
        latest = sorted(fy, key=lambda x: (x["end"], x.get("filed","")))[-1]
        mark = " <-- IN CHAIN" if tag in CHAIN else ""
        print(f"  {tag[:60]:<60} {latest['val']:>16,.0f}{mark}")
    print()
