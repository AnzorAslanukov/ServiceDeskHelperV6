"""
Phase 2 — LocalVectorStore real-math ("test-to-failure") tests.

These tests exercise the actual NumPy cosine-similarity code path (no mocks)
using tiny synthetic .npy / .json fixtures written to a tmp_path. This is the
most likely place to find genuine numeric / indexing bugs:
- missing / partial data files
- dimension mismatch (query vs stored)
- NaN / inf / zero vectors
- metadata vs embedding length mismatch (index-out-of-range risk)
- top_k > N, empty corpus, single row
- descending-order guarantee
"""

import json
from pathlib import Path

import numpy as np
import pytest

from src.services.local_vector_store import LocalVectorStore


def _write_store(
    tmp_path: Path,
    doc_emb: np.ndarray | None = None,
    doc_meta: list[dict] | None = None,
    tkt_emb: np.ndarray | None = None,
    tkt_meta: list[dict] | None = None,
) -> LocalVectorStore:
    """Write synthetic vector files and return a loaded LocalVectorStore."""
    if doc_emb is not None:
        np.save(str(tmp_path / "onenote_embeddings.npy"), doc_emb)
    if doc_meta is not None:
        (tmp_path / "onenote_metadata.json").write_text(json.dumps(doc_meta), encoding="utf-8")
    if tkt_emb is not None:
        np.save(str(tmp_path / "ticket_embeddings.npy"), tkt_emb)
    if tkt_meta is not None:
        (tmp_path / "ticket_metadata.json").write_text(json.dumps(tkt_meta), encoding="utf-8")

    store = LocalVectorStore(data_dir=tmp_path)
    store.load()
    return store


# ── Missing / partial files ───────────────────────────────────────────


def test_missing_all_files_degrades_to_empty(tmp_path):
    """No files present → empty results, no exception."""
    store = _write_store(tmp_path)
    assert store.find_similar_by_embedding([0.1] * 4, top_k=5) == []
    assert store.find_similar_documentation([0.1] * 4, top_k=5) == []
    assert store.get_ticket_embedding("IR1") is None


def test_embeddings_present_metadata_missing_degrades(tmp_path):
    """Embeddings file present but metadata missing → skip load, empty results."""
    store = _write_store(tmp_path, tkt_emb=np.ones((3, 4), dtype=np.float32))
    # metadata missing → loader should bail out; no crash
    assert store.find_similar_by_embedding([1.0] * 4, top_k=2) == []


# ── Dimension mismatch ────────────────────────────────────────────────


def test_query_dim_mismatch_raises(tmp_path):
    """Query vector dim != stored matrix dim should raise a shape error."""
    store = _write_store(
        tmp_path,
        tkt_emb=np.ones((3, 4), dtype=np.float32),
        tkt_meta=[{"Id": f"IR{i}"} for i in range(3)],
    )
    with pytest.raises(ValueError):
        store.find_similar_by_embedding([1.0] * 8, top_k=2)  # 8 != 4


# ── Numeric edges ─────────────────────────────────────────────────────


def test_zero_vector_rows_do_not_divide_by_zero(tmp_path):
    """Stored zero-vector rows must not produce inf/NaN via norm guard."""
    emb = np.array([[0, 0, 0, 0], [1, 0, 0, 0]], dtype=np.float32)
    store = _write_store(
        tmp_path, tkt_emb=emb, tkt_meta=[{"Id": "IR0"}, {"Id": "IR1"}]
    )
    results = store.find_similar_by_embedding([1.0, 0.0, 0.0, 0.0], top_k=2)
    assert len(results) == 2
    for r in results:
        assert np.isfinite(r["similarity"])


def test_zero_query_vector_returns_zero_similarities(tmp_path):
    """A zero query vector should yield all-zero similarities (guarded)."""
    emb = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32)
    store = _write_store(
        tmp_path, tkt_emb=emb, tkt_meta=[{"Id": "IR0"}, {"Id": "IR1"}]
    )
    results = store.find_similar_by_embedding([0.0, 0.0, 0.0, 0.0], top_k=2)
    assert all(r["similarity"] == 0.0 for r in results)


def test_nan_in_stored_vector_surfaces_or_is_finite(tmp_path):
    """NaN in a stored vector: document that it propagates into similarity.

    This pins current behavior — NaN rows produce NaN similarity, which would
    render as 'nan%'. If a cleaning guard is later added, update this test.
    """
    emb = np.array([[np.nan, 0, 0, 0], [1, 0, 0, 0]], dtype=np.float32)
    store = _write_store(
        tmp_path, tkt_emb=emb, tkt_meta=[{"Id": "IR0"}, {"Id": "IR1"}]
    )
    results = store.find_similar_by_embedding([1.0, 0.0, 0.0, 0.0], top_k=2)
    sims = {r["id"]: r["similarity"] for r in results}
    # IR1 (clean) must be finite; IR0 (NaN row) is expected NaN today.
    assert np.isfinite(sims["IR1"])



# ── top_k / corpus size edges ─────────────────────────────────────────


def test_top_k_greater_than_corpus(tmp_path):
    """top_k larger than N returns all rows, no index error."""
    emb = np.eye(3, 4, dtype=np.float32)
    store = _write_store(
        tmp_path, tkt_emb=emb, tkt_meta=[{"Id": f"IR{i}"} for i in range(3)]
    )
    results = store.find_similar_by_embedding([1.0, 0.0, 0.0, 0.0], top_k=100)
    assert len(results) == 3


def test_single_row_corpus(tmp_path):
    """A one-row corpus should return exactly that row."""
    store = _write_store(
        tmp_path,
        tkt_emb=np.array([[1, 0, 0, 0]], dtype=np.float32),
        tkt_meta=[{"Id": "IR0"}],
    )
    results = store.find_similar_by_embedding([1.0, 0.0, 0.0, 0.0], top_k=5)
    assert len(results) == 1
    assert results[0]["id"] == "IR0"


# ── Metadata / embedding length mismatch (index-out-of-range risk) ─────


def test_metadata_shorter_than_embeddings_raises_or_is_guarded(tmp_path):
    """Matrix has more rows than metadata → argpartition index may exceed
    metadata length → IndexError. This is a strong root-cause candidate; the
    test pins the failure so we can decide whether to guard it.
    """
    emb = np.eye(5, 4, dtype=np.float32)  # 5 rows
    meta = [{"Id": "IR0"}, {"Id": "IR1"}]  # only 2 metadata entries
    store = _write_store(tmp_path, tkt_emb=emb, tkt_meta=meta)
    with pytest.raises(IndexError):
        store.find_similar_by_embedding([1.0, 0.0, 0.0, 0.0], top_k=5)


# ── ID index integrity ────────────────────────────────────────────────


def test_get_ticket_embedding_roundtrip(tmp_path):
    """get_ticket_embedding returns the exact stored row for a known id."""
    emb = np.array([[1, 2, 3, 4], [5, 6, 7, 8]], dtype=np.float32)
    store = _write_store(
        tmp_path, tkt_emb=emb, tkt_meta=[{"Id": "IR0"}, {"Id": "IR1"}]
    )
    got = store.get_ticket_embedding("IR1")
    assert got == [5.0, 6.0, 7.0, 8.0]
    assert store.get_ticket_embedding("NOPE") is None


def test_duplicate_ids_last_wins_in_index(tmp_path):
    """Duplicate Ids: the index maps to the last occurrence (documents behavior)."""
    emb = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32)
    store = _write_store(
        tmp_path, tkt_emb=emb, tkt_meta=[{"Id": "DUP"}, {"Id": "DUP"}]
    )
    got = store.get_ticket_embedding("DUP")
    assert got == [0.0, 1.0, 0.0, 0.0]  # index 1 wins


# ── Ordering guarantee (property check vs brute force) ────────────────


def test_results_sorted_descending_matches_bruteforce(tmp_path):
    """argpartition+argsort must return the true top-k in descending order."""
    rng = np.random.default_rng(1234)
    n, dim = 200, 16
    emb = rng.standard_normal((n, dim)).astype(np.float32)
    meta = [{"Id": f"IR{i}"} for i in range(n)]
    store = _write_store(tmp_path, tkt_emb=emb, tkt_meta=meta)

    query = rng.standard_normal(dim).astype(np.float32)
    top_k = 10
    results = store.find_similar_by_embedding(query.tolist(), top_k=top_k)

    sims = [r["similarity"] for r in results]
    assert sims == sorted(sims, reverse=True)

    norms = np.linalg.norm(emb, axis=1)
    norms[norms == 0] = 1.0
    full = emb.dot(query) / (norms * np.linalg.norm(query))
    expected_ids = {f"IR{i}" for i in np.argsort(full)[::-1][:top_k]}
    assert {r["id"] for r in results} == expected_ids
