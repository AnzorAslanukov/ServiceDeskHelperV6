"""
Pydantic models for search request/response payloads.
"""

from enum import Enum

from pydantic import BaseModel, Field


class TicketType(str, Enum):
    """Supported ticket types for search."""
    incident = "incident"
    servicerequest = "servicerequest"


# ── Request Models ────────────────────────────────────────────────────


class FieldSearchRequest(BaseModel):
    """Request to search tickets by a specific field value."""
    field: str = Field(
        ...,
        description="Athena property name to filter on (e.g., 'contactMethod', 'supportGroup', 'title').",
        examples=["contactMethod"],
    )
    value: str = Field(
        ...,
        description="Value to match against the field.",
        examples=["215-555-1234"],
    )
    operator: str = Field(
        default="eq",
        description="Filter operator: 'eq', 'ne', 'contains', 'like', 'gt', 'lt', etc.",
    )
    ticket_type: TicketType = Field(
        default=TicketType.incident,
        description="Type of ticket to search.",
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Page number (1-based).",
    )
    page_size: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Number of results per page.",
    )


class DescriptionSearchRequest(BaseModel):
    """Request to search tickets by substring in the description field."""
    text: str = Field(
        ...,
        description="Text to search for within ticket descriptions.",
        examples=["printer not printing"],
    )
    ticket_type: TicketType = Field(
        default=TicketType.incident,
        description="Type of ticket to search.",
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Page number (1-based).",
    )
    page_size: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Number of results per page.",
    )


class SemanticSearchRequest(BaseModel):
    """Request for natural-language semantic search across historical tickets."""
    query: str = Field(
        ...,
        description="Natural language description of the issue to search for.",
        examples=["user cannot log into PennChart after password reset"],
    )
    top_k: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Number of similar tickets to return.",
    )


class SimilarTicketRequest(BaseModel):
    """Request to find tickets similar to a given ticket ID."""
    top_k: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Number of similar tickets to return.",
    )


# ── Response Models ───────────────────────────────────────────────────


class TicketSummary(BaseModel):
    """Abbreviated ticket data returned in search results."""
    id: str = Field(description="Ticket ID (e.g., 'IR1959493').")
    title: str | None = Field(default=None, description="Ticket title/summary.")
    status: str | None = Field(default=None, description="Current status.")
    priority: str | int | None = Field(default=None, description="Priority level.")
    support_group: str | None = Field(default=None, description="Assigned support group.")
    affected_user: str | None = Field(default=None, description="Affected user display name.")
    created_date: str | None = Field(default=None, description="Creation timestamp (MM-DD-YYYY HH:MM).")
    description: str | None = Field(default=None, description="Ticket description (may be truncated).")
    location: str | None = Field(default=None, description="Physical location (e.g., 'HUP', 'PPMC').")


class SimilarTicketResult(BaseModel):
    """A ticket match from semantic or similarity search with a similarity score."""
    id: str = Field(description="Ticket ID.")
    title: str | None = Field(default=None, description="Ticket title/summary.")
    similarity: float = Field(description="Cosine similarity score (0.0 to 1.0).")


class DocumentationResult(BaseModel):
    """A knowledge base article match from semantic search."""
    content: str = Field(description="Full text content of the documentation page.")
    notebook: str = Field(description="Source notebook name.")
    section: str = Field(description="Section within the notebook.")
    title: str = Field(description="Page title.")
    similarity: float = Field(description="Cosine similarity score.")


class FieldSearchResponse(BaseModel):
    """Response from field-based or description-based ticket search."""
    tickets: list[TicketSummary] = Field(default_factory=list)
    total: int = Field(default=0, description="Total number of results returned.")
    page: int = Field(default=1, description="Current page number (1-based).")
    page_size: int = Field(default=50, description="Number of results per page.")
    has_more: bool = Field(default=False, description="Whether more pages are available.")


class SemanticSearchResponse(BaseModel):
    """Response from semantic search across ticket embeddings."""
    similar_tickets: list[SimilarTicketResult] = Field(default_factory=list)
    documentation: list[DocumentationResult] = Field(default_factory=list)


class SimilarTicketResponse(BaseModel):
    """Response from ticket similarity search."""
    source_ticket_id: str = Field(description="The ticket ID that was used as the query.")
    similar_tickets: list[SimilarTicketResult] = Field(default_factory=list)