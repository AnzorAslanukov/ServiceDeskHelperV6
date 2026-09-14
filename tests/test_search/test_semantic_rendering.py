"""
Phase 3b — Rendering & data-completeness tests for semantic search results.

Verifies that tickets appearing in the results list actually render their
information across BOTH rendering paths:

1. List-row rendering — the semantic_results.html / similar_results.html
   partials render id, title (or em-dash), and a similarity % without error,
   including with adversarial contexts (None/NaN similarity, HTML in title).
2. Detail-row rendering — GET /ui/ticket/{id}/details fetches the ticket from
   Athena and renders rich fields, degrading cleanly when data is sparse and
   showing an error alert when the fetch fails.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.dependencies import get_athena_client
from src.main import app

_TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent.parent / "frontend" / "templates"
)


def _render(partial: str, **context) -> str:
    """Render a search partial template to a string (autoescape on, like FastAPI)."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template(f"search/partials/{partial}")
    return template.render(**context)


# ── List-row rendering: happy path ────────────────────────────────────


@pytest.mark.parametrize("partial", ["semantic_results.html", "similar_results.html"])
def test_list_rows_render_id_title_similarity(partial):
    """Each ticket's id, title, and similarity % must appear in the HTML."""
    tickets = [
        {"id": "IR1959100", "title": "Printer not printing", "similarity": 0.95},
        {"id": "IR1959101", "title": "VPN disconnects", "similarity": 0.812},
    ]
    html = _render(
        partial,
        similar_tickets=tickets,
        documentation=[],
        source_ticket_id="IR100",
    )
    assert "IR1959100" in html
    assert "Printer not printing" in html
    assert "95.0%" in html   # 0.95 * 100 |round(1)
    assert "81.2%" in html   # 0.812 * 100 |round(1)


@pytest.mark.parametrize("partial", ["semantic_results.html", "similar_results.html"])
def test_list_row_missing_title_renders_em_dash(partial):
    """A ticket with title=None must render the em-dash placeholder, not crash."""
    tickets = [{"id": "IR1", "title": None, "similarity": 0.5}]
    html = _render(
        partial,
        similar_tickets=tickets,
        documentation=[],
        source_ticket_id="IR100",
    )
    assert "IR1" in html
    assert "—" in html  # em-dash fallback for missing title


def test_semantic_documentation_renders_all_fields():
    """Documentation entries must render title, notebook, section, content, %."""
    docs = [
        {
            "content": "Step 1: reboot",
            "notebook": "uphs_notebook",
            "section": "Printers",
            "title": "Printer Guide",
            "similarity": 0.9,
        }
    ]
    html = _render(
        "semantic_results.html", similar_tickets=[], documentation=docs
    )
    assert "Printer Guide" in html
    assert "uphs_notebook" in html
    assert "Printers" in html
    assert "Step 1: reboot" in html
    assert "90.0%" in html


@pytest.mark.parametrize("partial", ["semantic_results.html", "similar_results.html"])
def test_empty_results_render_empty_state(partial):
    """No results → the empty-state block renders (no table, no crash)."""
    html = _render(
        partial, similar_tickets=[], documentation=[], source_ticket_id="IR100"
    )
    assert "No" in html  # "No results found" / "No similar tickets found"


@pytest.mark.parametrize("partial", ["semantic_results.html", "similar_results.html"])
def test_html_in_title_is_escaped(partial):
    """A title containing HTML must be escaped, not injected raw."""
    tickets = [{"id": "IR1", "title": "<b>xss</b>", "similarity": 0.5}]
    html = _render(
        partial,
        similar_tickets=tickets,
        documentation=[],
        source_ticket_id="IR100",
    )
    assert "<b>xss</b>" not in html
    assert "&lt;b&gt;xss&lt;/b&gt;" in html



# ── List-row rendering: adversarial similarity values ─────────────────


def test_template_nan_similarity_renders_nan_percent():
    """Direct template render with NaN similarity produces 'nan%'.

    Demonstrates WHY the service-layer guard (dropping non-finite similarities)
    is necessary: the template itself has no protection. With the service guard
    in place, such rows never reach this template in production.
    """
    tickets = [{"id": "IR1", "title": "x", "similarity": float("nan")}]
    html = _render(
        "similar_results.html",
        similar_tickets=tickets,
        documentation=[],
        source_ticket_id="IR100",
    )
    assert "nan%" in html.lower()


# ── Detail-row rendering: GET /ui/ticket/{id}/details ─────────────────


@pytest.fixture(autouse=True)
def _bypass_auth():
    """Bypass AuthMiddleware so protected /ui routes execute in tests."""
    with patch("src.main.get_current_user", return_value=object()):
        yield


def _detail_client(mock_athena) -> httpx.AsyncClient:
    app.dependency_overrides[get_athena_client] = lambda: mock_athena
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_detail_row_full_ticket_renders_fields():
    """A fully-populated ticket must render its rich fields in the detail row."""
    mock_athena = AsyncMock()
    mock_athena.get_ticket.return_value = {
        "id": "IR100",
        "title": "Printer not printing",
        "description": "The 3rd floor printer is jammed.",
        "status": {"name": "Active"},
        "priority": 3,
        "supportGroup": {"name": "EUS\\HUP"},
        "affectedUser": {"displayName": "John Smith"},
        "location": {"name": "HUP"},
        "createdDate": "2026-04-13T10:30:00Z",
    }
    try:
        async with _detail_client(mock_athena) as client:
            resp = await client.get("/ui/ticket/IR100/details")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    html = resp.text
    assert "Printer not printing" in html
    assert "The 3rd floor printer is jammed." in html
    assert "John Smith" in html
    assert "Active" in html


@pytest.mark.asyncio
async def test_detail_row_sparse_ticket_degrades_without_crash():
    """A ticket with only an id must render without crashing (blanks allowed)."""
    mock_athena = AsyncMock()
    mock_athena.get_ticket.return_value = {"id": "IR200"}
    try:
        async with _detail_client(mock_athena) as client:
            resp = await client.get("/ui/ticket/IR200/details")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "alert-error" not in resp.text  # sparse != error


@pytest.mark.asyncio
async def test_detail_row_empty_dict_does_not_error():
    """An empty Athena response should still render (no exception path)."""
    mock_athena = AsyncMock()
    mock_athena.get_ticket.return_value = {}
    try:
        async with _detail_client(mock_athena) as client:
            resp = await client.get("/ui/ticket/IR300/details")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_detail_row_fetch_failure_shows_error_alert():
    """If Athena raises, the detail row must show a friendly error alert."""
    mock_athena = AsyncMock()
    mock_athena.get_ticket.side_effect = RuntimeError("Athena down")
    try:
        async with _detail_client(mock_athena) as client:
            resp = await client.get("/ui/ticket/IR400/details")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "alert-error" in resp.text
    assert "IR400" in resp.text
