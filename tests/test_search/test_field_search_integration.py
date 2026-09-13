"""
Feature #1 — Field Search LIVE integration tests (real Athena API).

These tests call the REAL Athena view/filter endpoint and document the
behaviour observed while probing field search. They are marked
@pytest.mark.integration and skipped by default.

Run: pytest tests/test_search/test_field_search_integration.py -v -m integration

Findings encoded here (as of the probe run):
  * A `Title contains <word>` filter works and returns data.
  * `Status eq <value>` and `Status ne <value>` filters return HTTP 500.
  * The view endpoint ignores $top/$skip: page_size is NOT honored and the
    server caps at ~1000 rows (total==len(results)==1000, has_more=True).
  * Phone search is separator-tolerant: a dashed input finds dashless records.
"""

import httpx
import pytest

from src.config import get_settings
from src.clients.athena_client import AthenaClient
from src.services.ticket_search import TicketSearchService

pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def athena_client() -> AthenaClient:
    return AthenaClient(get_settings())


@pytest.fixture
def field_search_service(athena_client) -> TicketSearchService:
    # vector store not needed for field search
    return TicketSearchService(athena_client=athena_client, databricks_client=None)


# ── Happy path ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_title_contains_returns_results(field_search_service):
    """A broad 'title contains printer' search should return incidents."""
    result = await field_search_service.search_by_field(
        field="Title", value="printer", operator="contains", ticket_type="incident",
    )
    assert isinstance(result.tickets, list)
    assert result.total >= 1
    # Sanity: mapped tickets have IR-style ids
    assert all(t.id for t in result.tickets)


# ── DEFECT: pagination is ignored by the view endpoint ────────────────


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="LIVE DEFECT: Athena view endpoint ignores $top; page_size not honored "
    "(observed page_size=5 -> 1000 rows).",
)
async def test_live_page_size_is_respected(field_search_service):
    result = await field_search_service.search_by_field(
        field="Title", value="printer", operator="contains",
        ticket_type="incident", page=1, page_size=5,
    )
    assert len(result.tickets) <= 5


# ── DEFECT: status filters crash Athena with HTTP 500 ─────────────────


@pytest.mark.asyncio
async def test_live_status_eq_returns_500(athena_client):
    """Document that 'Status eq Active' currently returns HTTP 500."""
    filt = AthenaClient.build_field_filter("Status", "Active", "eq")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await athena_client.search_incidents(filt, page=1, page_size=5)
    assert exc.value.response.status_code == 500


@pytest.mark.asyncio
async def test_live_status_ne_returns_500(athena_client):
    """Document that a 'Status ne <value>' filter currently returns HTTP 500."""
    filt = AthenaClient.build_field_filter("Status", "zzz_nonexistent", "ne")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await athena_client.search_incidents(filt, page=1, page_size=5)
    assert exc.value.response.status_code == 500


# ── Phone search: separator tolerance (a genuine strength) ────────────


@pytest.mark.asyncio
async def test_live_phone_search_is_separator_tolerant(field_search_service):
    """
    Find a real dashless contactMethod, then search with a DASHED version of
    the same number and confirm the record is still found.
    """
    number = None
    # Directly query the client to grab a real contactMethod value.
    raw = await field_search_service._athena.search_incidents(
        AthenaClient.build_field_filter("Title", "printer", "contains"),
        page=1, page_size=50,
    )
    for rec in raw["results"]:
        cm = rec.get("contactMethod") or ""
        if len(AthenaClient.normalize_phone(cm)) == 10:
            number = AthenaClient.normalize_phone(cm)
            break

    if not number:
        pytest.skip("No 10-digit contactMethod found in sample to test with.")

    dashed = AthenaClient.format_phone_with_dashes(number)
    result = await field_search_service.search_by_field(
        field="contactMethod", value=dashed, ticket_type="incident",
    )
    # At least one record should match the normalized digits.
    assert result.total >= 1
