"""
Pydantic models for Feature #5: Turnover Email Draft Generator.
"""

from pydantic import BaseModel, Field


# ── Request Models ────────────────────────────────────────────────────


class TurnoverRequest(BaseModel):
    """Input parameters for the turnover email generation endpoint."""

    turnover_agent_name: str = Field(
        description="Name of the agent taking over the shift.",
    )
    sender_name: str = Field(
        description="Name of the person sending the turnover email.",
    )
    notes: str = Field(
        default="",
        description="Free-text notes for the 'Verbal Turnover' section.",
    )
    escalation_notes: str = Field(
        default="",
        description="Notes for the 'Escalated to Manager/ISOD/ISMT' section.",
    )
    voicemail_notes: str = Field(
        default="",
        description="Notes for the 'On-Call Analyst Voicemails Left' section.",
    )
    hours_lookahead: int = Field(
        default=24,
        ge=1,
        le=168,
        description="How many hours ahead to look for upcoming change requests.",
    )


# ── Data Models ───────────────────────────────────────────────────────


class TurnoverTicketSummary(BaseModel):
    """Summary of a SEV incident ticket for the turnover email."""

    id: str = Field(description="Ticket ID (e.g., 'IR1959493').")
    title: str | None = Field(default=None, description="Ticket title/summary.")
    status: str | None = Field(default=None, description="Current status.")
    priority: int | None = Field(default=None, description="Numeric priority (1=P1, 2=P2).")
    support_group: str | None = Field(default=None, description="Assigned support group.")
    affected_user: str | None = Field(default=None, description="Affected user display name.")
    assigned_user: str | None = Field(default=None, description="Assigned analyst display name.")
    created_date: str | None = Field(default=None, description="Creation timestamp.")
    is_parent: bool = Field(default=False, description="Whether this is a parent incident.")


class ChangeRequestSummary(BaseModel):
    """Summary of an upcoming change request for the turnover email."""

    id: str = Field(description="Change request ID (e.g., 'CR10312956').")
    title: str | None = Field(default=None, description="CR title/summary.")
    status: str | None = Field(default=None, description="Current status.")
    scheduled_start: str | None = Field(default=None, description="Scheduled start date/time.")
    scheduled_end: str | None = Field(default=None, description="Scheduled end date/time.")
    downtime: str | None = Field(default=None, description="Downtime information.")
    category: str | None = Field(default=None, description="Change category (Standard, Minor, Major, Emergency).")


# ── Response Models ───────────────────────────────────────────────────


class TurnoverResponse(BaseModel):
    """Response from the turnover email generation endpoint."""

    email_to: str = Field(description="Email To field.")
    email_cc: str = Field(description="Email CC field.")
    email_subject: str = Field(description="Email Subject line.")
    email_body: str = Field(description="Full email body text, ready to copy-paste.")
    upcoming_outages: list[ChangeRequestSummary] = Field(
        default_factory=list,
        description="Upcoming change requests with scheduled outages/downtimes.",
    )
    parent_tickets: list[TurnoverTicketSummary] = Field(
        default_factory=list,
        description="Active parent (major) incidents.",
    )
    active_sevs: list[TurnoverTicketSummary] = Field(
        default_factory=list,
        description="Active P1/P2 severity incidents.",
    )
    pended_sevs: list[TurnoverTicketSummary] = Field(
        default_factory=list,
        description="Pended (pending) P1/P2 severity incidents.",
    )
    total_tickets: int = Field(
        default=0,
        description="Total number of SEV tickets (active + pended + parent).",
    )