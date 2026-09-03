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

from .schema import SCHEMA, LineItem

BASE_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
REQUEST_DELAY_SECONDS = 0.15  # SEC asks for <=10 req/sec; stay well under


class IngestionError(Exception):
    pass


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
    sess = session or requests.Session()
    resp = sess.get(url, headers=_headers(), timeout=15)
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
    """Try each tag in the item's fallback chain; use the first that returns data.
    No silent merging across tags -- matched_tag is recorded on every observation."""
    sess = session or requests.Session()
    for tag in item.xbrl_tags:
        payload = fetch_concept_raw(cik, tag, sess)
        if payload is None or "units" not in payload:
            continue
        rows: list[dict] = []
        for unit, entries in payload["units"].items():
            for e in entries:
                if not _form_matches(e.get("form", ""), forms):
                    continue
                e = {**e, "_unit": unit}
                rows.append(e)
        if not rows:
            continue
        deduped = _dedupe_restatements(rows)
        return [
            Observation(
                canonical_name=item.canonical_name,
                matched_tag=tag,
                fiscal_year=r["fy"],
                fiscal_period=r["fp"],
                period_end=r["end"],
                form=r["form"],
                filed=r["filed"],
                accession_number=r["accn"],
                value=r["val"],
                unit=r["_unit"],
            )
            for r in sorted(deduped, key=lambda r: r["end"])
        ]
    return []  # every tag in the fallback chain came up empty -- caller decides if that's fatal


def fetch_all(cik: int, forms: tuple[str, ...] = ("10-K",)) -> dict[str, list[Observation]]:
    session = requests.Session()
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
