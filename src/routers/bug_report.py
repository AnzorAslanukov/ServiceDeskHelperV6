"""
Bug Report API Router — REST endpoints for the Bug Report feature.

Endpoints (JSON — used by the CLI and programmatic clients):
    POST /bug-report              — Submit a new bug report
    GET  /bug-report              — List reports (admin only; optional ?status=)
    GET  /bug-report/{id}         — Get a single report (admin only)
    PATCH /bug-report/{id}/status — Update a report's status (admin only)
    DELETE /bug-report/{id}       — Delete a report (admin only)

The always-visible submission widget and the password-protected admin page are
served as HTML/HTMX from ``src/routers/frontend.py``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from src.dependencies import get_bug_report_service, get_current_user
from src.models.bug_report import BugReport, BugReportRequest, BugReportResponse
from src.services.bug_report import BugReportService

router = APIRouter(prefix="/bug-report", tags=["bug-report"])

# Cookie set once the admin password has been verified.
ADMIN_COOKIE = "sdh_bug_admin"


class StatusUpdate(BaseModel):
    """Payload for updating a report's lifecycle status."""

    status: str


def _require_admin(
    request: Request,
    service: BugReportService,
) -> None:
    """Raise 403 unless the request carries a valid admin unlock cookie."""
    token = request.cookies.get(ADMIN_COOKIE)
    if not service.validate_admin_token(token):
        raise HTTPException(status_code=403, detail="Admin authentication required.")


@router.post("", response_model=BugReportResponse)
async def submit_bug_report(
    payload: BugReportRequest,
    request: Request,
    service: BugReportService = Depends(get_bug_report_service),
) -> BugReportResponse:
    """Submit a new bug report. Available to any authenticated user."""
    user = get_current_user(request)
    reported_by = user.username if user else "unknown"
    report = service.submit(payload, reported_by=reported_by)
    return BugReportResponse(
        id=report.id,
        message=f"Thanks! Your bug report was filed as {report.id}.",
    )


@router.get("", response_model=list[BugReport])
async def list_bug_reports(
    request: Request,
    status: str | None = Query(default=None),
    service: BugReportService = Depends(get_bug_report_service),
) -> list[BugReport]:
    """List all bug reports (admin only)."""
    _require_admin(request, service)
    return service.list_reports(status=status)


@router.get("/{report_id}", response_model=BugReport)
async def get_bug_report(
    report_id: str,
    request: Request,
    service: BugReportService = Depends(get_bug_report_service),
) -> BugReport:
    """Get a single bug report by id (admin only)."""
    _require_admin(request, service)
    report = service.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found.")
    return report


@router.patch("/{report_id}/status", response_model=BugReport)
async def update_bug_report_status(
    report_id: str,
    payload: StatusUpdate,
    request: Request,
    service: BugReportService = Depends(get_bug_report_service),
) -> BugReport:
    """Update a report's status (admin only)."""
    _require_admin(request, service)
    report = service.update_status(report_id, payload.status)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found.")
    return report


@router.delete("/{report_id}")
async def delete_bug_report(
    report_id: str,
    request: Request,
    service: BugReportService = Depends(get_bug_report_service),
) -> dict:
    """Delete a report by id (admin only)."""
    _require_admin(request, service)
    if not service.delete_report(report_id):
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found.")
    return {"deleted": report_id}
