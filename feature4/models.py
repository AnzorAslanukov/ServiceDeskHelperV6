"""
Pydantic models for Feature #4: Bulk Ticket Recommendation and Assignment.

Covers queue display, ticket locking, batch recommendations,
bulk assignment, and WebSocket event payloads.
"""

from enum import Enum

from pydantic import BaseModel, Field

# Read-only imports from core (never modify these source files)
from src.models.assignment import AssignmentRecommendation, TicketInfo


# ── Enums ─────────────────────────────────────────────────────────────


class WSEventType(str, Enum):
    """WebSocket event types for real-time queue synchronization."""

    LOCK = "lock"
    UNLOCK = "unlock"
    ASSIGN = "assign"
    STATE_SYNC = "state_sync"


# ── Queue Models ──────────────────────────────────────────────────────


class QueueRequest(BaseModel):
    """Parameters for fetching the Validation queue."""

    tier_queue_name: str = Field(
        default="Validation",
        description="Tier queue name to fetch tickets from.",
    )
    statuses: list[str] = Field(
        default_factory=lambda: ["Active", "Work in Progress"],
        description="Ticket statuses to include in the queue.",
    )


class QueueTicketSummary(BaseModel):
    """Lightweight ticket summary for queue display."""

    id: str = Field(description="Ticket ID (e.g., 'IR1959493').")
    entity_id: str = Field(description="Athena entityId GUID (required for PUT updates).")
    ticket_type: str = Field(description="'incident' or 'servicerequest'.")
    title: str | None = Field(default=None, description="Ticket title/summary.")
    description: str | None = Field(default=None, description="Ticket description (truncated).")
    status: str | None = Field(default=None, description="Current status.")
    priority: str | int | None = Field(default=None, description="Current priority.")
    tier_queue: str | None = Field(default=None, description="Current tier queue name.")
    affected_user: str | None = Field(default=None, description="Affected user display name.")
    assigned_user: str | None = Field(default=None, description="Assigned analyst display name.")
    location: str | None = Field(default=None, description="Ticket location.")
    created_date: str | None = Field(default=None, description="Creation timestamp.")
    locked_by: str | None = Field(
        default=None,
        description="User ID who has locked this ticket, or None if unlocked.",
    )


class QueueResponse(BaseModel):
    """Response from the queue fetch endpoint."""

    tickets: list[QueueTicketSummary] = Field(
        default_factory=list,
        description="List of tickets in the queue.",
    )
    total: int = Field(default=0, description="Total number of tickets in the queue.")
    locks: dict[str, str] = Field(
        default_factory=dict,
        description="Current lock state: ticket_id → user_id.",
    )


# ── Lock Models ───────────────────────────────────────────────────────


class LockRequest(BaseModel):
    """Request to lock or unlock tickets."""

    ticket_ids: list[str] = Field(
        ...,
        min_length=1,
        description="List of ticket IDs to lock/unlock.",
    )
    user_id: str = Field(
        ...,
        description="User ID requesting the lock/unlock.",
    )


class ClaimBatchRequest(BaseModel):
    """Request to auto-claim the next N unlocked tickets."""

    user_id: str = Field(..., description="User ID claiming the batch.")
    batch_size: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Number of tickets to claim.",
    )
    ticket_ids: list[str] | None = Field(
        default=None,
        description=(
            "Ordered list of ticket IDs from the client's current queue view. "
            "When provided, the server skips re-fetching the queue from Athena "
            "and uses these IDs directly for claim selection. "
            "If omitted, the server falls back to fetching the queue."
        ),
    )


class ClaimBatchResponse(BaseModel):
    """Response from the claim batch endpoint."""

    claimed_ticket_ids: list[str] = Field(
        default_factory=list,
        description="Ticket IDs that were successfully claimed/locked.",
    )
    total_claimed: int = Field(default=0, description="Number of tickets claimed.")


# ── Recommendation Models ─────────────────────────────────────────────


class BulkRecommendRequest(BaseModel):
    """Request to generate classifier recommendations for a batch of tickets."""

    ticket_ids: list[str] = Field(
        ...,
        min_length=1,
        description="List of ticket IDs to generate recommendations for.",
    )
    user_id: str | None = Field(
        default=None,
        description="User ID who initiated the recommendation batch (for WebSocket progress events).",
    )


class TicketRecommendation(BaseModel):
    """AI recommendation for a single ticket in a bulk batch."""

    ticket_id: str = Field(description="Ticket ID this recommendation is for.")
    ticket_info: TicketInfo = Field(description="Summary of the ticket.")
    recommendation: AssignmentRecommendation = Field(
        description="AI-generated assignment recommendation.",
    )
    success: bool = Field(default=True, description="Whether recommendation generation succeeded.")
    error: str | None = Field(default=None, description="Error message if generation failed.")


class BulkRecommendResponse(BaseModel):
    """Response from the bulk recommendation endpoint."""

    recommendations: list[TicketRecommendation] = Field(
        default_factory=list,
        description="Per-ticket recommendations.",
    )
    total: int = Field(default=0, description="Total recommendations generated.")
    failed: int = Field(default=0, description="Number of tickets that failed recommendation.")


# ── Assignment Models ─────────────────────────────────────────────────


class TicketAssignment(BaseModel):
    """Assignment details for a single ticket."""

    ticket_id: str = Field(description="Ticket ID (e.g., 'IR1959493').")
    entity_id: str = Field(description="Athena entityId GUID (required for PUT).")
    tier_queue_guid: str = Field(
        description="GUID of the target support group/tier queue.",
    )
    tier_queue_name: str = Field(
        default="",
        description="Human-readable name of the target tier queue (for display).",
    )
    priority: int | str | None = Field(
        default=None,
        description="Priority to set. None means don't change priority.",
    )


class BulkAssignRequest(BaseModel):
    """Request to assign a batch of tickets in Athena."""

    assignments: list[TicketAssignment] = Field(
        ...,
        min_length=1,
        description="List of ticket assignments to execute.",
    )
    user_id: str = Field(
        ...,
        description="User ID performing the assignments.",
    )


class TicketAssignResult(BaseModel):
    """Result of assigning a single ticket."""

    ticket_id: str = Field(description="Ticket ID.")
    success: bool = Field(description="Whether the assignment succeeded.")
    error: str | None = Field(default=None, description="Error message if assignment failed.")
    updated_tier_queue: str | None = Field(
        default=None,
        description="Tier queue name after update (from Athena response).",
    )
    updated_priority: int | str | None = Field(
        default=None,
        description="Priority after update (from Athena response).",
    )


class BulkAssignResponse(BaseModel):
    """Response from the bulk assignment endpoint."""

    results: list[TicketAssignResult] = Field(
        default_factory=list,
        description="Per-ticket assignment results.",
    )
    total_assigned: int = Field(default=0, description="Number of tickets successfully assigned.")
    total_failed: int = Field(default=0, description="Number of tickets that failed assignment.")


# ── WebSocket Event Models ────────────────────────────────────────────


class WSLockEvent(BaseModel):
    """WebSocket event: a ticket was locked by a user."""

    event: WSEventType = Field(default=WSEventType.LOCK)
    ticket_id: str = Field(description="Ticket ID that was locked.")
    user_id: str = Field(description="User who locked the ticket.")


class WSUnlockEvent(BaseModel):
    """WebSocket event: a ticket was unlocked."""

    event: WSEventType = Field(default=WSEventType.UNLOCK)
    ticket_id: str = Field(description="Ticket ID that was unlocked.")
    user_id: str = Field(description="User who released the lock.")


class WSAssignEvent(BaseModel):
    """WebSocket event: a ticket was assigned and removed from the queue."""

    event: WSEventType = Field(default=WSEventType.ASSIGN)
    ticket_id: str = Field(description="Ticket ID that was assigned.")
    user_id: str = Field(description="User who performed the assignment.")


class WSStateSyncEvent(BaseModel):
    """WebSocket event: full lock state sent on connect."""

    event: WSEventType = Field(default=WSEventType.STATE_SYNC)
    locks: dict[str, str] = Field(
        default_factory=dict,
        description="Full lock state: ticket_id → user_id.",
    )