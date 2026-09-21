"""
Frontend Router — Serves HTML pages and HTMX partial responses.

This router handles:
    - Full page renders (GET requests returning complete HTML pages)
    - HTMX partial renders (POST requests returning HTML fragments for search results)

The HTMX partials call the existing backend services directly,
avoiding an extra HTTP hop through the API endpoints.
"""

import logging
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from src.dependencies import (
    get_assignment_service,
    get_athena_client,
    get_bug_report_service,
    get_chatbot_service,
    get_current_user,
    get_search_service,
)
from src.models.bug_report import BugReportRequest
from src.routers.bug_report import ADMIN_COOKIE
from src.services.assignment import AssignmentService, LOCATION_GUID_TO_FULLNAME
from src.services.bug_report import BugReportService
from src.services.chatbot import ChatbotService
from src.services.ticket_search import TicketSearchService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["frontend"])

# Templates directory
TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Operators that trigger a slow server-side substring scan on free-text fields.
_SUBSTRING_OPERATORS = frozenset({"contains", "like"})


def _field_search_error_message(exc: Exception, field: str, operator: str) -> str:
    """
    Translate a field-search exception into a clear, actionable message.

    Never returns an empty string (httpx.ReadTimeout's str() is empty), which
    previously rendered as a blank error box that looked like "nothing found".
    """
    op = (operator or "").lower()
    if isinstance(exc, httpx.TimeoutException):
        if op in _SUBSTRING_OPERATORS:
            return (
                f"Search timed out. The “{operator}” operator on “{field}” runs a "
                "slow full-text scan on the server. Try a more specific value, use "
                "the “eq” (Equals) operator, or use Description / Semantic Search "
                "for free-text lookups."
            )
        return (
            "Search timed out while contacting the ticketing system. Please try "
            "again, or narrow your search."
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = (
            exc.response.status_code if exc.response is not None else "unknown"
        )
        if op in _SUBSTRING_OPERATORS:
            return (
                f"The ticketing system rejected this search (HTTP {status}). The "
                f"“{operator}” operator is not supported on “{field}”. Try the "
                "“eq” (Equals) operator, or use Description / Semantic Search."
            )
        return (
            f"The ticketing system returned an error (HTTP {status}). This "
            f"field/operator combination (“{field}” “{operator}”) may not be "
            "supported. Try a different operator such as “eq”."
        )
    # Fallback: surface the string but guarantee it is non-empty.
    return str(exc) or f"Search failed: {type(exc).__name__}."


# ── Page Routes ────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Redirect root UI to the search page."""
    return RedirectResponse(url="/ui/search", status_code=302)


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    """Render the Enhanced Ticket Search page."""
    return templates.TemplateResponse(
        request, "search/index.html", {"active_page": "search"}
    )


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """Render the Q&A Chatbot page."""
    return templates.TemplateResponse(
        request, "chat/index.html", {"active_page": "chat"}
    )


@router.get("/assignment", response_class=HTMLResponse)
async def assignment_page(request: Request):
    """Render the Ticket Assignment Recommendation page."""
    return templates.TemplateResponse(
        request, "assignment/index.html", {"active_page": "assignment"}
    )


# ── HTMX Partial Routes (Search Feature) ──────────────────────────────


@router.post("/search/field", response_class=HTMLResponse)
async def search_field_partial(
    request: Request,
    field: str = Form(...),
    value: str = Form(...),
    operator: str = Form("eq"),
    ticket_type: str = Form("incident"),
    page: int = Form(1),
    page_size: int = Form(50),
    service: TicketSearchService = Depends(get_search_service),
):
    """HTMX partial: Execute field search and return results HTML."""
    try:
        result = await service.search_by_field(
            field=field,
            value=value,
            ticket_type=ticket_type,
            operator=operator,
            page=page,
            page_size=page_size,
        )
        return templates.TemplateResponse(
            request,
            "search/partials/field_results.html",
            {
                "tickets": [t.model_dump() for t in result.tickets],
                "total": result.total,
                "page": result.page,
                "page_size": result.page_size,
                "has_more": result.has_more,
                "form_field": field,
                "form_value": value,
                "form_operator": operator,
                "form_ticket_type": ticket_type,
            },
        )
    except Exception as e:
        logger.exception("Field search failed")
        return templates.TemplateResponse(
            request,
            "search/partials/field_results.html",
            {
                "error": _field_search_error_message(e, field, operator),
                "tickets": [],
                "total": 0,
            },
        )


@router.post("/search/description", response_class=HTMLResponse)
async def search_description_partial(
    request: Request,
    text: str = Form(...),
    ticket_type: str = Form("incident"),
    page: int = Form(1),
    page_size: int = Form(50),
    service: TicketSearchService = Depends(get_search_service),
):
    """HTMX partial: Execute description search and return results HTML."""
    try:
        result = await service.search_by_description(
            text=text,
            ticket_type=ticket_type,
            page=page,
            page_size=page_size,
        )
        return templates.TemplateResponse(
            request,
            "search/partials/description_results.html",
            {
                "tickets": [t.model_dump() for t in result.tickets],
                "total": result.total,
                "page": result.page,
                "page_size": result.page_size,
                "has_more": result.has_more,
                "form_text": text,
                "form_ticket_type": ticket_type,
            },
        )
    except Exception as e:
        logger.exception("Description search failed")
        return templates.TemplateResponse(
            request,
            "search/partials/description_results.html",
            {"error": str(e), "tickets": [], "total": 0},
        )


@router.post("/search/semantic", response_class=HTMLResponse)
async def search_semantic_partial(
    request: Request,
    query: str = Form(...),
    top_k: int = Form(10),
    service: TicketSearchService = Depends(get_search_service),
):
    """HTMX partial: Execute semantic search and return results HTML."""
    try:
        result = await service.semantic_search(
            query=query,
            top_k=top_k,
        )
        return templates.TemplateResponse(
            request,
            "search/partials/semantic_results.html",
            {
                "similar_tickets": [t.model_dump() for t in result.similar_tickets],
                "documentation": [d.model_dump() for d in result.documentation],
            },
        )
    except Exception as e:
        logger.exception("Semantic search failed")
        return templates.TemplateResponse(
            request,
            "search/partials/semantic_results.html",
            {
                "error": str(e),
                "similar_tickets": [],
                "documentation": [],
            },
        )


@router.get("/ticket/{ticket_id}/details", response_class=HTMLResponse)
async def ticket_detail_partial(
    request: Request,
    ticket_id: str,
    athena=Depends(get_athena_client),
):
    """HTMX partial: Fetch a single ticket from Athena and return rich detail HTML."""
    try:
        raw = await athena.get_ticket(ticket_id)
        # Athena can return a null body (HTTP 200) for a ticket that exists in
        # the embedding store but is no longer retrievable. Show a friendly
        # notice rather than a near-empty detail card or an error alert.
        if not isinstance(raw, dict) or not raw:
            return HTMLResponse(
                '<div class="alert alert-info">'
                "<span>ℹ️</span>"
                f"<span>Details for {ticket_id} are no longer available in Athena "
                "(the ticket may have been archived or deleted).</span>"
                "</div>"
            )
        ticket = _extract_rich_ticket_detail(raw, ticket_id)
        return templates.TemplateResponse(
            request,
            "search/partials/ticket_detail.html",
            {"ticket": ticket},
        )
    except Exception as e:
        logger.exception("Ticket detail fetch failed for %s", ticket_id)
        return HTMLResponse(
            f'<div class="alert alert-error"><span>⚠️</span><span>Could not load details for {ticket_id}: {e}</span></div>'
        )


# ── HTMX Partial Routes (Assignment Feature) ──────────────────────────


@router.post("/assignment/recommend", response_class=HTMLResponse)
async def assignment_recommend_partial(
    request: Request,
    ticket_id: str = Form(...),
    use_triage: str = Form("off"),
    service: AssignmentService = Depends(get_assignment_service),
):
    """HTMX partial: Get assignment recommendation and return results HTML."""
    try:
        use_triage_bool = use_triage.lower() in ("on", "true", "1", "yes")
        result = await service.recommend_assignment(
            ticket_id=ticket_id,
            use_triage=use_triage_bool,
        )
        return templates.TemplateResponse(
            request,
            "assignment/partials/recommendation.html",
            {
                "ticket": result.ticket.model_dump(),
                "recommendation": result.recommendation.model_dump(),
            },
        )
    except ValueError as e:
        # Validation errors from _validate_and_normalize_ticket_id() — user-friendly messages
        return templates.TemplateResponse(
            request,
            "assignment/partials/recommendation.html",
            {"error": str(e)},
        )
    except Exception as e:
        logger.exception("Assignment recommendation failed for %s", ticket_id)
        error_msg = str(e)
        normalized = ticket_id.strip().upper() if ticket_id else ticket_id
        if "404" in error_msg or "not found" in error_msg.lower():
            error_msg = f"Ticket '{normalized}' not found in Athena. Please verify the ticket ID exists."
        else:
            error_msg = f"Failed to generate recommendation: {error_msg}"
        return templates.TemplateResponse(
            request,
            "assignment/partials/recommendation.html",
            {"error": error_msg},
        )


# ── HTMX Partial Routes (Search Feature — continued) ──────────────────


@router.post("/search/similar", response_class=HTMLResponse)
async def search_similar_partial(
    request: Request,
    ticket_id: str = Form(...),
    top_k: int = Form(10),
    service: TicketSearchService = Depends(get_search_service),
):
    """HTMX partial: Find similar tickets and return results HTML."""
    try:
        result = await service.find_similar_tickets(
            ticket_id=ticket_id,
            top_k=top_k,
        )
        return templates.TemplateResponse(
            request,
            "search/partials/similar_results.html",
            {
                "source_ticket_id": result.source_ticket_id,
                "similar_tickets": [t.model_dump() for t in result.similar_tickets],
            },
        )
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "search/partials/similar_results.html",
            {
                "error": str(e),
                "source_ticket_id": ticket_id,
                "similar_tickets": [],
            },
        )
    except Exception as e:
        logger.exception("Similar ticket search failed")
        return templates.TemplateResponse(
            request,
            "search/partials/similar_results.html",
            {
                "error": str(e),
                "source_ticket_id": ticket_id,
                "similar_tickets": [],
            },
        )


# ── Helper Functions ───────────────────────────────────────────────────


def _is_guid(value: Any) -> bool:
    """Check if a value is a string that looks like a GUID.

    Tolerant of non-string input (e.g., an integer priority): anything that is
    not a ``str`` is simply not a GUID, so we return False instead of letting
    ``re.match`` raise a TypeError.
    """
    if not isinstance(value, str):
        return False
    return bool(re.match(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        value,
    ))


def _get_field(data: dict[str, Any], *keys: str) -> Any:
    """Get the first non-None value from a dict for the given keys.

    If the value is a dict, extracts name/displayName.
    If the value is a GUID string, returns None.
    """
    for key in keys:
        # Check *Value companion field first (view endpoint flat format)
        companion = data.get(f"{key}Value")
        if companion is not None:
            return companion
        val = data.get(key)
        if val is not None:
            if isinstance(val, dict):
                return val.get("name") or val.get("displayName")
            if isinstance(val, str) and _is_guid(val):
                continue
            return val
    return None


def _format_datetime(raw: str | None) -> str | None:
    """Format an Athena ISO date string to HH:MM MM/DD/YYYY in US Eastern time.

    Delegates to :meth:`TicketSearchService._format_date` so the ticket detail
    view uses the exact same UTC→Eastern conversion as Feature #1 search results.
    """
    return TicketSearchService._format_date(raw)


def _extract_location_path(raw_ticket: dict[str, Any]) -> str | None:
    """Extract location as parent\\child (e.g., 'HUP\\RAVDIN') using GUID lookup."""

    def _last_two_segments(path: str) -> str:
        parts = path.split("\\")
        if len(parts) >= 2:
            return "\\".join(parts[-2:])
        return path

    loc = raw_ticket.get("location")
    location_guid: str | None = None
    full_path_from_dict: str | None = None
    leaf_from_dict: str | None = None

    if isinstance(loc, dict):
        location_guid = loc.get("id")
        full_path_from_dict = loc.get("path") or loc.get("fullName") or loc.get("fullname")
        leaf_from_dict = loc.get("name") or loc.get("displayName")
    elif isinstance(loc, str):
        if _is_guid(loc):
            location_guid = loc
        elif "\\" in loc:
            full_path_from_dict = loc
        else:
            leaf_from_dict = loc

    # Check locationValue companion field (view endpoint format)
    location_value = raw_ticket.get("locationValue")
    location_value_str: str | None = None
    if location_value and isinstance(location_value, str):
        location_value_str = location_value

    # Debug logging for location resolution
    logger.debug(
        "Location extraction: loc=%r, guid=%s, leaf=%s, lookup_size=%d",
        loc, location_guid, leaf_from_dict, len(LOCATION_GUID_TO_FULLNAME),
    )

    # Resolve GUID to full path using the location lookup table
    if location_guid and LOCATION_GUID_TO_FULLNAME:
        resolved_path = LOCATION_GUID_TO_FULLNAME.get(location_guid)
        if resolved_path:
            logger.debug("Location GUID %s resolved to: %s", location_guid, resolved_path)
            return _last_two_segments(resolved_path)
        else:
            logger.debug("Location GUID %s NOT FOUND in lookup table", location_guid)

    # Fallback: use explicit path fields if available
    if full_path_from_dict and "\\" in full_path_from_dict:
        return _last_two_segments(full_path_from_dict)

    if location_value_str and "\\" in location_value_str:
        return _last_two_segments(location_value_str)

    if full_path_from_dict:
        return _last_two_segments(full_path_from_dict)

    if location_value_str:
        return location_value_str

    if leaf_from_dict:
        return leaf_from_dict

    return None


def _extract_rich_ticket_detail(raw_ticket: dict[str, Any], ticket_id: str) -> dict[str, Any]:
    """Extract rich ticket detail from raw Athena response for the detail partial.

    Returns a dict with all fields needed by the ticket_detail.html template,
    matching the same structure as Feature #3's TicketInfo.

    Tolerates a missing/None/non-dict ``raw_ticket``: Athena can return a null
    JSON body (HTTP 200) for a ticket that exists in the embedding store but is
    no longer retrievable (deleted/archived). In that case all optional fields
    resolve to None instead of raising ``'NoneType' object has no attribute
    'get'``.
    """
    if not isinstance(raw_ticket, dict):
        raw_ticket = {}

    # Determine ticket type from ID prefix
    ticket_type = "incident" if ticket_id.upper().startswith("IR") else "servicerequest"

    # Extract affected user details
    affected_user_obj = raw_ticket.get("affectedUser")
    affected_user = None
    affected_user_title = None
    affected_user_phone = None
    if isinstance(affected_user_obj, dict):
        affected_user = affected_user_obj.get("displayName") or affected_user_obj.get("userName")
        affected_user_title = affected_user_obj.get("title")
        affected_user_phone = affected_user_obj.get("businessPhone") or affected_user_obj.get("mobile")
    elif isinstance(affected_user_obj, str) and not _is_guid(affected_user_obj):
        affected_user = affected_user_obj

    # Fallback for flat format (view endpoint)
    if not affected_user:
        affected_user = raw_ticket.get("affectedUser_DisplayName")
    if not affected_user_title:
        affected_user_title = raw_ticket.get("affectedUserTitle")

    # Extract created by
    created_by_obj = raw_ticket.get("createdBy")
    created_by = None
    if isinstance(created_by_obj, dict):
        created_by = created_by_obj.get("displayName") or created_by_obj.get("userName")
    elif isinstance(created_by_obj, str) and not _is_guid(created_by_obj):
        created_by = created_by_obj

    # Extract location with full path (parent\child format)
    location = _extract_location_path(raw_ticket)

    # Status
    status = _get_field(raw_ticket, "status")

    # Support group
    support_group = _get_field(raw_ticket, "supportGroup", "tierQueue", "assignedGroup")

    # Determine if ticket is resolved/closed for labeling
    is_resolved_or_closed = False
    if status and isinstance(status, str):
        is_resolved_or_closed = any(
            s in status.lower() for s in ("resolved", "closed", "completed")
        )

    return {
        "id": ticket_id,
        "ticket_type": ticket_type,
        "title": _get_field(raw_ticket, "title", "shortDescription", "summary"),
        "description": raw_ticket.get("description"),
        "status": status,
        "priority": _get_field(raw_ticket, "priority"),
        "support_group": support_group,
        "is_resolved_or_closed": is_resolved_or_closed,
        "affected_user": affected_user,
        "affected_user_title": affected_user_title,
        "affected_user_phone": affected_user_phone,
        "location": location,
        "floor": _get_field(raw_ticket, "floor"),
        "room": raw_ticket.get("room"),
        "classification": _get_field(raw_ticket, "classificationPath", "classification"),
        "source": _get_field(raw_ticket, "source"),
        "created_by": created_by,
        "created_date": _format_datetime(_get_field(raw_ticket, "createdDate", "createDate")),
        "modified_date": _format_datetime(_get_field(raw_ticket, "lastModifiedDate", "lastModified")),
    }


# ── Bug Report Feature ─────────────────────────────────────────────────


@router.post("/bug-report", response_class=HTMLResponse)
async def bug_report_submit_partial(
    request: Request,
    summary: str = Form(...),
    description: str = Form(...),
    severity: str = Form("medium"),
    feature: str = Form(""),
    page_url: str = Form(""),
    user_agent: str = Form(""),
    service: BugReportService = Depends(get_bug_report_service),
):
    """HTMX partial: submit a bug report from the always-visible widget."""
    try:
        payload = BugReportRequest(
            summary=summary,
            description=description,
            severity=severity,  # type: ignore[arg-type]
            feature=feature,
            page_url=page_url,
            user_agent=user_agent,
        )
    except ValidationError:
        return templates.TemplateResponse(
            request,
            "bug_report/partials/submit_result.html",
            {"error": "Please provide a short summary and a description (3+ characters each)."},
        )

    user = get_current_user(request)
    reported_by = user.username if user else "unknown"
    report = service.submit(payload, reported_by=reported_by)
    return templates.TemplateResponse(
        request,
        "bug_report/partials/submit_result.html",
        {"report_id": report.id},
    )


@router.get("/bug-report/admin", response_class=HTMLResponse)
async def bug_report_admin_page(
    request: Request,
    service: BugReportService = Depends(get_bug_report_service),
):
    """Render the admin page — the unlock form or the report list."""
    token = request.cookies.get(ADMIN_COOKIE)
    if not service.validate_admin_token(token):
        return templates.TemplateResponse(
            request,
            "bug_report/unlock.html",
            {"active_page": "bug_admin"},
        )
    reports = service.list_reports()
    return templates.TemplateResponse(
        request,
        "bug_report/admin.html",
        {
            "active_page": "bug_admin",
            "reports": [r.model_dump() for r in reports],
            "total": len(reports),
        },
    )


@router.post("/bug-report/admin/unlock", response_class=HTMLResponse)
async def bug_report_admin_unlock(
    request: Request,
    password: str = Form(...),
    service: BugReportService = Depends(get_bug_report_service),
):
    """Verify the admin password and set the unlock cookie."""
    if not service.verify_admin_password(password):
        return templates.TemplateResponse(
            request,
            "bug_report/unlock.html",
            {
                "active_page": "bug_admin",
                "error": "Incorrect password.",
            },
            status_code=401,
        )
    response = RedirectResponse(url="/ui/bug-report/admin", status_code=302)
    response.set_cookie(
        key=ADMIN_COOKIE,
        value=service.create_admin_token(),
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/bug-report/admin/logout")
async def bug_report_admin_logout():
    """Clear the admin unlock cookie."""
    response = RedirectResponse(url="/ui/bug-report/admin", status_code=302)
    response.delete_cookie(key=ADMIN_COOKIE, path="/")
    return response


@router.post("/bug-report/admin/{report_id}/status", response_class=HTMLResponse)
async def bug_report_admin_update_status(
    request: Request,
    report_id: str,
    status: str = Form(...),
    service: BugReportService = Depends(get_bug_report_service),
):
    """HTMX partial: update a report's status and re-render its row."""
    if not service.validate_admin_token(request.cookies.get(ADMIN_COOKIE)):
        return HTMLResponse(
            '<div class="alert alert-error"><span>⚠️</span><span>Admin session expired. '
            'Reload the page.</span></div>',
            status_code=403,
        )
    try:
        report = service.update_status(report_id, status)
    except ValueError as exc:
        return HTMLResponse(
            f'<div class="alert alert-error"><span>⚠️</span><span>{exc}</span></div>',
            status_code=400,
        )
    if report is None:
        return HTMLResponse(
            f'<div class="alert alert-error"><span>⚠️</span><span>Report {report_id} '
            'not found.</span></div>',
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "bug_report/partials/report_row.html",
        {"report": report.model_dump()},
    )


@router.delete("/bug-report/admin/{report_id}", response_class=HTMLResponse)
async def bug_report_admin_delete(
    request: Request,
    report_id: str,
    service: BugReportService = Depends(get_bug_report_service),
):
    """HTMX partial: delete a report. Returns empty HTML to remove the row."""
    if not service.validate_admin_token(request.cookies.get(ADMIN_COOKIE)):
        return HTMLResponse(
            '<div class="alert alert-error"><span>⚠️</span><span>Admin session expired. '
            'Reload the page.</span></div>',
            status_code=403,
        )
    service.delete_report(report_id)
    return HTMLResponse("")

