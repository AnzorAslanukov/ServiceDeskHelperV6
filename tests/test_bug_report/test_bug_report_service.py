"""
Unit tests for BugReportService.

Covers: id sequencing, timestamping, JSONL persistence round-trip, status
filtering, status update, deletion, malformed-line tolerance, and the admin
password / unlock-token logic.
"""

import time

import pytest
from pydantic import ValidationError

from src.models.bug_report import BugReport, BugReportRequest
from src.services.bug_report import BugReportService


@pytest.fixture
def service(tmp_path):
    """A BugReportService writing to an isolated temp JSONL file."""
    return BugReportService(
        storage_path=tmp_path / "reports.jsonl",
        admin_password="s3cret",
        admin_secret="unit-test-secret",
        admin_unlock_hours=8.0,
    )


def _req(summary="Search is broken", description="It times out on Title contains."):
    return BugReportRequest(summary=summary, description=description, severity="high")


# ── Submission & Persistence ─────────────────────────────────────────


def test_submit_assigns_sequential_ids(service):
    r1 = service.submit(_req(), reported_by="alice")
    r2 = service.submit(_req(), reported_by="bob")
    assert r1.id == "BUG-1"
    assert r2.id == "BUG-2"


def test_submit_sets_metadata(service):
    report = service.submit(_req(), reported_by="alice")
    assert report.reported_by == "alice"
    assert report.status == "open"
    assert report.reported_at is not None


def test_submit_persists_to_jsonl(service, tmp_path):
    service.submit(_req(), reported_by="alice")
    lines = (tmp_path / "reports.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    restored = BugReport.model_validate_json(lines[0])
    assert restored.summary == "Search is broken"


def test_ids_continue_after_reload(service, tmp_path):
    service.submit(_req(), reported_by="alice")
    # New instance pointing at the same file should continue numbering.
    fresh = BugReportService(storage_path=tmp_path / "reports.jsonl")
    r2 = fresh.submit(_req(), reported_by="bob")
    assert r2.id == "BUG-2"


def test_empty_reporter_defaults_to_unknown(service):
    report = service.submit(_req(), reported_by="")
    assert report.reported_by == "unknown"


# ── Retrieval ────────────────────────────────────────────────────────


def test_list_reports_newest_first(service):
    service.submit(_req(summary="First"), reported_by="a")
    service.submit(_req(summary="Second"), reported_by="b")
    reports = service.list_reports()
    assert [r.summary for r in reports] == ["Second", "First"]


def test_list_filter_by_status(service):
    r1 = service.submit(_req(), reported_by="a")
    service.submit(_req(), reported_by="b")
    service.update_status(r1.id, "resolved")
    resolved = service.list_reports(status="resolved")
    assert len(resolved) == 1
    assert resolved[0].id == r1.id


def test_get_report(service):
    r = service.submit(_req(), reported_by="a")
    assert service.get_report(r.id).id == r.id
    assert service.get_report("BUG-999") is None


# ── Management ───────────────────────────────────────────────────────


def test_update_status(service):
    r = service.submit(_req(), reported_by="a")
    updated = service.update_status(r.id, "in_progress")
    assert updated.status == "in_progress"
    # Persisted
    assert service.get_report(r.id).status == "in_progress"


def test_update_status_invalid_raises(service):
    r = service.submit(_req(), reported_by="a")
    with pytest.raises(ValueError):
        service.update_status(r.id, "banana")


def test_update_status_missing_returns_none(service):
    assert service.update_status("BUG-999", "resolved") is None


def test_delete_report(service):
    r = service.submit(_req(), reported_by="a")
    assert service.delete_report(r.id) is True
    assert service.get_report(r.id) is None
    assert service.delete_report(r.id) is False


def test_malformed_line_is_skipped(service, tmp_path):
    service.submit(_req(), reported_by="a")
    path = tmp_path / "reports.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
    # Should not raise; the good record still loads.
    assert len(service.list_reports()) == 1


# ── Validation ───────────────────────────────────────────────────────


def test_summary_too_short_rejected():
    with pytest.raises(ValidationError):
        BugReportRequest(summary="x", description="valid enough")


def test_invalid_severity_rejected():
    with pytest.raises(ValidationError):
        BugReportRequest(summary="valid", description="valid", severity="nope")


# ── Admin Authentication ─────────────────────────────────────────────


def test_verify_admin_password(service):
    assert service.verify_admin_password("s3cret") is True
    assert service.verify_admin_password("wrong") is False


def test_no_password_configured_locks_admin(tmp_path):
    svc = BugReportService(storage_path=tmp_path / "r.jsonl", admin_password="")
    assert svc.verify_admin_password("") is False
    assert svc.verify_admin_password("anything") is False


def test_admin_token_roundtrip(service):
    token = service.create_admin_token()
    assert service.validate_admin_token(token) is True


def test_admin_token_rejects_tampering(service):
    token = service.create_admin_token()
    assert service.validate_admin_token(token + "x") is False
    assert service.validate_admin_token("garbage") is False
    assert service.validate_admin_token(None) is False


def test_admin_token_expiry(tmp_path):
    svc = BugReportService(
        storage_path=tmp_path / "r.jsonl",
        admin_password="pw",
        admin_secret="sec",
        admin_unlock_hours=0.0,  # immediate expiry
    )
    token = svc.create_admin_token()
    time.sleep(1.1)
    assert svc.validate_admin_token(token) is False
