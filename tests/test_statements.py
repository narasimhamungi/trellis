from trellis.ingest import Observation
from trellis.statements import (
    BuildTableResult,
    build_annual_table,
    check_balance_sheet_balances,
    check_cash_flow_ties_to_cash,
    check_retained_earnings_rollforward,
    fill_derived_gaps,
    run_all_checks,
)


def _obs(name, fy, val, fp="FY", period_end=None, period_start=None):
    return Observation(canonical_name=name, matched_tag="x", fiscal_year=fy, fiscal_period=fp,
                        period_end=period_end or f"{fy}-05-31", period_start=period_start,
                        form="10-K", filed=f"{fy}-07-20", accession_number="acc", value=val, unit="USD")


# --- build_annual_table -----------------------------------------------------

def test_build_annual_table_filters_to_fy_and_keys_by_year():
    observations = {
        "revenue": [_obs("revenue", 2023, 100), _obs("revenue", 2024, 110),
                    _obs("revenue", 2024, 55, fp="Q1")],  # quarterly noise, must be dropped
        "total_assets": [_obs("total_assets", 2023, 500), _obs("total_assets", 2024, 550)],
        "_missing": ["some_tag"],  # sentinel from ingest.fetch_all -- must be skipped, not crash
    }
    result = build_annual_table(observations)
    assert isinstance(result, BuildTableResult)
    table = result.table
    assert set(table.keys()) == {2023, 2024}
    assert table[2024]["revenue"] == 110  # the quarterly observation didn't leak in
    assert table[2023]["total_assets"] == 500
    assert result.fye_collisions == ()
    assert result.stub_periods == ()


def test_build_annual_table_keys_by_period_end_not_by_secs_fy_field():
    """Regression test for a real bug found against live Nike data: SEC's fy field on an
    observation reflects which filing reported it, not the period it describes, because
    older comparative-column periods get re-published in later filings. dedup correctly
    keeps the most-recently-filed VALUE for a period, but that filing's own 'fy' label can
    be 1-2 years later than the period actually is. Constructed from the real pattern
    (Nike FY2017 revenue, $34.35B, came back with fiscal_year=2019 attached to
    period_end='2017-05-31') -- not a hypothetical edge case."""
    mislabeled = Observation(
        canonical_name="revenue", matched_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        fiscal_year=2019,  # WRONG -- this is the filing year, not the period's year
        fiscal_period="FY", period_end="2017-05-31", form="10-K", filed="2019-07-23",
        accession_number="0000320187-19-000051", value=34_350_000_000, unit="USD",
    )
    table = build_annual_table({"revenue": [mislabeled]}).table
    assert 2017 in table  # keyed by the period it actually describes...
    assert 2019 not in table  # ...not by the filing's own mislabeled fy field
    assert table[2017]["revenue"] == 34_350_000_000


def test_build_annual_table_drops_a_franken_year_collision_not_mixes_it():
    """The real V4/F04 risk: a fiscal-year-end change can put two genuinely different
    periods in the same calendar-year bucket. Latent, not yet triggered by any of the
    four live companies validated so far (none has changed fiscal year-end) -- fixed
    proactively rather than waiting for it to silently corrupt a fifth company's data.
    Constructed here: 'revenue' reports period_end 2024-12-31 (the new, later FYE) while
    'net_income' still reports the old 2024-06-30 FYE for the same calendar-year key --
    without the fix, year=2024 would mix a revenue figure from one period with a
    net_income figure from a genuinely different one."""
    observations = {
        "revenue": [_obs("revenue", 2023, 1000, period_end="2023-06-30"),
                    _obs("revenue", 2024, 1100, period_end="2024-12-31")],  # new FYE
        "net_income": [_obs("net_income", 2023, 100, period_end="2023-06-30"),
                       _obs("net_income", 2024, 90, period_end="2024-06-30")],  # still old FYE
    }
    result = build_annual_table(observations)
    assert "net_income" not in result.table.get(2024, {})  # dropped, not silently kept
    assert result.table[2024]["revenue"] == 1100            # the authoritative (latest) period
    assert len(result.fye_collisions) == 1
    c = result.fye_collisions[0]
    assert c.year == 2024 and c.canonical_name == "net_income"
    assert c.dropped_period_end == "2024-06-30" and c.kept_period_end == "2024-12-31"


def test_build_annual_table_flags_a_stub_transition_period():
    """A 6-month transition period (fiscal-year-end change from June to December) filed
    as its own 'FY' period -- real, valid data, but averaging it in as a normal year
    would understate every flow-based ratio derived from it by roughly half."""
    observations = {
        "revenue": [_obs("revenue", 2024, 1000, period_end="2024-06-30", period_start="2023-07-01"),
                    _obs("revenue", 2024, 550, period_end="2024-12-31", period_start="2024-07-01")],
        # ^ two DIFFERENT periods both keying to year=2024 -- the 12-31 one wins as
        # authoritative (later), and its ~184-day duration should flag as a stub.
    }
    result = build_annual_table(observations)
    assert len(result.stub_periods) == 1
    stub = result.stub_periods[0]
    assert stub.year == 2024
    assert stub.duration_days < 300  # roughly 184 days, well outside the ~300-400 band


def test_build_annual_table_does_not_flag_a_normal_52_53_week_year_as_a_stub():
    observations = {
        "revenue": [_obs("revenue", 2024, 1000, period_end="2024-12-28", period_start="2023-12-31")],
        # 363 days -- a normal 52-week fiscal year, not a transition stub
    }
    result = build_annual_table(observations)
    assert result.stub_periods == ()


# --- fill_derived_gaps (Assets-Equity identity, not a guess) ----------------

def test_fill_derived_gaps_computes_total_liabilities_from_identity_when_tag_absent():
    """Mirrors the real gap found against live Nike data: 'Liabilities' wasn't tagged
    directly. Assets - Equity is an exact identity, not an approximation, so this should
    be filled rather than left blank -- but flagged as derived, not fetched."""
    table = {2024: {"total_assets": 1_000.0, "stockholders_equity": 400.0}}  # no total_liabilities
    derived = fill_derived_gaps(table)
    assert table[2024]["total_liabilities"] == 600.0
    assert len(derived) == 1
    assert derived[0].canonical_name == "total_liabilities"
    assert derived[0].method == "total_assets - stockholders_equity"


def test_fill_derived_gaps_never_overwrites_a_directly_reported_value():
    table = {2024: {"total_assets": 1_000.0, "stockholders_equity": 400.0, "total_liabilities": 555.0}}
    derived = fill_derived_gaps(table)
    assert table[2024]["total_liabilities"] == 555.0  # untouched -- reported value wins over derived
    assert derived == []


def test_fill_derived_gaps_derives_operating_income_when_tag_absent():
    """Mirrors Nike's actual income-statement format, which has no separately tagged
    operating-income subtotal at all -- not a pull failure, a different presentation."""
    table = {2024: {"gross_profit": 440.0, "sga_expense": 220.0}}
    derived = fill_derived_gaps(table)
    assert table[2024]["operating_income"] == 220.0
    assert [d.canonical_name for d in derived] == ["operating_income"]


def test_fill_derived_gaps_derives_sga_expense_when_no_consolidated_tag_exists():
    """Mirrors the real Amazon gap: no single SG&A tag exists at all (five separate
    expense categories -- Cost of sales, Fulfillment, Technology and content, Marketing,
    G&A -- with no combining element). Gross Profit - Operating Income is exact by
    definition regardless of how many categories a filer splits expenses into."""
    table = {2025: {"gross_profit": 200_000.0, "operating_income": 80_000.0}}
    derived = fill_derived_gaps(table)
    assert table[2025]["sga_expense"] == 120_000.0
    assert [d.canonical_name for d in derived] == ["sga_expense"]


def test_fill_derived_gaps_does_not_derive_sga_or_operating_income_from_each_other():
    """Neither rule may use a value the other just derived in the same pass -- if both
    are genuinely missing, both must stay missing rather than one being silently
    back-derived from the other's fallback."""
    table = {2025: {"gross_profit": 200_000.0}}  # both sga_expense and operating_income absent
    derived = fill_derived_gaps(table)
    assert "sga_expense" not in table[2025]
    assert "operating_income" not in table[2025]
    assert derived == []


def test_fill_derived_gaps_leaves_true_gaps_alone_when_inputs_also_missing():
    table = {2024: {"total_assets": 1_000.0}}  # no equity either -- can't derive liabilities
    derived = fill_derived_gaps(table)
    assert "total_liabilities" not in table[2024]
    assert derived == []


def test_fill_derived_gaps_derives_gross_profit_from_revenue_minus_cost_of_revenue():
    """Mirrors the real gap found against live Amazon and Costco data: neither tags a
    GrossProfit subtotal at all. Matters beyond this one field -- it's a dependency for
    the operating_income and sga_expense derivations, and for gross_margin downstream in
    forecast.py, none of which could resolve at all without this."""
    table = {2025: {"revenue": 1_000.0, "cost_of_revenue": 750.0}}
    derived = fill_derived_gaps(table)
    assert table[2025]["gross_profit"] == 250.0
    assert [d.canonical_name for d in derived] == ["gross_profit"]


def test_fill_derived_gaps_gross_profit_feeds_the_other_derivations_in_the_same_pass():
    """Ordering check: gross_profit must derive before operating_income/sga_expense try
    to use it, in the same call, not just across separate calls."""
    table = {2025: {"revenue": 1_000.0, "cost_of_revenue": 750.0, "operating_income": 100.0}}
    derived = fill_derived_gaps(table)
    names = [d.canonical_name for d in derived]
    assert "gross_profit" in names
    assert table[2025]["gross_profit"] == 250.0
    assert "sga_expense" in names
    assert table[2025]["sga_expense"] == 150.0  # 250 - 100, using the gross_profit just derived


# --- balance_sheet_balances (hard invariant) --------------------------------

CLEAN_YEAR = {
    "total_assets": 1_000_000_000.0,
    "total_liabilities": 600_000_000.0,
    "stockholders_equity": 400_000_000.0,
    "cash_and_equivalents": 200_000_000.0,
    "cfo": 150_000_000.0,
    "cfi": -80_000_000.0,
    "cff": -20_000_000.0,
    "net_income": 90_000_000.0,
    "retained_earnings": 300_000_000.0,
    "dividends_paid": 30_000_000.0,
}

CLEAN_PRIOR = {
    "cash_and_equivalents": 150_000_000.0,  # 200M - 150M = 50M = 150-80-20  ✓ ties
    "retained_earnings": 240_000_000.0,     # 240 + 90 - 30 = 300           ✓ ties
}


def test_balance_sheet_balances_passes_on_consistent_data():
    result = check_balance_sheet_balances(2024, CLEAN_YEAR)
    assert result.passed
    assert result.kind == "hard"
    assert abs(result.diff) < 1.0


def test_balance_sheet_balances_fails_loud_on_broken_data():
    broken = {**CLEAN_YEAR, "total_liabilities": 550_000_000.0}  # off by 50M
    result = check_balance_sheet_balances(2024, broken)
    assert not result.passed
    assert result.kind == "hard"
    assert result.diff == 50_000_000.0
    assert "50,000,000" in result.detail  # the size of the break is visible, not swallowed


def test_balance_sheet_balances_refuses_to_guess_on_missing_data():
    incomplete = {"total_assets": 1_000.0}  # no liabilities/equity at all
    result = check_balance_sheet_balances(2024, incomplete)
    assert not result.passed
    assert result.diff is None  # explicitly "cannot evaluate", not a false pass or a fabricated diff
    assert "missing" in result.detail


# --- cash_flow_ties_to_cash (soft diagnostic) -------------------------------

def test_cash_flow_ties_to_cash_passes_on_consistent_data():
    result = check_cash_flow_ties_to_cash(2024, CLEAN_YEAR, CLEAN_PRIOR)
    assert result.passed
    assert result.kind == "soft"


def test_cash_flow_ties_to_cash_flags_break_without_calling_it_a_bug():
    broken_prior = {**CLEAN_PRIOR, "cash_and_equivalents": 50_000_000.0}  # implausible jump
    result = check_cash_flow_ties_to_cash(2024, CLEAN_YEAR, broken_prior)
    assert not result.passed
    assert result.kind == "soft"
    assert "FX" in result.detail or "restricted" in result.detail  # explains itself, doesn't just say FAIL


def test_cash_flow_ties_to_cash_reports_insufficient_data_without_prior_year():
    result = check_cash_flow_ties_to_cash(2023, CLEAN_YEAR, prior_data=None)
    assert not result.passed
    assert result.diff is None
    assert "insufficient data" in result.detail


# --- retained_earnings_rollforward (soft diagnostic) ------------------------

def test_retained_earnings_rollforward_passes_on_consistent_data():
    result = check_retained_earnings_rollforward(2024, CLEAN_YEAR, CLEAN_PRIOR)
    assert result.passed
    assert result.kind == "soft"


def test_retained_earnings_rollforward_treats_absent_dividends_as_zero_not_missing():
    no_dividend_year = {k: v for k, v in CLEAN_YEAR.items() if k != "dividends_paid"}
    no_dividend_year = {**no_dividend_year, "retained_earnings": 330_000_000.0}  # 240 + 90 - 0 = 330
    result = check_retained_earnings_rollforward(2024, no_dividend_year, CLEAN_PRIOR)
    assert result.passed  # a non-dividend-paying filer shouldn't be penalized for lacking the tag


# --- orchestration -----------------------------------------------------------

def test_run_all_checks_first_year_has_no_prior_so_soft_checks_report_insufficient():
    table = {2023: CLEAN_YEAR, 2024: CLEAN_YEAR}
    results = run_all_checks(table)
    by_year_2023 = [r for r in results if r.detail.startswith("FY2023")]
    hard = next(r for r in by_year_2023 if r.name == "balance_sheet_balances")
    soft = next(r for r in by_year_2023 if r.name == "cash_flow_ties_to_cash")
    assert hard.passed  # hard check doesn't need a prior year
    assert not soft.passed and "insufficient data" in soft.detail  # soft check correctly does
