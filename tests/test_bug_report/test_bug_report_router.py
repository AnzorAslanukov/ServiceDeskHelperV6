"""
Router/endpoint tests for the Bug Report feature.

Uses FastAPI TestClient with:
    - the BugReportService overridden to write to a temp file
    - a real signed session cookie (via the AuthService test-account path) so
      the auth middleware lets authenticated requests through
    - a real admin unlock cookie for admin-only endpoints

Covers the JSON REST API and the HTMX frontend routes.
"""

import time

import pytest
from fastapi.testclient import TestClient

from src.dependencies import (
    SESSION_COOKIE,
    get_auth_service,
    get_bug_report_service,
)
from src.main import app
from src.routers.bug_report import ADMIN_COOKIE
from src.services.auth import AuthUser
from src.services.bug_report import BugReportService


@pytest.fixture
def bug_service(tmp_path) -> BugReportService:
    """A BugReportService writing to an isolated temp JSONL file."""
    return BugReportService(
        storage_path=tmp_path / "reports.jsonl",
        admin_password="admin-pw",
        admin_secret="router-test-secret",
        admin_unlock_hours=8.0,
    )


@pytest.fixture
def session_cookie() -> str:
    """A valid signed session token for the auth middleware."""
    auth = get_auth_service()
    user = AuthUser(
        username="tester", display_name="Tester", groups=["test"], login_time=time.time()
    )
    return auth.create_session_token(user)


@pytest.fixture
def client(bug_service, session_cookie) -> TestClient:
    """TestClient with the bug service overridden and a session cookie set."""
    app.dependency_overrides[get_bug_report_service] = lambda: bug_service
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, session_cookie)
    yield c
    app.dependency_overrides.clear()


def _admin_cookie(bug_service: BugReportService) -> str:
    return bug_service.create_admin_token()


# ── POST /bug-report (submit) ────────────────────────────────────────


def test_submit_returns_id(client):
    resp = client.post(
        "/bug-report",
        data={"summary": "Chat is broken", "description": "It never responds.", "severity": "high"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "BUG-1"
    assert "BUG-1" in body["message"]


def test_submit_validation_error(client):
    resp = client.post("/bug-report", data={"summary": "x", "description": "y"})
    assert resp.status_code == 422


def test_submit_with_attachment(client, bug_service):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    resp = client.post(
        "/bug-report",
        data={"summary": "Broken layout", "description": "See screenshot.", "severity": "low"},
        files=[("files", ("shot.png", png, "image/png"))],
    )
    assert resp.status_code == 200
    report = bug_service.get_report("BUG-1")
    assert len(report.attachments) == 1
    assert report.attachments[0].original_filename == "shot.png"


def test_submit_rejects_disallowed_type(client):
    resp = client.post(
        "/bug-report",
        data={"summary": "Bad file", "description": "Trying an exe.", "severity": "low"},
        files=[("files", ("evil.exe", b"MZ\x90\x00", "application/octet-stream"))],
    )
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"]


def test_download_attachment_requires_admin(client, bug_service):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    client.post(
        "/bug-report",
        data={"summary": "Broken layout", "description": "See screenshot.", "severity": "low"},
        files=[("files", ("shot.png", png, "image/png"))],
    )
    att_id = bug_service.get_report("BUG-1").attachments[0].id
    # No admin cookie → 403.
    resp = client.get(f"/bug-report/BUG-1/attachments/{att_id}")
    assert resp.status_code == 403
    # With admin cookie → file bytes returned.
    client.cookies.set(ADMIN_COOKIE, _admin_cookie(bug_service))
    resp = client.get(f"/bug-report/BUG-1/attachments/{att_id}")
    assert resp.status_code == 200
    assert resp.content == png


# ── GET /bug-report (list, admin only) ───────────────────────────────


def test_list_requires_admin(client):
    resp = client.get("/bug-report")
    assert resp.status_code == 403


def test_list_with_admin_cookie(client, bug_service):
    client.post(
        "/bug-report",
        data={"summary": "Something failed", "description": "Details here.", "severity": "low"},
    )
    client.cookies.set(ADMIN_COOKIE, _admin_cookie(bug_service))
    resp = client.get("/bug-report")
    assert resp.status_code == 200
    reports = resp.json()
    assert len(reports) == 1
    assert reports[0]["summary"] == "Something failed"


# ── PATCH /bug-report/{id}/status ────────────────────────────────────


def test_update_status(client, bug_service):
    client.post(
        "/bug-report",
        data={"summary": "Fix me", "description": "Broken thing.", "severity": "medium"},
    )
    client.cookies.set(ADMIN_COOKIE, _admin_cookie(bug_service))
    resp = client.patch("/bug-report/BUG-1/status", json={"status": "resolved"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"


def test_update_status_invalid(client, bug_service):
    client.post(
        "/bug-report",
        data={"summary": "Fix me", "description": "Broken thing.", "severity": "medium"},
    )
    client.cookies.set(ADMIN_COOKIE, _admin_cookie(bug_service))
    resp = client.patch("/bug-report/BUG-1/status", json={"status": "banana"})
    assert resp.status_code == 400


def test_delete_report(client, bug_service):
    client.post(
        "/bug-report",
        data={"summary": "Delete me", "description": "Remove this.", "severity": "low"},
    )
    client.cookies.set(ADMIN_COOKIE, _admin_cookie(bug_service))
    resp = client.request("DELETE", "/bug-report/BUG-1")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "BUG-1"}


# ── HTMX frontend routes ─────────────────────────────────────────────


def test_widget_submit_partial(client):
    resp = client.post(
        "/ui/bug-report",
        data={
            "summary": "UI glitch on search",
            "description": "The results box renders blank.",
            "severity": "medium",
            "feature": "search",
        },
    )
    assert resp.status_code == 200
    assert "BUG-1" in resp.text
    assert "alert-success" in resp.text


def test_widget_submit_validation_partial(client):
    resp = client.post("/ui/bug-report", data={"summary": "x", "description": "y", "severity": "medium"})
    assert resp.status_code == 200
    assert "alert-error" in resp.text


def test_admin_page_locked_without_cookie(client):
    resp = client.get("/ui/bug-report/admin")
    assert resp.status_code == 200
    assert "Admin access required" in resp.text


def test_admin_page_unlocked_with_cookie(client, bug_service):
    client.cookies.set(ADMIN_COOKIE, _admin_cookie(bug_service))
    resp = client.get("/ui/bug-report/admin")
    assert resp.status_code == 200
    assert "report" in resp.text.lower()


def test_admin_unlock_wrong_password(client):
    resp = client.post(
        "/ui/bug-report/admin/unlock", data={"password": "wrong"}, follow_redirects=False
    )
    assert resp.status_code == 401
    assert "Incorrect password" in resp.text


def test_admin_unlock_correct_password(client):
    resp = client.post(
        "/ui/bug-report/admin/unlock", data={"password": "admin-pw"}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.cookies.get(ADMIN_COOKIE) is not None
