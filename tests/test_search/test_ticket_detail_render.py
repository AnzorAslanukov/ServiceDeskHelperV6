"""
Reproduction + regression tests for the "expand ticket details" error on
Feature #1 semantic search results.

Semantic/similar results draw ticket IDs ONLY from the vector store
(ir_embeddings), which per skill.md includes ~2% numeric-only / legacy IDs
that are neither IR- nor SR-prefixed. Clicking expand calls
GET /ui/ticket/{id}/details, which:
  1. routes through AthenaClient.get_ticket() (raises ValueError for non IR/SR
     prefixes), and
  2. renders via _extract_rich_ticket_detail() whose _get_field()/_is_guid()
     helpers assume string values and crash on int priority.

Both surface as the generic "Could not load details" error alert.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.dependencies import get_athena_client
from src.main import app
from src.routers.frontend import _extract_rich_ticket_detail, _get_field, _is_guid


@pytest.fixture(autouse=True)
def _bypass_auth():
    with patch("src.main.get_current_user", return_value=object()):
        yield


def _detail_client(mock_athena) -> httpx.AsyncClient:
    app.dependency_overrides[get_athena_client] = lambda: mock_athena
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ── Bug #2: helper crashes on non-string values ───────────────────────


def test_is_guid_on_non_string_does_not_crash():
    """_is_guid must tolerate non-string input (e.g., int priority)."""
    assert _is_guid(3) is False
    assert _is_guid(None) is False


def test_get_field_returns_int_priority():
    """_get_field must return an integer priority without crashing."""
    raw = {"priority": 3}
    assert _get_field(raw, "priority") == 3


def test_extract_rich_detail_with_int_priority():
    """_extract_rich_ticket_detail must handle a numeric IR priority."""
    raw = {"title": "Printer", "priority": 2, "status": {"name": "Active"}}
    detail = _extract_rich_ticket_detail(raw, "IR100")
    assert detail["priority"] == 2
    assert detail["status"] == "Active"


# ── Bug #1: non IR/SR prefixed ids from the vector store ──────────────


@pytest.mark.asyncio
async def test_detail_numeric_legacy_id_renders_not_error():
    """A numeric-only legacy id (from ir_embeddings) must still render details.

    get_ticket should treat non-SR ids as incidents rather than raising.
    """
    mock_athena = AsyncMock()
    mock_athena.get_ticket.return_value = {
        "title": "Legacy imported ticket",
        "priority": 3,
        "status": {"name": "Closed"},
    }
    try:
        async with _detail_client(mock_athena) as client:
            resp = await client.get("/ui/ticket/1959493/details")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "alert-error" not in resp.text
    assert "Legacy imported ticket" in resp.text


@pytest.mark.asyncio
async def test_detail_none_raw_ticket_shows_friendly_not_found():
    """Athena returning null (JSON) for an embedded-but-unavailable ticket.

    Reproduces: "Could not load details for IR9079436: 'NoneType' object has
    no attribute 'get'". The ticket exists in ir_embeddings but Athena returns
    a null body (HTTP 200), so get_ticket() -> response.json() is None. The
    detail render must degrade to a friendly message, not crash.
    """
    mock_athena = AsyncMock()
    mock_athena.get_ticket.return_value = None
    try:
        async with _detail_client(mock_athena) as client:
            resp = await client.get("/ui/ticket/IR9079436/details")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "NoneType" not in resp.text
    assert "IR9079436" in resp.text


def test_extract_rich_detail_none_returns_dict_not_crash():
    """_extract_rich_ticket_detail must tolerate a None raw ticket."""
    detail = _extract_rich_ticket_detail(None, "IR9079436")
    assert detail["id"] == "IR9079436"
    # No data available -> all optional fields None, no exception.
    assert detail["title"] is None
    assert detail["location"] is None


@pytest.mark.asyncio
async def test_detail_int_priority_renders_not_error():
    """An IR ticket with integer priority must render without the error alert."""
    mock_athena = AsyncMock()
    mock_athena.get_ticket.return_value = {
        "title": "Printer jam",
        "description": "3rd floor printer",
        "priority": 2,
        "status": {"name": "Active"},
        "supportGroup": {"name": "EUS\\HUP"},
    }
    try:
        async with _detail_client(mock_athena) as client:
            resp = await client.get("/ui/ticket/IR100/details")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "alert-error" not in resp.text
    assert "Printer jam" in resp.text
