"""
Company registry.

Sourced overrides (see forecast.derive_drivers_from_history) are researched per company
-- they don't belong hardcoded inside a single script. This registry is where that
research lives, keyed by CIK, so any script or future report can look a company up by
CIK and get its known overrides plus a note on the shape of the business, without
re-deriving any of it.

A company with no entry here is still fully forecastable -- overrides is just an empty
dict, and derive_drivers_from_history's own assumption-tracking handles whatever isn't
researched yet. This registry only ever ADDS precision; it's never required.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CompanyProfile:
    cik: int
    name: str
    ticker: str
    fiscal_year_end: str  # e.g. "May 31" -- informational, not used by the pipeline itself
    overrides: dict[str, tuple[float, str]] = field(default_factory=dict)
    notes: str = ""


REGISTRY: dict[int, CompanyProfile] = {
    320187: CompanyProfile(
        cik=320187, name="Nike, Inc.", ticker="NKE", fiscal_year_end="May 31",
        overrides={
            "interest_rate": (0.0314, (
                "Nike FY2020 10-K debt schedule (sec.gov/Archives/edgar/data/320187/"
                "000032018720000047/R37.htm): weighted-average coupon across bond "
                "tranches maturing after Sept 2026 (excludes the 2.25% 2023 and 2.40% "
                "2025 tranches, both since matured). Does not capture any refinancing "
                "or new issuance since FY2020 -- not verified against a more recent "
                "debt footnote.")),
        },
        notes="interest_expense is not a standard tagged us-gaap element in Nike's "
              "primary statements (confirmed via direct diagnostic); inventory is "
              "tagged InventoryFinishedGoodsNetOfReserves since ~FY2019 (contract "
              "manufacturer, finished-goods-only inventory), InventoryNet only for "
              "FY2009-2011. Heavy share buybacks retired through RE, not treasury "
              "stock -- expect the retained_earnings_rollforward soft check to flag "
              "every year historically, and expect capital_return_policy to "
              "auto-detect as sweep_to_buybacks (real repurchase history every year "
              "in the lookback window) -- without it, projected cash pools "
              "unrealistically since Nike returns most FCF via buybacks, not "
              "dividends.",
    ),
    909832: CompanyProfile(
        cik=909832, name="Costco Wholesale Corporation", ticker="COST",
        fiscal_year_end="~Aug/Sept (52/53-week year)",
        notes="Pays periodic special dividends on top of the regular quarterly dividend "
              "-- expect dividends_paid to show large single-year spikes. A CAGR-based "
              "dividend_growth_rate across a window containing a special dividend year "
              "will likely be distorted by it; worth checking whether the growth_rate "
              "policy or payout_ratio policy handles this fixture's shape better before "
              "trusting either uncritically. accounts_receivable is tagged "
              "ReceivablesNetCurrent, not AccountsReceivableNetCurrent -- confirmed via "
              "diagnostic, now in schema.py's fallback chain. Not yet researched for "
              "override candidates.",
    ),
    1018724: CompanyProfile(
        cik=1018724, name="Amazon.com, Inc.", ticker="AMZN",
        fiscal_year_end="December 31",
        overrides={
            "revolver_limit": (20_000_000_000.0, (
                "Amazon 10-Q filed 10/31/2025 (period ended Sept 30, 2025): $15.0B "
                "unsecured revolving credit facility (the 'Credit Agreement', matures "
                "Nov 2028) + $5.0B unsecured 364-day revolving facility (the "
                "'Short-Term Credit Agreement') = $20.0B committed revolving capacity. "
                "Deliberately excludes the $30.0B commercial paper program (increased "
                "from $20.0B in April 2025) -- CP is market-access dependent, not a "
                "committed bank facility, so counting it would overstate guaranteed "
                "liquidity. No borrowings were outstanding under either revolver as of "
                "the filing date; this is undrawn committed capacity, not a current "
                "balance.")),
        },
        notes="Pays no dividend. Exercises the growth_rate-CAGR-undefined -> "
              "payout_ratio-fallback path, and payout_ratio itself should come back "
              "0.0 -- a real test that the pipeline handles 'no dividend' as a valid "
              "state rather than a gap to fill. No consolidated SG&A tag exists at all "
              "-- confirmed via diagnostic: income statement has five separate expense "
              "categories (Cost of sales, Fulfillment, Technology and content, "
              "Marketing, G&A) with no combining element, so sga_expense is now "
              "derived as gross_profit - operating_income (statements.fill_derived_gaps) "
              "rather than tagged directly. capex switched from "
              "PaymentsToAcquirePropertyPlantAndEquipment (through ~2016) to "
              "PaymentsToAcquireProductiveAssets since -- both now in schema.py's "
              "fallback chain. Massive recent capex (~$132B FY2025) reflects real "
              "AI/data-center buildout, not a data error. revolver_limit is now sourced "
              "(see overrides above) -- other potential overrides (a real cost-of-debt "
              "figure, since interest_rate is still auto-derived here) not yet researched.",
    ),
    320193: CompanyProfile(
        cik=320193, name="Apple Inc.", ticker="AAPL", fiscal_year_end="~Sept (Sat nearest Sept 30)",
        notes="Large, sustained buyback program -- expect the same "
              "retained_earnings_rollforward flag pattern seen with Nike, for the same "
              "underlying reason (shares retired through RE). Useful cross-check that "
              "the interpretation generalizes, not just the code. Not yet researched "
              "for override candidates.",
    ),
}


def get_profile(cik: int) -> CompanyProfile:
    return REGISTRY.get(cik) or CompanyProfile(
        cik=cik, name=f"CIK {cik}", ticker="?", fiscal_year_end="unknown",
        notes="Not in the registry -- running with no sourced overrides. Any driver "
              "that can't be auto-derived will show up in Drivers.assumptions, not "
              "silently.",
    )
