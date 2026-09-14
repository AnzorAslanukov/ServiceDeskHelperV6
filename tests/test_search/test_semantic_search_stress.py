"""
Phase 1 — Service-layer adversarial ("test-to-failure") tests for Feature #1
semantic search (Mode 3: semantic_search) and ticket similarity (Mode 4:
find_similar_tickets).

These tests deliberately feed malformed / boundary / hostile inputs through
the TicketSearchService with mocked clients + vector store, to find the point
at which the service crashes or silently returns broken data.
"""

import math
from unittest.mock import AsyncMock

import pytest

from src.services.ticket_search import TicketSearchService


# ── Mode 3: Semantic search — adversarial query strings ───────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("a", id="single_char"),
        pytest.param("12345", id="numeric"),
        pytest.param("x" * 100_000, id="very_long"),
        pytest.param("🚨🔥 émoji ünïcode RTL \u202e text", id="unicode_rtl"),
        pytest.param("line1\nline2\tcol\x00null", id="control_chars"),
        pytest.param("'; DROP TABLE tickets;--", id="sql_injection"),
        pytest.param("<script>alert(1)</script>", id="html_injection"),
    ],
)
async def test_semantic_search_tolerates_adversarial_queries(
    search_service: TicketSearchService,
    mock_databricks_client,
    mock_vector_store,
    query,
):
    """Semantic search must not crash on hostile/edge query strings."""
    mock_vector_store.find_similar_by_embedding.return_value = []
    mock_vector_store.find_similar_documentation.return_value = []

    result = await search_service.semantic_search(query=query, top_k=5)

    assert result.similar_tickets == []
    assert result.documentation == []
    mock_databricks_client.generate_embedding.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("top_k", [0, -1, 1, 51, 1000])
async def test_semantic_search_top_k_boundaries_do_not_crash(
    search_service: TicketSearchService,
    mock_vector_store,
    top_k,
):
    """Service layer (bypassing Pydantic bounds) must not crash for extreme top_k."""
    mock_vector_store.find_similar_by_embedding.return_value = []
    mock_vector_store.find_similar_documentation.return_value = []

    result = await search_service.semantic_search(query="printer", top_k=top_k)



# ── Mode 3: malformed vector-store return shapes ──────────────────────


@pytest.mark.asyncio
async def test_semantic_search_missing_similarity_key_is_dropped(
    search_service: TicketSearchService,
    mock_vector_store,
):
    """A row missing 'similarity' must be dropped, keeping valid rows."""
    mock_vector_store.find_similar_by_embedding.return_value = [
        {"id": "IR_BAD"},                       # no similarity → dropped
        {"id": "IR_GOOD", "similarity": 0.8},   # valid → kept
    ]
    mock_vector_store.find_similar_documentation.return_value = []

    result = await search_service.semantic_search(query="printer", top_k=5)
    ids = [t.id for t in result.similar_tickets]
    assert ids == ["IR_GOOD"]


@pytest.mark.asyncio
async def test_semantic_search_missing_id_key_is_dropped(
    search_service: TicketSearchService,
    mock_vector_store,
):
    """A row missing 'id' must be dropped, keeping valid rows."""
    mock_vector_store.find_similar_by_embedding.return_value = [
        {"similarity": 0.9},                    # no id → dropped
        {"id": "IR_GOOD", "similarity": 0.7},   # valid → kept
    ]
    mock_vector_store.find_similar_documentation.return_value = []

    result = await search_service.semantic_search(query="printer", top_k=5)
    ids = [t.id for t in result.similar_tickets]
    assert ids == ["IR_GOOD"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_sim", [float("nan"), float("inf"), float("-inf")])
async def test_semantic_search_nonfinite_similarity_is_rejected(
    search_service: TicketSearchService,
    mock_vector_store,
    bad_sim,
):
    """NaN/inf similarity must not silently flow into results.

    The bad row is dropped; any surviving rows must have finite similarity.
    """
    mock_vector_store.find_similar_by_embedding.return_value = [
        {"id": "IR_BAD", "similarity": bad_sim},   # dropped
        {"id": "IR_GOOD", "similarity": 0.5},      # kept
    ]
    mock_vector_store.find_similar_documentation.return_value = []

    result = await search_service.semantic_search(query="printer", top_k=5)

    ids = [t.id for t in result.similar_tickets]
    assert "IR_BAD" not in ids
    assert ids == ["IR_GOOD"]
    for t in result.similar_tickets:
        assert math.isfinite(t.similarity), (
            f"Non-finite similarity {t.similarity!r} leaked into results"
        )


@pytest.mark.asyncio
async def test_semantic_search_none_id_is_rejected(
    search_service: TicketSearchService,
    mock_vector_store,
):
    """An id of None must not render (breaks JS onclick + copy button).

    The None-id row is dropped rather than crashing with a ValidationError.
    """
    mock_vector_store.find_similar_by_embedding.return_value = [
        {"id": None, "similarity": 0.9},          # dropped
        {"id": "", "similarity": 0.8},            # dropped (empty)
        {"id": "IR_GOOD", "similarity": 0.7},     # kept
    ]
    mock_vector_store.find_similar_documentation.return_value = []

    result = await search_service.semantic_search(query="printer", top_k=5)

    ids = [t.id for t in result.similar_tickets]
    assert ids == ["IR_GOOD"]
    for t in result.similar_tickets:
        assert t.id, "A result with empty/None id must not be returned"


@pytest.mark.asyncio
async def test_semantic_search_empty_embedding_does_not_crash(
    search_service: TicketSearchService,
    mock_databricks_client,
    mock_vector_store,
):
    """A zero-length embedding from Databricks should not crash the service."""
    mock_databricks_client.generate_embedding = AsyncMock(return_value=[])
    mock_vector_store.find_similar_by_embedding.return_value = []
    mock_vector_store.find_similar_documentation.return_value = []

    result = await search_service.semantic_search(query="printer", top_k=5)
    assert result.similar_tickets == []



# ── Mode 3: title enrichment ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_search_enrichment_partial_failure_keeps_other_titles(
    search_service: TicketSearchService,
    mock_athena_client,
    mock_vector_store,
):
    """If Athena fails for one ticket, others should still get titles.

    Also verifies similarity ordering is preserved after enrichment.
    """
    mock_vector_store.find_similar_by_embedding.return_value = [
        {"id": "IR1", "similarity": 0.95},
        {"id": "IR2", "similarity": 0.90},
        {"id": "IR3", "similarity": 0.85},
    ]
    mock_vector_store.find_similar_documentation.return_value = []

    async def fake_get_ticket(ticket_id):
        if ticket_id == "IR2":
            raise RuntimeError("Athena down for IR2")
        return {"title": f"Title {ticket_id}"}

    mock_athena_client.get_ticket = AsyncMock(side_effect=fake_get_ticket)

    result = await search_service.semantic_search(query="printer", top_k=3)

    titles = {t.id: t.title for t in result.similar_tickets}
    assert titles["IR1"] == "Title IR1"
    assert titles["IR3"] == "Title IR3"
    assert titles["IR2"] is None  # failed enrichment → None, not a crash
    sims = [t.similarity for t in result.similar_tickets]
    assert sims == sorted(sims, reverse=True)


@pytest.mark.asyncio
async def test_semantic_search_all_titles_none_is_detectable(
    search_service: TicketSearchService,
    mock_athena_client,
    mock_vector_store,
):
    """Systemic enrichment failure: EVERY title is None (degraded rendering)."""
    mock_vector_store.find_similar_by_embedding.return_value = [
        {"id": "IR1", "similarity": 0.95},
        {"id": "IR2", "similarity": 0.90},
    ]
    mock_vector_store.find_similar_documentation.return_value = []
    mock_athena_client.get_ticket = AsyncMock(side_effect=RuntimeError("down"))

    result = await search_service.semantic_search(query="printer", top_k=2)

    assert len(result.similar_tickets) == 2
    assert all(t.title is None for t in result.similar_tickets)


# ── Mode 4: ticket similarity ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_similar_not_found_raises_value_error(
    search_service: TicketSearchService,
    mock_athena_client,
):
    """Unknown ticket → ValueError (router maps to 404)."""
    mock_athena_client.get_ticket = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="not found"):
        await search_service.find_similar_tickets(ticket_id="IR9999999", top_k=5)


@pytest.mark.asyncio
async def test_find_similar_empty_content_raises_value_error(
    search_service: TicketSearchService,
    mock_athena_client,
):
    """Ticket with no title/description → ValueError (nothing to embed)."""
    mock_athena_client.get_ticket = AsyncMock(
        return_value={"id": "IR1", "title": "", "description": ""}
    )
    with pytest.raises(ValueError, match="no title or description"):
        await search_service.find_similar_tickets(ticket_id="IR1", top_k=5)


@pytest.mark.asyncio
async def test_find_similar_excludes_source_ticket(
    search_service: TicketSearchService,
    mock_athena_client,
    mock_vector_store,
):
    """The source ticket must never appear in its own similar results."""
    mock_athena_client.get_ticket = AsyncMock(
        return_value={"id": "IR100", "title": "Printer", "description": "jam"}
    )
    mock_vector_store.find_similar_by_embedding.return_value = [
        {"id": "IR100", "similarity": 1.0},   # itself
        {"id": "IR101", "similarity": 0.9},
        {"id": "IR102", "similarity": 0.8},
    ]

    result = await search_service.find_similar_tickets(ticket_id="IR100", top_k=2)

    ids = [t.id for t in result.similar_tickets]
    assert "IR100" not in ids
    assert ids == ["IR101", "IR102"]
    assert result.source_ticket_id == "IR100"


@pytest.mark.asyncio
async def test_find_similar_corpus_smaller_than_top_k(
    search_service: TicketSearchService,
    mock_athena_client,
    mock_vector_store,
):
    """Requesting more neighbors than exist should just return what's available."""
    mock_athena_client.get_ticket = AsyncMock(
        return_value={"id": "IR100", "title": "Printer", "description": "jam"}
    )
    mock_vector_store.find_similar_by_embedding.return_value = [
        {"id": "IR100", "similarity": 1.0},
        {"id": "IR101", "similarity": 0.9},
    ]

    result = await search_service.find_similar_tickets(ticket_id="IR100", top_k=50)
    ids = [t.id for t in result.similar_tickets]
    assert ids == ["IR101"]


# ── Phase 4: fuzz — invariants over random result sets ────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [1, 7, 42, 99, 2026])
async def test_semantic_search_invariants_over_random_results(
    search_service: TicketSearchService,
    mock_vector_store,
    seed,
):
    """For random (valid) vector-store rows, results must obey invariants:

    - never raise
    - every result has a finite similarity and non-empty id
    - count never exceeds the number of input rows
    """
    import random

    rng = random.Random(seed)
    n = rng.randint(0, 20)
    rows = [
        {"id": f"IR{rng.randint(1, 10_000)}", "similarity": rng.random()}
        for _ in range(n)
    ]
    mock_vector_store.find_similar_by_embedding.return_value = rows
    mock_vector_store.find_similar_documentation.return_value = []

    result = await search_service.semantic_search(query="fuzz", top_k=10)

    assert len(result.similar_tickets) <= len(rows)
    for t in result.similar_tickets:
        assert t.id
        assert math.isfinite(t.similarity)
