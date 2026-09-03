"""
Historical statement construction and structural integrity checks.

Checks split into two kinds, and the split is deliberate -- collapsing it into one
pass/fail bucket would misrepresent what a failure means:

  HARD invariant: balance_sheet_balances. Assets = Liabilities + Equity is a
  mechanical requirement of a balance sheet as filed. Any nonzero diff beyond
  float/rounding tolerance means a data-pull bug, not a business event. Fails loud.

  SOFT diagnostic: cash_flow_ties_to_cash, retained_earnings_rollforward. These can
  legitimately fail for reasons outside this schema's line items -- FX translation
  on cash, restricted-cash reconciliation, treasury stock retired through RE, stock
  comp routed through equity. A fail here is a flag to go read the filing, not proof
  of a bug. Reporting it as "diagnostic" rather than silently passing with a loose
  tolerance is the point (same refuse-rather-than-guess posture as Bridgework's TBA).
"""

from __future__ import annotations

from dataclasses import dataclass

from .ingest import Observation

AnnualTable = dict[int, dict[str, float]]


@dataclass(frozen=True)
class CheckResult:
    name: str
    kind: str  # "hard" or "soft"
    passed: bool
    diff: float | None
    detail: str


def build_annual_table(observations: dict[str, list[Observation]]) -> AnnualTable:
    """FY-period observations only, keyed by fiscal_year -> canonical_name -> value.
    Multiple filings can report the same fiscal year (e.g. a 10-K and next year's 10-K
    comparative) -- ingest.py's dedup already resolved that to one observation per
    (period_end, unit), so the last one standing here is authoritative."""
    table: AnnualTable = {}
    for canonical_name, obs_list in observations.items():
        if canonical_name == "_missing":
            continue
        for obs in obs_list:
            if obs.fiscal_period != "FY":
                continue
            table.setdefault(obs.fiscal_year, {})[canonical_name] = obs.value
    return table


def check_balance_sheet_balances(year: int, year_data: dict[str, float],
                                  tolerance: float = 1.0) -> CheckResult:
    required = ("total_assets", "total_liabilities", "stockholders_equity")
    if not all(k in year_data for k in required):
        missing = [k for k in required if k not in year_data]
        return CheckResult("balance_sheet_balances", "hard", passed=False, diff=None,
                            detail=f"FY{year}: missing {missing} -- cannot evaluate")
    diff = year_data["total_assets"] - (year_data["total_liabilities"] + year_data["stockholders_equity"])
    passed = abs(diff) <= tolerance
    detail = (f"FY{year}: Assets {year_data['total_assets']:,.0f} vs "
              f"L+E {year_data['total_liabilities'] + year_data['stockholders_equity']:,.0f} "
              f"(diff {diff:,.0f})")
    return CheckResult("balance_sheet_balances", "hard", passed, diff, detail)


def check_cash_flow_ties_to_cash(year: int, year_data: dict[str, float],
                                  prior_data: dict[str, float] | None,
                                  tolerance: float = 5_000_000.0) -> CheckResult:
    required = ("cash_and_equivalents", "cfo", "cfi", "cff")
    if prior_data is None or "cash_and_equivalents" not in prior_data or \
            not all(k in year_data for k in required):
        return CheckResult("cash_flow_ties_to_cash", "soft", passed=False, diff=None,
                            detail=f"FY{year}: insufficient data (need prior-year cash + this year's CFO/CFI/CFF)")
    implied_change = year_data["cfo"] + year_data["cfi"] + year_data["cff"]
    actual_change = year_data["cash_and_equivalents"] - prior_data["cash_and_equivalents"]
    diff = actual_change - implied_change
    passed = abs(diff) <= tolerance
    detail = (f"FY{year}: actual cash change {actual_change:,.0f} vs CFO+CFI+CFF {implied_change:,.0f} "
              f"(diff {diff:,.0f}). Soft check -- FX translation and restricted-cash reconciliation "
              f"aren't in this schema, so a fail here flags 'go read the filing', not 'bug'.")
    return CheckResult("cash_flow_ties_to_cash", "soft", passed, diff, detail)


def check_retained_earnings_rollforward(year: int, year_data: dict[str, float],
                                         prior_data: dict[str, float] | None,
                                         tolerance: float = 5_000_000.0) -> CheckResult:
    required = ("retained_earnings", "net_income")
    if prior_data is None or "retained_earnings" not in prior_data or \
            not all(k in year_data for k in required):
        return CheckResult("retained_earnings_rollforward", "soft", passed=False, diff=None,
                            detail=f"FY{year}: insufficient data (need prior-year RE + this year's net income)")
    dividends = year_data.get("dividends_paid", 0.0)  # legitimately absent for non-dividend payers
    implied_end = prior_data["retained_earnings"] + year_data["net_income"] - dividends
    diff = year_data["retained_earnings"] - implied_end
    passed = abs(diff) <= tolerance
    detail = (f"FY{year}: RE {year_data['retained_earnings']:,.0f} vs implied "
              f"{implied_end:,.0f} (diff {diff:,.0f}). Soft check -- treasury-stock retirement "
              f"and other direct-to-equity items can route through RE outside this schema.")
    return CheckResult("retained_earnings_rollforward", "soft", passed, diff, detail)


def run_all_checks(table: AnnualTable) -> list[CheckResult]:
    results: list[CheckResult] = []
    for year in sorted(table):
        prior = table.get(year - 1)
        results.append(check_balance_sheet_balances(year, table[year]))
        results.append(check_cash_flow_ties_to_cash(year, table[year], prior))
        results.append(check_retained_earnings_rollforward(year, table[year], prior))
    return results
