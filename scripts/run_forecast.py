"""
Run the full Trellis pipeline against a real filer: ingest -> build annual table ->
fill derived gaps -> derive drivers from the latest year -> project 5 years forward.

Requires network access to data.sec.gov (won't run in a network-sandboxed environment).

Usage:
    python scripts/run_forecast.py                # Nike, CIK 320187
    python scripts/run_forecast.py --cik 320193    # any other filer by CIK
    python scripts/run_forecast.py --years 3
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "src")

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

LINE_ITEMS = ("revenue", "gross_profit", "operating_income", "net_income",
              "cash_and_equivalents", "total_assets", "total_liabilities",
              "stockholders_equity", "retained_earnings")


def fmt(v: float | None) -> str:
    return "--" if v is None else f"{v:,.0f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cik", type=int, default=320187, help="SEC CIK (default: Nike)")
    parser.add_argument("--years", type=int, default=5, help="Forecast horizon")
    args = parser.parse_args()

    print(f"Fetching CIK {args.cik} from SEC EDGAR...")
    observations = fetch_all(args.cik)
    missing = observations.get("_missing", [])
    if missing:
        print(f"  Tags not found for this filer (may be legitimately absent): {missing}")

    table = build_annual_table(observations)
    derived = fill_derived_gaps(table)
    if derived:
        by_field: dict[str, int] = {}
        for d in derived:
            by_field[d.canonical_name] = by_field.get(d.canonical_name, 0) + 1
        print(f"  Derived via identity (not directly reported): {by_field}")

    print("\n--- Structural checks, historical ---")
    hard_fails = 0
    for c in run_all_checks(table):
        if c.kind == "hard" and not c.passed:
            hard_fails += 1
            print(f"  [HARD FAIL] {c.detail}")
    print(f"  Hard checks: {'all passed' if hard_fails == 0 else f'{hard_fails} FAILED -- do not trust the forecast below'}")

    latest_year = max(y for y, d in table.items() if "revenue" in d and "total_assets" in d)
    print(f"\n--- Drivers derived from FY{latest_year} (most recent complete year) ---")
    drivers = derive_drivers_from_history(table, latest_year)
    for field in ("revenue_growth", "gross_margin", "sga_pct_revenue", "tax_rate",
                  "interest_rate", "dividend_payout_ratio"):
        print(f"  {field}: {getattr(drivers, field):.4f}")
    if drivers.assumptions:
        print(f"  ASSUMPTIONS (not derived from a reported figure): {drivers.assumptions}")

    forecast = run_forecast(table, latest_year, drivers, years=args.years)

    print(f"\n--- {args.years}-year forecast, self-consistency check per year ---")
    prior_cash = table[latest_year]["cash_and_equivalents"]
    for year in sorted(forecast):
        r = reconcile_forecast_year(year, forecast[year], prior_cash)
        status = "OK" if r.passed else "FAILED -- driver set is internally inconsistent"
        print(f"  FY{year}: {status} (diff {r.diff:,.0f})")
        prior_cash = forecast[year]["cash_and_equivalents"]

    print(f"\n--- Forecast line items (FY{latest_year} actual, then {args.years}-year projection) ---")
    header = ["Line item", f"FY{latest_year} (actual)"] + [f"FY{y}" for y in sorted(forecast)]
    print("  " + " | ".join(header))
    for item in LINE_ITEMS:
        row = [item, fmt(table[latest_year].get(item))]
        row += [fmt(forecast[y].get(item)) for y in sorted(forecast)]
        print("  " + " | ".join(row))


if __name__ == "__main__":
    main()
