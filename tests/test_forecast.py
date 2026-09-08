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
    # This fixture's dividends grow very aggressively (60 -> 160 -> 300, ~127%/yr median)
    # to make payout-ratio averaging easy to verify by hand above -- that growth rate is
    # itself implausible for an ongoing policy and correctly triggers the sanity-band
    # fallback (see test_derive_drivers_dividend_growth_rejects_an_implausible_rate for
    # a test of that mechanism specifically). dividend_payout_ratio is unaffected either
    # way -- it's always computed regardless of which policy ends up active.
    assert d.dividend_policy == "payout_ratio"
    # Three flagged assumptions now: interest_rate (no debt data in this fixture -- not
    # what this test is checking), the dividend sanity-band fallback, and the follow-on
    # disclosure that payout_ratio shares the same lookback window as the rejected rate.
    assert len(d.assumptions) == 3
    assert any("interest_expense" in a for a in d.assumptions)
    assert any("plausible range" in a for a in d.assumptions)
    assert any("same lookback window" in a for a in d.assumptions)


def test_derive_drivers_dividend_growth_rejects_an_implausible_rate():
    """The mechanism behind the real Costco fix: even a well-defined median can be
    implausible for an ONGOING policy. Costco's actual case (two special dividends
    inside one 5-year window) made the median worse than the endpoint CAGR it replaced
    -- -45.2%/yr, not an improvement. No further cleverness on the raw series reliably
    fixes that; recognizing the result itself is implausible and falling back to the
    bounded payout_ratio driver does. Constructed with a steep-but-clean progression to
    isolate the sanity-band check from the lumpy-outlier case already covered above."""
    years = {
        2023: {"revenue": 1000.0, "net_income": 100.0, "dividends_paid": 100.0},
        2024: {"revenue": 1000.0, "net_income": 100.0, "dividends_paid": 60.0},   # -40%/yr
        2025: {"revenue": 1000.0, "net_income": 100.0, "dividends_paid": 36.0},   # -40%/yr
    }
    d = derive_drivers_from_history(years, base_year=2025, lookback_years=3)
    assert d.dividend_policy == "payout_ratio"  # -40%/yr is well outside the sanity band
    assert any("plausible range" in a for a in d.assumptions)
    assert any("same lookback window" in a for a in d.assumptions)  # the follow-on disclosure


def test_derive_drivers_dividend_growth_within_sanity_band_is_used_as_is():
    """Real Nike (+6.99%/yr) and Apple (+1.61%/yr) both sit comfortably inside the band
    -- confirms it's wide enough not to interfere with genuinely ordinary dividend
    growth, only implausible sustained rates."""
    years = {
        2023: {"revenue": 1000.0, "net_income": 100.0, "dividends_paid": 50.0},
        2024: {"revenue": 1000.0, "net_income": 100.0, "dividends_paid": 53.5},  # +7%
        2025: {"revenue": 1000.0, "net_income": 100.0, "dividends_paid": 57.2},  # +6.92%
    }
    d = derive_drivers_from_history(years, base_year=2025, lookback_years=3)
    assert d.dividend_policy == "growth_rate"
    assert not any("plausible range" in a for a in d.assumptions)


def test_derive_drivers_payout_ratio_uses_median_not_mean():
    """The V6/F08 fix applied to dividend_payout_ratio directly, not just
    dividend_growth_rate: both are computed over the identical lookback window, so a
    special-dividend year distorts both drivers, not just one. Two of five years here
    have an elevated ratio (mirroring Costco's real special-dividend years); a mean
    gets dragged to ~101%, a median lands on the actual ordinary-year level."""
    years = {
        2021: {"net_income": 100.0, "dividends_paid": 300.0},  # ratio 3.00 (special)
        2022: {"net_income": 100.0, "dividends_paid": 25.0},   # ratio 0.25
        2023: {"net_income": 100.0, "dividends_paid": 28.0},   # ratio 0.28
        2024: {"net_income": 100.0, "dividends_paid": 125.0},  # ratio 1.25 (special)
        2025: {"net_income": 100.0, "dividends_paid": 29.0},   # ratio 0.29
    }
    d = derive_drivers_from_history(years, base_year=2025, lookback_years=5)
    naive_mean = (3.00 + 0.25 + 0.28 + 1.25 + 0.29) / 5
    assert naive_mean > 1.0  # confirms the fixture reproduces the real distortion
    assert abs(d.dividend_payout_ratio - 0.29) < 1e-9  # median of the 5 sorted ratios


def test_derive_drivers_payout_ratio_excludes_loss_years_not_just_clamps_them():
    """The F09 fix: a maintained dividend against a LOSS produces a negative ratio
    (e.g. 50/-100 = -0.5) that, averaged in with ordinary years, can drag the result
    negative -- confirmed by tracing the code that a sufficiently negative average,
    applied to a later PROFITABLE year via max(net_income,0)*ratio, produces negative
    projected dividends with nothing catching it. Excluding the loss year entirely
    (rather than clamping its ratio to 0) is the correct fix: a payout ratio computed
    against negative earnings isn't a diluted signal, it's not a meaningful observation
    at all."""
    years = {
        2023: {"net_income": -100.0, "dividends_paid": 50.0},  # excluded: net_income <= 0
        2024: {"net_income": 100.0, "dividends_paid": 25.0},   # ratio 0.25
        2025: {"net_income": 100.0, "dividends_paid": 27.0},   # ratio 0.27
    }
    d = derive_drivers_from_history(years, base_year=2025, lookback_years=3)
    assert abs(d.dividend_payout_ratio - 0.26) < 1e-9  # median of [0.25, 0.27], loss year excluded
    assert d.dividend_payout_ratio > 0  # nowhere near the ~0.007 a contaminated mean would give


def test_derive_drivers_payout_ratio_defensive_clamp_never_negative():
    """Belt-and-suspenders: even in a contrived case where the median of the surviving
    (positive-net-income) ratios is itself negative -- possible if dividends_paid is
    negative in the raw data, an edge case the exclusion above doesn't catch -- the
    final driver must never be negative."""
    years = {
        2023: {"net_income": 100.0, "dividends_paid": -10.0},  # ratio -0.10
        2024: {"net_income": 100.0, "dividends_paid": -20.0},  # ratio -0.20
        2025: {"net_income": 100.0, "dividends_paid": 50.0},   # ratio 0.50
    }
    d = derive_drivers_from_history(years, base_year=2025, lookback_years=3)
    # Median of [-0.20, -0.10, 0.50] is -0.10 -- the clamp must catch this.
    assert d.dividend_payout_ratio == 0.0


def test_derive_drivers_dividend_growth_uses_median_not_endpoint_cagr():
    """Mirrors the real Costco bug: a lumpy special dividend landing at one END of the
    lookback window makes an endpoint CAGR wildly misleading, even though the
    underlying trend in every OTHER year is a steady, ordinary increase. Constructed so
    the endpoint CAGR would show a sharp decline while the actual multi-year trend is
    mildly positive -- the same shape as Costco's real -21.5% CAGR sitting on top of a
    dividend that has actually grown steadily for years."""
    years = {
        2021: {"revenue": 1000.0, "dividends_paid": 300.0},  # a special dividend year
        2022: {"revenue": 1000.0, "dividends_paid": 110.0},  # back to the regular dividend
        2023: {"revenue": 1000.0, "dividends_paid": 115.0},
        2024: {"revenue": 1000.0, "dividends_paid": 120.0},
        2025: {"revenue": 1000.0, "dividends_paid": 126.0},
    }
    d = derive_drivers_from_history(years, base_year=2025, lookback_years=5)

    endpoint_cagr_would_have_been = (126.0 / 300.0) ** (1 / 4) - 1
    assert endpoint_cagr_would_have_been < -0.15  # confirms the fixture reproduces the real distortion

    # Median of the 4 YoY rates (-63.3%, +4.55%, +4.35%, +5.0%) is the average of the
    # middle two ordinary years: (1/22 + 1/23) / 2.
    assert abs(d.dividend_growth_rate - (1 / 22 + 1 / 23) / 2) < 1e-9
    assert d.dividend_growth_rate > 0.03  # correctly reads as a mild, real increase...
    assert d.dividend_growth_rate < 0.06  # ...not the ~-20% an endpoint CAGR would have shown


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


def test_derive_drivers_applies_a_revolver_limit_override():
    """Real gap found and fixed before it shipped: revolver_limit was defined on Drivers
    and used by project_year, but derive_drivers_from_history never extracted it from
    overrides or passed it through -- an Amazon revolver_limit override would have been
    silently ignored, defaulting to None (unbounded) regardless of what was supplied."""
    d = derive_drivers_from_history(
        TABLE, base_year=2023,
        overrides={"revolver_limit": (20_000_000_000.0, (
            "Amazon 10-Q Sept 2025: $15.0B Credit Agreement + "
            "$5.0B Short-Term Credit Agreement, committed "
            "revolving facilities only (excludes the $30.0B "
            "commercial paper program, which is market-access "
            "dependent, not a committed bank facility)"))},
    )
    assert d.revolver_limit == 20_000_000_000.0
    assert any("revolver_limit" in o for o in d.overrides_applied)


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


def test_dividend_trajectory_recovers_after_a_loss_year_instead_of_staying_zero_forever():
    """The real F03 fix: a single loss year must not permanently extinguish the
    dividend under growth_rate policy. Confirmed as a real bug by tracing the old code:
    the payout ceiling correctly zeroed dividends_paid IN the loss year, but the
    following year's growth compounding read prior['dividends_paid'] -- now 0 -- so
    0*(1+g) stayed 0 forever, the opposite of the Lintner stickiness the policy exists
    to model. Uses two different hand-picked driver sets across two direct project_year
    calls (not run_forecast) specifically so the loss and the recovery are each
    independently verifiable, rather than relying on one driver set to produce both."""
    year0 = {"revenue": 1000.0, "dividends_paid": 100.0, "long_term_debt": 0.0,
             "ppe_net": 500.0, "total_assets": 900.0, "total_liabilities": 400.0,
             "stockholders_equity": 500.0, "cash_and_equivalents": 200.0,
             "accounts_receivable": 80.0, "inventory": 90.0, "accounts_payable": 70.0,
             "retained_earnings": 300.0}
    common = {"revenue_growth": 0.0, "ar_days": 0.0, "inventory_days": 0.0, "ap_days": 0.0,
              "capex_pct_revenue": 0.0, "da_pct_revenue": 0.0, "interest_rate": 0.0,
              "debt_repayment": 0.0, "dividend_payout_ratio": 0.0, "dividend_growth_rate": 0.05,
              "dividend_policy": "growth_rate", "max_payout_ratio": 1.0,
              "capital_return_policy": "none", "cash_floor_pct_revenue": 0.0}
    loss_drivers = Drivers(**common, gross_margin=0.10, sga_pct_revenue=0.50, tax_rate=0.20)
    # gross_profit=100, sga=500, operating_income=-400, tax=-400*0.20=-80, NI=-320

    year1 = project_year(year0, loss_drivers)
    assert year1["net_income"] < 0
    assert year1["dividends_paid"] == 0.0        # correctly zeroed by the payout ceiling
    assert year1["dividend_capped"] is True
    assert abs(year1["dividend_trajectory"] - 105.0) < 1e-6  # 100 * 1.05 -- kept alive uncapped

    normal_drivers = Drivers(**common, gross_margin=0.40, sga_pct_revenue=0.20, tax_rate=0.20)
    # gross_profit=400, sga=200, operating_income=200, tax=40, NI=160
    year2 = project_year(year1, normal_drivers)
    assert year2["net_income"] > 0
    assert abs(year2["dividend_trajectory"] - 110.25) < 1e-6  # 105 * 1.05, not 0 * 1.05
    assert abs(year2["dividends_paid"] - 110.25) < 1e-6       # resumed, not stuck at 0
    assert year2["dividend_capped"] is False                  # 110.25 < ceiling of 160


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


def test_project_year_draws_on_an_unbounded_revolver_to_reach_the_floor():
    """The real V2/F02 fix: without this, cash below the floor (or negative) was a
    valid, unflagged output -- confirmed by direct testing that a distressed driver set
    could produce five straight years of increasingly negative cash, every year still
    passing reconcile_forecast_year (that check only tests arithmetic consistency, never
    economic feasibility). Default revolver_limit=None means unbounded: cash always gets
    topped up to the floor, an honest disclosed limitation (no company can ACTUALLY
    borrow without limit), not a claim of universal solvency -- see the next test for
    the bounded case."""
    high_floor_drivers = Drivers(
        **{**_SWEEP_DRIVERS.__dict__, "cash_floor_pct_revenue": 0.80},  # floor = 800 > 740 available
    )
    y = project_year(_SWEEP_PRIOR, high_floor_drivers)
    assert y["buybacks"] == 0.0  # can't return capital while drawing on credit
    assert abs(y["revolver_draw"] - 60.0) < 1e-6           # 800 - 740
    assert abs(y["revolver_balance"] - 60.0) < 1e-6
    assert abs(y["cash_and_equivalents"] - 800.0) < 1e-6   # topped up to the floor exactly
    assert y["insolvent"] is False                          # fully funded, just via debt
    assert abs(y["total_assets"] - (y["total_liabilities"] + y["stockholders_equity"])) < 1e-6


def test_project_year_revolver_limit_caps_the_draw_and_flags_insolvent():
    """Once a real, cited revolver_limit is supplied, a shortfall the facility can't
    cover becomes genuinely observable -- the check the report specifically asked for.
    Same fixture as above, but capacity (30) is less than the shortfall (60)."""
    limited_drivers = Drivers(
        **{**_SWEEP_DRIVERS.__dict__, "cash_floor_pct_revenue": 0.80, "revolver_limit": 30.0},
    )
    y = project_year(_SWEEP_PRIOR, limited_drivers)
    assert abs(y["revolver_draw"] - 30.0) < 1e-6           # capped at the limit, not the full 60 needed
    assert abs(y["cash_and_equivalents"] - 770.0) < 1e-6   # 740 + 30 -- short of the 800 floor
    assert y["insolvent"] is True
    # Still balances -- insolvency is an economic signal, not an arithmetic failure;
    # the balance sheet and self-consistency checks must still both pass even here.
    assert abs(y["total_assets"] - (y["total_liabilities"] + y["stockholders_equity"])) < 1e-6
    result = reconcile_forecast_year(2027, y, prior_cash=_SWEEP_PRIOR["cash_and_equivalents"])
    assert result.passed, result.detail


def test_project_year_revolver_paydown_happens_before_buybacks():
    """The 'two-sided' behavior: debt service isn't a capital-return decision, so an
    existing revolver balance gets paid down first, and only what's left after that
    gets swept to buybacks."""
    prior_with_revolver = {**_SWEEP_PRIOR, "total_liabilities": 100.0, "revolver_balance": 100.0}
    # total_liabilities=100 here IS entirely the revolver (other_liabilities computes to
    # 100 - 0(AP) - 0(LTdebt) - 100(revolver) = 0) -- an internally consistent fixture,
    # not an arbitrary pairing of numbers.
    y = project_year(prior_with_revolver, _SWEEP_DRIVERS)  # default floor 0.30 -> 300
    assert abs(y["revolver_paydown"] - 100.0) < 1e-6   # fully paid off
    assert y["revolver_balance"] == 0.0
    assert abs(y["buybacks"] - 340.0) < 1e-6            # 740 - 300(floor) - 100(paydown)
    assert abs(y["cash_and_equivalents"] - 300.0) < 1e-6  # lands exactly at the floor
    assert abs(y["total_assets"] - (y["total_liabilities"] + y["stockholders_equity"])) < 1e-6


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


def test_derive_drivers_flags_assumption_instead_of_silently_defaulting_when_a_ratio_has_no_data():
    """The real bug found against live Costco data: gross_margin averaged to a silent
    0.0 with no warning at all (empty list -> `avg(...) or 0.0`, indistinguishable from
    a genuinely-computed 0%), because the ratio checked for a literal 'gross_profit' key
    that Costco doesn't tag. The forecast built on it printed five years of large losses
    and still said 'reconciled cleanly' -- self-consistency was never the failure, an
    unflagged bad input was. Constructed here with a year that has NEITHER gross_profit
    NOR cost_of_revenue, so the gap is genuine, not just this one company's tagging."""
    years = {2026: {"revenue": 1000.0, "sga_expense": 100.0, "net_income": 50.0,
                     "income_tax_expense": 10.0, "accounts_receivable": 20.0,
                     "inventory": 30.0, "accounts_payable": 15.0, "capex": 5.0,
                     "depreciation_amortization": 3.0}}  # no gross_profit, no cost_of_revenue
    d = derive_drivers_from_history(years, base_year=2026, lookback_years=1)
    assert d.gross_margin == 0.0  # still 0.0 -- but now it must be flagged, not silent
    assert any("gross_margin" in a and "no data available" in a for a in d.assumptions)


def test_derive_drivers_gross_margin_falls_back_to_cost_of_revenue_when_gross_profit_absent():
    """Belt-and-suspenders: derive_drivers_from_history can be called directly on a table
    that never went through fill_derived_gaps, so this fallback needs to work here too,
    not just rely on the pipeline having already derived gross_profit upstream."""
    years = {2026: {"revenue": 1000.0, "cost_of_revenue": 600.0}}  # no gross_profit key at all
    d = derive_drivers_from_history(years, base_year=2026, lookback_years=1)
    assert abs(d.gross_margin - 0.40) < 1e-9  # (1000-600)/1000, not silently 0.0
    assert not any("gross_margin" in a for a in d.assumptions)


def test_project_year_debt_repayment_exceeding_balance_stays_reconciled():
    """Real bug caught via external review, confirmed by direct code inspection before
    trusting the claim: the balance sheet correctly floors long_term_debt at 0 when
    debt_repayment exceeds what's outstanding, but CFF used the raw, un-floored driver
    value -- a repayment of 150 against a balance of 100 meant the balance sheet
    reflected a $100 reduction while CFF claimed $150 left the building, a genuine $50
    internal contradiction that reconcile_forecast_year would correctly have failed on.
    Hand-verified before writing this assertion: with the fix, CFO+CFI+CFF-implied cash
    change is exactly 140 (240 NI - 100 actual repayment), matching the plug exactly."""
    drivers = Drivers(
        revenue_growth=0.0, gross_margin=0.50, sga_pct_revenue=0.20, tax_rate=0.20,
        ar_days=0.0, inventory_days=0.0, ap_days=0.0, capex_pct_revenue=0.0, da_pct_revenue=0.0,
        interest_rate=0.0, debt_repayment=150.0,  # exceeds the 100 outstanding below
        dividend_payout_ratio=0.0, dividend_growth_rate=0.0, dividend_policy="payout_ratio",
        capital_return_policy="none", cash_floor_pct_revenue=0.0,
    )
    prior = {
        "revenue": 1000.0, "long_term_debt": 100.0, "ppe_net": 0.0,
        "total_assets": 600.0, "total_liabilities": 200.0, "stockholders_equity": 400.0,
        "cash_and_equivalents": 400.0, "accounts_receivable": 0.0, "inventory": 0.0,
        "accounts_payable": 0.0, "retained_earnings": 400.0,
    }
    y = project_year(prior, drivers)
    assert y["long_term_debt"] == 0.0  # floored correctly, unaffected by this fix
    assert abs(y["cash_and_equivalents"] - 540.0) < 1e-6  # 400 + 140 implied change
    result = reconcile_forecast_year(2027, y, prior_cash=prior["cash_and_equivalents"])
    assert result.passed, result.detail  # would have failed by exactly 50 before the fix


def test_derive_drivers_interest_rate_excludes_a_negative_debt_year():
    """Real gap caught via external review, confirmed by direct code inspection: the old
    guard used y.get('long_term_debt') as a plain truthiness check, which treats a
    NEGATIVE balance as present the same as a positive one (Python truthiness only
    excludes 0/None). Reported debt is never actually negative in real filings -- this
    doesn't fire against any of the five registered companies -- but the guard should
    say '> 0', not rely on that being incidentally true. Constructed so an included
    negative-debt year would visibly corrupt the average if the guard failed."""
    years = {
        2024: {"revenue": 1000.0, "interest_expense": 20.0, "long_term_debt": 400.0},  # rate 0.05
        2025: {"revenue": 1000.0, "interest_expense": -10.0, "long_term_debt": -50.0},  # excluded
    }
    d = derive_drivers_from_history(years, base_year=2025, lookback_years=2)
    assert abs(d.interest_rate - 0.05) < 1e-9  # only the valid year -- not (0.05+0.20)/2
