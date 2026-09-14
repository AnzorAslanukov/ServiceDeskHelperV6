"""
Feature #1 — Description Search LIVE integration tests (real Athena API).

These tests call the REAL Athena view/filter endpoint via
TicketSearchService.search_by_description and document the behaviour
observed while probing description search. They are marked
@pytest.mark.integration and skipped by default.

Run: pytest tests/test_search/test_description_search_integration.py -v -m integration

Findings expected (parity with the field-search probe, since description
search reuses search_incidents/_parse_paged_response):
  * A single-word 'Description contains <word>' filter returns data.
  * Multi-word input is AND-of-contains: every returned description should
    contain ALL words.
  * The view endpoint ignores $top/$skip: page_size is NOT honored and the
    server caps at ~1000 rows (total==len(results), has_more may be True).
  * Empty / whitespace-only text builds an empty filter group (probe whether
    Athena treats it as match-all or errors).
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
def description_search_service(athena_client) -> TicketSearchService:
    # vector store not needed for description search
    return TicketSearchService(athena_client=athena_client, databricks_client=None)


# ── Happy path ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_single_word_returns_results(description_search_service):
    """A broad single-word description search should return incidents."""
    result = await description_search_service.search_by_description(
        text="printer", ticket_type="incident"
    )
    assert isinstance(result.tickets, list)
    assert result.total >= 1
    assert all(t.id for t in result.tickets)


@pytest.mark.asyncio
async def test_live_multi_word_is_and_of_contains(description_search_service):
    """
    Multi-word input must behave as AND: every returned ticket's description
    should contain BOTH words (case-insensitive check). Confirms the
    'must contain ALL words' contract against real data.
    """
    result = await description_search_service.search_by_description(
        text="password reset", ticket_type="incident"
    )
    for t in result.tickets:
        if t.description:  # description may be truncated but should hold both
            desc = t.description.lower()
            assert "password" in desc and "reset" in desc


@pytest.mark.asyncio
async def test_live_service_request_ticket_type(description_search_service):
    """Description search must also work against the SR view endpoint."""
    result = await description_search_service.search_by_description(
        text="access", ticket_type="servicerequest"
    )
    assert isinstance(result.tickets, list)
    assert all(t.id for t in result.tickets)


# ── DEFECT: pagination is ignored by the view endpoint ────────────────


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="LIVE DEFECT: Athena view endpoint ignores $top; page_size not "
    "honored for description search (observed on field search: page_size=5 "
    "-> ~1000 rows).",
)
async def test_live_page_size_is_respected(description_search_service):
    result = await description_search_service.search_by_description(
        text="printer", ticket_type="incident", page=1, page_size=5
    )
    assert len(result.tickets) <= 5


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="LIVE DEFECT: inconsistent pagination metadata — expect "
    "total==len(results) AND has_more=True simultaneously on large sets.",
)
async def test_live_has_more_consistent_with_total(description_search_service):
    result = await description_search_service.search_by_description(
        text="the", ticket_type="incident"  # very common token -> large set
    )
    assert not (result.has_more and result.total == len(result.tickets))


# ── DEFECT: empty text -> match-all / server error ────────────────────


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="LIVE DEFECT: empty/whitespace-only text builds an empty filter "
    "group; probe whether Athena returns match-all (unbounded) or 500.",
)
async def test_live_empty_text_does_not_match_all(description_search_service):
    result = await description_search_service.search_by_description(
        text="   ", ticket_type="incident"
    )
    # A well-behaved system should NOT return a full, unbounded result set
    # for an effectively empty query.
    assert result.total == 0


# ── Special characters ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_special_characters_do_not_500(description_search_service):
    """
    Special characters in the search text should not crash the request with
    an unhandled HTTP 500. If Athena rejects them, we capture the status for
    the defect ledger rather than letting it escape untyped.
    """
    try:
        result = await description_search_service.search_by_description(
            text='printer" OR %', ticket_type="incident"
        )
        assert isinstance(result.tickets, list)
    except httpx.HTTPStatusError as e:  # documents a fail-mode if it occurs
        pytest.fail(
            f"Special characters caused an unhandled HTTP {e.response.status_code} "
            "— needs graceful handling in the description-search path."
        )
