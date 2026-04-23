"""
Pydantic models for Feature #3: Ticket Assignment Recommendation.
"""

from pydantic import BaseModel, Field

from src.models.chat import SourceCitation


# ── Request Models ────────────────────────────────────────────────────


class AssignmentRequest(BaseModel):
    """Optional parameters for the assignment recommendation endpoint."""

    top_k_docs: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of documentation articles to retrieve for context.",
    )
    top_k_tickets: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of similar tickets to retrieve for context.",
    )
    max_tokens: int = Field(
        default=2048,
        ge=100,
        le=4096,
        description="Maximum tokens in the LLM response.",
    )


# ── Response Models ───────────────────────────────────────────────────


class AssignmentRecommendation(BaseModel):
    """The LLM's structured recommendation for ticket assignment."""

    support_group_name: str = Field(
        description="Recommended support group name (e.g., 'EUS\\HUP', 'PennChart\\User Provisioning').",
    )
    support_group_guid: str = Field(
        description="GUID for the recommended support group, correct for the ticket type (IR vs SR).",
    )
    priority: str | int = Field(
        description="Recommended priority level (e.g., 1, 2, 3 for IR or 'Low', 'Medium', 'High' for SR).",
    )
    rationale: str = Field(
        description="Explanation of why this support group and priority were recommended.",
    )


class TicketInfo(BaseModel):
    """Summary of the fetched ticket used for the recommendation."""

    id: str = Field(description="Ticket ID (e.g., 'IR1959493').")
    ticket_type: str = Field(description="Ticket type: 'incident' or 'servicerequest'.")
    title: str | None = Field(default=None, description="Ticket title/summary.")
    description: str | None = Field(default=None, description="Ticket description.")
    status: str | None = Field(default=None, description="Current status.")
    priority: str | int | None = Field(default=None, description="Current priority.")
    support_group: str | None = Field(default=None, description="Currently assigned support group.")
    affected_user: str | None = Field(default=None, description="Affected user display name.")
    affected_user_title: str | None = Field(default=None, description="Affected user job title.")
    location: str | None = Field(default=None, description="Physical location.")
    created_date: str | None = Field(default=None, description="Creation timestamp.")


class AssignmentResponse(BaseModel):
    """Response from the ticket assignment recommendation endpoint."""

    ticket: TicketInfo = Field(description="Summary of the ticket being analyzed.")
    recommendation: AssignmentRecommendation = Field(
        description="The AI-generated assignment recommendation.",
    )
    sources: list[SourceCitation] = Field(
        default_factory=list,
        description="Sources used to generate the recommendation.",
    )