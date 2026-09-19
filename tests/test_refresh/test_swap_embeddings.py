"""
Unit tests for scripts/swap_embeddings.py — the remote atomic-swap helper that
deploy.py runs on the server after SCP'ing the new embedding files as *.tmp.

These cover the validation + atomic-replace contract using small temp files
(no Databricks / SSH), consistent with the repo's I/O-light test style.
"""

import json
import os
import sys

import numpy as np
import pytest

# scripts/ is a sibling of tests/ at the repo root; make it importable.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import swap_embeddings as swap  # noqa: E402


def _write_tmp(vdir, rows, meta_rows, dims=1024):
    with open(os.path.join(vdir, "ticket_embeddings.npy.tmp"), "wb") as fh:
        np.save(fh, np.ones((rows, dims), dtype=np.float32))
    with open(os.path.join(vdir, "ticket_metadata.json.tmp"), "w", encoding="utf-8") as fh:
        json.dump([{"Id": f"X{i}"} for i in range(meta_rows)], fh)


def test_swap_happy_path_replaces_live_files(tmp_path):
    _write_tmp(str(tmp_path), 3, 3)
    rows = swap.swap_embeddings(str(tmp_path))
    assert rows == 3
    assert (tmp_path / "ticket_embeddings.npy").exists()
    assert (tmp_path / "ticket_metadata.json").exists()
    # Temp files are consumed by the swap.
    assert not (tmp_path / "ticket_embeddings.npy.tmp").exists()
    assert not (tmp_path / "ticket_metadata.json.tmp").exists()


def test_swap_backs_up_existing_live_files(tmp_path):
    _write_tmp(str(tmp_path), 2, 2)
    swap.swap_embeddings(str(tmp_path))          # first swap: no prior live
    _write_tmp(str(tmp_path), 4, 4)
    swap.swap_embeddings(str(tmp_path))          # second: should create .bak
    assert (tmp_path / "ticket_embeddings.npy.bak").exists()
    assert (tmp_path / "ticket_metadata.json.bak").exists()


def test_swap_rejects_row_meta_mismatch_without_touching_live(tmp_path):
    _write_tmp(str(tmp_path), 5, 4)              # 5 vectors, 4 metadata rows
    with pytest.raises(ValueError, match="row/meta mismatch"):
        swap.swap_embeddings(str(tmp_path))
    assert not (tmp_path / "ticket_embeddings.npy").exists()
    assert not (tmp_path / "ticket_metadata.json").exists()


def test_swap_rejects_bad_dims(tmp_path):
    _write_tmp(str(tmp_path), 3, 3, dims=512)    # wrong embedding dimension
    with pytest.raises(ValueError, match="bad dims"):
        swap.swap_embeddings(str(tmp_path))


def test_swap_errors_when_temp_files_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        swap.swap_embeddings(str(tmp_path))


def test_main_returns_nonzero_and_prints_on_failure(tmp_path, capsys):
    # main() reads sys.argv; point it at an empty dir (no *.tmp files present).
    old_argv = sys.argv[:]
    try:
        sys.argv = ["swap_embeddings.py", str(tmp_path)]  # no tmp files present
        result = swap.main()
    finally:
        sys.argv = old_argv
    assert result == 1
    assert "SWAP_FAILED" in capsys.readouterr().out
