"""
Unit tests for scripts/check_live_matches_manifest.py — the remote check that
deploy.py uses to tell "already applied" (LIVE_MATCH) from "a prior swap failed
so the live files are stale" (LIVE_STALE).
"""

import json
import os
import sys

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import check_live_matches_manifest as chk  # noqa: E402


def _write_live(vdir, npy_rows, manifest_rows, dims=1024):
    with open(os.path.join(vdir, "ticket_embeddings.npy"), "wb") as fh:
        np.save(fh, np.ones((npy_rows, dims), dtype=np.float32))
    with open(os.path.join(vdir, "ticket_vectors_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"rows": manifest_rows, "dims": dims}, fh)


def test_live_matches_when_rows_equal(tmp_path):
    _write_live(str(tmp_path), 10, 10)
    assert chk.live_matches_manifest(str(tmp_path)) is True


def test_live_stale_when_rows_differ(tmp_path):
    # Manifest says 879808 but the live file still has the old 797608 rows.
    _write_live(str(tmp_path), 797608 % 1000 or 7, 8)
    assert chk.live_matches_manifest(str(tmp_path)) is False


def test_missing_files_report_not_matched(tmp_path):
    assert chk.live_matches_manifest(str(tmp_path)) is False


def test_main_exit_codes(tmp_path, capsys):
    _write_live(str(tmp_path), 5, 5)
    old = sys.argv[:]
    try:
        sys.argv = ["check_live_matches_manifest.py", str(tmp_path)]
        rc = chk.main()
    finally:
        sys.argv = old
    assert rc == 0
    assert "LIVE_MATCH" in capsys.readouterr().out
