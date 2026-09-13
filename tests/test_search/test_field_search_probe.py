"""
Feature #1 — Field Search PROBE suite (mocked, no network).

This module exhaustively probes the *field search* mode only
(POST /search/field -> TicketSearchService.search_by_field) and encodes
the behaviours discovered while stress-testing the live Athena API.

Two kinds of tests live here:

1. Functional probes — confirm documented behaviour works.
2. Defect probes — reproduce concrete bugs found during live testing.
   These are marked with @pytest.mark.xfail(strict=True) so the suite stays
   green while clearly flagging the defect. When a defect is fixed, its
   xfail turns into an XPASS (strict=True makes that a failure), which is
   the signal to remove the marker.

Run: pytest tests/test_search/test_field_search_probe.py -v
"""

from unittest.mock import AsyncMock

import pytest

from src.clients.athena_client import AthenaClient
from src.services.ticket_search import TicketSearchService


# ── Helpers ───────────────────────────────────────────────────────────


def _paged(results, total=None, page=1, page_size=50, has_more=False):
    """Build a normalized paged response like AthenaClient returns."""
    return {
        "results": results,
        "total": total if total is not None else len(results),
        "page": page,
        "page_size": page_size,
        "has_more": has_more,
    }


# ═══════════════════════════════════════════════════════════════════════
# SECTION 1 — build_field_filter structure (pure, no mocks)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("operator", ["eq", "ne", "contains", "like", "gt", "lt"])
def test_build_field_filter_wraps_operator(operator):
    """Every operator should be embedded verbatim in the nested filter."""
    filt = AthenaClient.build_field_filter("Title", "printer", operator)
    leaf = filt[0]["filters"][0]
    assert leaf["operator"] == operator
    assert leaf["property"] == "Title"
    assert leaf["value"] == "printer"
    assert leaf["condition"] == "and"
    assert filt[0]["condition"] == "and"


def test_build_field_filter_passes_arbitrary_field_unvalidated():
    """The builder performs no validation of the field name."""
    filt = AthenaClient.build_field_filter("notARealField", "x", "eq")
    assert filt[0]["filters"][0]["property"] == "notARealField"


# ═══════════════════════════════════════════════════════════════════════
# SECTION 2 — phone normalization / formatting (pure)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("215-555-1234", "2155551234"),
        ("(215) 555-1234", "2155551234"),
        ("215.555.1234", "2155551234"),
        ("2155551234", "2155551234"),
        ("+1 215 555 1234", "12155551234"),
        ("ext. 5", "5"),
        ("", ""),
        ("no-digits-here", ""),
    ],
)
def test_normalize_phone(raw, expected):
    assert AthenaClient.normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "digits,expected",
    [
        ("2155551234", "215-555-1234"),
        ("123", "123"),
        ("12155551234", "12155551234"),
    ],
)
def test_format_phone_with_dashes(digits, expected):
    assert AthenaClient.format_phone_with_dashes(digits) == expected



# ═══════════════════════════════════════════════════════════════════════
# SECTION 3 — search_by_field happy paths (mocked)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_non_phone_field_single_query(search_service, mock_athena_client):
    """A non-phone field issues exactly ONE Athena query and maps results."""
    mock_athena_client.search_tickets.return_value = _paged(
        [{"id": "IR1", "title": "printer down"}], total=1
    )
    result = await search_service.search_by_field(
        field="title", value="printer", operator="contains", ticket_type="incident"
    )
    assert mock_athena_client.search_tickets.call_count == 1
    assert result.total == 1
    assert result.tickets[0].id == "IR1"


@pytest.mark.asyncio
async def test_ticket_type_forwarded(search_service, mock_athena_client):
    """ticket_type must be forwarded to the client verbatim."""
    mock_athena_client.search_tickets.return_value = _paged([])
    await search_service.search_by_field(
        field="title", value="x", ticket_type="servicerequest"
    )
    assert mock_athena_client.search_tickets.call_args[0][1] == "servicerequest"


@pytest.mark.asyncio
async def test_phone_field_issues_two_queries_and_merges(mock_athena_client):
    """
    Phone field: dashless + dashed queries are merged, de-duped by id, and
    an authoritative digit-match is applied. Proven live: a dashed input
    finds a dashless-stored record.
    """
    svc = TicketSearchService(mock_athena_client, AsyncMock())

    def side_effect(filters, ticket_type, page, page_size):
        val = filters[0]["filters"][0]["value"]
        if val == "2153452661":  # dashless query
            return _paged([{"id": "IRA", "contactMethod": "2153452661"}])
        return _paged([{"id": "IRB", "contactMethod": "215-345-2661"}])  # dashed query

    mock_athena_client.search_tickets.side_effect = side_effect
    result = await svc.search_by_field(
        field="contactMethod", value="215-345-2661", ticket_type="incident"
    )
    assert mock_athena_client.search_tickets.call_count == 2
    ids = {t.id for t in result.tickets}
    assert ids == {"IRA", "IRB"}


@pytest.mark.asyncio
async def test_phone_non_numeric_falls_back_to_standard_filter(mock_athena_client):
    """A contactMethod with no digits (email) falls back to a single filter query."""
    svc = TicketSearchService(mock_athena_client, AsyncMock())
    mock_athena_client.search_tickets.return_value = _paged([{"id": "IRX"}])
    result = await svc.search_by_field(
        field="contactMethod", value="user@example.org", ticket_type="incident"
    )
    assert mock_athena_client.search_tickets.call_count == 1
    assert result.tickets[0].id == "IRX"


@pytest.mark.asyncio
async def test_phone_client_side_digit_match_filters_false_positives(mock_athena_client):
    """Client-side digit equality must drop Athena 'contains' false positives."""
    svc = TicketSearchService(mock_athena_client, AsyncMock())
    mock_athena_client.search_tickets.return_value = _paged(
        [
            {"id": "GOOD", "contactMethod": "215-345-2661"},
            {"id": "BAD", "contactMethod": "999-999-9999"},
        ]
    )
    result = await svc.search_by_field(
        field="contactMethod", value="2153452661", ticket_type="incident"
    )
    ids = {t.id for t in result.tickets}
    assert "GOOD" in ids


# ═══════════════════════════════════════════════════════════════════════
# SECTION 4 — DEFECT PROBES (reproduce real bugs found against live Athena)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: page_size not honored. Athena view endpoint ignores "
    "$top/$skip; service returns up to ~1000 rows regardless. "
    "Live: page_size=5 returned 1000 rows.",
)
async def test_page_size_is_respected(search_service, mock_athena_client):
    big = [{"id": f"IR{i}"} for i in range(1000)]
    mock_athena_client.search_tickets.return_value = _paged(
        big, total=1000, page=1, page_size=5, has_more=True
    )
    result = await search_service.search_by_field(
        field="title", value="printer", operator="contains", page=1, page_size=5
    )
    assert len(result.tickets) <= 5


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: inconsistent pagination metadata. Live returned "
    "total==len(results)==1000 AND has_more=True simultaneously.",
)
async def test_has_more_consistent_with_total(search_service, mock_athena_client):
    big = [{"id": f"IR{i}"} for i in range(1000)]
    mock_athena_client.search_tickets.return_value = _paged(
        big, total=1000, page=1, page_size=50, has_more=True
    )
    result = await search_service.search_by_field(
        field="title", value="printer", operator="contains"
    )
    assert not (result.has_more and result.total == len(result.tickets))


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: no graceful handling of Athena HTTP 500. Live: "
    "'Status eq Active' and any 'ne' status filter return 500, which "
    "propagates as an unhandled httpx.HTTPStatusError.",
)
async def test_athena_500_is_handled_gracefully(search_service, mock_athena_client):
    import httpx

    request = httpx.Request("POST", "https://example/v1/view/workitem?type=incident")
    response = httpx.Response(500, request=request)
    mock_athena_client.search_tickets.side_effect = httpx.HTTPStatusError(
        "Server error '500 Internal Server Error'", request=request, response=response
    )
    result = await search_service.search_by_field(
        field="status", value="Active", operator="eq"
    )
    assert result.total == 0


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: phone-search path disables pagination — always sets "
    "has_more=False and total=len(matches), and only scans a single page_size "
    "of Athena rows (not _PHONE_SCAN_LIMIT=1000), so later matches are lost.",
)
async def test_phone_search_paginates_beyond_first_page(mock_athena_client):
    svc = TicketSearchService(mock_athena_client, AsyncMock())
    all_records = [{"id": f"IR{i}", "contactMethod": "2153452661"} for i in range(60)]

    def side_effect(filters, ticket_type, page, page_size):
        return _paged(all_records[:page_size], total=60, has_more=True)

    mock_athena_client.search_tickets.side_effect = side_effect
    result = await svc.search_by_field(
        field="contactMethod", value="2153452661", page=1, page_size=50
    )
    assert result.total == 60


# ═══════════════════════════════════════════════════════════════════════
# SECTION 5 — mapping robustness (mocked)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_missing_id_maps_to_unknown(search_service, mock_athena_client):
    mock_athena_client.search_tickets.return_value = _paged([{"title": "no id"}])
    result = await search_service.search_by_field(field="title", value="x")
    assert result.tickets[0].id == "unknown"


@pytest.mark.asyncio
async def test_description_truncated_to_500(search_service, mock_athena_client):
    long_desc = "x" * 900
    mock_athena_client.search_tickets.return_value = _paged(
        [{"id": "IR1", "description": long_desc}]
    )
    result = await search_service.search_by_field(field="title", value="x")
    assert result.tickets[0].description.endswith("...")
    assert len(result.tickets[0].description) == 503  # 500 + "..."
