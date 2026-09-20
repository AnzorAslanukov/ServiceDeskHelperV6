"""
Feature #1 — Field Search error-handling / timeout fixes (mocked, no network).

Covers the fixes for the "Title contains <word> returns nothing" defect:

* AthenaClient now uses a separate, longer-timeout HTTP client for the
  slow view/search endpoint (a substring scan on Title exceeded the old 30s
  timeout and raised httpx.ReadTimeout, which surfaced as an empty error).
* The frontend router translates timeouts / HTTP errors into clear,
  non-empty, actionable messages (never str(ReadTimeout) == "").
* The API /search/field endpoint maps the same failures to clean HTTP
  status codes (504 for timeout, 502 for upstream errors, 400 for ValueError).

Run: pytest tests/test_search/test_field_search_error_handling.py -v
"""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from src.clients.athena_client import AthenaClient
from src.models.search import FieldSearchRequest
from src.routers import search as search_router
from src.routers.frontend import _field_search_error_message


# ═══════════════════════════════════════════════════════════════════════
# SECTION 1 — frontend router error-message helper (pure)
# ═══════════════════════════════════════════════════════════════════════


def test_timeout_on_contains_produces_actionable_nonempty_message():
    """A ReadTimeout on 'Title contains' must NOT render as an empty string."""
    exc = httpx.ReadTimeout("")  # str(exc) == "" — the original blank-error bug
    msg = _field_search_error_message(exc, field="title", operator="contains")
    assert msg  # non-empty
    assert msg.strip() != ""
    assert "timed out" in msg.lower()
    # Steers the user toward a working alternative.
    assert "eq" in msg.lower() or "equals" in msg.lower()


def test_timeout_generic_operator_message():
    exc = httpx.ConnectTimeout("")
    msg = _field_search_error_message(exc, field="status", operator="eq")
    assert msg
    assert "timed out" in msg.lower()


def test_http_status_error_contains_operator_message():
    request = httpx.Request("POST", "https://example/v1/view/workitem")
    response = httpx.Response(500, request=request)
    exc = httpx.HTTPStatusError("500", request=request, response=response)
    msg = _field_search_error_message(exc, field="title", operator="like")
    assert "500" in msg
    assert msg  # non-empty


def test_http_status_error_generic_operator_message():
    request = httpx.Request("POST", "https://example/v1/view/workitem")
    response = httpx.Response(500, request=request)
    exc = httpx.HTTPStatusError("500", request=request, response=response)
    msg = _field_search_error_message(exc, field="status", operator="ne")
    assert "500" in msg


def test_fallback_message_is_never_empty():
    """An unknown exception with an empty str() must still yield a message."""

    class Weird(Exception):
        def __str__(self):
            return ""

    msg = _field_search_error_message(Weird(), field="title", operator="contains")
    assert msg  # non-empty
    assert "Weird" in msg


# ═══════════════════════════════════════════════════════════════════════
# SECTION 2 — API /search/field endpoint error translation
# ═══════════════════════════════════════════════════════════════════════


def _req(operator: str = "contains") -> FieldSearchRequest:
    return FieldSearchRequest(field="title", value="heartbeat", operator=operator)


@pytest.mark.asyncio
async def test_api_timeout_maps_to_504():
    service = AsyncMock()
    service.search_by_field.side_effect = httpx.ReadTimeout("")
    with pytest.raises(HTTPException) as exc:
        await search_router.search_by_field(_req(), service=service)
    assert exc.value.status_code == 504
    assert exc.value.detail  # non-empty


@pytest.mark.asyncio
async def test_api_http_status_error_maps_to_502():
    request = httpx.Request("POST", "https://example/v1/view/workitem")
    response = httpx.Response(500, request=request)
    service = AsyncMock()
    service.search_by_field.side_effect = httpx.HTTPStatusError(
        "500", request=request, response=response
    )
    with pytest.raises(HTTPException) as exc:
        await search_router.search_by_field(_req("like"), service=service)
    assert exc.value.status_code == 502
    assert "500" in exc.value.detail


@pytest.mark.asyncio
async def test_api_value_error_maps_to_400():
    service = AsyncMock()
    service.search_by_field.side_effect = ValueError("bad field")
    with pytest.raises(HTTPException) as exc:
        await search_router.search_by_field(_req("eq"), service=service)
    assert exc.value.status_code == 400
    assert "bad field" in exc.value.detail


@pytest.mark.asyncio
async def test_api_success_passes_through():
    from src.models.search import FieldSearchResponse

    service = AsyncMock()
    service.search_by_field.return_value = FieldSearchResponse(
        tickets=[], total=0, page=1, page_size=50, has_more=False
    )
    result = await search_router.search_by_field(_req("eq"), service=service)
    assert result.total == 0
    service.search_by_field.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════
# SECTION 3 — AthenaClient timeout wiring (no network)
# ═══════════════════════════════════════════════════════════════════════


class _FakeSettings:
    """Minimal settings stub exposing only the timeout attributes."""

    athena_request_timeout = 30.0
    athena_search_timeout = 90.0


@pytest.mark.asyncio
async def test_search_client_uses_longer_timeout_than_default():
    client = AthenaClient(_FakeSettings())
    default_client = await client._get_http_client()
    search_client = await client._get_search_http_client()
    try:
        assert default_client is not search_client
        assert search_client.timeout.read == 90.0
        assert default_client.timeout.read == 30.0
        assert search_client.timeout.read > default_client.timeout.read
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_close_disposes_both_clients():
    client = AthenaClient(_FakeSettings())
    d = await client._get_http_client()
    s = await client._get_search_http_client()
    await client.close()
    assert d.is_closed
    assert s.is_closed
    assert client._http_client is None
    assert client._search_http_client is None


@pytest.mark.asyncio
async def test_timeout_falls_back_to_defaults_when_settings_missing():
    """If settings lack the new attrs, sensible defaults are used."""

    class Bare:
        pass

    client = AthenaClient(Bare())
    try:
        assert client._default_timeout() == 30.0
        assert client._search_timeout() == 90.0
    finally:
        await client.close()
