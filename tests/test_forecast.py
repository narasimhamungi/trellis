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
