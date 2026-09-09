"""
Trellis: a hostile-data-grade pipeline for turning SEC XBRL filings into fully-traced
historical financial statements, plus a labeled baseline forecast scenario built on
them.

See the top-level README for the actual argument for this project's existence --
this docstring is deliberately thin. The five submodules, in pipeline order:

    schema.py      canonical line-item definitions and their XBRL tag fallback chains
    ingest.py      SEC EDGAR pulls, tag merging, restatement dedup
    statements.py  historical table construction, structural integrity checks
    forecast.py    driver derivation and the 5-year projection engine
    companies.py   the company registry: sourced overrides and industry gating
"""

__version__ = "0.1.0"
