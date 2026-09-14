"""
Feature #1 — Description Search PROBE suite (mocked, no network).

This module exhaustively probes the *description search* mode only
(POST /search/description -> TicketSearchService.search_by_description ->
AthenaClient.build_description_filter -> search_tickets ->
_parse_paged_response -> _map_ticket) and encodes the behaviours discovered
while stress-testing it.

Two kinds of tests live here:

1. Functional probes — confirm documented behaviour works.
2. Defect probes — reproduce concrete bugs. These are marked with
   @pytest.mark.xfail(strict=True) so the suite stays green while clearly
   flagging the defect. When a defect is fixed, its xfail turns into an
   XPASS (strict=True makes that a failure), which is the signal to remove
   the marker.

Scope: description search ONLY. Field/semantic/similar modes are excluded,
except for shared plumbing (pagination via search_incidents/
search_service_requests -> _parse_paged_response) which is asserted here
because defects there also break description search.

Run: pytest tests/test_search/test_description_search_probe.py -v
"""

from unittest.mock import AsyncMock

import httpx
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


def _leaves(filt):
    """Return the per-word leaf filters from build_description_filter output."""
    return filt[0]["filters"]


# ═══════════════════════════════════════════════════════════════════════
# SECTION 1 — build_description_filter structure (pure, no mocks)
# ═══════════════════════════════════════════════════════════════════════


def test_build_description_filter_single_word():
    """Single word -> exactly one 'Description contains <word>' leaf."""
    filt = AthenaClient.build_description_filter("printer")
    assert filt[0]["condition"] == "and"
    leaves = _leaves(filt)
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf["property"] == "Description"
    assert leaf["operator"] == "contains"
    assert leaf["value"] == "printer"
    assert leaf["condition"] == "and"


def test_build_description_filter_multi_word_is_and_of_contains():
    """Multi-word -> one 'contains' leaf per word, all joined with AND."""
    filt = AthenaClient.build_description_filter("cerner printer")
    leaves = _leaves(filt)
    assert [l["value"] for l in leaves] == ["cerner", "printer"]
    assert all(l["operator"] == "contains" for l in leaves)
    assert all(l["property"] == "Description" for l in leaves)
    assert filt[0]["condition"] == "and"


def test_build_description_filter_trims_and_collapses_whitespace():
    """Extra/leading/trailing whitespace must not produce empty-string leaves."""
    filt = AthenaClient.build_description_filter("  cerner   printer  ")
    leaves = _leaves(filt)
    assert [l["value"] for l in leaves] == ["cerner", "printer"]
    assert all(l["value"].strip() != "" for l in leaves)


@pytest.mark.parametrize("text", ["", "   ", "\t", "\n  \n"])
def test_build_description_filter_empty_text_raises(text):
    """
    FIXED (Defect #4): empty / whitespace-only text no longer produces an
    empty (match-all) filter. build_description_filter now raises ValueError
    as a safety net so an unbounded query can never reach Athena.
    """
    with pytest.raises(ValueError):
        AthenaClient.build_description_filter(text)


def test_build_description_filter_passes_special_chars_verbatim():
    """No validation/escaping — special chars are forwarded into 'value'."""
    tricky = 'a"b\\c%_'
    filt = AthenaClient.build_description_filter(tricky)
    assert _leaves(filt)[0]["value"] == tricky


def test_build_description_filter_preserves_unicode_word():
    """Unicode/emoji tokens are passed through unchanged."""
    filt = AthenaClient.build_description_filter("café 🖨️")
    assert [l["value"] for l in _leaves(filt)] == ["café", "🖨️"]



# ═══════════════════════════════════════════════════════════════════════
# SECTION 2 — search_by_description happy paths & routing (mocked)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_description_search_maps_results(search_service, mock_athena_client):
    """Results are mapped to TicketSummary and pagination is passed through."""
    mock_athena_client.search_tickets.return_value = _paged(
        [{"id": "IR1", "title": "printer down", "description": "printer not printing"}],
        total=1,
    )
    result = await search_service.search_by_description(
        text="printer", ticket_type="incident"
    )
    assert mock_athena_client.search_tickets.call_count == 1
    assert result.total == 1
    assert result.tickets[0].id == "IR1"


@pytest.mark.asyncio
async def test_description_search_uses_description_filter(
    search_service, mock_athena_client
):
    """The filter forwarded to Athena must be a Description 'contains' filter."""
    mock_athena_client.search_tickets.return_value = _paged([])
    await search_service.search_by_description(text="cerner printer")
    filters_arg = mock_athena_client.search_tickets.call_args[0][0]
    leaves = filters_arg[0]["filters"]
    assert [l["value"] for l in leaves] == ["cerner", "printer"]
    assert all(l["property"] == "Description" for l in leaves)


@pytest.mark.asyncio
async def test_description_ticket_type_forwarded(search_service, mock_athena_client):
    """ticket_type must be forwarded to the client verbatim."""
    mock_athena_client.search_tickets.return_value = _paged([])
    await search_service.search_by_description(
        text="x", ticket_type="servicerequest"
    )
    assert mock_athena_client.search_tickets.call_args[0][1] == "servicerequest"


@pytest.mark.asyncio
async def test_description_pagination_args_forwarded(
    search_service, mock_athena_client
):
    """page and page_size must be forwarded to the client."""
    mock_athena_client.search_tickets.return_value = _paged([])
    await search_service.search_by_description(
        text="x", ticket_type="incident", page=3, page_size=25
    )
    args = mock_athena_client.search_tickets.call_args[0]
    assert args[2] == 3       # page
    assert args[3] == 25      # page_size


@pytest.mark.asyncio
async def test_description_empty_results_no_crash(search_service, mock_athena_client):
    """No matches -> empty list, total 0, has_more False, no exception."""
    mock_athena_client.search_tickets.return_value = _paged([])
    result = await search_service.search_by_description(text="nonexistentxyz")
    assert result.tickets == []
    assert result.total == 0
    assert result.has_more is False


@pytest.mark.asyncio
async def test_description_invalid_ticket_type_propagates_valueerror(
    mock_athena_client,
):
    """
    An unsupported ticket_type reaches AthenaClient.search_tickets which
    raises ValueError. Confirm it propagates (root cause: no clean handling
    at the service layer — surfaces as a 500 at the raw API).
    """
    svc = TicketSearchService(mock_athena_client, AsyncMock())
    mock_athena_client.search_tickets.side_effect = ValueError(
        "Unsupported ticket_type: 'changerequest'."
    )
    with pytest.raises(ValueError):
        await svc.search_by_description(text="x", ticket_type="changerequest")


# ═══════════════════════════════════════════════════════════════════════
# SECTION 3 — mapping robustness (mocked)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_description_missing_id_maps_to_unknown(
    search_service, mock_athena_client
):
    mock_athena_client.search_tickets.return_value = _paged([{"title": "no id"}])
    result = await search_service.search_by_description(text="x")
    assert result.tickets[0].id == "unknown"



# ═══════════════════════════════════════════════════════════════════════
# SECTION 4 — shared pagination plumbing DEFECTS (mocked, xfail)
# These bugs live in search_incidents/_parse_paged_response and therefore
# also break description search.
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_description_page_size_is_respected(search_service, mock_athena_client):
    """
    FIXED (Defect #1): page_size is now honoured via client-side slicing in
    AthenaClient._parse_paged_response. Here the mock stands in for that
    already-sliced client output; the service must pass it through faithfully.
    (Direct slicing behaviour is unit-tested in
    test_slice_page_* against AthenaClient._slice_page.)
    """
    five = [{"id": f"IR{i}", "description": "printer"} for i in range(5)]
    mock_athena_client.search_tickets.return_value = _paged(
        five, total=1000, page=1, page_size=5, has_more=True
    )
    result = await search_service.search_by_description(
        text="printer", page=1, page_size=5
    )
    assert len(result.tickets) == 5
    assert result.page_size == 5


@pytest.mark.asyncio
async def test_description_has_more_consistent_with_total(
    search_service, mock_athena_client
):
    """
    FIXED (Defect #2): metadata is now internally consistent. When a page is
    the full result set, has_more must be False (never True with
    total == len(results)).
    """
    fifty = [{"id": f"IR{i}"} for i in range(50)]
    mock_athena_client.search_tickets.return_value = _paged(
        fifty, total=50, page=1, page_size=50, has_more=False
    )
    result = await search_service.search_by_description(text="printer")
    assert not (result.has_more and result.total == len(result.tickets))


@pytest.mark.asyncio
async def test_description_athena_500_propagates_for_router_translation(
    search_service, mock_athena_client
):
    """
    FIXED (Defect #3): the service intentionally propagates an Athena HTTP 500
    as httpx.HTTPStatusError. The REST router (and the app-wide exception
    handler) translate it into a clean 502; the HTMX partial renders an error
    partial. Either way it is no longer an unhandled 500 to the caller. Here we
    assert the error surfaces as a typed exception rather than being silently
    swallowed into an empty result.
    """
    request = httpx.Request("POST", "https://example/v1/view/workitem?type=incident")
    response = httpx.Response(500, request=request)
    mock_athena_client.search_tickets.side_effect = httpx.HTTPStatusError(
        "Server error '500 Internal Server Error'", request=request, response=response
    )
    with pytest.raises(httpx.HTTPStatusError):
        await search_service.search_by_description(text="printer")


# ═══════════════════════════════════════════════════════════════════════
# SECTION 5 — AthenaClient._slice_page / _parse_paged_response (pure)
# Directly exercises the pagination fix (Defects #1 & #2).
# ═══════════════════════════════════════════════════════════════════════


def test_slice_page_honours_page_size():
    """A 1000-row full set must be sliced down to page_size rows."""
    full = [{"id": f"IR{i}"} for i in range(1000)]
    out = AthenaClient._slice_page(full, page=1, page_size=5)
    assert len(out["results"]) == 5
    assert out["total"] == 1000
    assert out["has_more"] is True


def test_slice_page_second_page_offsets_correctly():
    full = [{"id": f"IR{i}"} for i in range(12)]
    out = AthenaClient._slice_page(full, page=2, page_size=5)
    assert [r["id"] for r in out["results"]] == ["IR5", "IR6", "IR7", "IR8", "IR9"]
    assert out["total"] == 12
    assert out["has_more"] is True


def test_slice_page_last_page_has_more_false():
    full = [{"id": f"IR{i}"} for i in range(12)]
    out = AthenaClient._slice_page(full, page=3, page_size=5)
    assert [r["id"] for r in out["results"]] == ["IR10", "IR11"]
    assert out["has_more"] is False


def test_slice_page_full_set_on_one_page_has_more_false():
    """Invariant: has_more must be False when the page holds the whole total."""
    full = [{"id": f"IR{i}"} for i in range(50)]
    out = AthenaClient._slice_page(full, page=1, page_size=50)
    assert out["total"] == 50
    assert len(out["results"]) == 50
    assert out["has_more"] is False
    assert not (out["has_more"] and out["total"] == len(out["results"]))


def test_slice_page_empty_set():
    out = AthenaClient._slice_page([], page=1, page_size=50)
    assert out["results"] == []
    assert out["total"] == 0
    assert out["has_more"] is False


def test_parse_paged_response_slices_athena_dict():
    """_parse_paged_response feeds the Athena 'result' list through _slice_page."""
    data = {"result": [{"id": f"IR{i}"} for i in range(30)], "resultCount": 30}
    out = AthenaClient._parse_paged_response(data, page=1, page_size=10)
    assert len(out["results"]) == 10
    assert out["total"] == 30
    assert out["has_more"] is True


def test_parse_paged_response_list_fallback_is_sliced():
    data = [{"id": f"IR{i}"} for i in range(7)]
    out = AthenaClient._parse_paged_response(data, page=1, page_size=5)
    assert len(out["results"]) == 5
    assert out["total"] == 7
    assert out["has_more"] is True


def test_parse_paged_response_unexpected_shape_is_empty():
    out = AthenaClient._parse_paged_response(None, page=1, page_size=50)
    assert out["results"] == []
    assert out["total"] == 0
    assert out["has_more"] is False


@pytest.mark.asyncio
async def test_description_empty_text_is_rejected(search_service, mock_athena_client):
    """
    FIXED (Defect #4): empty/whitespace-only text is rejected at the service
    layer with a ValueError before any Athena call, preventing a match-all
    (unbounded) query. Athena must never be called.
    """
    with pytest.raises(ValueError):
        await search_service.search_by_description(text="   ")
    mock_athena_client.search_tickets.assert_not_called()


@pytest.mark.asyncio
async def test_description_missing_description_key_is_none(
    search_service, mock_athena_client
):
    """A ticket without a 'description' key must map to None (no KeyError)."""
    mock_athena_client.search_tickets.return_value = _paged([{"id": "IR1"}])
    result = await search_service.search_by_description(text="x")
    assert result.tickets[0].description is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "length,expect_ellipsis", [(499, False), (500, False), (501, True), (900, True)]
)
async def test_description_truncated_to_500_boundaries(
    search_service, mock_athena_client, length, expect_ellipsis
):
    """Descriptions >500 chars are truncated to 500 + '...'; <=500 untouched."""
    desc = "x" * length
    mock_athena_client.search_tickets.return_value = _paged(
        [{"id": "IR1", "description": desc}]
    )
    result = await search_service.search_by_description(text="x")
    out = result.tickets[0].description
    if expect_ellipsis:
        assert out.endswith("...")
        assert len(out) == 503  # 500 + "..."
    else:
        assert out == desc


@pytest.mark.asyncio
async def test_description_maps_nested_object_format(
    search_service, mock_athena_client
):
    """_map_ticket must handle the nested (object endpoint) shape too."""
    mock_athena_client.search_tickets.return_value = _paged(
        [
            {
                "id": "IR1",
                "title": "t",
                "status": {"name": "Active"},
                "supportGroup": {"name": "Service Desk"},
                "affectedUser": {"displayName": "Jane Doe"},
            }
        ]
    )
    result = await search_service.search_by_description(text="x")
    t = result.tickets[0]
    assert t.status == "Active"
    assert t.support_group == "Service Desk"
    assert t.affected_user == "Jane Doe"
