"""
Unit tests for the ticket-embeddings refresh pipeline pure helpers
(exploration/refresh_ticket_embeddings.py).

Scope: the fast, deterministic, I/O-light logic — validation invariants,
manifest checksums, current-row-count reading, and the atomic swap/backup
behaviour. The Databricks compute/export and SCP transfer are I/O-bound and
intentionally left to integration testing, consistent with the repo's style.
"""

import json

import numpy as np
import pytest

from exploration import refresh_ticket_embeddings as rte

DIM = rte.EMBEDDING_DIM  # 1024


def _matrix(rows: int, dim: int = DIM) -> np.ndarray:
    """Build a small deterministic float32 matrix of the given shape."""
    return np.ones((rows, dim), dtype=np.float32)


def _metadata(rows: int, with_ids: bool = True) -> list[dict]:
    """Build metadata records aligned to a matrix of `rows` rows."""
    return [
        {
            "Id": f"IR{1000 + i}" if with_ids else "",
            "Title": f"Ticket {i}",
            "Description": "desc",
            "SupportGroup": "Service Desk",
            "Location": "HUP",
        }
        for i in range(rows)
    ]


# ── validate_export ────────────────────────────────────────────────────


def test_validate_export_accepts_aligned_first_build():
    """A valid, aligned pair passes even with no prior file (current=0)."""
    rte.validate_export(_matrix(5), _metadata(5), current_row_count=0)


def test_validate_export_accepts_growth_over_current():
    """Row count above the safety floor of an existing file is accepted."""
    rte.validate_export(_matrix(120), _metadata(120), current_row_count=100)


def test_validate_export_rejects_row_metadata_mismatch():
    with pytest.raises(ValueError, match="Row count mismatch"):
        rte.validate_export(_matrix(5), _metadata(4), current_row_count=0)


def test_validate_export_rejects_wrong_dims():
    with pytest.raises(ValueError, match="Embedding dimension"):
        rte.validate_export(_matrix(5, dim=512), _metadata(5), current_row_count=0)


def test_validate_export_rejects_non_2d():
    with pytest.raises(ValueError, match="2-D"):
        rte.validate_export(
            np.ones(DIM, dtype=np.float32), _metadata(1), current_row_count=0
        )


def test_validate_export_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        rte.validate_export(_matrix(0), _metadata(0), current_row_count=0)


def test_validate_export_rejects_missing_ids():
    with pytest.raises(ValueError, match="missing an 'Id'"):
        rte.validate_export(
            _matrix(3), _metadata(3, with_ids=False), current_row_count=0
        )


def test_validate_export_rejects_below_safety_floor():
    """A shrunken export (below 98% of current) is refused."""
    with pytest.raises(ValueError, match="safety floor"):
        rte.validate_export(_matrix(50), _metadata(50), current_row_count=100)


def test_validate_export_allows_below_floor_with_lower_ratio():
    """An explicit lower ratio lets an intentional shrink through."""
    rte.validate_export(
        _matrix(50), _metadata(50), current_row_count=100, min_row_ratio=0.1
    )


# ── read_current_row_count ───────────────────────────────────────────────


def test_read_current_row_count_missing_file(tmp_path):
    assert rte.read_current_row_count(tmp_path / "nope.json") == 0


def test_read_current_row_count_valid(tmp_path):
    p = tmp_path / "meta.json"
    p.write_text(json.dumps(_metadata(7)), encoding="utf-8")
    assert rte.read_current_row_count(p) == 7


def test_read_current_row_count_corrupt_file(tmp_path):
    p = tmp_path / "meta.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert rte.read_current_row_count(p) == 0


# ── sha256_of_file / build_manifest ──────────────────────────────────────


def test_sha256_is_stable_and_content_sensitive(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"hello world")
    b.write_bytes(b"hello world")
    assert rte.sha256_of_file(a) == rte.sha256_of_file(b)
    b.write_bytes(b"different")
    assert rte.sha256_of_file(a) != rte.sha256_of_file(b)


def test_build_manifest_shape(tmp_path):
    emb = tmp_path / rte.EMBEDDINGS_NAME
    meta = tmp_path / rte.METADATA_NAME
    np.save(str(emb), _matrix(3))
    meta.write_text(json.dumps(_metadata(3)), encoding="utf-8")

    manifest = rte.build_manifest(emb, meta, rows=3, dims=DIM)
    assert manifest["rows"] == 3
    assert manifest["dims"] == DIM
    assert len(manifest["sha256_npy"]) == 64
    assert len(manifest["sha256_meta"]) == 64
    assert "built_at" in manifest


# ── atomic_swap_with_backup ──────────────────────────────────────────────


def test_atomic_swap_first_time_no_backup(tmp_path):
    live = tmp_path / "live.txt"
    tmp = tmp_path / "live.txt.tmp"
    tmp.write_text("new", encoding="utf-8")

    backup = rte.atomic_swap_with_backup(tmp, live)

    assert backup is None
    assert live.read_text(encoding="utf-8") == "new"
    assert not tmp.exists()


def test_atomic_swap_backs_up_existing(tmp_path):
    live = tmp_path / "live.txt"
    tmp = tmp_path / "live.txt.tmp"
    live.write_text("old", encoding="utf-8")
    tmp.write_text("new", encoding="utf-8")

    backup = rte.atomic_swap_with_backup(tmp, live)

    assert backup is not None
    assert backup.read_text(encoding="utf-8") == "old"
    assert live.read_text(encoding="utf-8") == "new"
    assert not tmp.exists()


def test_atomic_swap_roundtrip_preserves_alignment(tmp_path):
    """Swapping both files together keeps .npy row count == metadata length."""
    live_emb = tmp_path / rte.EMBEDDINGS_NAME
    live_meta = tmp_path / rte.METADATA_NAME
    tmp_emb = tmp_path / (rte.EMBEDDINGS_NAME + ".tmp")
    tmp_meta = tmp_path / (rte.METADATA_NAME + ".tmp")

    # np.save appends ".npy" unless the path already ends in it; write to a
    # file handle so the exact "*.npy.tmp" path is used.
    with open(tmp_emb, "wb") as fh:
        np.save(fh, _matrix(6))
    tmp_meta.write_text(json.dumps(_metadata(6)), encoding="utf-8")

    rte.atomic_swap_with_backup(tmp_meta, live_meta)
    rte.atomic_swap_with_backup(tmp_emb, live_emb)

    loaded = np.load(str(live_emb))
    meta = json.loads(live_meta.read_text(encoding="utf-8"))
    assert loaded.shape[0] == len(meta) == 6
    assert loaded.shape[1] == DIM

