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
from datetime import date

from .ingest import Observation

AnnualTable = dict[int, dict[str, float]]


@dataclass(frozen=True)
class CheckResult:
    name: str
    kind: str  # "hard" or "soft"
    passed: bool
    diff: float | None
    detail: str


@dataclass(frozen=True)
class FyeCollision:
    year: int
    canonical_name: str
    dropped_period_end: str
    kept_period_end: str


@dataclass(frozen=True)
class StubPeriod:
    year: int
    period_end: str
    duration_days: int


@dataclass(frozen=True)
class BuildTableResult:
    table: AnnualTable
    fye_collisions: tuple[FyeCollision, ...]
    stub_periods: tuple[StubPeriod, ...]


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def build_annual_table(observations: dict[str, list[Observation]]) -> BuildTableResult:
    """FY-period observations only, keyed by the calendar year of period_end -- NOT by
    the SEC-reported fiscal_year field.

    This is not a style choice. A 10-K's income statement shows three years of
    comparatives, so an older period gets re-published (unchanged) in each subsequent
    year's filing. SEC's own 'fy' field on that fact reflects which filing reported it,
    not the period it describes -- so the *most recently filed* instance of an older
    period (which _dedupe_restatements correctly prefers, since that's the right rule
    for genuine restatements) can carry a 'fy' label that's 1-2 years later than the
    period it actually is. Confirmed against live Nike data: FY2017 revenue ($34.35B,
    period_end 2017-05-31) came back labeled fiscal_year=2019 -- correct value, correct
    period, wrong label, because the FY2019 10-K happened to be the last filing to
    include FY2017 as a trailing comparative. Keying off period_end sidesteps the
    ambiguity entirely; deriving the calendar year from an ISO date is not.

    Also guards against a franken-year: a fiscal-year-end change can put two genuinely
    different periods in the same calendar-year bucket (e.g. a period ending 2024-06-30
    and another ending 2024-12-31 both map to year=2024 under int(period_end[:4])).
    Silently keeping whichever field a canonical_name's own obs_list happened to report
    last would mix fields from two DIFFERENT periods under one year-key. Resolution: for
    each year, determine the single AUTHORITATIVE period_end across all fields, and
    accept only fields whose own period_end matches it -- every accepted field for a
    given year comes from the same period, never mixed. Anything dropped for disagreeing
    is reported in fye_collisions, not silently discarded.

    The authoritative period_end is chosen by DURATION closeness to 365 days, not by
    which date is latest in the calendar year -- confirmed as a real, damaging bug from
    the earlier 'latest date wins' version: SEC's own data contains short periods
    (quarterly or half-year cumulative figures) spuriously tagged fp='FY' in early-XBRL-
    era filings (roughly 2008-2019), a known real-world data-quality issue, not
    something this code invented. When such a spurious short period's date happened to
    fall LATER in the calendar year than the genuine annual period, 'latest wins' picked
    the wrong one -- caught live: Nike FY2014 chose period_end=2014-11-30 (182 days)
    over the real 2014-05-31 annual close, and Costco lost nearly every field for
    FY2018-2019 the same way, choosing a spurious ~83-day November period over its real
    ~August fiscal year end. Duration closeness to 365 days is the correct signal
    because it's what actually distinguishes 'genuine annual period' from 'quarterly
    figure mislabeled as annual', which recency of date does not.

    Also flags stub/transition periods: even the correctly-chosen authoritative period
    can genuinely be a short transition period from a real fiscal-year-end change (this
    is the case duration-closeness can't rule out, since a true 6-month transition period
    IS the only 'FY' period for that year -- there's no 365-day alternative to prefer
    instead). One whose duration falls well outside ~300-400 days (wide enough for
    52/53-week fiscal years) is reported separately in stub_periods so a caller can
    exclude it from trend/lookback averaging rather than treating half a year of flow
    data as a normal one."""
    candidates: dict[int, dict[str, int | None]] = {}
    for canonical_name, obs_list in observations.items():
        if canonical_name == "_missing":
            continue
        for obs in obs_list:
            if obs.fiscal_period != "FY":
                continue
            year = int(obs.period_end[:4])
            year_candidates = candidates.setdefault(year, {})
            duration = None
            if obs.period_start:
                duration = (_parse_date(obs.period_end) - _parse_date(obs.period_start)).days
            if year_candidates.get(obs.period_end) is None:
                year_candidates[obs.period_end] = duration

    authoritative_period_end: dict[int, str] = {}
    for year, cands in candidates.items():
        with_duration = {pe: d for pe, d in cands.items() if d is not None}
        if with_duration:
            authoritative_period_end[year] = min(with_duration, key=lambda pe: abs(with_duration[pe] - 365))
        else:
            # No duration-bearing fact for any candidate this year (e.g. only instant/
            # balance-sheet facts ever hit it) -- nothing to judge duration by, so fall
            # back to the latest date as a last resort, same as before.
            authoritative_period_end[year] = max(cands)

    table: AnnualTable = {}
    fye_collisions: list[FyeCollision] = []
    duration_by_year: dict[int, int] = {}
    for canonical_name, obs_list in observations.items():
        if canonical_name == "_missing":
            continue
        for obs in obs_list:
            if obs.fiscal_period != "FY":
                continue
            year = int(obs.period_end[:4])
            if obs.period_end != authoritative_period_end[year]:
                fye_collisions.append(FyeCollision(
                    year=year, canonical_name=canonical_name,
                    dropped_period_end=obs.period_end, kept_period_end=authoritative_period_end[year],
                ))
                continue
            table.setdefault(year, {})[canonical_name] = obs.value
            if obs.period_start and year not in duration_by_year:
                duration_by_year[year] = (_parse_date(obs.period_end) - _parse_date(obs.period_start)).days

    stub_periods = tuple(
        StubPeriod(year=yr, period_end=authoritative_period_end[yr], duration_days=dur)
        for yr, dur in duration_by_year.items()
        if not (300 <= dur <= 400)
    )
    return BuildTableResult(table=table, fye_collisions=tuple(fye_collisions), stub_periods=stub_periods)


@dataclass(frozen=True)
class DerivedField:
    year: int
    canonical_name: str
    method: str


def fill_derived_gaps(table: AnnualTable) -> list[DerivedField]:
    """Some line items are legitimately absent from a filer's own tagging -- not a pull
    failure, just a different presentation format (Nike, for instance, has no separately
    tagged operating-income subtotal; its income statement goes straight from expenses to
    pretax income). Where the gap can be closed with an exact identity rather than a guess,
    close it here -- but track that it was derived, not reported, the same
    Demonstrated-vs-Inferred distinction Bridgework's claim register uses. Mutates table
    in place; returns what was filled so it can be surfaced in reporting, not hidden."""
    filled: list[DerivedField] = []
    for year in sorted(table):
        data = table[year]
        if "total_liabilities" not in data and "total_assets" in data and "stockholders_equity" in data:
            data["total_liabilities"] = data["total_assets"] - data["stockholders_equity"]
            filled.append(DerivedField(year, "total_liabilities", "total_assets - stockholders_equity"))
        if "stockholders_equity" not in data and "total_assets" in data and "total_liabilities" in data:
            # The same identity run the other direction. Confirmed live and predicted in
            # advance by this session's own red-team review: a filer with noncontrolling
            # interests can tag total_liabilities directly while StockholdersEquity
            # itself doesn't resolve (likely needs
            # StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest
            # instead -- a fallback tag not yet added, since the identity below closes
            # the gap immediately without needing to confirm which alternate tag a given
            # filer actually uses). Mutually exclusive with the rule above by
            # construction (each requires the field the OTHER one produces to already
            # be absent), so there's no risk of deriving one from a value the other
            # rule just invented in the same pass.
            data["stockholders_equity"] = data["total_assets"] - data["total_liabilities"]
            filled.append(DerivedField(year, "stockholders_equity", "total_assets - total_liabilities"))
        if "gross_profit" not in data and "revenue" in data and "cost_of_revenue" in data:
            # Some filers (Amazon, Costco among the ones tested here) don't tag a
            # GrossProfit subtotal at all -- their income statement goes straight from
            # revenue through cost of sales into other expense lines with no gross-profit
            # line on the face of the statement. Deriving it from Revenue - Cost of
            # Revenue is exact, and matters beyond just filling this one field: it's a
            # dependency for the operating_income and sga_expense derivations below, and
            # for gross_margin in forecast.derive_drivers_from_history. Without this,
            # gross_margin silently averaged to 0.0 for Costco (confirmed against real
            # data) because nothing downstream could compute it at all.
            data["gross_profit"] = data["revenue"] - data["cost_of_revenue"]
            filled.append(DerivedField(year, "gross_profit", "revenue - cost_of_revenue"))
        if "operating_income" not in data and "gross_profit" in data and "sga_expense" in data:
            data["operating_income"] = data["gross_profit"] - data["sga_expense"]
            filled.append(DerivedField(year, "operating_income", "gross_profit - sga_expense"))
        if "sga_expense" not in data and "gross_profit" in data and "operating_income" in data:
            # Not every filer tags a single consolidated SG&A line -- Amazon, for one,
            # reports Cost of sales, Fulfillment, Technology and content, Marketing, and
            # G&A as five separate categories with no combining tag at all. Rather than
            # chase an unbounded list of category-specific tags (and still risk missing
            # one), use the identity every income statement satisfies by definition:
            # Gross Profit - Operating Income = all operating expenses, however many
            # categories a filer splits them into. Exact, not an approximation, and it
            # generalizes to any expense-line structure without company-specific tags.
            # Guarded to run after the operating_income rule above so this can't derive
            # from a value that was itself just derived from sga_expense in this same
            # pass -- it only fires when operating_income was already present (reported
            # or already resolved), never when both are simultaneously missing.
            data["sga_expense"] = data["gross_profit"] - data["operating_income"]
            filled.append(DerivedField(year, "sga_expense", "gross_profit - operating_income"))
    return filled


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
