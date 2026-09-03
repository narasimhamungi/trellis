from trellis.forecast import (
    derive_drivers_from_history,
    project_year,
    reconcile_forecast_year,
    run_forecast,
)

# Hand-derived fixture (see build-session notes): a fully self-consistent FY2023 balance
# sheet, plus enough of FY2022 to compute a growth rate. All downstream numbers in these
# tests were worked out by hand against this fixture before the code was run, specifically
# to catch the kind of "looks plausible, isn't actually right" bug that testing against
# your own code's output can't catch.

YEAR_2022 = {"revenue": 1000.0}

YEAR_2023 = {
    "revenue": 1100.0, "cost_of_revenue": 660.0, "gross_profit": 440.0,
    "sga_expense": 220.0, "operating_income": 220.0, "interest_expense": 10.0,
    "income_tax_expense": 42.0, "net_income": 168.0,
    "accounts_receivable": 88.0, "inventory": 99.0, "accounts_payable": 77.0,
    "ppe_net": 511.0, "long_term_debt": 200.0,
    "capex": 66.0, "depreciation_amortization": 55.0,
    "cash_and_equivalents": 200.0, "total_assets": 1000.0,
    "total_liabilities": 400.0, "stockholders_equity": 600.0,
    "retained_earnings": 400.0, "dividends_paid": 40.0,
}

TABLE = {2022: YEAR_2022, 2023: YEAR_2023}


def test_derive_drivers_matches_hand_calculation():
    d = derive_drivers_from_history(TABLE, base_year=2023)
    assert abs(d.revenue_growth - 0.10) < 1e-9
    assert abs(d.gross_margin - 0.40) < 1e-9
    assert abs(d.sga_pct_revenue - 0.20) < 1e-9
    assert abs(d.tax_rate - 0.20) < 1e-9
    assert abs(d.interest_rate - 0.05) < 1e-9
    assert abs(d.dividend_payout_ratio - 40 / 168) < 1e-9
    assert d.debt_repayment == 0.0  # no schedule info -- flat debt is the honest default
    assert d.overrides_applied == ()
    # Only 2 years exist (2022 has revenue only) against the default 3-yr lookback --
    # that shortfall itself must be flagged, not silently absorbed.
    assert len(d.assumptions) == 1
    assert "2 year" in d.assumptions[0]


def test_derive_drivers_flags_assumption_when_interest_expense_untagged():
    """Mirrors the real gap found against live Nike data: interest expense is not a
    standard tagged element in Nike's primary statements at all (confirmed via direct
    diagnostic against SEC, not assumed). interest_rate must default to 0.0 rather than
    crash on a KeyError -- and that default must be visible, not silent."""
    year_without_interest = {k: v for k, v in YEAR_2023.items() if k != "interest_expense"}
    table = {2022: YEAR_2022, 2023: year_without_interest}
    d = derive_drivers_from_history(table, base_year=2023)
    assert d.interest_rate == 0.0
    assert any("interest_expense" in a for a in d.assumptions)


def test_derive_drivers_averages_ratios_across_the_lookback_window_not_just_base_year():
    """Built so every year contributes a different, clean value -- proves the averaging
    is real, not an accident of only one year having data. Mirrors the two real Nike
    distortions directly: inventory_days pinned to one anomalous year (2023-24 glut) and
    dividend_payout_ratio pinned to one depressed-earnings year both get smoothed by
    using 3 years instead of 1."""
    years = {
        2030: {"revenue": 1000.0, "gross_profit": 300.0, "net_income": 300.0,
               "income_tax_expense": 75.0, "dividends_paid": 60.0,   # payout 0.20
               "accounts_receivable": 1000 * 30 / 365, "accounts_payable": 700 * 40 / 365,
               "inventory": 700 * 100 / 365,                          # inv_days 100
               "capex": 50.0, "depreciation_amortization": 40.0, "sga_expense": 200.0},
        2031: {"revenue": 1100.0, "gross_profit": 440.0, "net_income": 400.0,
               "income_tax_expense": 100.0, "dividends_paid": 160.0,  # payout 0.40
               "accounts_receivable": 1100 * 30 / 365, "accounts_payable": 660 * 40 / 365,
               "inventory": 660 * 150 / 365,                          # inv_days 150
               "capex": 55.0, "depreciation_amortization": 44.0, "sga_expense": 220.0},
        2032: {"revenue": 1210.0, "gross_profit": 605.0, "net_income": 500.0,
               "income_tax_expense": 125.0, "dividends_paid": 300.0,  # payout 0.60
               "accounts_receivable": 1210 * 30 / 365, "accounts_payable": 605 * 40 / 365,
               "inventory": 605 * 200 / 365,                          # inv_days 200
               "capex": 60.5, "depreciation_amortization": 48.4, "sga_expense": 242.0},
    }
    d = derive_drivers_from_history(years, base_year=2032, lookback_years=3)

    assert abs(d.gross_margin - 0.40) < 1e-9        # (0.30 + 0.40 + 0.50) / 3
    assert abs(d.inventory_days - 150.0) < 1e-6      # (100 + 150 + 200) / 3, not 200
    assert abs(d.dividend_payout_ratio - 0.40) < 1e-9  # (0.20 + 0.40 + 0.60) / 3, not 0.60
    assert abs(d.revenue_growth - 0.10) < 1e-9        # CAGR: (1210/1000)^(1/2) - 1 = 0.10 exactly
    # Full 3-year window was available for every ratio actually present in the fixture --
    # the one flagged assumption is interest_rate, because this fixture has no debt data
    # at all (not what this test is checking), not a lookback-window shortfall.
    assert len(d.assumptions) == 1
    assert "interest_expense" in d.assumptions[0]


def test_derive_drivers_applies_a_sourced_override_and_records_it_distinctly_from_assumptions():
    """The general mechanism behind Nike's interest_rate fix: a cited, researched value
    used in place of auto-derivation, tracked separately from a silent last-resort
    default so a reader can tell 'someone checked this' from 'nothing was available'."""
    year_without_interest = {k: v for k, v in YEAR_2023.items() if k != "interest_expense"}
    table = {2022: YEAR_2022, 2023: year_without_interest}
    d = derive_drivers_from_history(
        table, base_year=2023,
        overrides={"interest_rate": (0.0314, ("Nike FY2020 10-K debt schedule (R37.htm), "
                                               "weighted avg coupon on tranches outstanding "
                                               "past Sept 2026"))},
    )
    assert abs(d.interest_rate - 0.0314) < 1e-9
    assert len(d.overrides_applied) == 1
    assert "0.0314" in d.overrides_applied[0]
    assert not any("interest_expense" in a for a in d.assumptions)  # overridden, not assumed


def test_project_year_balances_by_construction():
    d = derive_drivers_from_history(TABLE, base_year=2023)
    y2024 = project_year(YEAR_2023, d)
    assert abs(y2024["total_assets"] - (y2024["total_liabilities"] + y2024["stockholders_equity"])) < 1e-6


def test_project_year_matches_hand_worked_figures():
    d = derive_drivers_from_history(TABLE, base_year=2023)
    y2024 = project_year(YEAR_2023, d)
    assert abs(y2024["revenue"] - 1210.0) < 1e-6
    assert abs(y2024["net_income"] - 185.6) < 1e-6
    assert abs(y2024["accounts_receivable"] - 96.8) < 1e-6
    assert abs(y2024["cash_and_equivalents"] - 318.309524) < 1e-4


def test_reconcile_forecast_year_passes_when_drivers_are_self_consistent():
    d = derive_drivers_from_history(TABLE, base_year=2023)
    y2024 = project_year(YEAR_2023, d)
    result = reconcile_forecast_year(2024, y2024, prior_cash=YEAR_2023["cash_and_equivalents"])
    assert result.passed
    assert result.kind == "hard"  # by construction, drivers CAN'T legitimately disagree
    assert abs(result.diff) < 1e-3


def test_reconcile_forecast_year_catches_an_inconsistent_driver_set():
    """Not derived from run_forecast -- a hand-broken year to prove the check actually
    checks something, the same 'one clean + one broken fixture' pattern as statements.py."""
    broken = {
        "cash_and_equivalents": 500.0,  # BS says cash jumped by 300...
        "cfo": 100.0, "cfi": -20.0, "cff": -10.0,  # ...but CF only explains +70
    }
    result = reconcile_forecast_year(2024, broken, prior_cash=200.0)
    assert not result.passed
    assert result.diff == 230.0  # 300 actual - 70 implied


def test_run_forecast_produces_requested_horizon_and_stays_reconciled_throughout():
    d = derive_drivers_from_history(TABLE, base_year=2023)
    forecast = run_forecast(TABLE, base_year=2023, drivers=d, years=5)
    assert sorted(forecast.keys()) == [2024, 2025, 2026, 2027, 2028]

    prior_cash = YEAR_2023["cash_and_equivalents"]
    for year in sorted(forecast):
        result = reconcile_forecast_year(year, forecast[year], prior_cash)
        assert result.passed, result.detail
        prior_cash = forecast[year]["cash_and_equivalents"]

    # Revenue should compound at the derived 10% growth rate every year, not drift.
    assert abs(forecast[2028]["revenue"] - 1100.0 * 1.10 ** 5) < 1e-3
