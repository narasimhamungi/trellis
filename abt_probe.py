import os
from trellis.ingest import fetch_all
from trellis.statements import build_annual_table, fill_derived_gaps

REQUIRED = ("revenue", "operating_income", "income_tax_expense", "capex",
            "depreciation_amortization", "accounts_receivable", "inventory",
            "accounts_payable", "long_term_debt", "cash_and_equivalents")

raw = fetch_all(1800)
result = build_annual_table(raw)
table = result.table
fill_derived_gaps(table)

print("ABT years:", sorted(table)[-8:])
print("stub years:", sorted(s.year for s in result.stub_periods))
print()
for year in sorted(table)[-6:]:
    missing = [f for f in REQUIRED if f not in table[year]]
    status = "COMPLETE" if not missing else f"missing {len(missing)}: {missing}"
    print(f"  FY{year}  {status}")
print()
year = max(table)
print(f"--- all fields actually present in FY{year} ---")
print(" ", sorted(table[year]))
