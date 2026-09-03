"""
Fixtures here are structurally identical to what data.sec.gov/api/xbrl/companyconcept
actually returns (confirmed against a live pull during Trellis's build session) but with
invented values -- they exist to prove the parsing/dedup/fallback logic, not to assert
anything about a real filer's financials. Real-data validation is a separate, later step
that requires network egress this test suite doesn't need.
"""

import os

os.environ["TRELLIS_USER_AGENT"] = "Trellis-Tests/0.1 test@example.com"

from trellis.ingest import _dedupe_restatements, fetch_line_item
from trellis.schema import BY_NAME


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 404:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Maps URL -> canned response, so fetch_line_item's tag-fallback loop is exercised
    exactly as it would be against the real API."""

    def __init__(self, responses: dict[str, _FakeResponse]):
        self._responses = responses
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        return self._responses.get(url, _FakeResponse(404))


REVENUE_FIXTURE = {
    "units": {
        "USD": [
            # FY2023: reported once in the 10-K, then repeated (unchanged) as a
            # comparative in the FY2024 10-K -- later filing should win the dedup,
            # same value here, but the accession number should update.
            {"end": "2023-05-31", "val": 51217000000, "accn": "0000320187-23-000041",
             "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-07-20"},
            {"end": "2023-05-31", "val": 51217000000, "accn": "0000320187-24-000037",
             "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-07-25"},
            # FY2024: genuinely restated between the original 10-K and a later 10-K/A --
            # dedup must keep the later filing's value, not the earlier one.
            {"end": "2024-05-31", "val": 51362000000, "accn": "0000320187-24-000037",
             "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-07-25"},
            {"end": "2024-05-31", "val": 51360000000, "accn": "0000320187-24-000099",
             "fy": 2024, "fp": "FY", "form": "10-K/A", "filed": "2024-09-10"},
        ]
    }
}


def test_dedupe_keeps_most_recently_filed_value_per_period():
    rows = [{**r, "_unit": "USD"} for r in REVENUE_FIXTURE["units"]["USD"]]
    deduped = {(r["end"]): r for r in _dedupe_restatements(rows)}
    assert len(deduped) == 2
    assert deduped["2024-05-31"]["val"] == 51360000000  # the restated figure, not the original
    assert deduped["2024-05-31"]["accn"] == "0000320187-24-000099"


def test_fetch_line_item_uses_the_only_tag_that_has_data():
    item = BY_NAME["revenue"]
    assert item.xbrl_tags[0] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    url_first_tag = (
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320187/"
        "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax.json"
    )
    session = _FakeSession({url_first_tag: _FakeResponse(200, REVENUE_FIXTURE)})
    obs = fetch_line_item(320187, item, forms=("10-K",), session=session)

    assert len(obs) == 2  # FY23 and FY24, deduped
    assert all(o.matched_tag == "RevenueFromContractWithCustomerExcludingAssessedTax" for o in obs)
    assert obs[-1].value == 51360000000
    assert len(session.calls) == len(item.xbrl_tags)  # every tag queried, not just the first


def test_fetch_line_item_uses_data_from_a_later_tag_when_earlier_ones_are_absent():
    item = BY_NAME["revenue"]
    url_first_tag = (
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320187/"
        "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax.json"
    )
    url_second_tag = (
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320187/us-gaap/Revenues.json"
    )
    session = _FakeSession({
        url_first_tag: _FakeResponse(404),
        url_second_tag: _FakeResponse(200, REVENUE_FIXTURE),
    })
    obs = fetch_line_item(320187, item, forms=("10-K",), session=session)

    assert len(obs) == 2
    assert all(o.matched_tag == "Revenues" for o in obs)
    assert len(session.calls) == len(item.xbrl_tags)  # every tag queried regardless


def test_fetch_line_item_merges_across_tags_when_filer_switched_mid_history():
    """Regression test for a real bug found against live Nike data: the old
    'first tag with any data wins' rule permanently truncated inventory to FY2009-2011,
    because InventoryNet had exactly those three years and nothing else -- the second
    fallback tag (which actually covers the years since) was never even queried. Filers
    switching XBRL elements for the same line partway through their history turned out
    to be routine, not an edge case (same pattern hit both inventory and D&A)."""
    item = BY_NAME["inventory"]
    assert item.xbrl_tags == ("InventoryNet", "InventoryFinishedGoodsNetOfReserves")

    old_tag_fixture = {"units": {"USD": [
        {"end": "2009-05-31", "val": 2_357_000_000, "accn": "old-09", "fy": 2009,
         "fp": "FY", "form": "10-K", "filed": "2009-07-20"},
        {"end": "2010-05-31", "val": 2_041_000_000, "accn": "old-10", "fy": 2010,
         "fp": "FY", "form": "10-K", "filed": "2010-07-20"},
    ]}}
    new_tag_fixture = {"units": {"USD": [
        {"end": "2024-05-31", "val": 7_519_000_000, "accn": "new-24", "fy": 2026,
         "fp": "FY", "form": "10-K", "filed": "2026-07-15"},
        {"end": "2025-05-31", "val": 7_489_000_000, "accn": "new-25", "fy": 2025,
         "fp": "FY", "form": "10-K", "filed": "2025-07-17"},
    ]}}
    session = _FakeSession({
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320187/us-gaap/InventoryNet.json":
            _FakeResponse(200, old_tag_fixture),
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320187/"
        "us-gaap/InventoryFinishedGoodsNetOfReserves.json":
            _FakeResponse(200, new_tag_fixture),
    })
    obs = fetch_line_item(320187, item, forms=("10-K",), session=session)

    assert len(obs) == 4  # both eras present, not just whichever tag was tried first
    by_year = {o.period_end: o for o in obs}
    assert by_year["2009-05-31"].matched_tag == "InventoryNet"
    assert by_year["2025-05-31"].matched_tag == "InventoryFinishedGoodsNetOfReserves"
    assert by_year["2025-05-31"].value == 7_489_000_000


def test_fetch_line_item_returns_empty_when_no_tag_matches():
    item = BY_NAME["long_term_debt"]  # a filer with no debt legitimately has neither tag
    session = _FakeSession({})  # every URL 404s
    obs = fetch_line_item(320187, item, forms=("10-K",), session=session)
    assert obs == []
