from trellis.ingest import Observation
from trellis.statements import (
    build_annual_table,
    check_balance_sheet_balances,
    check_cash_flow_ties_to_cash,
    check_retained_earnings_rollforward,
    fill_derived_gaps,
    run_all_checks,
)


def _obs(name, fy, val, fp="FY"):
    return Observation(canonical_name=name, matched_tag="x", fiscal_year=fy, fiscal_period=fp,
                        period_end=f"{fy}-05-31", form="10-K", filed=f"{fy}-07-20",
                        accession_number="acc", value=val, unit="USD")


# --- build_annual_table -----------------------------------------------------

def test_build_annual_table_filters_to_fy_and_keys_by_year():
    observations = {
        "revenue": [_obs("revenue", 2023, 100), _obs("revenue", 2024, 110),
                    _obs("revenue", 2024, 55, fp="Q1")],  # quarterly noise, must be dropped
        "total_assets": [_obs("total_assets", 2023, 500), _obs("total_assets", 2024, 550)],
        "_missing": ["some_tag"],  # sentinel from ingest.fetch_all -- must be skipped, not crash
    }
    table = build_annual_table(observations)
    assert set(table.keys()) == {2023, 2024}
    assert table[2024]["revenue"] == 110  # the quarterly observation didn't leak in
    assert table[2023]["total_assets"] == 500


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
    table = build_annual_table({"revenue": [mislabeled]})
    assert 2017 in table  # keyed by the period it actually describes...
    assert 2019 not in table  # ...not by the filing's own mislabeled fy field
    assert table[2017]["revenue"] == 34_350_000_000


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
