"""
One-off diagnostic, not part of the library: which XBRL tag does Nike actually use for
the two fields that came back empty? Probes several candidate tags directly against SEC
and reports exactly what's there for each -- root cause first, code change second.

    python scripts/diagnose_missing_tags.py
"""

import os

import requests

CIK = 320187

CANDIDATES = {
    "inventory": [
        "InventoryNet",
        "InventoryFinishedGoodsNetOfReserves",
        "InventoryFinishedGoods",
        "InventoryGross",
        "InventoryRawMaterialsNetOfReserves",
    ],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
        "DepreciationNonproduction",
    ],
}


def main():
    ua = os.environ.get("TRELLIS_USER_AGENT")
    if not ua:
        print("Set TRELLIS_USER_AGENT first (same as before).")
        return
    headers = {"User-Agent": ua}

    for canonical, tags in CANDIDATES.items():
        print(f"\n=== {canonical} ===")
        for tag in tags:
            url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{CIK:010d}/us-gaap/{tag}.json"
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 404:
                print(f"  {tag}: not used by this filer (404)")
                continue
            resp.raise_for_status()
            data = resp.json()
            entries = [
                e for unit_entries in data.get("units", {}).values() for e in unit_entries
                if e.get("form") == "10-K" and e.get("fp") == "FY"
            ]
            entries.sort(key=lambda e: e["end"])
            recent = entries[-4:]
            print(f"  {tag}: USED -- {len(entries)} total FY/10-K entries")
            for e in recent:
                print(f"      {e['end']} = {e['val']:,} (filed {e['filed']}, accn {e['accn']})")


if __name__ == "__main__":
    main()
