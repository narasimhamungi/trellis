from trellis.forecast import (
    Drivers,
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
    assert d.years_used == (2022, 2023)
    # 2022 has no dividends_paid at all -- can't extrapolate a trend that doesn't exist,
    # so growth_rate (the default policy) must fall back to payout_ratio for this driver.
    assert d.dividend_policy == "payout_ratio"
    # Two things to flag: the lookback shortfall (default is now 5 yrs, only 2 exist),
    # and the dividend-growth-rate fallback. Neither should be silent.
    assert len(d.assumptions) == 2
    assert any("5-yr lookback" in a and "2 year" in a for a in d.assumptions)
    assert any("dividend" in a and "payout_ratio" in a for a in d.assumptions)


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
    assert d.years_used == (2030, 2031, 2032)

    assert abs(d.gross_margin - 0.40) < 1e-9        # (0.30 + 0.40 + 0.50) / 3
    assert abs(d.inventory_days - 150.0) < 1e-6      # (100 + 150 + 200) / 3, not 200
    assert abs(d.dividend_payout_ratio - 0.40) < 1e-9  # (0.20 + 0.40 + 0.60) / 3, not 0.60
    assert abs(d.revenue_growth - 0.10) < 1e-9        # CAGR: (1210/1000)^(1/2) - 1 = 0.10 exactly
    # Dividends grew every year in this fixture (60 -> 160 -> 300), so a real CAGR is
    # computable -- growth_rate policy should be used as-is, no fallback.
    assert d.dividend_policy == "growth_rate"
    assert abs(d.dividend_growth_rate - (5 ** 0.5 - 1)) < 1e-9  # (300/60)^(1/2) - 1
    # Full 3-year window was available for every ratio actually present in the fixture --
    # the one flagged assumption is interest_rate, because this fixture has no debt data
    # at all (not what this test is checking), not a lookback-window shortfall.
    assert len(d.assumptions) == 1
    assert "interest_expense" in d.assumptions[0]


def test_derive_drivers_dividend_policy_can_be_explicitly_set_to_payout_ratio():
    """The growth_rate default is a choice, not a hard rule -- an analyst who has a
    reason to expect a company to target a payout ratio (rather than smooth the
    dividend the way Nike does) can select it explicitly."""
    years = {
        2030: {"revenue": 1000.0, "net_income": 300.0, "dividends_paid": 60.0},
        2031: {"revenue": 1100.0, "net_income": 400.0, "dividends_paid": 160.0},
        2032: {"revenue": 1210.0, "net_income": 500.0, "dividends_paid": 300.0},
    }
    d = derive_drivers_from_history(years, base_year=2032, lookback_years=3,
                                     dividend_policy="payout_ratio")
    assert d.dividend_policy == "payout_ratio"
    assert abs(d.dividend_payout_ratio - 0.40) < 1e-9  # still averaged, just now the active driver
    assert not any("payout_ratio" in a and "falling back" in a for a in d.assumptions)


def test_project_year_dividend_policy_changes_the_forecasted_dividend():
    """The whole point of the fix: growth_rate and payout_ratio give genuinely different
    numbers for the same underlying drivers, not just different labels on the same math."""
    base_drivers = {
        "revenue_growth": 0.10, "gross_margin": 0.40, "sga_pct_revenue": 0.20, "tax_rate": 0.20,
        "ar_days": 30.0, "inventory_days": 100.0, "ap_days": 40.0, "capex_pct_revenue": 0.05,
        "da_pct_revenue": 0.04, "interest_rate": 0.0, "debt_repayment": 0.0,
        "dividend_payout_ratio": 0.20, "dividend_growth_rate": 0.50,
    }
    prior = {"revenue": 1000.0, "net_income": 300.0, "dividends_paid": 60.0,
             "long_term_debt": 0.0, "ppe_net": 500.0, "total_assets": 900.0,
             "total_liabilities": 400.0, "stockholders_equity": 500.0,
             "cash_and_equivalents": 160.0, "accounts_receivable": 80.0,
             "inventory": 90.0, "accounts_payable": 70.0, "retained_earnings": 300.0}

    growth_year = project_year(prior, Drivers(**base_drivers, dividend_policy="growth_rate"))
    payout_year = project_year(prior, Drivers(**base_drivers, dividend_policy="payout_ratio"))

    assert abs(growth_year["dividends_paid"] - 60.0 * 1.50) < 1e-6  # prior * (1 + growth)
    assert abs(payout_year["net_income"] - growth_year["net_income"]) < 1e-9  # same NI either way
    assert abs(payout_year["dividends_paid"] - payout_year["net_income"] * 0.20) < 1e-6
    assert growth_year["dividends_paid"] != payout_year["dividends_paid"]  # genuinely different


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


def test_project_year_caps_dividends_at_max_payout_ratio_of_net_income():
    """The real risk found in the Nike forecast: growth_rate compounds a fixed rate with
    no floor tied to earnings. Constructed so the uncapped growth-rate trajectory would
    clearly exceed net income, to prove the ceiling actually binds."""
    drivers = Drivers(
        revenue_growth=0.0, gross_margin=0.40, sga_pct_revenue=0.20, tax_rate=0.20,
        ar_days=30.0, inventory_days=100.0, ap_days=40.0, capex_pct_revenue=0.05,
        da_pct_revenue=0.04, interest_rate=0.0, debt_repayment=0.0,
        dividend_payout_ratio=0.20, dividend_growth_rate=1.00,  # doubling every year
        dividend_policy="growth_rate", max_payout_ratio=1.0,
    )
    prior = {"revenue": 1000.0, "net_income": 176.0, "dividends_paid": 150.0,  # -> uncapped 300
             "long_term_debt": 0.0, "ppe_net": 500.0, "total_assets": 900.0,
             "total_liabilities": 400.0, "stockholders_equity": 500.0,
             "cash_and_equivalents": 160.0, "accounts_receivable": 80.0,
             "inventory": 90.0, "accounts_payable": 70.0, "retained_earnings": 300.0}
    y = project_year(prior, drivers)

    assert y["dividend_capped"] is True
    assert abs(y["dividends_paid"] - y["net_income"] * 1.0) < 1e-6  # capped at 100% of NI
    assert y["dividends_paid"] < 150.0 * 2.0  # meaningfully less than the uncapped 300


def test_project_year_does_not_cap_when_comfortably_under_ceiling():
    drivers = Drivers(
        revenue_growth=0.10, gross_margin=0.40, sga_pct_revenue=0.20, tax_rate=0.20,
        ar_days=30.0, inventory_days=100.0, ap_days=40.0, capex_pct_revenue=0.05,
        da_pct_revenue=0.04, interest_rate=0.0, debt_repayment=0.0,
        dividend_payout_ratio=0.20, dividend_growth_rate=0.05,
        dividend_policy="growth_rate", max_payout_ratio=1.0,
    )
    prior = {"revenue": 1000.0, "net_income": 176.0, "dividends_paid": 60.0,
             "long_term_debt": 0.0, "ppe_net": 500.0, "total_assets": 900.0,
             "total_liabilities": 400.0, "stockholders_equity": 500.0,
             "cash_and_equivalents": 160.0, "accounts_receivable": 80.0,
             "inventory": 90.0, "accounts_payable": 70.0, "retained_earnings": 300.0}
    y = project_year(prior, drivers)

    assert y["dividend_capped"] is False
    assert abs(y["dividends_paid"] - 60.0 * 1.05) < 1e-6  # untouched by the ceiling


def test_project_year_ceiling_applies_to_payout_ratio_policy_too():
    """The identical runaway risk exists if the derived average payout_ratio itself
    exceeds 1.0 -- the ceiling isn't specific to growth_rate policy."""
    drivers = Drivers(
        revenue_growth=0.0, gross_margin=0.40, sga_pct_revenue=0.20, tax_rate=0.20,
        ar_days=30.0, inventory_days=100.0, ap_days=40.0, capex_pct_revenue=0.05,
        da_pct_revenue=0.04, interest_rate=0.0, debt_repayment=0.0,
        dividend_payout_ratio=1.50, dividend_growth_rate=0.0,  # historically paid out 150% of NI
        dividend_policy="payout_ratio", max_payout_ratio=1.0,
    )
    prior = {"revenue": 1000.0, "net_income": 176.0, "dividends_paid": 60.0,
             "long_term_debt": 0.0, "ppe_net": 500.0, "total_assets": 900.0,
             "total_liabilities": 400.0, "stockholders_equity": 500.0,
             "cash_and_equivalents": 160.0, "accounts_receivable": 80.0,
             "inventory": 90.0, "accounts_payable": 70.0, "retained_earnings": 300.0}
    y = project_year(prior, drivers)

    assert y["dividend_capped"] is True
    assert abs(y["dividends_paid"] - y["net_income"]) < 1e-6  # capped at 100%, not 150%


def test_run_forecast_capped_dividend_becomes_the_base_for_next_years_growth():
    """No shadow trajectory: once the ceiling binds, the following year's growth-rate
    compounding starts from the capped figure actually paid, not the uncapped one."""
    drivers = Drivers(
        revenue_growth=0.0, gross_margin=0.40, sga_pct_revenue=0.20, tax_rate=0.20,
        ar_days=30.0, inventory_days=100.0, ap_days=40.0, capex_pct_revenue=0.05,
        da_pct_revenue=0.04, interest_rate=0.0, debt_repayment=0.0,
        dividend_payout_ratio=0.20, dividend_growth_rate=1.00,
        dividend_policy="growth_rate", max_payout_ratio=1.0,
    )
    table = {2026: {"revenue": 1000.0, "net_income": 176.0, "dividends_paid": 150.0,
                    "long_term_debt": 0.0, "ppe_net": 500.0, "total_assets": 900.0,
                    "total_liabilities": 400.0, "stockholders_equity": 500.0,
                    "cash_and_equivalents": 160.0, "accounts_receivable": 80.0,
                    "inventory": 90.0, "accounts_payable": 70.0, "retained_earnings": 300.0}}
    forecast = run_forecast(table, base_year=2026, drivers=drivers, years=2)

    assert forecast[2027]["dividend_capped"] is True
    # FY2028's uncapped growth-rate trajectory starts from FY2027's actual (capped) payout,
    # not from the 150 * 2 * 2 = 600 an uncorrected compounding would imply.
    naive_uncapped_year2 = 150.0 * 2.0 * 2.0
    assert forecast[2028]["dividends_paid"] < naive_uncapped_year2


# --- Capital-return module (floor-and-sweep buybacks) ---
# Fixture deliberately zeroes out AR/inventory/AP/PPE/capex/D&A/debt so the balance-sheet
# arithmetic reduces to something checkable by hand: with those at zero, non_cash_assets
# stays 0 every year, isolating the buyback mechanism from everything else this engine
# already does. Hand-worked before running, same discipline as the original cash-as-plug
# proof.

_SWEEP_DRIVERS = Drivers(
    revenue_growth=0.0, gross_margin=0.50, sga_pct_revenue=0.20, tax_rate=0.20,
    ar_days=0.0, inventory_days=0.0, ap_days=0.0, capex_pct_revenue=0.0, da_pct_revenue=0.0,
    interest_rate=0.0, debt_repayment=0.0, dividend_payout_ratio=0.0, dividend_growth_rate=0.0,
    dividend_policy="payout_ratio",  # 0 either way -- picks the simpler branch
    capital_return_policy="sweep_to_buybacks", cash_floor_pct_revenue=0.30,
)

_SWEEP_PRIOR = {
    "revenue": 1000.0, "long_term_debt": 0.0, "ppe_net": 0.0,
    "total_assets": 500.0, "total_liabilities": 100.0, "stockholders_equity": 400.0,
    "cash_and_equivalents": 500.0, "accounts_receivable": 0.0, "inventory": 0.0,
    "accounts_payable": 0.0, "retained_earnings": 400.0,
}
# Hand-derived expectation: NI = (1000*0.5 - 1000*0.2) * (1-0.2) = 300 * 0.8 = 240.
# equity_before_buybacks = 400 + 240 - 0 = 640. non_cash_assets = 0 (all zeroed).
# other_liab_and_equity (flat carry) = 100 + 400 - 0 - 0 - 400 = 100.
# cash_before_buybacks = (0 + 0 + 100 + 640) - 0 = 740.
# floor = 1000 * 0.30 = 300. buybacks = 740 - 300 = 440. cash_after = 300 exactly.


def test_project_year_sweeps_excess_cash_to_buybacks_and_lands_exactly_at_floor():
    y = project_year(_SWEEP_PRIOR, _SWEEP_DRIVERS)
    assert abs(y["net_income"] - 240.0) < 1e-6
    assert abs(y["buybacks"] - 440.0) < 1e-6
    assert abs(y["cash_and_equivalents"] - 300.0) < 1e-6  # exactly the floor, not below
    assert abs(y["stockholders_equity"] - 200.0) < 1e-6   # 640 - 440
    assert abs(y["retained_earnings"] - 200.0) < 1e-6      # 400 + 240 - 0 - 440
    assert abs(y["total_assets"] - (y["total_liabilities"] + y["stockholders_equity"])) < 1e-6


def test_project_year_sweep_preserves_self_consistency():
    y = project_year(_SWEEP_PRIOR, _SWEEP_DRIVERS)
    result = reconcile_forecast_year(2027, y, prior_cash=_SWEEP_PRIOR["cash_and_equivalents"])
    assert result.passed, result.detail  # buybacks are in CFF; plug and CF-implied change still agree


def test_project_year_no_buyback_when_cash_is_already_below_the_floor():
    high_floor_drivers = Drivers(
        **{**_SWEEP_DRIVERS.__dict__, "cash_floor_pct_revenue": 0.80},  # floor = 800 > 740 available
    )
    y = project_year(_SWEEP_PRIOR, high_floor_drivers)
    assert y["buybacks"] == 0.0
    assert abs(y["cash_and_equivalents"] - 740.0) < 1e-6  # left as computed, not forced up to the floor


def test_project_year_no_buyback_when_policy_is_none():
    no_policy_drivers = Drivers(**{**_SWEEP_DRIVERS.__dict__, "capital_return_policy": "none"})
    y = project_year(_SWEEP_PRIOR, no_policy_drivers)
    assert y["buybacks"] == 0.0
    assert abs(y["cash_and_equivalents"] - 740.0) < 1e-6  # the old cash-as-plug figure, untouched


def test_run_forecast_cash_stabilizes_at_the_floor_instead_of_pooling():
    """The actual fix for the real gap found in the Nike run: with revenue held flat,
    excess cash should get swept back to the floor every single year, not compound."""
    forecast = run_forecast({2026: _SWEEP_PRIOR}, base_year=2026, drivers=_SWEEP_DRIVERS, years=3)
    for year in sorted(forecast):
        assert abs(forecast[year]["cash_and_equivalents"] - 300.0) < 1e-6, (
            f"FY{year} cash pooled instead of staying at the floor: "
            f"{forecast[year]['cash_and_equivalents']}"
        )


def test_derive_drivers_auto_detects_sweep_policy_from_buyback_history():
    years = {
        2024: {"revenue": 1000.0, "cash_and_equivalents": 200.0, "buybacks": 50.0},
        2025: {"revenue": 1100.0, "cash_and_equivalents": 250.0, "buybacks": 60.0},
        2026: {"revenue": 1210.0, "cash_and_equivalents": 240.0, "buybacks": 0.0},  # a quiet year
    }
    d = derive_drivers_from_history(years, base_year=2026, lookback_years=3)
    assert d.capital_return_policy == "sweep_to_buybacks"  # any year with real buybacks is enough


def test_derive_drivers_defaults_to_no_capital_return_without_buyback_history():
    years = {
        2024: {"revenue": 1000.0, "cash_and_equivalents": 200.0},
        2025: {"revenue": 1100.0, "cash_and_equivalents": 250.0},
    }
    d = derive_drivers_from_history(years, base_year=2025, lookback_years=2)
    assert d.capital_return_policy == "none"  # never invents a buyback policy from nothing


def test_derive_drivers_cash_floor_uses_minimum_not_average_ratio():
    years = {
        2024: {"revenue": 1000.0, "cash_and_equivalents": 400.0},  # ratio 0.40
        2025: {"revenue": 1000.0, "cash_and_equivalents": 100.0},  # ratio 0.10 -- the real floor
        2026: {"revenue": 1000.0, "cash_and_equivalents": 300.0},  # ratio 0.30
    }
    d = derive_drivers_from_history(years, base_year=2026, lookback_years=3)
    assert abs(d.cash_floor_pct_revenue - 0.10) < 1e-9  # min, not (0.40+0.10+0.30)/3 = 0.267
