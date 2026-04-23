"""
Pydantic models for the Q&A Chatbot request/response payloads.
"""

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────


class MessageRole(str, Enum):
    """Roles in a chat conversation."""

    user = "user"
    assistant = "assistant"
    system = "system"


class SourceType(str, Enum):
    """Types of source citations returned with chatbot responses."""

    documentation = "documentation"
    ticket = "ticket"


# ── Request Models ────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    """Request to send a message to the Q&A chatbot."""

    message: str = Field(
        ...,
        min_length=1,
        description="The user's question or message.",
        examples=["How do I reset a user's PennChart password?"],
    )
    session_id: str | None = Field(
        default=None,
        description="Session ID for multi-turn conversation. "
        "If omitted, a new session is created.",
    )
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


class ResetSessionRequest(BaseModel):
    """Request to reset (clear) a chat session."""

    session_id: str = Field(
        ...,
        description="Session ID to reset.",
    )


# ── Response Models ───────────────────────────────────────────────────


class SourceCitation(BaseModel):
    """A source used to generate the chatbot's response."""

    type: SourceType = Field(description="Whether this source is documentation or a ticket.")
    title: str = Field(description="Title or ID of the source.")
    similarity: float = Field(description="Cosine similarity score (0.0 to 1.0).")
    content_preview: str | None = Field(
        default=None,
        description="Preview of the source content (truncated).",
    )
    notebook: str | None = Field(
        default=None,
        description="Source notebook name (documentation only).",
    )
    section: str | None = Field(
        default=None,
        description="Section within the notebook (documentation only).",
    )


class ChatMessage(BaseModel):
    """A single message in a chat conversation."""

    role: MessageRole = Field(description="Who sent the message.")
    content: str = Field(description="Message content.")
    sources: list[SourceCitation] = Field(
        default_factory=list,
        description="Source citations (only present on assistant messages).",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the message was created.",
    )


class ChatResponse(BaseModel):
    """Response from the Q&A chatbot."""

    message: str = Field(description="The assistant's response text.")
    sources: list[SourceCitation] = Field(
        default_factory=list,
        description="Sources used to generate the response.",
    )
    session_id: str = Field(description="Session ID for continuing the conversation.")


class ChatHistoryResponse(BaseModel):
    """Response containing the conversation history for a session."""

    session_id: str = Field(description="The session ID.")
    messages: list[ChatMessage] = Field(
        default_factory=list,
        description="Ordered list of messages in the conversation.",
    )