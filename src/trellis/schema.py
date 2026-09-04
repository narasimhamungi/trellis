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
    xbrl_tags: tuple[str, ...]  # known aliases for this concept -- ALL are queried and
    # merged (see ingest.fetch_line_item); order has no effect on which value wins for
    # a given period, only on API call sequence
    instant: bool  # True = point-in-time (balance sheet), False = period (flow)


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
    LineItem("operating_income", Statement.INCOME, ("OperatingIncomeLoss",), instant=False),
    LineItem("interest_expense", Statement.INCOME,
             ("InterestExpense", "InterestExpenseDebt", "InterestIncomeExpenseNet"), instant=False),
    LineItem("income_tax_expense", Statement.INCOME, ("IncomeTaxExpenseBenefit",), instant=False),
    LineItem("net_income", Statement.INCOME, ("NetIncomeLoss",), instant=False),

    # --- Balance sheet (instant) ---
    LineItem("cash_and_equivalents", Statement.BALANCE,
             ("CashAndCashEquivalentsAtCarryingValue",), instant=True),
    LineItem("accounts_receivable", Statement.BALANCE,
             ("AccountsReceivableNetCurrent",), instant=True),
    LineItem("inventory", Statement.BALANCE,
             ("InventoryNet", "InventoryFinishedGoodsNetOfReserves"), instant=True),
    LineItem("assets_current", Statement.BALANCE, ("AssetsCurrent",), instant=True),
    LineItem("ppe_net", Statement.BALANCE,
             ("PropertyPlantAndEquipmentNet",), instant=True),
    LineItem("total_assets", Statement.BALANCE, ("Assets",), instant=True),
    LineItem("accounts_payable", Statement.BALANCE, ("AccountsPayableCurrent",), instant=True),
    LineItem("liabilities_current", Statement.BALANCE, ("LiabilitiesCurrent",), instant=True),
    LineItem("long_term_debt", Statement.BALANCE,
             ("LongTermDebtNoncurrent", "LongTermDebt"), instant=True),
    LineItem("total_liabilities", Statement.BALANCE, ("Liabilities",), instant=True),
    LineItem("stockholders_equity", Statement.BALANCE, ("StockholdersEquity",), instant=True),
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
             ("PaymentsToAcquirePropertyPlantAndEquipment",), instant=False),
    LineItem("depreciation_amortization", Statement.CASHFLOW,
             ("DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
              "DepreciationAndAmortization", "Depreciation"),
             instant=False),
    LineItem("dividends_paid", Statement.CASHFLOW,
             ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends"), instant=False),
    LineItem("buybacks", Statement.CASHFLOW,
             ("PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"),
             instant=False),
)

BY_NAME = {item.canonical_name: item for item in SCHEMA}
