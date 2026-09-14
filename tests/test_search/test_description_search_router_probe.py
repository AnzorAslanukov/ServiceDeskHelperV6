"""
Feature #1 — Description Search ROUTER/UI PROBE suite (mocked, no network).

Probes the description-search request model validation, the REST handler
(src.routers.search.search_by_description) and the HTMX partial handler
(src.routers.frontend.search_description_partial).

NOTE ON APPROACH: FastAPI's TestClient is currently broken in this
environment (pre-existing, unrelated: starlette/httpx version
incompatibility causes JSONDecodeError — reproduced by
tests/test_search/test_search_router.py). To avoid coupling this suite to
that defect, we exercise:
  * Pydantic request-model validation directly (equivalent to the 422s the
    router would raise), and
  * the async handler functions directly with a mocked service (equivalent
    to what FastAPI would call after DI + validation).

Scope: description search ONLY.

Run: pytest tests/test_search/test_description_search_router_probe.py -v
"""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.models.search import (
    DescriptionSearchRequest,
    FieldSearchResponse,
    TicketSummary,
    TicketType,
)
from src.routers.search import search_by_description as api_search_by_description
from src.routers.frontend import search_description_partial
from src.services.ticket_search import TicketSearchService


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_service() -> AsyncMock:
    service = AsyncMock(spec=TicketSearchService)
    service.search_by_description.return_value = FieldSearchResponse(
        tickets=[TicketSummary(id="IR1", title="printer down")],
        total=1,
        page=1,
        page_size=50,
        has_more=False,
    )
    return service


# ═══════════════════════════════════════════════════════════════════════
# SECTION D1 — request model validation (equivalent to router 422s)
# ═══════════════════════════════════════════════════════════════════════


def test_model_defaults():
    req = DescriptionSearchRequest(text="printer")
    assert req.ticket_type == TicketType.incident
    assert req.page == 1
    assert req.page_size == 50


def test_model_missing_text_is_invalid():
    """Missing 'text' -> ValidationError (router would return 422)."""
    with pytest.raises(ValidationError):
        DescriptionSearchRequest()


@pytest.mark.parametrize("page", [0, -1])
def test_model_page_must_be_ge_1(page):
    with pytest.raises(ValidationError):
        DescriptionSearchRequest(text="x", page=page)


@pytest.mark.parametrize("size", [0, -5, 201, 1000])
def test_model_page_size_bounds(size):
    with pytest.raises(ValidationError):
        DescriptionSearchRequest(text="x", page_size=size)


def test_model_invalid_ticket_type_enum():
    """An unsupported ticket_type is rejected at the model layer (422),
    before it can reach the ValueError inside AthenaClient.search_tickets."""
    with pytest.raises(ValidationError):
        DescriptionSearchRequest(text="x", ticket_type="changerequest")


@pytest.mark.parametrize("size", [1, 50, 200])
def test_model_page_size_valid_boundaries(size):
    req = DescriptionSearchRequest(text="x", page_size=size)
    assert req.page_size == size


# ═══════════════════════════════════════════════════════════════════════
# SECTION D2 — REST handler (called directly, mocked service)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_api_handler_forwards_all_params(mock_service):
    req = DescriptionSearchRequest(
        text="access request",
        ticket_type=TicketType.servicerequest,
        page=2,
        page_size=25,
    )
    resp = await api_search_by_description(req, service=mock_service)
    assert resp.total == 1
    mock_service.search_by_description.assert_awaited_once_with(
        text="access request",
        ticket_type="servicerequest",
        page=2,
        page_size=25,
    )


@pytest.mark.asyncio
async def test_api_handler_returns_field_search_response(mock_service):
    req = DescriptionSearchRequest(text="printer")
    resp = await api_search_by_description(req, service=mock_service)
    assert isinstance(resp, FieldSearchResponse)
    assert resp.tickets[0].id == "IR1"


@pytest.mark.asyncio
async def test_api_handler_translates_athena_500_to_502(mock_service):
    """
    FIXED (Defect #3): an upstream Athena HTTP 500 is translated into a clean
    HTTPException(502) instead of escaping as an unhandled 500.
    """
    import httpx
    from fastapi import HTTPException

    request = httpx.Request("POST", "https://example/v1/view/workitem?type=incident")
    response = httpx.Response(500, request=request)
    mock_service.search_by_description.side_effect = httpx.HTTPStatusError(
        "Server error '500'", request=request, response=response
    )
    req = DescriptionSearchRequest(text="printer")
    with pytest.raises(HTTPException) as exc_info:
        await api_search_by_description(req, service=mock_service)
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_api_handler_translates_value_error_to_400(mock_service):
    """
    FIXED (Defect #3/#4): a ValueError from the service (e.g. empty text or
    unsupported ticket_type) is translated into HTTPException(400).
    """
    from fastapi import HTTPException

    mock_service.search_by_description.side_effect = ValueError("bad input")
    req = DescriptionSearchRequest(text="printer")
    with pytest.raises(HTTPException) as exc_info:
        await api_search_by_description(req, service=mock_service)
    assert exc_info.value.status_code == 400



# ═══════════════════════════════════════════════════════════════════════
# SECTION E — HTMX partial handler (called directly, mocked service)
# ═══════════════════════════════════════════════════════════════════════


def _fake_request():
    """Build a minimal ASGI Request usable by Jinja2Templates.TemplateResponse."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/ui/search/description",
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


def _body(resp) -> str:
    return resp.body.decode("utf-8")


@pytest.mark.asyncio
async def test_htmx_partial_renders_results(mock_service):
    resp = await search_description_partial(
        _fake_request(),
        text="printer",
        ticket_type="incident",
        page=1,
        page_size=50,
        service=mock_service,
    )
    assert resp.status_code == 200
    assert "IR1" in _body(resp)
    mock_service.search_by_description.assert_awaited_once()


@pytest.mark.asyncio
async def test_htmx_partial_empty_state(mock_service):
    mock_service.search_by_description.return_value = FieldSearchResponse(
        tickets=[], total=0, page=1, page_size=50, has_more=False
    )
    resp = await search_description_partial(
        _fake_request(), text="nomatch", service=mock_service
    )
    assert resp.status_code == 200
    assert "No tickets found" in _body(resp)


@pytest.mark.asyncio
async def test_htmx_partial_degrades_gracefully_on_service_error(mock_service):
    """
    The HTMX handler wraps the service call in try/except and renders the
    error partial instead of crashing — this is the graceful path that the
    raw REST handler lacks.
    """
    mock_service.search_by_description.side_effect = RuntimeError("athena 500")
    resp = await search_description_partial(
        _fake_request(), text="printer", service=mock_service
    )
    assert resp.status_code == 200
    body = _body(resp)
    assert "athena 500" in body or "alert-error" in body


@pytest.mark.asyncio
async def test_htmx_partial_escapes_html_in_echoed_text(mock_service):
    """
    XSS fail-mode: user-supplied 'text' is echoed into the results partial
    (form_text). Jinja2 autoescape must neutralise embedded HTML/script.
    We render a populated result set so the pagination block (which echoes
    form_text) is present.
    """
    mock_service.search_by_description.return_value = FieldSearchResponse(
        tickets=[TicketSummary(id="IR1", title="t")],
        total=100,
        page=1,
        page_size=50,
        has_more=True,
    )
    payload = '<script>alert(1)</script>'
    resp = await search_description_partial(
        _fake_request(), text=payload, ticket_type="incident", service=mock_service
    )
    body = _body(resp)
    # The raw script tag must NOT appear unescaped anywhere in the output.
    assert "<script>alert(1)</script>" not in body
