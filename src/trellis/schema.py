"""
Canonical financial statement schema for Trellis.

XBRL filers don't use one consistent tag per line item — e.g. revenue has been
tagged as Revenues, SalesRevenueNet, or RevenueFromContractWithCustomerExcludingAssessedTax
depending on filing year and ASC 606 adoption. Each canonical line item below carries an
ordered list of acceptable XBRL tags; the ingestion module tries them in order and records
which one actually matched (no silent merging across tags).

Confirmed against live Nike data: 'inventory' started failing for FY2019 onward under
InventoryNet alone -- same filing where Nike adopted the new revenue-recognition tag,
suggesting a broader tagging-convention change at that transition, not a data gap.
InventoryFinishedGoodsNetOfReserves fits Nike's business model (contract manufacturing,
so only finished-goods inventory on the balance sheet, no raw materials/WIP) and is a
known convention for footwear/apparel filers reporting a single "Inventories" line.
"""

from dataclasses import dataclass
from enum import Enum


class Statement(Enum):
    INCOME = "income_statement"
    BALANCE = "balance_sheet"
    CASHFLOW = "cash_flow"


@dataclass(frozen=True)
class LineItem:
    canonical_name: str
    statement: Statement
    xbrl_tags: tuple[str, ...]  # candidate tags for this concept
    instant: bool  # True = point-in-time (balance sheet), False = period (flow)
    merge_strategy: str = "alias"  # "alias" or "priority" -- see ingest.fetch_line_item
    # "alias" (default): all tags are TRUE SYNONYMS of the same concept, used at
    # different times by the same filer (Nike's inventory tag switching from
    # InventoryNet to InventoryFinishedGoodsNetOfReserves ~FY2019 -- they never overlap
    # for the same period). All tags are merged and, when two DO compete for the same
    # period, the most-recently-filed value wins -- the right rule for resolving a
    # restatement of the SAME concept. Order only affects API call sequence, never which
    # value wins.
    # "priority": tags represent GENUINELY DIFFERENT SCOPES that can both be actively
    # reported for the SAME period -- LongTermDebtNoncurrent (excludes the current
    # portion) vs LongTermDebt (may include it) are not aliases of each other the way
    # the inventory tags are; a filer can tag both for the same year. "Most recently
    # filed wins" is a category error here -- filing recency says nothing about which
    # SCOPE is the intended one. Order IS the preference: xbrl_tags[0] wins for any
    # period it covers at all, regardless of the other tag's filing date; a later tag
    # only fills periods the higher-priority one doesn't cover. Recency still resolves
    # genuine restatements WITHIN a single tag's own history, just never decides BETWEEN
    # competing tags.


# us-gaap taxonomy, in fallback priority order per canonical concept.
SCHEMA: tuple[LineItem, ...] = (
    # --- Income statement ---
    LineItem("revenue", Statement.INCOME,
             ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
             instant=False),
    LineItem("cost_of_revenue", Statement.INCOME,
             ("CostOfGoodsAndServicesSold", "CostOfRevenue"), instant=False),
    LineItem("gross_profit", Statement.INCOME, ("GrossProfit",), instant=False),
    LineItem("sga_expense", Statement.INCOME,
             ("SellingGeneralAndAdministrativeExpense",), instant=False),
    LineItem("rnd_expense", Statement.INCOME,
             ("ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
              "ResearchAndDevelopmentExpense"),
             instant=False, merge_strategy="priority"),
             
    # ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost is preferred over the
    # plain tag. J&J tags BOTH for the same period with the scopes inverted from what the
    # names suggest: the "Excluding" tag carries its real R&D line ($14,665M FY2025) while
    # the plain tag holds only the IPR&D charge ($109M). Surveyed against the five pharma
    # peers: PFE and ABBV tag only the Excluding variant, MRK, BMY and ABT only the plain
    # one, and none tags both. So priority order resolves all six correctly. A filer that
    # tagged both with the conventional scopes (plain = total, Excluding = subset) would
    # break this -- not observed in this set, but not ruled out generally.
    # Known gap: the IPR&D charge is a real operating expense and is dropped here
    # ($1,841M for J&J in FY2024, ~2% of revenue). "priority" chooses between tags, it
    # cannot sum them; capturing both would need a new merge strategy.

    LineItem("operating_income", Statement.INCOME, ("OperatingIncomeLoss",), instant=False),
    LineItem("interest_expense", Statement.INCOME,
             ("InterestExpense", "InterestExpenseDebt", "InterestIncomeExpenseNet"), instant=False),
    LineItem("income_tax_expense", Statement.INCOME, ("IncomeTaxExpenseBenefit",), instant=False),
    LineItem("net_income", Statement.INCOME, ("NetIncomeLoss",), instant=False),

    # --- Balance sheet (instant) ---
    LineItem("cash_and_equivalents", Statement.BALANCE,
             ("CashAndCashEquivalentsAtCarryingValue",), instant=True),
    LineItem("accounts_receivable", Statement.BALANCE,
             ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent"), instant=True),
    LineItem("inventory", Statement.BALANCE,
             ("InventoryNet", "InventoryFinishedGoodsNetOfReserves"), instant=True),
    LineItem("assets_current", Statement.BALANCE, ("AssetsCurrent",), instant=True),
    LineItem("ppe_net", Statement.BALANCE,
             ("PropertyPlantAndEquipmentNet",), instant=True),
    LineItem("total_assets", Statement.BALANCE, ("Assets",), instant=True),
    LineItem("accounts_payable", Statement.BALANCE, ("AccountsPayableCurrent",), instant=True),
    LineItem("liabilities_current", Statement.BALANCE, ("LiabilitiesCurrent",), instant=True),
    LineItem("long_term_debt", Statement.BALANCE,
             ("LongTermDebtNoncurrent", "LongTermDebt"), instant=True, merge_strategy="priority"),
    # LongTermDebtNoncurrent (excludes the current portion) is preferred over LongTermDebt
    # (may include it) whenever a filer reports both for the same period -- confirmed via
    # external review that Nike, Amazon, and J&J all tag both, with different values
    # (Nike FY2026: $5,942M noncurrent-only vs $7,942M under the broader tag). Treating
    # these as simple aliases (the "alias" default) would let filing recency arbitrarily
    # decide between two different economic scopes for a given period -- this is exactly
    # the failure mode "priority" exists to prevent.
    LineItem("total_liabilities", Statement.BALANCE, ("Liabilities",), instant=True),
    LineItem("stockholders_equity", Statement.BALANCE,
             ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
             instant=True),
    LineItem("retained_earnings", Statement.BALANCE,
             ("RetainedEarningsAccumulatedDeficit",), instant=True),

    # --- Cash flow (period) ---
    LineItem("cfo", Statement.CASHFLOW,
             ("NetCashProvidedByUsedInOperatingActivities",), instant=False),
    LineItem("cfi", Statement.CASHFLOW,
             ("NetCashProvidedByUsedInInvestingActivities",), instant=False),
    LineItem("cff", Statement.CASHFLOW,
             ("NetCashProvidedByUsedInFinancingActivities",), instant=False),
    LineItem("capex", Statement.CASHFLOW,
             ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
             instant=False),
    LineItem("depreciation_amortization", Statement.CASHFLOW,
             ("DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
              "DepreciationAndAmortization"),
             instant=False, merge_strategy="priority"),
    # Bare "Depreciation" REMOVED from this chain -- it is PP&E depreciation only, a
    # different scope, not an alias. AbbVie tags no combined DD&A at all, so the old
    # chain fell through to it and reported $762M against a true ~$8,139M, omitting
    # $7,377M of acquired-intangible amortization and inflating its EV/EBITDA to 32x.
    # Split out below and recombined in fill_derived_gaps instead.
    # priority (not alias) because BMY tags both the combined and the bare tag for the
    # same period with different values -- letting filing recency choose between two
    # scopes is the same category error documented on long_term_debt.
    LineItem("depreciation_only", Statement.CASHFLOW,
             ("Depreciation",), instant=False),
    LineItem("amortization_intangibles", Statement.CASHFLOW,
             ("AmortizationOfIntangibleAssets",), instant=False),
    LineItem("dividends_paid", Statement.CASHFLOW,
             ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends",
              "PaymentsOfOrdinaryDividends", "DividendsCommonStockCash"),
             instant=False),
    LineItem("buybacks", Statement.CASHFLOW,
             ("PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"),
             instant=False),
)

BY_NAME = {item.canonical_name: item for item in SCHEMA}
