"""
Router-level tests for the search API endpoints.

Uses FastAPI TestClient with mocked service dependencies to verify:
- Request validation (Pydantic models)
- HTTP status codes
- Response shapes
- Dependency injection wiring
- Error handling (404 for missing embeddings)
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.dependencies import get_search_service
from src.models.search import (
    FieldSearchResponse,
    SemanticSearchResponse,
    SimilarTicketResponse,
    SimilarTicketResult,
    TicketSummary,
    DocumentationResult,
)
from src.services.ticket_search import TicketSearchService


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_search_service() -> AsyncMock:
    """Create a mock TicketSearchService."""
    service = AsyncMock(spec=TicketSearchService)
    # Default returns
    service.search_by_field.return_value = FieldSearchResponse(
        tickets=[], total=0, page=1, page_size=50, has_more=False,
    )
    service.search_by_description.return_value = FieldSearchResponse(
        tickets=[], total=0, page=1, page_size=50, has_more=False,
    )
    service.semantic_search.return_value = SemanticSearchResponse(
        similar_tickets=[], documentation=[]
    )
    service.find_similar_tickets.return_value = SimilarTicketResponse(
        source_ticket_id="IR0000000", similar_tickets=[]
    )
    return service


@pytest.fixture
def client(mock_search_service) -> TestClient:
    """Create a TestClient with the mock service injected."""
    app.dependency_overrides[get_search_service] = lambda: mock_search_service
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Health Check ──────────────────────────────────────────────────────


def test_health_check(client):
    """GET /health should return 200 with status ok."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ── POST /search/field ────────────────────────────────────────────────


def test_field_search_valid_request(client, mock_search_service):
    """Valid field search request should return 200."""
    mock_search_service.search_by_field.return_value = FieldSearchResponse(
        tickets=[
            TicketSummary(id="IR1959493", title="Printer issue", status="Active")
        ],
        total=1,
        page=1,
        page_size=50,
        has_more=False,
    )

    response = client.post("/search/field", json={
        "field": "contactMethod",
        "value": "215-555-1234",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["page"] == 1
    assert data["page_size"] == 50
    assert data["has_more"] is False
    assert data["tickets"][0]["id"] == "IR1959493"
    assert data["tickets"][0]["title"] == "Printer issue"


def test_field_search_with_all_params(client, mock_search_service):
    """Field search with all optional params should pass them to the service."""
    response = client.post("/search/field", json={
        "field": "title",
        "value": "printer",
        "operator": "contains",
        "ticket_type": "servicerequest",
        "page": 2,
        "page_size": 25,
    })

    assert response.status_code == 200
    mock_search_service.search_by_field.assert_called_once_with(
        field="title",
        value="printer",
        ticket_type="servicerequest",
        operator="contains",
        page=2,
        page_size=25,
    )


def test_field_search_missing_required_field(client):
    """Field search without required 'field' param should return 422."""
    response = client.post("/search/field", json={
        "value": "215-555-1234",
    })
    assert response.status_code == 422


def test_field_search_missing_required_value(client):
    """Field search without required 'value' param should return 422."""
    response = client.post("/search/field", json={
        "field": "contactMethod",
    })
    assert response.status_code == 422


def test_field_search_invalid_ticket_type(client):
    """Field search with invalid ticket_type should return 422."""
    response = client.post("/search/field", json={
        "field": "contactMethod",
        "value": "215-555-1234",
        "ticket_type": "changerequest",
    })
    assert response.status_code == 422


def test_field_search_empty_results(client, mock_search_service):
    """Field search with no matches should return 200 with empty list."""
    response = client.post("/search/field", json={
        "field": "contactMethod",
        "value": "000-000-0000",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["tickets"] == []
    assert data["has_more"] is False


# ── POST /search/description ─────────────────────────────────────────


def test_description_search_valid_request(client, mock_search_service):
    """Valid description search should return 200."""
    mock_search_service.search_by_description.return_value = FieldSearchResponse(
        tickets=[
            TicketSummary(id="IR1959494", title="Monitor flickering")
        ],
        total=1,
        page=1,
        page_size=50,
        has_more=False,
    )

    response = client.post("/search/description", json={
        "text": "monitor flickering",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["page"] == 1
    assert data["tickets"][0]["id"] == "IR1959494"


def test_description_search_missing_text(client):
    """Description search without 'text' should return 422."""
    response = client.post("/search/description", json={})
    assert response.status_code == 422


def test_description_search_with_ticket_type(client, mock_search_service):
    """Description search should pass ticket_type and pagination to service."""
    response = client.post("/search/description", json={
        "text": "access request",
        "ticket_type": "servicerequest",
        "page": 3,
        "page_size": 100,
    })

    assert response.status_code == 200
    mock_search_service.search_by_description.assert_called_once_with(
        text="access request",
        ticket_type="servicerequest",
        page=3,
        page_size=100,
    )


# ── POST /search/semantic ────────────────────────────────────────────


def test_semantic_search_valid_request(client, mock_search_service):
    """Valid semantic search should return 200 with tickets and docs."""
    mock_search_service.semantic_search.return_value = SemanticSearchResponse(
        similar_tickets=[
            SimilarTicketResult(id="IR1959100", similarity=0.95),
            SimilarTicketResult(id="IR1959101", similarity=0.91),
        ],
        documentation=[
            DocumentationResult(
                content="Troubleshooting steps...",
                notebook="uphs_notebook",
                section="Helpdesk Printer Issues",
                title="Printer Guide",
                similarity=0.92,
            )
        ],
    )

    response = client.post("/search/semantic", json={
        "query": "printer not working on 3rd floor",
    })

    assert response.status_code == 200
    data = response.json()
    assert len(data["similar_tickets"]) == 2
    assert data["similar_tickets"][0]["id"] == "IR1959100"
    assert data["similar_tickets"][0]["similarity"] == 0.95
    assert len(data["documentation"]) == 1
    assert data["documentation"][0]["title"] == "Printer Guide"


def test_semantic_search_missing_query(client):
    """Semantic search without 'query' should return 422."""
    response = client.post("/search/semantic", json={})
    assert response.status_code == 422


def test_semantic_search_custom_top_k(client, mock_search_service):
    """Semantic search should pass top_k to service."""
    response = client.post("/search/semantic", json={
        "query": "VPN not connecting",
        "top_k": 20,
    })

    assert response.status_code == 200
    mock_search_service.semantic_search.assert_called_once_with(
        query="VPN not connecting",
        top_k=20,
    )


def test_semantic_search_top_k_too_high(client):
    """Semantic search with top_k > 50 should return 422."""
    response = client.post("/search/semantic", json={
        "query": "test",
        "top_k": 100,
    })
    assert response.status_code == 422


def test_semantic_search_top_k_too_low(client):
    """Semantic search with top_k < 1 should return 422."""
    response = client.post("/search/semantic", json={
        "query": "test",
        "top_k": 0,
    })
    assert response.status_code == 422


# ── POST /search/similar/{ticket_id} ─────────────────────────────────


def test_similar_tickets_valid_request(client, mock_search_service):
    """Valid similar ticket request should return 200."""
    mock_search_service.find_similar_tickets.return_value = SimilarTicketResponse(
        source_ticket_id="IR1959493",
        similar_tickets=[
            SimilarTicketResult(id="IR1959100", similarity=0.95),
        ],
    )

    response = client.post("/search/similar/IR1959493")

    assert response.status_code == 200
    data = response.json()
    assert data["source_ticket_id"] == "IR1959493"
    assert len(data["similar_tickets"]) == 1


def test_similar_tickets_with_top_k(client, mock_search_service):
    """Similar tickets should accept top_k in request body."""
    response = client.post("/search/similar/IR1959493", json={"top_k": 5})

    assert response.status_code == 200
    mock_search_service.find_similar_tickets.assert_called_once_with(
        ticket_id="IR1959493",
        top_k=5,
    )


def test_similar_tickets_not_found_returns_404(client, mock_search_service):
    """Similar tickets for unknown ticket should return 404."""
    mock_search_service.find_similar_tickets.side_effect = ValueError(
        "Ticket 'IR9999999' not found in Athena."
    )

    response = client.post("/search/similar/IR9999999")

    assert response.status_code == 404
    assert "not found in Athena" in response.json()["detail"]


def test_similar_tickets_default_top_k(client, mock_search_service):
    """Similar tickets without body should use default top_k=10."""
    response = client.post("/search/similar/IR1959493")

    assert response.status_code == 200
    mock_search_service.find_similar_tickets.assert_called_once_with(
        ticket_id="IR1959493",
        top_k=10,
    )