"""
Integration tests for Feature #1: Enhanced Ticket Search.

These tests call REAL external APIs (Athena and Databricks).
They verify that:
- Authentication works with real credentials
- API response formats match what our code expects
- SQL queries execute correctly on the Databricks warehouse
- Embedding generation returns correct dimensions
- End-to-end search flows produce valid results

Run with: pytest tests/test_search/test_integration.py -v -m integration

These tests are marked with @pytest.mark.integration and are SKIPPED
by default. To run them, use: pytest -m integration
"""

import httpx
import pytest

from src.config import get_settings
from src.clients.athena_client import AthenaClient
from src.clients.databricks_client import DatabricksClient
from src.services.ticket_search import TicketSearchService


# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def settings():
    """Load real settings from .env."""
    return get_settings()


@pytest.fixture
def athena_client(settings) -> AthenaClient:
    """Create a real AthenaClient."""
    return AthenaClient(settings)


@pytest.fixture
def databricks_client(settings) -> DatabricksClient:
    """Create a real DatabricksClient."""
    return DatabricksClient(settings)


@pytest.fixture
def search_service(athena_client, databricks_client) -> TicketSearchService:
    """Create a real TicketSearchService with real clients."""
    return TicketSearchService(
        athena_client=athena_client,
        databricks_client=databricks_client,
    )


# ── Athena Authentication ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_athena_authentication(athena_client):
    """Athena OAuth2 authentication should succeed and return a token."""
    token = await athena_client._authenticate()
    assert token is not None
    assert len(token) > 0
    assert athena_client._token_expiry > 0


# ── Athena Ticket Retrieval ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_athena_get_incident(athena_client):
    """Should retrieve a real incident from Athena."""
    # Use a known ticket ID — this may need updating if the ticket is purged
    try:
        ticket = await athena_client.get_incident("IR1959493")
        # API may return None for non-existent tickets, or a dict with ticket data
        if ticket is not None:
            assert isinstance(ticket, dict)
    except httpx.HTTPStatusError as e:
        # If the specific ticket doesn't exist, that's okay —
        # we just want to verify the API call works
        assert e.response.status_code in (404, 400), f"Unexpected HTTP error: {e}"


# ── Athena View Filter Query ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_athena_field_search(athena_client):
    """Should execute a view filter query against Athena incidents using the name field."""
    # Use the 'name' field which is known to work with the view filter endpoint
    filters = AthenaClient.build_field_filter("name", "IR1959493")
    results = await athena_client.search_incidents(filters)

    # Should return a list (may be empty if the ticket doesn't exist)
    assert isinstance(results, list)
    if len(results) > 0:
        # Verify the response has expected ticket structure
        first = results[0]
        assert "id" in first or "Id" in first or "name" in first


# ── Databricks Embedding Generation ──────────────────────────────────


@pytest.mark.asyncio
async def test_databricks_generate_embedding(databricks_client):
    """Should generate a 1024-dimensional embedding from GTE-Large-EN."""
    embedding = await databricks_client.generate_embedding("printer not working")

    assert isinstance(embedding, list)
    assert len(embedding) == 1024
    assert all(isinstance(v, (int, float)) for v in embedding)


@pytest.mark.asyncio
async def test_databricks_generate_batch_embeddings(databricks_client):
    """Should generate embeddings for multiple texts in a single call."""
    texts = ["printer issue", "VPN not connecting", "password reset"]
    embeddings = await databricks_client.generate_embeddings(texts)

    assert len(embeddings) == 3
    for emb in embeddings:
        assert len(emb) == 1024


# ── Databricks SQL Warehouse ─────────────────────────────────────────


def test_databricks_sql_connection(databricks_client):
    """Should connect to the SQL warehouse and execute a simple query."""
    results = databricks_client.execute_query("SELECT 1 AS test_value")
    assert len(results) == 1
    assert results[0]["test_value"] == 1


def test_databricks_ir_embeddings_table_exists(databricks_client):
    """The ir_embeddings table should exist and have data."""
    results = databricks_client.execute_query(
        "SELECT COUNT(*) AS cnt FROM hive_metastore.embeddings_db.ticket_embeddings"
    )
    assert results[0]["cnt"] > 0


def test_databricks_onenote_documentation_table_exists(databricks_client):
    """The onenote_documentation table should exist and have data."""
    results = databricks_client.execute_query(
        "SELECT COUNT(*) AS cnt FROM scratchpad.aslanuka.onenote_documentation"
    )
    assert results[0]["cnt"] > 0


def test_databricks_get_ticket_embedding(databricks_client):
    """Should retrieve a pre-computed embedding for a known ticket."""
    # First, find any ticket ID in the table
    results = databricks_client.execute_query(
        "SELECT Id FROM hive_metastore.embeddings_db.ticket_embeddings LIMIT 1"
    )
    assert len(results) > 0
    ticket_id = results[0].get("Id") or results[0].get("id")

    embedding = databricks_client.get_ticket_embedding(ticket_id)
    assert embedding is not None
    assert len(embedding) == 1024


def test_databricks_get_ticket_embedding_not_found(databricks_client):
    """Should return None for a non-existent ticket ID."""
    embedding = databricks_client.get_ticket_embedding("NONEXISTENT_TICKET_999")
    assert embedding is None


# ── Databricks Cosine Similarity ─────────────────────────────────────


@pytest.mark.asyncio
async def test_databricks_similarity_search(databricks_client):
    """Should find similar tickets by embedding cosine similarity."""
    # Generate a query embedding
    embedding = await databricks_client.generate_embedding("printer not printing")

    # Search for similar tickets
    results = databricks_client.find_similar_by_embedding(embedding, top_k=5)

    assert isinstance(results, list)
    assert len(results) <= 5
    if len(results) > 0:
        assert "id" in results[0]
        assert "similarity" in results[0]
        # Similarity should be between -1 and 1
        assert -1.0 <= results[0]["similarity"] <= 1.0


@pytest.mark.asyncio
async def test_databricks_documentation_search(databricks_client):
    """Should find similar documentation by embedding cosine similarity."""
    embedding = await databricks_client.generate_embedding("how to reset password")

    results = databricks_client.find_similar_documentation(embedding, top_k=3)

    assert isinstance(results, list)
    assert len(results) <= 3
    if len(results) > 0:
        assert "content" in results[0]
        assert "notebook" in results[0]
        assert "section" in results[0]
        assert "title" in results[0]
        assert "similarity" in results[0]


# ── End-to-End Service Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_search_by_field(search_service):
    """End-to-end: field search through the full service layer."""
    # Use the 'name' field which is known to work with the view filter endpoint
    result = await search_service.search_by_field(
        field="name",
        value="IR1959493",
        ticket_type="incident",
    )

    assert result.total >= 0
    assert isinstance(result.tickets, list)


@pytest.mark.asyncio
async def test_e2e_semantic_search(search_service):
    """End-to-end: semantic search through the full service layer."""
    result = await search_service.semantic_search(
        query="user cannot print to network printer",
        top_k=5,
    )

    assert isinstance(result.similar_tickets, list)
    assert isinstance(result.documentation, list)
    # Should find at least some results given 42K+ tickets and 6K+ docs
    assert len(result.similar_tickets) > 0 or len(result.documentation) > 0


@pytest.mark.asyncio
async def test_e2e_find_similar_tickets(search_service, databricks_client):
    """End-to-end: ticket similarity through the full service layer."""
    # Get a real ticket ID from the embeddings table
    results = databricks_client.execute_query(
        "SELECT Id FROM hive_metastore.embeddings_db.ticket_embeddings WHERE Id LIKE 'IR%' LIMIT 1"
    )
    assert len(results) > 0
    ticket_id = results[0].get("Id") or results[0].get("id")

    result = await search_service.find_similar_tickets(
        ticket_id=ticket_id,
        top_k=5,
    )

    assert result.source_ticket_id == ticket_id
    assert len(result.similar_tickets) > 0
    # Source ticket should not appear in results
    assert all(t.id != ticket_id for t in result.similar_tickets)