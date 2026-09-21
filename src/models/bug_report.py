"""
Pydantic models for the Bug Report feature.

Users submit bug reports from an always-available widget anchored to the
bottom of every page. Reports are persisted to an append-only JSONL file and
managed from a password-protected admin page.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# Severity levels a reporter can choose from.
Severity = Literal["low", "medium", "high", "blocker"]

# Lifecycle statuses an admin can set on a report.
BugStatus = Literal["open", "in_progress", "resolved", "wont_fix"]

# Valid status transitions the admin page/API accepts.
VALID_STATUSES: tuple[str, ...] = ("open", "in_progress", "resolved", "wont_fix")


# ── Request Models ────────────────────────────────────────────────────


class BugReportRequest(BaseModel):
    """User-submitted bug report from the in-app widget."""

    feature: str = Field(
        default="",
        max_length=100,
        description="Feature/page where the bug occurred, e.g. 'search'.",
    )
    summary: str = Field(
        min_length=3,
        max_length=200,
        description="One-line summary of the bug.",
    )
    description: str = Field(
        min_length=3,
        max_length=5000,
        description="What happened, plus steps to reproduce.",
    )
    severity: Severity = Field(
        default="medium",
        description="Reporter-assessed severity.",
    )
    page_url: str = Field(
        default="",
        max_length=500,
        description="URL of the page, captured client-side.",
    )
    user_agent: str = Field(
        default="",
        max_length=500,
        description="Browser user-agent string, captured client-side.",
    )


# ── Persisted / Response Models ───────────────────────────────────────


class BugReport(BugReportRequest):
    """A persisted bug report with server-assigned metadata."""

    id: str = Field(description="Sequential identifier, e.g. 'BUG-42'.")
    reported_by: str = Field(description="Username of the reporter.")
    reported_at: datetime = Field(description="UTC timestamp when submitted.")
    status: BugStatus = Field(default="open", description="Lifecycle status.")


class BugReportResponse(BaseModel):
    """Response returned after a successful submission."""

    id: str
    message: str
