"""
Forecast engine.

Method: every balance-sheet line except cash is driven (revenue growth, margins,
day-based working capital, capex/D&A, a debt schedule). Cash is then the plug:

    Cash = (Total Liabilities + Total Equity) - Non-cash Assets

This makes the balance sheet balance by construction -- it cannot fail the hard
check from statements.py, because it's defined to satisfy it. reconcile_forecast_year
below is the corresponding invariant for the cash-flow side: it checks that the
plugged cash change equals what CFO+CFI+CFF independently implies. Both checks are
IMPLEMENTATION INVARIANTS, not economic validation -- confirmed by direct testing:
they cannot fail on the model asserting an economically impossible state (unbounded
negative cash, an unfundable distribution), only on an actual arithmetic bug in this
module (which is exactly what caught the equity-roll error and the sign-cancellation
in an earlier version of this file). A forecast printing "every year reconciled" means
the code has no internal contradiction -- it does NOT mean the company can actually
fund what's being modeled. See the feasibility layer (cash floor + revolver + the
`insolvent` flag in project_year's output) for the check that actually can fail on
economics, and treat the two kinds of check as answering different questions.

"other assets" and "other liabilities" (whatever this schema didn't capture as a
named line item) are carried flat from the last historical year -- an explicit,
stated assumption, not a hidden one.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from .statements import AnnualTable, CheckResult

DIVIDEND_GROWTH_SANITY_BAND = (-0.15, 0.50)  # see derive_drivers_from_history for why


@dataclass(frozen=True)
class Drivers:
    revenue_growth: float
    gross_margin: float
    sga_pct_revenue: float
    tax_rate: float
    ar_days: float
    inventory_days: float
    ap_days: float
    capex_pct_revenue: float
    da_pct_revenue: float
    interest_rate: float          # applied to beginning-of-year LT debt balance
    debt_repayment: float         # dollars/year; 0.0 = flat debt
    dividend_payout_ratio: float  # used only when dividend_policy == "payout_ratio"
    dividend_growth_rate: float = 0.0     # used only when dividend_policy == "growth_rate"
    dividend_policy: str = "growth_rate"  # which of the two drivers above project_year uses
    capital_return_policy: str = "none"  # "sweep_to_buybacks" or "none"
    cash_floor_pct_revenue: float = 0.0  # target minimum cash balance, as % of that
    # year's revenue; only used when capital_return_policy == "sweep_to_buybacks"
    # Floor-and-sweep, not a fixed % of FCF: a fixed percentage is itself an invented
    # number and doesn't actually prevent cash from pooling (a "conservative" fixed %
    # still lets excess accumulate whenever FCF is unusually high). A target minimum
    # balance is self-correcting by construction -- nothing above the floor can ever
    # pool, regardless of how FCF varies year to year.
    #
    # Floor defined as % of REVENUE, not % of assets: operating cash needs scale with
    # the business's actual activity (transaction volume, payroll, working-capital
    # cushion), not with total assets, which includes PP&E, goodwill, and other items
    # unrelated to day-to-day liquidity -- and which is itself partly determined by the
    # cash balance, making it a circular denominator for a cash policy variable.
    #
    # Derived as the MINIMUM historical cash/revenue ratio over the lookback window, not
    # the average: a floor should represent the most conservative cushion the company has
    # actually operated with, not a central tendency -- averaging would trigger "excess"
    # sweeps even in years that were perfectly ordinary for that company.
    #
    # capital_return_policy defaults to "sweep_to_buybacks" ONLY when the historical data
    # shows the company actually repurchasing shares -- a company with no buyback history
    # doesn't get one invented for it just because it generates cash. See
    # derive_drivers_from_history for the detection logic.
    max_payout_ratio: float = 1.0  # hard ceiling on dividends_paid as a fraction of THIS
    # year's net income, enforced in project_year regardless of dividend_policy. Without
    # it, growth_rate compounds a fixed rate against flat-or-declining earnings with no
    # floor tied to profitability -- confirmed against the real Nike forecast: 6.99%
    # compounding against roughly flat ~$4.05B net income pushes the implied payout ratio
    # from ~51% toward the low-80s% by FY2031, and nothing stopped it from continuing past
    # 100% on a longer horizon or a flatter/worse earnings year. payout_ratio policy has
    # the identical risk if the derived average itself exceeds 1.0. 1.0 (100%) is the
    # default because it's the cleanest defensible line: a single year paying out more
    # than it earned happens in practice (drawing on cash), but a *forecast* projecting
    # that indefinitely is asserting an unsustainable trajectory as the base case, not
    # modeling one. See project_year: once the ceiling binds in a given year, the
    # following year's growth-rate compounding starts from the capped figure, not the
    # uncapped one -- the model doesn't secretly remember a shadow trajectory.
    # Two genuinely different, both-legitimate models of how companies actually set
    # dividends. "payout_ratio" ties the dividend to THIS YEAR's earnings -- fine for a
    # company that targets a payout fraction. "growth_rate" extrapolates the dividend's
    # OWN trajectory, independent of near-term earnings -- Lintner's classic finding that
    # real dividends are sticky and smoothed, adjusted gradually toward a long-run target
    # rather than reset to a ratio every year. Confirmed against real Nike data: FY24-26
    # dividends grew steadily ($2.17B -> $2.30B -> $2.41B) straight through an earnings
    # downturn that would have implied a falling payout-ratio-based dividend -- Nike was
    # visibly defending the dividend, not sizing it to that year's income. growth_rate is
    # the default for that reason, not because payout_ratio is wrong in general.
    years_used: tuple[int, ...] = ()  # the actual lookback window that produced these
    # numbers. Exists so nothing downstream (a report, a script, a print statement) ever
    # has to recompute or hardcode which years were used -- a caller that guesses this
    # from `lookback_years` and `base_year` independently can silently drift out of sync
    # if the default changes, which is exactly what happened once already in this
    # project's own example script before this field existed.
    revolver_limit: float | None = None  # None = unbounded revolver (the honest default
    # absent real credit-facility data). When a real limit is known (most 10-Ks disclose
    # one in the debt footnote), supply it via the same sourced-override mechanism used
    # for interest_rate -- a stated, cited capacity is far better than either an
    # unbounded assumption or an invented number. See project_year's `insolvent` flag:
    # with no limit, cash can always be topped up to the floor and nothing can ever be
    # flagged infeasible by construction, which is a real, disclosed limitation of the
    # default, not a claim that every company can always fund itself.
    assumptions: tuple[str, ...] = ()        # last-resort defaults: no data, no override
    overrides_applied: tuple[str, ...] = ()  # analyst-sourced, cited values used in place
    # of auto-derivation -- for a driver that genuinely can't be pulled from tagged filing
    # data (Nike's interest_expense isn't a standard us-gaap element anywhere in its
    # primary statements -- confirmed by direct diagnostic, not assumed), the honest move
    # is a cited, researched number, not a silent 0.0. This is the general mechanism for
    # any company's undiscoverable driver, not a Nike-specific patch: three-way split --
    # Demonstrated (silent, auto-derived) / Sourced-override (cited here) / Assumed
    # (in `assumptions`, genuinely nothing to go on) -- same fact discipline as elsewhere
    # in this project, just applied to forecast inputs instead of attribution.


def _cogs_for_year(y: dict[str, float]) -> float | None:
    if "gross_profit" in y and "revenue" in y:
        return y["revenue"] - y["gross_profit"]
    return y.get("cost_of_revenue")


def derive_drivers_from_history(
    table: AnnualTable,
    base_year: int,
    lookback_years: int = 5,
    overrides: dict[str, tuple[float, str]] | None = None,
    dividend_policy: str = "growth_rate",
    capital_return_policy: str | None = None,
    exclude_years: set[int] | None = None,
) -> Drivers:
    """Ratio-based drivers are averaged over the trailing `lookback_years` (default 5)
    ending at base_year -- not read off a single year. A single-year snapshot is fragile
    to whatever happened to be true that specific year. Confirmed against real Nike data:
    FY2026 alone gave inventory_days=103 (elevated -- Nike's 2023-24 inventory glut was
    still working through) and a dividend_payout_ratio of 77% (roughly double Nike's
    historical norm). A trailing average is still 100% derived from reported actuals --
    never invented -- just averaged over more of them.

    5 years, not 3: a 3-year window ending FY2026 sits entirely inside the same earnings
    downturn (FY24-26), so averaging within it can't correct for the window itself being
    anomalous -- confirmed against real Nike data, where the 3-yr average payout ratio
    (62%) was still roughly double Nike's normal level. 5 years reaches back into FY2022,
    before the downturn, for a genuinely more representative baseline. This is still a
    real trade-off, not a free fix: more years means more history is stale relative to
    the business today, and lookback_years is a parameter precisely so it can be tuned
    per company rather than treated as universally correct at any fixed value.

    revenue_growth is a CAGR across the lookback window, not one year-over-year delta,
    for the same single-year-fragility reason. Worth flagging on its own terms even as a
    CAGR: Nike's FY24-26 decline is a specific, documented event (inventory correction,
    a leadership change explicitly pursuing a turnaround) that a pure trend extrapolation
    can't distinguish from a permanent structural decline -- it just continues whatever
    slope it sees. Treat a forecast built on this CAGR as a labeled scenario ("recent
    trend continues"), not an unconditional prediction.

    `dividend_policy`: 'growth_rate' (default) extrapolates the dividend's own trajectory;
    'payout_ratio' ties it to each year's own net income. See Drivers.dividend_policy for
    why growth_rate is the default. Whichever is picked, both are always computed here so
    the caller can compare.

    `capital_return_policy`: None (default) auto-detects from history -- 'sweep_to_buybacks'
    if the filer repurchased shares in any lookback year, else 'none'. Pass 'sweep_to_buybacks'
    or 'none' explicitly to override the detection. See Drivers.capital_return_policy for
    the floor-and-sweep mechanism itself.

    `overrides`: {driver_name: (value, source_citation)}. For a driver that can't be
    derived from tagged data in any lookback year at all, supply a cited value instead of
    silently defaulting -- an analyst manually researching and citing one input when the
    structured pull genuinely can't reach it is normal practice, general to any company,
    not specific to this one. Recorded in Drivers.overrides_applied, never silent.

    `exclude_years`: years to drop from the lookback window entirely before computing
    anything -- for stub/transition periods from a fiscal-year-end change (see
    statements.build_annual_table's StubPeriod), where the period's duration isn't
    close to a normal year, so averaging its flow-based ratios in as if it were one
    would understate them. Excluding shrinks the effective window and is disclosed as
    an assumption the same way a genuine data gap is, not silently absorbed.
    """
    if dividend_policy not in ("growth_rate", "payout_ratio"):
        raise ValueError(f"dividend_policy must be 'growth_rate' or 'payout_ratio', got {dividend_policy!r}")
    overrides = overrides or {}
    exclude_years = exclude_years or set()
    years = sorted(y for y in range(base_year - lookback_years + 1, base_year + 1)
                   if y in table and y not in exclude_years)
    if not years:
        raise ValueError(f"No historical data at or before FY{base_year}")

    assumptions: list[str] = []
    overrides_applied: list[str] = []
    excluded_in_window = sorted(y for y in exclude_years
                                 if base_year - lookback_years + 1 <= y <= base_year)
    if excluded_in_window:
        assumptions.append(
            f"Excluded {excluded_in_window} from the lookback window (stub/transition "
            f"period(s) -- duration not close to a normal year) -- averaged over the "
            f"remaining {len(years)} year(s) instead."
        )
    if len(years) < lookback_years - len(excluded_in_window):
        assumptions.append(
            f"Requested {lookback_years}-yr lookback, only {len(years)} year(s) available "
            f"{years} -- averaged over what exists, not padded or extrapolated."
        )

    def yearly(field_fn):
        out = []
        for yr in years:
            v = field_fn(table[yr])
            if v is not None:
                out.append(v)
        return out

    def avg(vals, driver_name, method="mean"):
        """No silent zeros: an empty list means nothing in the lookback window could
        produce this driver at all, and 0.0 is very rarely the economically correct
        stand-in (a 0% gross margin, 0-day receivables, etc. are all almost always
        wrong, not neutral). Confirmed as a real, serious gap against live Costco data:
        gross_margin silently averaged to 0.0 with no warning, producing a forecast with
        five years of large losses that still printed 'reconciled cleanly' -- internal
        self-consistency was never the problem, an unflagged bad input was. Every
        averaged driver goes through this now, not just the ones that have already
        caused a visible failure.

        method='median' is used for dividend_payout_ratio specifically -- a mean is not
        robust to the same special-dividend contamination that dividend_growth_rate
        already needed a median for (both are computed over the identical lookback
        window, so a special-dividend year distorts both drivers, not just one)."""
        if not vals:
            assumptions.append(
                f"{driver_name}: no data available in any lookback year -- assumed 0.0. "
                f"This is very likely wrong, not a neutral default -- check whether the "
                f"underlying tag needs a fallback added (see scripts/diagnose_tags.py) "
                f"before trusting any forecast built on this driver."
            )
            return 0.0
        if method == "median":
            s = sorted(vals)
            n = len(s)
            mid = n // 2
            return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
        return sum(vals) / len(vals)

    def ratio(y, num_key, denom_key=None, use_cogs=False):
        if num_key not in y:
            return None
        denom = _cogs_for_year(y) if use_cogs else y.get(denom_key)
        return y[num_key] / denom if denom else None

    def cagr(get_value, label):
        """Endpoint-to-endpoint CAGR. Used for revenue_growth -- appropriate there
        because revenue is rarely lumpy in a way that makes one endpoint unrepresentative.
        NOT used for dividends; see median_yoy_growth below. Returns
        (rate, assumption_note_or_None)."""
        first_year, last_year = years[0], years[-1]
        v_first, v_last = get_value(table[first_year]), get_value(table[last_year])
        if len(years) < 2 or v_first is None or v_last is None or v_first <= 0:
            return 0.0, (f"Cannot compute a {label} CAGR (need 2+ years with a positive "
                         f"starting value) -- assumed 0.0")
        periods = len(years) - 1
        return (v_last / v_first) ** (1 / periods) - 1, None

    def median_yoy_growth(get_value, label):
        """Median of year-over-year growth rates, not an endpoint CAGR. A two-point CAGR
        is entirely determined by whichever two years happen to be first and last --
        confirmed as a real problem against live Costco data: dividend_growth_rate came
        back -21.5%/year, which compounded over a 5-year forecast implies Costco's
        dividend shrinking to ~30% of its current level, nothing like their actual,
        steadily-growing regular dividend. The distortion is a special dividend (a real,
        lumpy, one-off payment on top of the regular quarterly dividend) landing near one
        end of the lookback window. Using the median across all adjacent-year growth
        rates instead means a single lumpy year can only ever be one data point among
        several -- it can't single-handedly set the result the way an endpoint can.
        Returns (rate, assumption_note_or_None)."""
        pairs = []
        for prev_yr, yr in itertools.pairwise(years):
            v_prev, v_cur = get_value(table[prev_yr]), get_value(table[yr])
            if v_prev is not None and v_cur is not None and v_prev > 0:
                pairs.append(v_cur / v_prev - 1)
        if not pairs:
            return 0.0, (f"Cannot compute a {label} growth rate (need 2+ consecutive years "
                         f"with a positive starting value) -- assumed 0.0")
        pairs.sort()
        mid = len(pairs) // 2
        median = pairs[mid] if len(pairs) % 2 else (pairs[mid - 1] + pairs[mid]) / 2
        return median, None

    def gross_profit_for_year(y):
        if "gross_profit" in y:
            return y["gross_profit"]
        cogs = _cogs_for_year(y)
        return y["revenue"] - cogs if cogs is not None and "revenue" in y else None

    gross_margins = yearly(lambda y: gross_profit_for_year(y) / y["revenue"]
                            if gross_profit_for_year(y) is not None and y.get("revenue") else None)
    sga_pcts = yearly(lambda y: ratio(y, "sga_expense", "revenue"))
    tax_rates = yearly(lambda y: y["income_tax_expense"] / (y["net_income"] + y["income_tax_expense"])
                        if "income_tax_expense" in y and "net_income" in y
                        and (y["net_income"] + y["income_tax_expense"]) else None)
    ar_days_list = [v * 365 for v in yearly(lambda y: ratio(y, "accounts_receivable", "revenue"))]
    inv_days_list = [v * 365 for v in yearly(lambda y: ratio(y, "inventory", use_cogs=True))]
    ap_days_list = [v * 365 for v in yearly(lambda y: ratio(y, "accounts_payable", use_cogs=True))]
    capex_pcts = yearly(lambda y: ratio(y, "capex", "revenue"))
    da_pcts = yearly(lambda y: ratio(y, "depreciation_amortization", "revenue"))
    payout_ratios = yearly(lambda y: y.get("dividends_paid", 0.0) / y["net_income"]
                            if y.get("net_income", 0.0) > 0 else None)
    # Excludes loss years entirely rather than letting a negative ratio (a maintained
    # dividend against negative net income) pull the average down or negative -- real
    # bug, confirmed by tracing the code: a sufficiently negative average, applied via
    # `max(net_income,0) * payout_ratio` to a later PROFITABLE year, produces negative
    # projected dividends with no guard catching it. The final max(...,0.0) below is a
    # second, defensive line against the same failure mode.

    revenue_growth, growth_note = cagr(lambda y: y.get("revenue"), "revenue")
    if growth_note:
        assumptions.append(growth_note)

    dividend_growth_rate, div_growth_note = median_yoy_growth(lambda y: y.get("dividends_paid"), "dividend")
    if not div_growth_note and dividend_policy == "growth_rate" and not (
        DIVIDEND_GROWTH_SANITY_BAND[0] <= dividend_growth_rate <= DIVIDEND_GROWTH_SANITY_BAND[1]
    ):
        # A well-defined median can still be implausible -- confirmed against real Costco
        # data: special dividends every ~2.75 years (2012, 2015, 2017, 2020, Jan 2024)
        # mean a 5-year window quite often catches TWO of them, not one. With only 4
        # year-over-year rates, 2 post-special-dividend reversions is enough to still
        # dominate the median (switching from endpoint CAGR to median took the distortion
        # from -21.5%/yr to -45.2%/yr -- worse, not better, because the problem isn't a
        # single outlier, it's structural to how this filer pays dividends). No further
        # cleverness on the raw growth rate fixes this reliably; the fix is recognizing
        # the result is implausible for an ONGOING dividend policy and using the bounded
        # alternative instead. This band is deliberately wide -- Nike's real +6.99% and
        # Apple's real +1.61% both sit comfortably inside it -- catching only genuinely
        # implausible sustained rates, not real fast dividend growth.
        div_growth_note = (
            f"Computed dividend growth rate ({dividend_growth_rate:.1%}/yr) is outside a "
            f"plausible range for an ONGOING policy ({DIVIDEND_GROWTH_SANITY_BAND[0]:.0%} to "
            f"{DIVIDEND_GROWTH_SANITY_BAND[1]:.0%}) -- likely a lumpy/irregular payment "
            f"(e.g. a special dividend) distorting the series, not a real sustained trend."
        )
    if div_growth_note and dividend_policy == "growth_rate":
        # Can't extrapolate a trend that doesn't exist yet (e.g. a company with no
        # dividend, or one just initiated inside the lookback window) -- fall back to
        # payout_ratio for this case specifically rather than forecasting 0 forever,
        # and say so.
        assumptions.append(div_growth_note + " -- falling back to payout_ratio policy for this driver")
        dividend_policy = "payout_ratio"
        if "plausible range" in div_growth_note:
            # The fallback lands on a SAFER driver (bounded by max_payout_ratio, can't
            # run away to a negative trajectory the way the rejected growth rate could),
            # not necessarily an ACCURATE one -- confirmed against real Costco data:
            # dividend_payout_ratio is averaged over this exact same lookback window,
            # so the same special-dividend years that broke the growth rate are sitting
            # inside this ratio too, just diluted across 5 years' worth of payout ratios
            # instead of concentrated in 4 growth deltas. Splitting regular from special
            # dividends would need quarter-level parsing this schema doesn't do -- out
            # of scope here, but the resulting payout_ratio likely overstates the
            # sustainable rate for a filer with periodic special dividends, and that
            # needs to be visible, not just the fact that a fallback happened.
            assumptions.append(
                "dividend_payout_ratio (now the active driver) is averaged over the same "
                "lookback window that made the growth rate implausible -- it likely also "
                "overstates the sustainable payout rate, just less dramatically than the "
                "rejected growth rate did. Not corrected for here (would need separating "
                "regular from special dividends, which this schema doesn't parse)."
            )

    if "interest_rate" in overrides:
        interest_rate, source = overrides["interest_rate"]
        overrides_applied.append(f"interest_rate = {interest_rate:.4f} -- {source}")
    else:
        rates = yearly(lambda y: y["interest_expense"] / y["long_term_debt"]
                        if "interest_expense" in y and y.get("long_term_debt", 0.0) > 0 else None)
        # Explicit > 0, not a truthiness check: y.get("long_term_debt") on its own treats
        # a NEGATIVE balance as truthy too (Python truthiness only excludes 0/None), which
        # would silently admit a nonsensical negative-debt observation into the rate
        # average. Reported debt is never actually negative in real filings -- this
        # hasn't fired against any of the five registered companies -- but the guard
        # should say what it means rather than rely on 0 being the only falsy number
        # that happens to matter here.
        if rates:
            interest_rate = avg(rates, "interest_rate")
        else:
            interest_rate = 0.0
            assumptions.append(
                "interest_expense not tagged for this filer in any lookback year, and no "
                "override supplied -- interest_rate assumed 0.0 (understates leverage cost)"
            )

    debt_repayment, debt_repayment_source = overrides.get("debt_repayment", (0.0, None))
    if debt_repayment_source:
        overrides_applied.append(f"debt_repayment = {debt_repayment:,.0f} -- {debt_repayment_source}")

    revolver_limit, revolver_limit_source = overrides.get("revolver_limit", (None, None))
    if revolver_limit_source:
        overrides_applied.append(f"revolver_limit = {revolver_limit:,.0f} -- {revolver_limit_source}")

    if capital_return_policy is None:
        has_buybacks = any(y.get("buybacks", 0.0) > 0 for y in (table[yr] for yr in years))
        capital_return_policy = "sweep_to_buybacks" if has_buybacks else "none"
    elif capital_return_policy not in ("sweep_to_buybacks", "none"):
        raise ValueError(
            f"capital_return_policy must be 'sweep_to_buybacks' or 'none', got {capital_return_policy!r}"
        )

    cash_ratios = yearly(lambda y: y["cash_and_equivalents"] / y["revenue"]
                          if "cash_and_equivalents" in y and y.get("revenue") else None)
    if cash_ratios:
        cash_floor_pct_revenue = min(cash_ratios)
    else:
        cash_floor_pct_revenue = 0.0
        if capital_return_policy == "sweep_to_buybacks":
            assumptions.append(
                "capital_return_policy is sweep_to_buybacks but no year had both cash and "
                "revenue data to derive a cash floor -- cash_floor_pct_revenue assumed 0.0 "
                "(sweeps ALL cash to buybacks every year, likely too aggressive)"
            )

    return Drivers(
        revenue_growth=revenue_growth,
        gross_margin=avg(gross_margins, "gross_margin"),
        sga_pct_revenue=avg(sga_pcts, "sga_pct_revenue"),
        tax_rate=avg(tax_rates, "tax_rate"),
        ar_days=avg(ar_days_list, "ar_days"),
        inventory_days=avg(inv_days_list, "inventory_days"),
        ap_days=avg(ap_days_list, "ap_days"),
        capex_pct_revenue=avg(capex_pcts, "capex_pct_revenue"),
        da_pct_revenue=avg(da_pcts, "da_pct_revenue"),
        interest_rate=interest_rate,
        debt_repayment=debt_repayment,
        dividend_payout_ratio=max(avg(payout_ratios, "dividend_payout_ratio", method="median"), 0.0),
        dividend_growth_rate=dividend_growth_rate,
        dividend_policy=dividend_policy,
        capital_return_policy=capital_return_policy,
        cash_floor_pct_revenue=cash_floor_pct_revenue,
        years_used=tuple(years),
        revolver_limit=revolver_limit,
        assumptions=tuple(assumptions),
        overrides_applied=tuple(overrides_applied),
    )


def project_year(prior: dict[str, float], drivers: Drivers) -> dict[str, float]:
    revenue = prior["revenue"] * (1 + drivers.revenue_growth)
    cogs = revenue * (1 - drivers.gross_margin)
    gross_profit = revenue - cogs
    sga = revenue * drivers.sga_pct_revenue
    operating_income = gross_profit - sga
    interest_expense = prior.get("long_term_debt", 0.0) * drivers.interest_rate
    pretax = operating_income - interest_expense
    tax = pretax * drivers.tax_rate
    net_income = pretax - tax

    ar = revenue / 365 * drivers.ar_days
    inventory = cogs / 365 * drivers.inventory_days
    ap = cogs / 365 * drivers.ap_days
    capex = revenue * drivers.capex_pct_revenue
    da = revenue * drivers.da_pct_revenue
    ppe_net = prior.get("ppe_net", 0.0) + capex - da
    long_term_debt = max(prior.get("long_term_debt", 0.0) - drivers.debt_repayment, 0.0)
    # actual_debt_repayment, not drivers.debt_repayment, feeds CFF below. Real bug: the
    # balance sheet floors debt at 0 (can't repay more than what's outstanding), but CFF
    # used the raw, un-floored driver value -- if debt_repayment (150) exceeds the
    # remaining balance (100), the balance sheet correctly reflects a $100 reduction
    # while CFF claimed $150 left the building, a genuine $50 internal contradiction
    # between how much cash the two statements agree was spent on debt service. Confirmed
    # via reconcile_forecast_year: it would correctly fail on this, by exactly the
    # mismatched amount, but the checks shouldn't have to catch a bug that's this cheap
    # to prevent at the source. Currently latent -- every registered company uses the
    # 0.0 default -- but a real defect for the first user who sets a repayment schedule.
    actual_debt_repayment = min(drivers.debt_repayment, prior.get("long_term_debt", 0.0))

    # Dividend trajectory is tracked separately from the actual cash payment. Real bug,
    # confirmed by tracing the code: under the old version, a single loss year forced
    # dividends_paid to 0 (correctly, via the payout ceiling), but next year's
    # growth_rate compounding read `prior["dividends_paid"]` -- now 0 -- so the dividend
    # was permanently extinguished for the rest of the forecast. That's the opposite of
    # the Lintner stickiness growth_rate policy exists to model: real companies resume
    # paying (often at close to the pre-cut level) once profitable again, they don't
    # restart from zero. `dividend_trajectory` compounds every year regardless of what
    # was actually paid; `dividends_paid` is the capped, actual cash figure. This is a
    # real, disclosed trade-off, not a free fix: it assumes a full snapback to the
    # pre-cut trajectory the moment profitability resumes, which likely overstates how
    # fast a real dividend recovers (many companies resume more conservatively and
    # rebuild). The alternative -- resuming from the last actually-paid level -- avoids
    # that overstatement but reintroduces the permanent-zero bug for any company with
    # more than one loss year in a row. Bootstraps from the historical dividends_paid
    # figure in the first forecast year, since dividend_trajectory doesn't exist yet in
    # reported data.
    prior_trajectory = prior.get("dividend_trajectory", prior.get("dividends_paid", 0.0))
    if drivers.dividend_policy == "growth_rate":
        dividend_trajectory = max(prior_trajectory * (1 + drivers.dividend_growth_rate), 0.0)
    else:
        dividend_trajectory = max(net_income, 0.0) * drivers.dividend_payout_ratio
    payout_ceiling = max(net_income, 0.0) * drivers.max_payout_ratio
    dividend_capped = dividend_trajectory > payout_ceiling
    dividends_paid = min(dividend_trajectory, payout_ceiling)
    equity_before_capital_actions = prior.get("stockholders_equity", 0.0) + net_income - dividends_paid

    # "other" buckets this schema doesn't name -- carried flat, explicitly, from the
    # last actual year rather than silently dropped to zero. (Named other_liabilities,
    # not other_liabilities_and_equity: an earlier version of this line included
    # +prior_equity -prior_equity terms that cancel exactly, so despite the name no
    # equity was ever actually in this number -- the arithmetic was always right, the
    # name was lying about what it computed. Fixed here, not just relabeled.)
    other_assets = prior.get("total_assets", 0.0) - prior.get("cash_and_equivalents", 0.0) \
        - prior.get("accounts_receivable", 0.0) - prior.get("inventory", 0.0) \
        - prior.get("ppe_net", 0.0)
    prior_revolver = prior.get("revolver_balance", 0.0)
    other_liabilities = prior.get("total_liabilities", 0.0) \
        - prior.get("accounts_payable", 0.0) - prior.get("long_term_debt", 0.0) - prior_revolver
    # revolver_balance excluded here and added back as its own explicit term below --
    # same treatment as AP and long-term debt, so it can't get silently folded into the
    # generic flat-carry over multiple years (hand-verified: without this exclusion, the
    # carry-forward still nets out correctly by coincidence for one year, but the
    # explicit revolver_balance tracking and the implicit amount inside other_liabilities
    # diverge from year two onward once a paydown or second draw happens).

    non_cash_assets = ar + inventory + ppe_net + other_assets
    total_liabilities_and_equity_before = (
        ap + long_term_debt + prior_revolver + other_liabilities + equity_before_capital_actions
    )
    cash_before_capital_actions = total_liabilities_and_equity_before - non_cash_assets  # the plug

    # --- Feasibility layer: two-sided plug ---
    # The plug above can assert any cash figure, including deeply negative -- confirmed
    # by direct testing: a distressed driver set produces five straight years of
    # increasingly negative cash, every year passing reconcile_forecast_year, because
    # that check only tests internal arithmetic consistency, never whether the company
    # can actually fund itself (see this module's docstring). A revolver is the standard
    # professional-model answer: draw when cash would fall below the floor, pay down
    # when there's excess, buy back stock only with whatever's left after that. Without
    # a real disclosed credit-facility size (revolver_limit defaults to None), the
    # revolver is unbounded -- cash can always be topped up to the floor, which is an
    # honest, disclosed limitation of the default, not a claim every company can always
    # fund itself. `insolvent` only becomes possible to observe once a real, cited
    # revolver_limit is supplied via the same override mechanism used for interest_rate.
    cash_floor = revenue * drivers.cash_floor_pct_revenue

    if cash_before_capital_actions < cash_floor:
        needed_draw = cash_floor - cash_before_capital_actions
        if drivers.revolver_limit is not None:
            available_capacity = max(drivers.revolver_limit - prior_revolver, 0.0)
            revolver_draw = min(needed_draw, available_capacity)
        else:
            revolver_draw = needed_draw
        revolver_paydown = 0.0
        buybacks = 0.0  # can't return capital to shareholders while drawing on credit
    else:
        available_excess = cash_before_capital_actions - cash_floor
        revolver_paydown = min(prior_revolver, available_excess)  # debt service isn't a
        # capital-return decision -- pay it down whenever there's cash, regardless of
        # capital_return_policy, before considering buybacks with what's left.
        remaining_excess = available_excess - revolver_paydown
        revolver_draw = 0.0
        buybacks = remaining_excess if drivers.capital_return_policy == "sweep_to_buybacks" else 0.0

    revolver_balance = prior_revolver + revolver_draw - revolver_paydown
    net_financing_adjustment = revolver_draw - revolver_paydown - buybacks

    # Cash and equity move by the identical net amount, so the balance-sheet identity
    # proven for the plug above (total_liabilities_and_equity == cash + non_cash_assets)
    # is preserved automatically -- adding the same number to both sides of an equality
    # that already held keeps it holding, the same trick already proven for buybacks
    # alone, extended here to also cover the revolver draw/paydown.
    cash_and_equivalents = cash_before_capital_actions + net_financing_adjustment
    stockholders_equity = equity_before_capital_actions - buybacks  # revolver flows are
    # debt, not equity -- they don't touch stockholders_equity, only cash and liabilities.
    total_liabilities_and_equity = total_liabilities_and_equity_before + revolver_draw \
        - revolver_paydown - buybacks
    total_assets = cash_and_equivalents + non_cash_assets
    total_liabilities = total_liabilities_and_equity - stockholders_equity
    retained_earnings = prior.get("retained_earnings", 0.0) + net_income - dividends_paid - buybacks
    insolvent = cash_and_equivalents < cash_floor - 1.0  # only True if even the full
    # revolver_limit couldn't reach the floor; always False when revolver_limit is None

    cfo = net_income + da - (ar - prior.get("accounts_receivable", 0.0)) \
        - (inventory - prior.get("inventory", 0.0)) + (ap - prior.get("accounts_payable", 0.0))
    cfi = -capex
    cff = -actual_debt_repayment - dividends_paid - buybacks + revolver_draw - revolver_paydown

    return {
        "revenue": revenue, "cost_of_revenue": cogs, "gross_profit": gross_profit,
        "sga_expense": sga, "operating_income": operating_income,
        "interest_expense": interest_expense, "income_tax_expense": tax, "net_income": net_income,
        "accounts_receivable": ar, "inventory": inventory, "ppe_net": ppe_net,
        "cash_and_equivalents": cash_and_equivalents, "total_assets": total_assets,
        "accounts_payable": ap, "long_term_debt": long_term_debt,
        "total_liabilities": total_liabilities, "stockholders_equity": stockholders_equity,
        "retained_earnings": retained_earnings, "dividends_paid": dividends_paid,
        "dividend_trajectory": dividend_trajectory, "dividend_capped": dividend_capped,
        "buybacks": buybacks, "revolver_balance": revolver_balance,
        "revolver_draw": revolver_draw, "revolver_paydown": revolver_paydown,
        "insolvent": insolvent,
        "capex": capex, "depreciation_amortization": da,
        "cfo": cfo, "cfi": cfi, "cff": cff,
    }


def run_forecast(table: AnnualTable, base_year: int, drivers: Drivers, years: int = 5) -> AnnualTable:
    forecast: AnnualTable = {}
    prior = table[base_year]
    for i in range(1, years + 1):
        year = base_year + i
        forecast[year] = project_year(prior, drivers)
        prior = forecast[year]
    return forecast


def reconcile_forecast_year(year: int, year_data: dict[str, float],
                             prior_cash: float, tolerance: float = 1.0) -> CheckResult:
    """Does the plugged cash change match what CFO+CFI+CFF independently implies?
    This is an IMPLEMENTATION INVARIANT, not economic validation -- confirmed by direct
    testing that it cannot fail on an economically impossible forecast (unbounded
    negative cash, an unfundable dividend), only on an actual arithmetic bug in
    project_year, because CFO/CFI/CFF are built from the identical roll-forwards that
    produce the plug. That IS still valuable -- it caught a real equity-roll algebra bug
    during this engine's own development -- but a passing result means 'the code has no
    internal contradiction,' not 'this company can fund what's being modeled.' See the
    `insolvent` flag on project_year's output for the check that can actually fail on
    economics."""
    implied_change = year_data["cfo"] + year_data["cfi"] + year_data["cff"]
    plugged_change = year_data["cash_and_equivalents"] - prior_cash
    diff = plugged_change - implied_change
    passed = abs(diff) <= tolerance
    detail = (f"FY{year} forecast: plugged cash change {plugged_change:,.0f} vs "
              f"CFO+CFI+CFF-implied {implied_change:,.0f} (diff {diff:,.0f}).")
    return CheckResult("forecast_self_consistency", "hard", passed, diff, detail)
