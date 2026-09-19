"""
Unit tests for deploy.py's embedding staging/apply flow and progress helpers.

Scope: the pure, I/O-free formatting helpers (``_fmt_bytes``, ``_fmt_eta``) and
the decision logic of ``stage_embeddings`` / ``apply_embeddings`` — the latter
tested by monkeypatching the I/O boundary (run_local_streaming, ssh, scp,
scp_streaming, and the remote-probe helpers) so no real SSH/SCP is performed.
This mirrors the repo convention of unit-testing pure logic while leaving the
live SSH/SCP transfer to manual/integration testing.

These tests deliberately never build large vector files; where a local file
must exist for os.path.exists() checks, tiny stub files (a handful of bytes)
are created — well under the 200-record cap.
"""

import pytest

import deploy


# ── Formatting helpers ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "num,expected",
    [
        (0, "0.0 B"),
        (None, "0.0 B"),
        (512, "512.0 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024, "1.0 MB"),
        (int(3.4 * 1024 * 1024 * 1024), "3.4 GB"),
    ],
)
def test_fmt_bytes(num, expected):
    assert deploy._fmt_bytes(num) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (None, "--:--"),
        (-5, "--:--"),
        (0, "0:00"),
        (9, "0:09"),
        (65, "1:05"),
        (3661, "1:01:01"),
        (99 * 3600, "99:00:00"),                 # just under the clamp: still shown
        (100 * 3600, "--:--"),                   # at the clamp: treated as unknown
        (50552625048050902414750782, "--:--"),   # runaway ETA from the stuck log
    ],
)
def test_fmt_eta(seconds, expected):
    assert deploy._fmt_eta(seconds) == expected


# ── stage_embeddings decision logic ────────────────────────────────────

def _make_local_vectors(tmp_path, monkeypatch):
    """
    Point deploy.py at a temp project dir with tiny stub embedding files so the
    os.path.exists() guards pass without building real vectors.
    """
    vectors = tmp_path / deploy.VECTORS_SUBDIR
    vectors.mkdir(parents=True)
    (vectors / deploy.EMBEDDINGS_NAME).write_bytes(b"stub-npy")
    (vectors / deploy.METADATA_NAME).write_text("[]", encoding="utf-8")
    # deploy.py derives project_root from __file__; redirect that to tmp_path.
    fake_file = str(tmp_path / "deploy.py")
    monkeypatch.setattr(deploy.os.path, "abspath", lambda _p: fake_file)
    return vectors


def test_stage_returns_fail_when_refresh_fails(tmp_path, monkeypatch):
    _make_local_vectors(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "run_local_streaming", lambda *a, **k: False)
    assert deploy.stage_embeddings(limit=100) == "fail"


def test_stage_returns_fail_when_local_files_missing(tmp_path, monkeypatch):
    # No stub files created -> os.path.exists() guard should trip.
    vectors = tmp_path / deploy.VECTORS_SUBDIR
    vectors.mkdir(parents=True)
    fake_file = str(tmp_path / "deploy.py")
    monkeypatch.setattr(deploy.os.path, "abspath", lambda _p: fake_file)
    monkeypatch.setattr(deploy, "run_local_streaming", lambda *a, **k: True)
    assert deploy.stage_embeddings(limit=100) == "fail"


def test_stage_returns_skip_when_remote_already_matches(tmp_path, monkeypatch):
    _make_local_vectors(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "run_local_streaming", lambda *a, **k: True)
    # Manifest sha present locally, and it appears in the remote manifest output.
    monkeypatch.setattr(deploy, "_local_manifest_sha", lambda: ("SHA_NPY", "SHA_META"))
    monkeypatch.setattr(deploy, "ssh", lambda _c: (True, "SHA_NPY SHA_META"))
    monkeypatch.setattr(deploy, "_remote_live_matches_manifest", lambda: True)
    assert deploy.stage_embeddings(limit=100) == "skip"


def test_stage_returns_swap_reusing_temp_when_live_stale(tmp_path, monkeypatch):
    _make_local_vectors(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "run_local_streaming", lambda *a, **k: True)
    monkeypatch.setattr(deploy, "_local_manifest_sha", lambda: ("SHA_NPY", "SHA_META"))
    monkeypatch.setattr(deploy, "ssh", lambda _c: (True, "SHA_NPY SHA_META"))
    # Live is stale, but temp files are still present -> reuse them (no transfer).
    monkeypatch.setattr(deploy, "_remote_live_matches_manifest", lambda: False)
    monkeypatch.setattr(deploy, "_remote_temp_files_present", lambda: True)

    def _boom(*a, **k):
        raise AssertionError("scp_streaming should NOT run when reusing temp files")

    monkeypatch.setattr(deploy, "scp_streaming", _boom)
    assert deploy.stage_embeddings(limit=100) == "swap"


def test_stage_transfers_and_returns_swap_on_success(tmp_path, monkeypatch):
    _make_local_vectors(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "run_local_streaming", lambda *a, **k: True)
    # No usable manifest -> go straight to transfer.
    monkeypatch.setattr(deploy, "_local_manifest_sha", lambda: (None, None))
    calls = []
    monkeypatch.setattr(
        deploy, "scp_streaming",
        lambda local, rel, **k: (calls.append(rel), (True, ""))[1],
    )
    monkeypatch.setattr(deploy, "scp", lambda local, rel: (True, ""))
    assert deploy.stage_embeddings(limit=50) == "swap"
    # Both big files were streamed to their *.tmp paths.
    assert any(r.endswith(deploy.EMBEDDINGS_NAME + ".tmp") for r in calls)
    assert any(r.endswith(deploy.METADATA_NAME + ".tmp") for r in calls)


def test_stage_returns_fail_when_transfer_fails(tmp_path, monkeypatch):
    _make_local_vectors(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "run_local_streaming", lambda *a, **k: True)
    monkeypatch.setattr(deploy, "_local_manifest_sha", lambda: (None, None))
    monkeypatch.setattr(deploy, "scp_streaming", lambda local, rel, **k: (False, "boom"))
    monkeypatch.setattr(deploy, "scp", lambda local, rel: (True, ""))
    assert deploy.stage_embeddings(limit=50) == "fail"


# ── apply_embeddings delegates to the remote swap ──────────────────────

def test_apply_embeddings_returns_true_on_swap_ok(monkeypatch):
    monkeypatch.setattr(deploy, "_remote_validate_and_swap", lambda: True)
    assert deploy.apply_embeddings() is True


def test_apply_embeddings_returns_false_on_swap_failure(monkeypatch):
    monkeypatch.setattr(deploy, "_remote_validate_and_swap", lambda: False)
    assert deploy.apply_embeddings() is False
