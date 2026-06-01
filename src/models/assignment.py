"""
Pydantic models for Feature #3: Ticket Assignment Recommendation.

Uses a TF-IDF classifier for fast, reliable support group prediction.
"""

from pydantic import BaseModel, Field


# ── Request Models ────────────────────────────────────────────────────


class AssignmentRequest(BaseModel):
    """Optional parameters for the assignment recommendation endpoint."""

    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of alternative predictions to return.",
    )


# ── Response Models ───────────────────────────────────────────────────


class ClassifierPrediction(BaseModel):
    """A single support group prediction from the classifier."""

    support_group: str = Field(
        description="Predicted support group name.",
    )
    confidence: float = Field(
        description="Confidence score (0.0 to 1.0).",
    )


class AssignmentRecommendation(BaseModel):
    """The classifier's structured recommendation for ticket assignment."""

    support_group_name: str = Field(
        description="Recommended support group name (e.g., 'HUP', 'User Provisioning').",
    )
    support_group_guid: str = Field(
        description="GUID for the recommended support group, correct for the ticket type (IR vs SR).",
    )
    confidence: float = Field(
        description="Confidence score for the top prediction (0.0 to 1.0).",
    )
    method: str = Field(
        description="Method used: 'classifier' or 'triage_rule'.",
    )
    alternatives: list[ClassifierPrediction] = Field(
        default_factory=list,
        description="Alternative predictions ranked by confidence.",
    )


class TicketInfo(BaseModel):
    """Summary of the fetched ticket used for the recommendation."""

    id: str = Field(description="Ticket ID (e.g., 'IR1959493').")
    ticket_type: str = Field(description="Ticket type: 'incident' or 'servicerequest'.")
    title: str | None = Field(default=None, description="Ticket title/summary.")
    description: str | None = Field(default=None, description="Ticket description (full, no truncation).")
    status: str | None = Field(default=None, description="Current status.")
    priority: str | int | None = Field(default=None, description="Current priority.")
    support_group: str | None = Field(default=None, description="Currently assigned support group.")
    affected_user: str | None = Field(default=None, description="Affected user display name.")
    affected_user_title: str | None = Field(default=None, description="Affected user job title.")
    affected_user_phone: str | None = Field(default=None, description="Affected user phone number.")
    location: str | None = Field(default=None, description="Physical location.")
    floor: str | None = Field(default=None, description="Floor.")
    room: str | None = Field(default=None, description="Room.")
    classification: str | None = Field(default=None, description="Classification/Area.")
    source: str | None = Field(default=None, description="Ticket source (e.g., Web Portal, Phone).")
    created_by: str | None = Field(default=None, description="User who created the ticket.")
    created_date: str | None = Field(default=None, description="Creation timestamp.")
    modified_date: str | None = Field(default=None, description="Last modified timestamp.")


class AssignmentResponse(BaseModel):
    """Response from the ticket assignment recommendation endpoint."""

    ticket: TicketInfo = Field(description="Summary of the ticket being analyzed.")
    recommendation: AssignmentRecommendation = Field(
        description="The classifier-generated assignment recommendation.",
    )