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



# ── incremental helpers: parsing, metadata, estimates ────────────────────


def test_parse_embedding_variants():
    assert rte._parse_embedding([0.0] * DIM) == [0.0] * DIM
    assert rte._parse_embedding(np.zeros(DIM)) == [0.0] * DIM
    assert rte._parse_embedding(json.dumps([1.0] * DIM)) == [1.0] * DIM


def test_parse_embedding_rejects_bad():
    assert rte._parse_embedding([0.0] * 10) is None      # wrong dim
    assert rte._parse_embedding("not json") is None       # unparseable
    assert rte._parse_embedding(12345) is None            # wrong type


def test_row_to_metadata_shape():
    md = rte._row_to_metadata(
        {"Id": "IR1", "Title": "t", "Description": "d",
         "SupportGroup": "SD", "Location": "HUP", "extra": "ignored"}
    )
    assert md == {"Id": "IR1", "Title": "t", "Description": "d",
                  "SupportGroup": "SD", "Location": "HUP"}


def test_estimate_stage1_seconds_measured():
    # 120 tickets @ 10/s + 3 batches * 0.5s delay = 12 + 1.5 = 13.5
    assert rte.estimate_stage1_seconds(120, 10.0, 50, 0.5) == pytest.approx(13.5)


def test_estimate_stage1_seconds_constants_fallback():
    # rate=0 -> 3 batches * (2.0 + 0.5) = 7.5
    assert rte.estimate_stage1_seconds(120, 0.0, 50, 0.5) == pytest.approx(7.5)


def test_estimate_stage1_seconds_zero_is_none():
    assert rte.estimate_stage1_seconds(0, 10.0, 50, 0.5) is None


def test_format_duration_units():
    assert rte._format_duration(30).endswith("s")
    assert rte._format_duration(90).endswith("min")
    assert rte._format_duration(7200).endswith("hr")


# ── load_local_matrix_and_metadata ───────────────────────────────────────


def test_load_local_missing_returns_empty(tmp_path):
    matrix, meta, ids = rte.load_local_matrix_and_metadata(
        tmp_path / rte.EMBEDDINGS_NAME, tmp_path / rte.METADATA_NAME
    )
    assert matrix is None and meta == [] and ids == set()


def test_load_local_roundtrip(tmp_path):
    emb = tmp_path / rte.EMBEDDINGS_NAME
    meta_p = tmp_path / rte.METADATA_NAME
    np.save(str(emb), _matrix(4))
    meta_p.write_text(json.dumps(_metadata(4)), encoding="utf-8")

    matrix, meta, ids = rte.load_local_matrix_and_metadata(emb, meta_p)
    assert matrix.shape == (4, DIM)
    assert len(meta) == 4
    assert ids == {f"IR{1000 + i}" for i in range(4)}


# ── fetch_ticket_rows_by_ids (fake connection) ───────────────────────────


class _FakeCursor:
    """Minimal cursor that serves rows for a WHERE Id IN (...) query."""

    _COLS = ["Id", "Title", "Description", "SupportGroup", "Location", "embedding"]

    def __init__(self, table_rows):
        self._table = table_rows  # dict: Id -> row dict
        self.description = [(c,) for c in self._COLS]
        self._result = []

    def execute(self, sql):
        # Parse the quoted IDs out of the IN(...) clause for the test.
        import re
        ids = re.findall(r"'([^']+)'", sql.split("IN (", 1)[1]) if "IN (" in sql else list(self._table)
        self._result = [self._table[i] for i in ids if i in self._table]

    def fetchall(self):
        return [[r[c] for c in self._COLS] for r in self._result]

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, table_rows):
        self._table = table_rows

    def cursor(self):
        return _FakeCursor(self._table)


def test_fetch_ticket_rows_by_ids_returns_only_requested():
    table = {
        f"IR{i}": {
            "Id": f"IR{i}", "Title": f"t{i}", "Description": "d",
            "SupportGroup": "SD", "Location": "HUP",
            "embedding": [float(i)] * DIM,
        }
        for i in range(5)
    }
    conn = _FakeConnection(table)

    embs, meta = rte.fetch_ticket_rows_by_ids(conn, "tbl", ["IR1", "IR3"], chunk_size=10)

    assert [m["Id"] for m in meta] == ["IR1", "IR3"]
    assert len(embs) == 2 and len(embs[0]) == DIM


def test_fetch_ticket_rows_by_ids_batches_and_skips_bad():
    table = {
        "IR1": {"Id": "IR1", "Title": "t", "Description": "d",
                "SupportGroup": "SD", "Location": "HUP", "embedding": [1.0] * DIM},
        "IR2": {"Id": "IR2", "Title": "t", "Description": "d",
                "SupportGroup": "SD", "Location": "HUP", "embedding": [1.0] * 3},  # bad dim
    }
    conn = _FakeConnection(table)

    embs, meta = rte.fetch_ticket_rows_by_ids(conn, "tbl", ["IR1", "IR2"], chunk_size=1)

    assert [m["Id"] for m in meta] == ["IR1"]  # bad-dim row skipped
    assert len(embs) == 1



# ── in-place progress bar ────────────────────────────────────────────────


def test_print_progress_uses_carriage_return_no_newline(capsys):
    rte.print_progress(10, 100)
    out = capsys.readouterr().out
    assert out.startswith("\r")       # returns to start of line
    assert "\n" not in out            # never advances a line mid-progress
    assert "10/100" in out
    assert "10%" in out


def test_print_progress_reports_fraction_and_percent(capsys):
    rte.print_progress(50, 200)
    out = capsys.readouterr().out
    assert "50/200" in out
    assert "25%" in out


def test_print_progress_clamps_and_guards_zero_total(capsys):
    # done > total is clamped to total; total<=0 is guarded (no ZeroDivision).
    rte.print_progress(150, 100)
    rte.print_progress(5, 0)
    out = capsys.readouterr().out
    assert "100/100" in out
    assert "100%" in out


def test_finish_progress_emits_newline(capsys):
    rte.finish_progress()
    assert capsys.readouterr().out == "\n"


def test_print_heartbeat_reports_elapsed_rate_and_eta(capsys):
    # 100 done in 10s -> 10 tickets/sec; 900 remaining -> ETA 90s.
    rte.print_heartbeat(100, 1000, 10.0)
    out = capsys.readouterr().out
    assert out.startswith("\n")          # own line: does not clobber the bar
    assert out.endswith("\n")            # and terminates its own line
    assert "100/1,000" in out            # thousands-separated counts
    assert "10%" in out
    assert "10.0 tickets/sec" in out
    assert "ETA" in out
    assert "elapsed" in out


def test_print_heartbeat_handles_zero_elapsed_and_zero_done(capsys):
    # No division-by-zero; rate/ETA degrade gracefully before any progress.
    rte.print_heartbeat(0, 1000, 0.0)
    out = capsys.readouterr().out
    assert "0/1,000" in out
    assert "measuring..." in out
    assert "ETA unknown" in out


def test_heartbeat_interval_constant_is_positive():
    assert rte.HEARTBEAT_INTERVAL_SECONDS > 0


def test_suppress_stdout_captures_and_restores(capsys):
    with rte._SuppressStdout() as buf:
        print("hidden line")
    # The suppressed text is captured in the buffer, not shown to the user.
    assert "hidden line" in buf.getvalue()
    # After the context, stdout is restored (this print IS visible).
    print("visible again")
    assert "visible again" in capsys.readouterr().out

