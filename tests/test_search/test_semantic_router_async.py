"""
Phase 3 — Async router/contract tests for the semantic search endpoints.

The existing tests/test_search/test_search_router.py uses the SYNC
fastapi.testclient.TestClient, which is broken in this environment
(StarletteDeprecationWarning -> httpx incompatibility). These tests use
httpx.ASGITransport + AsyncClient (httpx is already a project dependency)
to exercise the ASGI app directly, without the broken TestClient.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.dependencies import get_search_service
from src.main import app
from src.models.search import (
    DocumentationResult,
    SemanticSearchResponse,
    SimilarTicketResponse,
    SimilarTicketResult,
)
from src.services.ticket_search import TicketSearchService


@pytest.fixture
def mock_search_service() -> AsyncMock:
    service = AsyncMock(spec=TicketSearchService)
    service.semantic_search.return_value = SemanticSearchResponse(
        similar_tickets=[], documentation=[]
    )
    service.find_similar_tickets.return_value = SimilarTicketResponse(
        source_ticket_id="IR0000000", similar_tickets=[]
    )
    return service


@pytest.fixture(autouse=True)
def _bypass_auth():
    """Bypass the AuthMiddleware by returning a stub authenticated user.

    The middleware calls src.main.get_current_user; patching it to return a
    truthy stub lets protected routes execute without a real session.
    """
    with patch("src.main.get_current_user", return_value=object()):
        yield


@pytest.fixture
def override_service(mock_search_service):
    app.dependency_overrides[get_search_service] = lambda: mock_search_service
    yield mock_search_service
    app.dependency_overrides.clear()


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ── POST /search/semantic ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_valid_returns_200(override_service):
    override_service.semantic_search.return_value = SemanticSearchResponse(
        similar_tickets=[SimilarTicketResult(id="IR1", title="Printer", similarity=0.95)],
        documentation=[
            DocumentationResult(
                content="steps", notebook="uphs_notebook",
                section="Printers", title="Guide", similarity=0.9,
            )
        ],
    )
    async with _client() as client:
        resp = await client.post("/search/semantic", json={"query": "printer down"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["similar_tickets"][0]["id"] == "IR1"
    assert data["similar_tickets"][0]["title"] == "Printer"
    assert data["documentation"][0]["title"] == "Guide"


@pytest.mark.asyncio
async def test_semantic_missing_query_422(override_service):
    async with _client() as client:
        resp = await client.post("/search/semantic", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("top_k", [0, 51, 100])
async def test_semantic_top_k_out_of_bounds_422(override_service, top_k):
    async with _client() as client:
        resp = await client.post("/search/semantic", json={"query": "x", "top_k": top_k})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_semantic_custom_top_k_passed_through(override_service):
    async with _client() as client:
        resp = await client.post("/search/semantic", json={"query": "vpn", "top_k": 20})
    assert resp.status_code == 200
    override_service.semantic_search.assert_awaited_once_with(query="vpn", top_k=20)


# ── POST /search/similar/{ticket_id} ─────────────────────────────────


@pytest.mark.asyncio
async def test_similar_valid_returns_200(override_service):
    override_service.find_similar_tickets.return_value = SimilarTicketResponse(
        source_ticket_id="IR100",
        similar_tickets=[SimilarTicketResult(id="IR101", similarity=0.9)],
    )
    async with _client() as client:
        resp = await client.post("/search/similar/IR100")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source_ticket_id"] == "IR100"
    assert data["similar_tickets"][0]["id"] == "IR101"


@pytest.mark.asyncio
async def test_similar_not_found_returns_404(override_service):
    override_service.find_similar_tickets.side_effect = ValueError(
        "Ticket 'IR9999999' not found in Athena."
    )
    async with _client() as client:
        resp = await client.post("/search/similar/IR9999999")
    assert resp.status_code == 404
    assert "not found in Athena" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_similar_default_top_k(override_service):
    async with _client() as client:
        resp = await client.post("/search/similar/IR100")
    assert resp.status_code == 200
    override_service.find_similar_tickets.assert_awaited_once_with(
        ticket_id="IR100", top_k=10
    )
