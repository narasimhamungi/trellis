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


def pick_base_year(table, stub_years=frozenset(), max_staleness=5):
    """Most recent year with everything the forecast needs -- but capped at
    `max_staleness` years behind the newest year with ANY data at all, rather than
    silently walking back an unbounded distance. Confirmed as a real failure mode
    against live Amazon data: recent years were missing sga_expense (no consolidated
    SG&A tag -- since fixed via an identity-based derivation, but this safeguard stays
    regardless of that fix, because SOME future gap on SOME other company could trigger
    the identical failure), so the old version silently walked back to FY2009 -- a
    company with ~$24.5B revenue standing in for one with ~$638B today. Returns
    (year_or_None, newest_year_with_any_data) so the caller can report why a refusal
    happened, not just that it did.

    stub_years are also skipped as a base-year candidate: a stub period's balance sheet
    is a perfectly valid point-in-time snapshot, but its flow figures (revenue, capex,
    dividends, ...) cover a truncated period, so bootstrapping the forecast's starting
    scale from them would understate every flow-based figure the first forecast year
    reads directly off the base year."""
    newest = max(table)
    for year in sorted(table, reverse=True):
        if newest - year > max_staleness:
            return None, newest
        if year not in stub_years and not missing_fields(table[year]):
            return year, newest
    return None, newest


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

    build_result = build_annual_table(raw)
    table = build_result.table
    if build_result.fye_collisions:
        print(f"\nFYE-change collisions detected: {len(build_result.fye_collisions)} field(s) "
              f"dropped because their period_end disagreed with the year's authoritative "
              f"(latest) period -- likely a fiscal-year-end change:")
        for c in build_result.fye_collisions[:5]:
            print(f"  FY{c.year} {c.canonical_name}: dropped period_end={c.dropped_period_end}, "
                  f"kept period_end={c.kept_period_end}")
        if len(build_result.fye_collisions) > 5:
            print(f"  ... and {len(build_result.fye_collisions) - 5} more")
    if build_result.stub_periods:
        print("\nNon-standard period duration detected (possible stub/transition period "
              "from a fiscal-year-end change) -- excluded from driver averaging below:")
        for s in build_result.stub_periods:
            print(f"  FY{s.year} (period_end {s.period_end}): {s.duration_days} days, not ~365")
    derived = fill_derived_gaps(table)
    print(f"Derived (not reported) fields filled: {len(derived)} instances across {len(table)} years")

    print("\n--- Field availability, last 8 years (blank = present) ---")
    recent = sorted(table)[-8:]
    for year in recent:
        gaps = missing_fields(table[year])
        print(f"  FY{year}: {'complete' if not gaps else 'missing ' + str(gaps)}")

    stub_years = {s.year for s in build_result.stub_periods}
    base_year, newest_year = pick_base_year(table, stub_years=stub_years)
    if base_year is None:
        print(f"\nNo year within 5 years of the most recent data (FY{newest_year}) has "
              f"everything the forecast needs -- see gaps above. Refusing to silently fall "
              f"back further; that's what let a real run for a different company forecast "
              f"off 16-year-stale data. Add a tag fallback (see scripts/diagnose_tags.py) "
              f"for whichever field is blocking the recent years, rather than widening this "
              f"limit.")
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
        exclude_years=stub_years,
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
    any_insolvent = False
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
        if y.get("revolver_draw", 0.0) > 0:
            print(f"  note: drew {y['revolver_draw'] / 1e6:,.0f}M on the revolver in FY{year} "
                  f"to hold cash at the floor (balance now {y['revolver_balance'] / 1e6:,.0f}M)")
        if y.get("insolvent"):
            any_insolvent = True
            print(f"  !! FY{year}: cash floor NOT reachable even with the full revolver_limit "
                  f"-- this scenario is not fundable as modeled, not just tight")
        prior_cash = y["cash_and_equivalents"]

    print()
    print("Every forecast year passed its internal arithmetic check."
          if not any_failed else "At least one year failed to reconcile -- see !! lines above.")
    print("This confirms the code has no internal contradiction. It does NOT confirm the "
          "forecast is economically realistic -- see any insolvency or dividend-cap notes "
          "above for the checks that actually can fail on economics.")
    if any_insolvent:
        print("At least one year is flagged insolvent under the stated revolver_limit -- "
              "treat this scenario's later years as not meaningfully fundable, not merely tight.")


if __name__ == "__main__":
    main()
