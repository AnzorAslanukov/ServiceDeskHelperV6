"""
Unit tests for LocalVectorStore site/notebook filtering and result enrichment.

Covers the Feature #2/#3 additions:
- ``find_similar_documentation(..., notebook=...)`` same-notebook preference with
  backfill from the other notebook.
- ``find_similar_by_embedding`` enriched fields (title, support_group, location).

These tests build a tiny in-memory store by setting the private arrays directly,
which avoids depending on on-disk vector data.
"""

import numpy as np

from src.services.local_vector_store import LocalVectorStore


def _make_store_with_docs() -> LocalVectorStore:
    """Build a store with 3 UPHS docs and 2 LGH docs (unit-norm embeddings)."""
    store = LocalVectorStore()
    # 5 docs in 3-dim space; the query [1,0,0] ranks doc0 highest, etc.
    embeddings = np.array(
        [
            [1.0, 0.0, 0.0],   # doc0 UPHS  (best match)
            [0.9, 0.1, 0.0],   # doc1 LGH
            [0.8, 0.2, 0.0],   # doc2 UPHS
            [0.2, 0.8, 0.0],   # doc3 LGH
            [0.0, 1.0, 0.0],   # doc4 UPHS
        ],
        dtype=np.float32,
    )
    store._doc_embeddings = embeddings
    store._doc_norms = np.linalg.norm(embeddings, axis=1)
    store._doc_norms[store._doc_norms == 0] = 1.0
    store._doc_metadata = [
        {"content": "c0", "notebook": "uphs_notebook", "section": "s0", "title": "t0"},
        {"content": "c1", "notebook": "lgh_notebook", "section": "s1", "title": "t1"},
        {"content": "c2", "notebook": "uphs_notebook", "section": "s2", "title": "t2"},
        {"content": "c3", "notebook": "lgh_notebook", "section": "s3", "title": "t3"},
        {"content": "c4", "notebook": "uphs_notebook", "section": "s4", "title": "t4"},
    ]
    return store


def _make_store_with_tickets() -> LocalVectorStore:
    embeddings = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    store = LocalVectorStore()
    store._ticket_embeddings = embeddings
    store._ticket_norms = np.linalg.norm(embeddings, axis=1)
    store._ticket_metadata = [
        {
            "Id": "IR1000001",
            "Title": "Printer down",
            "Description": "desc",
            "SupportGroup": "EUS\\HUP",
            "Location": "HUP\\Floor 3",
        },
        {
            "Id": "IR1000002",
            "Title": "Epic access",
            "Description": "desc",
            "SupportGroup": "LGH\\Epic",
            "Location": "LGH\\Main",
        },
    ]
    return store


# ── find_similar_documentation notebook filter ────────────────────────


def test_docs_no_notebook_returns_global_ranking():
    store = _make_store_with_docs()
    results = store.find_similar_documentation([1.0, 0.0, 0.0], top_k=3)
    assert [r["title"] for r in results] == ["t0", "t1", "t2"]


def test_docs_notebook_filter_prefers_same_notebook():
    """With a notebook filter, same-notebook docs are preferred (ranked)."""
    store = _make_store_with_docs()
    results = store.find_similar_documentation(
        [1.0, 0.0, 0.0], top_k=3, notebook="lgh_notebook"
    )
    # Only two LGH docs exist (t1, t3); they should come first, ranked.
    assert results[0]["title"] == "t1"
    assert results[1]["title"] == "t3"
    assert results[0]["notebook"] == "lgh_notebook"
    assert results[1]["notebook"] == "lgh_notebook"


def test_docs_notebook_filter_backfills_when_insufficient():
    """When same-notebook docs < top_k, backfill from the other notebook."""
    store = _make_store_with_docs()
    results = store.find_similar_documentation(
        [1.0, 0.0, 0.0], top_k=3, notebook="lgh_notebook"
    )
    assert len(results) == 3
    # First two are LGH; the third is backfilled from UPHS (highest remaining).
    assert results[0]["notebook"] == "lgh_notebook"
    assert results[1]["notebook"] == "lgh_notebook"
    assert results[2]["notebook"] == "uphs_notebook"
    assert results[2]["title"] == "t0"  # best-ranked UPHS doc


def test_docs_empty_store_returns_empty():
    store = LocalVectorStore()
    assert store.find_similar_documentation([1.0, 0.0, 0.0], top_k=3) == []


# ── find_similar_by_embedding enrichment ──────────────────────────────


def test_tickets_results_are_enriched():
    store = _make_store_with_tickets()
    results = store.find_similar_by_embedding([1.0, 0.0, 0.0], top_k=2)

    top = results[0]
    assert top["id"] == "IR1000001"
    assert top["title"] == "Printer down"
    assert top["support_group"] == "EUS\\HUP"
    assert top["location"] == "HUP\\Floor 3"
    assert "similarity" in top


def test_tickets_empty_store_returns_empty():
    store = LocalVectorStore()
    assert store.find_similar_by_embedding([1.0, 0.0, 0.0], top_k=2) == []
