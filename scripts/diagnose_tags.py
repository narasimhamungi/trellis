"""
General diagnostic, not part of the library: for a given filer, probe several candidate
XBRL tags per canonical field directly against SEC and report exactly what's there.
Root cause first, code change second -- same discipline used to find Nike's real
inventory/D&A tags, generalized to any company and any set of gaps.

    python scripts/diagnose_tags.py --cik 909832 --field accounts_receivable
    python scripts/diagnose_tags.py --cik 1018724 --field sga_expense --field capex
"""

import argparse
import os

import requests

# Extra candidates beyond what's already in schema.py, tried when a field is still
# missing after the schema's own fallback chain -- these are guesses to be CONFIRMED
# against real data here, not assumptions to add to schema.py without verification.
EXTRA_CANDIDATES = {
    "accounts_receivable": [
        "AccountsReceivableNetCurrent", "ReceivablesNetCurrent",
        "AccountsAndOtherReceivablesNetCurrent", "OtherReceivablesNetCurrent",
        "ReceivablesNet", "AccountsReceivableNet",
    ],
    "sga_expense": [
        "SellingGeneralAndAdministrativeExpense", "GeneralAndAdministrativeExpense",
        "SellingAndMarketingExpense", "MarketingExpense",
        "SellingExpense", "OperatingExpenses",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
        "PaymentsToAcquireProductiveAssets", "PaymentsForCapitalImprovements",
    ],
    "operating_income": [
        "OperatingIncomeLoss", "CostsAndExpenses",
    ],
    "buybacks": [
        "PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity",
        "PaymentsForRepurchaseOfCommonStockAndPreferredStock",
    ],
    "dividends_paid": [
        "PaymentsOfDividendsCommonStock", "PaymentsOfDividends",
        "PaymentsOfDividendsMinorityInterest", "PaymentsOfOrdinaryDividends",
        "DividendsCommonStockCash", "DividendsPaid",
    ],
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cik", type=int, required=True)
    parser.add_argument("--field", action="append", required=True,
                         help="canonical field name to diagnose; repeatable")
    args = parser.parse_args()

    ua = os.environ.get("TRELLIS_USER_AGENT")
    if not ua:
        print("Set TRELLIS_USER_AGENT first.")
        return
    headers = {"User-Agent": ua}

    for field in args.field:
        tags = EXTRA_CANDIDATES.get(field)
        if not tags:
            print(f"\n=== {field}: no candidate list defined -- add one to EXTRA_CANDIDATES ===")
            continue
        print(f"\n=== {field} ===")
        for tag in tags:
            url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{args.cik:010d}/us-gaap/{tag}.json"
            resp = requests.get(url, headers=headers, timeout=30)
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
