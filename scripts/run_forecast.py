"""
Runnable example: pull any SEC filer's real data, build the historical table, and
produce a forecast. Same call sequence any user of this library would make.

Requires TRELLIS_USER_AGENT (see docs/NETWORK.md) and network access to data.sec.gov.

    python scripts/run_forecast.py --cik 320187              # Nike
    python scripts/run_forecast.py --cik 909832              # Costco
    python scripts/run_forecast.py --cik 1018724 --years 3   # Amazon, shorter horizon
"""

import argparse
import sys

sys.path.insert(0, "src")  # run without PYTHONPATH set, for convenience

from trellis.companies import get_profile
from trellis.forecast import (
    derive_drivers_from_history,
    reconcile_forecast_year,
    run_forecast,
)
from trellis.ingest import fetch_all
from trellis.statements import (
    build_annual_table,
    fill_derived_gaps,
    run_all_checks,
)

REQUIRED = ["revenue", "sga_expense", "income_tax_expense", "net_income",
            "accounts_receivable", "inventory", "accounts_payable", "capex",
            "depreciation_amortization", "ppe_net", "retained_earnings", "total_assets",
            "cash_and_equivalents", "total_liabilities", "stockholders_equity"]


def missing_fields(data):
    missing = [f for f in REQUIRED if f not in data]
    if "gross_profit" not in data and "cost_of_revenue" not in data:
        missing.append("gross_profit/cost_of_revenue")
    return missing


def pick_base_year(table):
    for year in sorted(table, reverse=True):
        if not missing_fields(table[year]):
            return year
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cik", type=int, required=True, help="SEC CIK number (no leading zeros)")
    parser.add_argument("--years", type=int, default=5, help="forecast horizon (default 5)")
    parser.add_argument("--lookback", type=int, default=5, help="trailing-average window (default 5)")
    args = parser.parse_args()

    profile = get_profile(args.cik)
    print(f"Fetching {profile.name} ({profile.ticker}, CIK {profile.cik}, "
          f"FYE {profile.fiscal_year_end}) from SEC EDGAR...")
    if profile.notes:
        print(f"Registry notes: {profile.notes}")

    raw = fetch_all(args.cik)
    if raw.get("_missing"):
        print(f"Tags never resolved for this filer (checked at ingest, not fatal): {raw['_missing']}")

    table = build_annual_table(raw)
    derived = fill_derived_gaps(table)
    print(f"Derived (not reported) fields filled: {len(derived)} instances across {len(table)} years")

    print("\n--- Field availability, last 8 years (blank = present) ---")
    recent = sorted(table)[-8:]
    for year in recent:
        gaps = missing_fields(table[year])
        print(f"  FY{year}: {'complete' if not gaps else 'missing ' + str(gaps)}")

    base_year = pick_base_year(table)
    if base_year is None:
        print("\nNo year has everything the forecast needs -- see gaps above.")
        return
    print(f"\nBase year for the forecast: FY{base_year}")

    recent_years = sorted(table)[-3:]
    print("\n--- Historical structural checks, most recent 3 years ---")
    for c in run_all_checks(table):
        if any(f"FY{y}:" in c.detail for y in recent_years):
            status = "PASS" if c.passed else "FLAG"
            print(f"[{c.kind.upper()}] {status} {c.name}: {c.detail}")

    drivers = derive_drivers_from_history(
        table, base_year, lookback_years=args.lookback, overrides=profile.overrides,
    )
    print(f"\n--- Drivers derived from FY{drivers.years_used[0]}-FY{drivers.years_used[-1]} "
          f"({len(drivers.years_used)}-yr trailing average) ---")
    for field_name, value in drivers.__dict__.items():
        if field_name in ("assumptions", "overrides_applied", "years_used"):
            continue
        print(f"  {field_name}: {value:.4f}" if isinstance(value, float) else f"  {field_name}: {value}")
    if drivers.overrides_applied:
        print("  Sourced overrides applied (cited, not auto-derived):")
        for o in drivers.overrides_applied:
            print(f"    - {o}")
    if drivers.assumptions:
        print("  Assumptions used (no data, no override -- genuine last resort):")
        for a in drivers.assumptions:
            print(f"    - {a}")

    forecast = run_forecast(table, base_year, drivers, years=args.years)

    print(f"\n--- {args.years}-year forecast (FY{base_year + 1}-FY{base_year + args.years}), $ millions ---")
    print(f"{'Year':<8}{'Revenue':>12}{'NetIncome':>12}{'Buybacks':>11}{'Cash':>12}{'TotalAssets':>14}")
    prior_cash = table[base_year]["cash_and_equivalents"]
    any_failed = False
    for year in sorted(forecast):
        y = forecast[year]
        print(f"FY{year:<6}{y['revenue'] / 1e6:>12,.0f}{y['net_income'] / 1e6:>12,.0f}"
              f"{y['buybacks'] / 1e6:>11,.0f}{y['cash_and_equivalents'] / 1e6:>12,.0f}"
              f"{y['total_assets'] / 1e6:>14,.0f}")
        check = reconcile_forecast_year(year, y, prior_cash)
        if not check.passed:
            any_failed = True
            print(f"  !! self-consistency check FAILED: {check.detail}")
        if y.get("dividend_capped"):
            print(f"  note: dividend payout ceiling ({drivers.max_payout_ratio:.0%} of net "
                  f"income) bound in FY{year} -- growth_rate trajectory would have exceeded it")
        prior_cash = y["cash_and_equivalents"]

    print("\n" + ("Every forecast year reconciled cleanly." if not any_failed
                   else "At least one year failed to reconcile -- see !! lines above."))


if __name__ == "__main__":
    main()
