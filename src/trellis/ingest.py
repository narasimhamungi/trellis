"""
SEC EDGAR XBRL ingestion.

Deliberately uses the per-concept endpoint (companyconcept), not the full
company-facts dump. companyfacts for one large filer runs several MB across
hundreds of concepts and decades of restatements; a 3-statement build needs
~20 line items. Pulling only those is not an optimization detail, it's the
point of building the pipeline this way instead of hand-copying a 10-K.

Requires no API key. SEC does require a descriptive User-Agent identifying
the requester (https://www.sec.gov/edgar/sec-api-documentation) -- set
TRELLIS_USER_AGENT before running, e.g. "Trellis/0.1 you@example.com".

NOTE: this module makes real outbound HTTP calls to data.sec.gov and will not
run inside a network-sandboxed environment without egress to that host. It is
written to run correctly wherever normal internet access is available (local
machine, CI, etc.) -- see docs/NETWORK.md.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .schema import SCHEMA, LineItem

BASE_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
REQUEST_DELAY_SECONDS = 0.15  # SEC asks for <=10 req/sec; stay well under
REQUEST_TIMEOUT_SECONDS = 30  # generous -- SEC can be slow under load, and merging
# across a full tag fallback chain (see fetch_line_item) means more requests per line
# item than a single-tag pull, so transient slowness is more likely to be hit, not less


class IngestionError(Exception):
    pass


def make_session() -> requests.Session:
    """A plain requests.Session() has no retry behavior -- a single dropped connection
    or slow handshake fails the whole pull. Retrying idempotent GETs with backoff is
    standard practice for any client hitting a real external API repeatedly, and becomes
    more necessary, not less, once this scales from one company to many."""
    session = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3,
        backoff_factor=1.0,  # 1s, 2s, 4s between attempts
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@dataclass(frozen=True)
class Observation:
    canonical_name: str
    matched_tag: str
    fiscal_year: int
    fiscal_period: str  # "FY", "Q1", ...
    period_end: str  # ISO date
    form: str  # "10-K", "10-Q", ...
    filed: str  # ISO date, the filing date -- used for restatement dedup
    accession_number: str
    value: float
    unit: str
    period_start: str | None = None  # ISO date; None for instant (balance-sheet) facts,
    # which have no duration -- only duration facts (income statement, cash flow) carry
    # this. Exists to detect stub/transition periods from a fiscal-year-end change: a
    # genuine annual period runs ~365 days; a 6-month transition period filed as its own
    # "FY" does not, and averaging it into a lookback window as if it were a normal year
    # would understate every flow-based driver derived from it.


def _headers() -> dict[str, str]:
    ua = os.environ.get("TRELLIS_USER_AGENT")
    if not ua:
        raise IngestionError(
            "Set TRELLIS_USER_AGENT (e.g. 'Trellis/0.1 you@example.com') before "
            "calling SEC EDGAR -- see docs/NETWORK.md."
        )
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def fetch_concept_raw(cik: int, tag: str, session: requests.Session | None = None) -> dict | None:
    """Fetch one us-gaap concept for one filer. Returns None if the filer never used this tag
    (a 404 from SEC -- not an error, just means try the next tag in the fallback chain)."""
    url = BASE_URL.format(cik=cik, tag=tag)
    sess = session or make_session()
    resp = sess.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
    time.sleep(REQUEST_DELAY_SECONDS)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _dedupe_restatements(rows: list[dict]) -> list[dict]:
    """Same (end, unit) period is often re-reported in later filings as a comparative.
    Keep only the most recently filed value per (end, unit) -- SEC's own recommended
    dedup pattern for this API."""
    best: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["end"], row.get("_unit", ""))
        prior = best.get(key)
        if prior is None or row["filed"] > prior["filed"]:
            best[key] = row
    return list(best.values())


def _form_matches(form: str, forms: tuple[str, ...]) -> bool:
    """A requested form like '10-K' must also accept its own amendments ('10-K/A').
    Filtering on exact form equality silently drops restatements -- the amendment
    IS the corrected figure for that same period, not a different filing type."""
    if form in forms:
        return True
    base = form.split("/")[0]
    return base in forms


def fetch_line_item(cik: int, item: LineItem,
                     forms: tuple[str, ...] = ("10-K",),
                     session: requests.Session | None = None) -> list[Observation]:
    """Query every tag in the fallback chain and merge the results -- do NOT stop at
    the first tag with any data.

    Real filers permanently switch which XBRL element they use for the same economic
    line partway through their reporting history; this isn't an edge case. Confirmed
    against live Nike data: InventoryNet has exactly 4 entries (FY2009-2011, when Nike
    used it), InventoryFinishedGoodsNetOfReserves has 32 (the years since). A
    'first tag with any data wins' rule would have permanently truncated the series to
    three years the moment InventoryNet returned anything at all -- which is exactly
    the bug this replaced. Tag order no longer decides which value wins for a given
    period (the merged dedup below does, by filed date); it's now just a list of known
    aliases to check, and matched_tag on each Observation preserves which one actually
    supplied it."""
    sess = session or make_session()
    all_rows: list[dict] = []
    for tag in item.xbrl_tags:
        payload = fetch_concept_raw(cik, tag, sess)
        if payload is None or "units" not in payload:
            continue
        for unit, entries in payload["units"].items():
            for e in entries:
                if not _form_matches(e.get("form", ""), forms):
                    continue
                all_rows.append({**e, "_unit": unit, "_tag": tag})
    if not all_rows:
        return []
    deduped = _dedupe_restatements(all_rows)
    return [
        Observation(
            canonical_name=item.canonical_name,
            matched_tag=r["_tag"],
            fiscal_year=r["fy"],
            fiscal_period=r["fp"],
            period_start=r.get("start"),  # absent for instant facts; SEC includes it
            # directly on duration facts, no extra request needed
            period_end=r["end"],
            form=r["form"],
            filed=r["filed"],
            accession_number=r["accn"],
            value=r["val"],
            unit=r["_unit"],
        )
        for r in sorted(deduped, key=lambda r: r["end"])
    ]


def fetch_company_metadata(cik: int, session: requests.Session | None = None) -> dict:
    """Company name, SIC code, and SIC description from SEC's submissions endpoint --
    used to check whether this schema's design (built around a Revenue -> COGS -> SG&A
    -> Operating Income waterfall) actually fits the company being requested, before
    spending ~20 tag-fetch requests on one that fundamentally won't work. See
    companies.check_industry_support for what SIC ranges are excluded and why."""
    sess = session or make_session()
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    resp = sess.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    sic_raw = data.get("sic")
    sic = int(sic_raw) if sic_raw else None  # SEC returns this as a string ("3021"), not
    # an int -- confirmed the hard way: every single run failed with a TypeError
    # comparing int to str until this was caught. Empty string (some shell companies/
    # funds have no SIC) is falsy, correctly becomes None rather than raising.
    return {"name": data.get("name"), "sic": sic, "sic_description": data.get("sicDescription")}


def fetch_all(cik: int, forms: tuple[str, ...] = ("10-K",)) -> dict[str, list[Observation]]:
    session = make_session()
    results: dict[str, list[Observation]] = {}
    missing: list[str] = []
    for item in SCHEMA:
        obs = fetch_line_item(cik, item, forms, session)
        if not obs:
            missing.append(item.canonical_name)
        results[item.canonical_name] = obs
    if missing:
        # Not fatal here -- some items are legitimately filer-specific (e.g. a company
        # with no long-term debt has no LongTermDebtNoncurrent tag). Statement construction
        # (next module) is what decides whether a gap breaks the balance check.
        results["_missing"] = missing  # type: ignore[assignment]
    return results
