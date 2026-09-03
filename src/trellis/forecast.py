"""
Forecast engine.

Method: every balance-sheet line except cash is driven (revenue growth, margins,
day-based working capital, capex/D&A, a debt schedule). Cash is then the plug:

    Cash = (Total Liabilities + Total Equity) - Non-cash Assets

This makes the balance sheet balance by construction -- it cannot fail the hard
check from statements.py, because it's defined to satisfy it. That's a feature,
not a bypass, IF a second thing also holds: the plugged cash change should equal
what the cash flow statement independently implies (CFO + CFI + CFF). If drivers
are internally consistent, both routes to "how much did cash move" agree. If they
don't -- e.g. debt was cut on the balance sheet but the repayment wasn't reflected
in financing cash flow -- the two cash figures diverge, and reconcile_forecast_year
below reports that as a soft-check failure rather than silently keeping the plug.
"other assets" and "other liabilities" (whatever this schema didn't capture as a
named line item) are carried flat from the last historical year -- an explicit,
stated assumption, not a hidden one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .statements import AnnualTable, CheckResult


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
    dividend_payout_ratio: float  # 0.0 = no dividends
    assumptions: tuple[str, ...] = ()  # non-empty when a driver had to fall back rather
    # than derive from a reported figure -- e.g. Nike doesn't tag interest_expense as a
    # standard us-gaap element in its primary statements (only in a supplementary fixed-
    # charges exhibit), so interest_rate can't be derived and defaults to 0.0. Silently
    # defaulting would misrepresent this as "no interest expense"; tracking it here keeps
    # it visible through to reporting, the same Assumed-vs-Demonstrated split as elsewhere.


def derive_drivers_from_history(table: AnnualTable, base_year: int) -> Drivers:
    """Every driver is a ratio pulled from the most recent actual year -- 'status quo'
    continuation, not an invented number. Nothing here is hardcoded; change the base
    year and every driver recomputes from a different actual."""
    y = table[base_year]
    py = table.get(base_year - 1)
    revenue = y["revenue"]
    cogs = revenue - y["gross_profit"] if "gross_profit" in y else y.get("cost_of_revenue")
    growth = (revenue / py["revenue"] - 1.0) if py and "revenue" in py else 0.0

    assumptions: list[str] = []
    if "interest_expense" in y:
        interest_rate = y["interest_expense"] / max(y.get("long_term_debt", 1.0), 1e-9)
    else:
        interest_rate = 0.0
        assumptions.append("interest_expense not tagged for this filer -- interest_rate assumed 0.0")

    return Drivers(
        revenue_growth=growth,
        gross_margin=y["gross_profit"] / revenue,
        sga_pct_revenue=y["sga_expense"] / revenue,
        tax_rate=y["income_tax_expense"] / max(y["net_income"] + y["income_tax_expense"], 1e-9),
        ar_days=y["accounts_receivable"] / revenue * 365,
        inventory_days=y["inventory"] / cogs * 365,
        ap_days=y["accounts_payable"] / cogs * 365,
        capex_pct_revenue=y["capex"] / revenue,
        da_pct_revenue=y["depreciation_amortization"] / revenue,
        interest_rate=interest_rate,
        debt_repayment=0.0,
        dividend_payout_ratio=(y.get("dividends_paid", 0.0) / y["net_income"]
                                if y["net_income"] else 0.0),
        assumptions=tuple(assumptions),
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
    dividends_paid = max(net_income, 0.0) * drivers.dividend_payout_ratio
    retained_earnings = prior.get("retained_earnings", 0.0) + net_income - dividends_paid

    # "other" buckets this schema doesn't name -- carried flat, explicitly, from the
    # last actual year rather than silently dropped to zero.
    other_assets = prior.get("total_assets", 0.0) - prior.get("cash_and_equivalents", 0.0) \
        - prior.get("accounts_receivable", 0.0) - prior.get("inventory", 0.0) \
        - prior.get("ppe_net", 0.0)
    other_liabilities_and_equity = prior.get("total_liabilities", 0.0) + prior.get("stockholders_equity", 0.0) \
        - prior.get("accounts_payable", 0.0) - prior.get("long_term_debt", 0.0) \
        - prior.get("stockholders_equity", 0.0)

    non_cash_assets = ar + inventory + ppe_net + other_assets
    stockholders_equity = prior.get("stockholders_equity", 0.0) + net_income - dividends_paid
    total_liabilities_and_equity = (
        ap + long_term_debt + other_liabilities_and_equity + stockholders_equity
    )
    cash_and_equivalents = total_liabilities_and_equity - non_cash_assets  # the plug

    total_assets = cash_and_equivalents + non_cash_assets
    total_liabilities = total_liabilities_and_equity - stockholders_equity

    cfo = net_income + da - (ar - prior.get("accounts_receivable", 0.0)) \
        - (inventory - prior.get("inventory", 0.0)) + (ap - prior.get("accounts_payable", 0.0))
    cfi = -capex
    cff = -drivers.debt_repayment - dividends_paid

    return {
        "revenue": revenue, "cost_of_revenue": cogs, "gross_profit": gross_profit,
        "sga_expense": sga, "operating_income": operating_income,
        "interest_expense": interest_expense, "income_tax_expense": tax, "net_income": net_income,
        "accounts_receivable": ar, "inventory": inventory, "ppe_net": ppe_net,
        "cash_and_equivalents": cash_and_equivalents, "total_assets": total_assets,
        "accounts_payable": ap, "long_term_debt": long_term_debt,
        "total_liabilities": total_liabilities, "stockholders_equity": stockholders_equity,
        "retained_earnings": retained_earnings, "dividends_paid": dividends_paid,
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
    Because project_year derives cash as a pure plug, this is the check that actually
    tests whether the driver set is self-consistent -- balance_sheet_balances alone
    would pass trivially here even on garbage drivers."""
    implied_change = year_data["cfo"] + year_data["cfi"] + year_data["cff"]
    plugged_change = year_data["cash_and_equivalents"] - prior_cash
    diff = plugged_change - implied_change
    passed = abs(diff) <= tolerance
    detail = (f"FY{year} forecast: plugged cash change {plugged_change:,.0f} vs "
              f"CFO+CFI+CFF-implied {implied_change:,.0f} (diff {diff:,.0f}).")
    return CheckResult("forecast_self_consistency", "hard", passed, diff, detail)
