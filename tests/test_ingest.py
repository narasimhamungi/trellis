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


def test_fetch_line_item_tries_tags_in_order_and_records_which_matched():
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
    assert len(session.calls) == 1  # first tag hit -- fallback tags never queried


def test_fetch_line_item_falls_back_when_first_tag_is_absent():
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
    assert len(session.calls) == 2  # had to try both tags


def test_fetch_line_item_returns_empty_when_no_tag_matches():
    item = BY_NAME["long_term_debt"]  # a filer with no debt legitimately has neither tag
    session = _FakeSession({})  # every URL 404s
    obs = fetch_line_item(320187, item, forms=("10-K",), session=session)
    assert obs == []
