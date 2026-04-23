"""
Chatbot Service — business logic for Feature #2: Q&A Chatbot.

Implements a RAG (Retrieval-Augmented Generation) pipeline:
1. Embed the user's query
2. Retrieve relevant documentation and similar tickets
3. Build a context-augmented prompt
4. Call the LLM for a response
5. Maintain conversation history per session
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from src.clients.databricks_client import DatabricksClient
from src.models.chat import (
    ChatHistoryResponse,
    ChatMessage,
    ChatResponse,
    MessageRole,
    SourceCitation,
    SourceType,
)

# Maximum number of conversation turns to include in the LLM context
MAX_HISTORY_TURNS = 10

SYSTEM_PROMPT = """You are an AI assistant for the Penn Medicine / UPHS IT Service Desk. \
Your role is to help service desk analysts resolve IT issues by providing accurate, \
step-by-step troubleshooting guidance.

You have access to:
1. **Knowledge Base Documentation** — extracted from the UPHS and LGH OneNote service desk notebooks
2. **Historical Ticket Data** — similar incidents that have been resolved in the past

When answering:
- Provide clear, actionable steps
- Reference specific documentation or ticket IDs when relevant
- If the documentation contains an escalation path or support group, include it
- If you're unsure, say so rather than guessing
- Keep responses concise but thorough

Below is the retrieved context for the current question:

{context}"""


class ChatbotService:
    """Orchestrates the RAG chatbot pipeline across Databricks services."""

    def __init__(self, databricks_client: DatabricksClient) -> None:
        self._databricks = databricks_client
        # In-memory session store: session_id -> list of ChatMessage
        self._sessions: dict[str, list[ChatMessage]] = {}

    # ── Public API ────────────────────────────────────────────────────

    async def chat(
        self,
        message: str,
        session_id: str | None = None,
        top_k_docs: int = 5,
        top_k_tickets: int = 5,
        max_tokens: int = 2048,
    ) -> ChatResponse:
        """
        Process a user message through the RAG pipeline and return an AI response.

        Args:
            message: The user's question or message.
            session_id: Existing session ID, or None to create a new session.
            top_k_docs: Number of documentation articles to retrieve.
            top_k_tickets: Number of similar tickets to retrieve.
            max_tokens: Maximum tokens in the LLM response.

        Returns:
            ChatResponse with the assistant's message, sources, and session ID.
        """
        # Ensure session exists
        if session_id is None or session_id not in self._sessions:
            session_id = session_id or str(uuid.uuid4())
            self._sessions[session_id] = []

        # Record the user message
        user_msg = ChatMessage(
            role=MessageRole.user,
            content=message,
            timestamp=datetime.now(UTC),
        )
        self._sessions[session_id].append(user_msg)

        # Step 1: Generate embedding for the user's message
        query_embedding = await self._databricks.generate_embedding(message)

        # Step 2: Retrieve documentation and similar tickets in parallel
        loop = asyncio.get_event_loop()
        doc_results, ticket_results = await asyncio.gather(
            loop.run_in_executor(
                None,
                self._databricks.find_similar_documentation,
                query_embedding,
                top_k_docs,
            ),
            loop.run_in_executor(
                None,
                self._databricks.find_similar_by_embedding,
                query_embedding,
                "scratchpad.aslanuka.ir_embeddings",
                "ticket_embedding",
                "id",
                top_k_tickets,
            ),
        )

        # Step 3: Build source citations
        sources = self._build_sources(doc_results, ticket_results)

        # Step 4: Build the context string and LLM messages
        context = self._build_context(doc_results, ticket_results)
        llm_messages = self._build_llm_messages(session_id, context)

        # Step 5: Call the LLM
        assistant_text = await self._databricks.call_llm(llm_messages, max_tokens=max_tokens)

        # Step 6: Record the assistant message
        assistant_msg = ChatMessage(
            role=MessageRole.assistant,
            content=assistant_text,
            sources=sources,
            timestamp=datetime.now(UTC),
        )
        self._sessions[session_id].append(assistant_msg)

        return ChatResponse(
            message=assistant_text,
            sources=sources,
            session_id=session_id,
        )

    def reset_session(self, session_id: str) -> bool:
        """
        Clear the conversation history for a session.

        Args:
            session_id: The session to reset.

        Returns:
            True if the session existed and was reset, False if not found.
        """
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def get_history(self, session_id: str) -> ChatHistoryResponse:
        """
        Retrieve the conversation history for a session.

        Args:
            session_id: The session to retrieve.

        Returns:
            ChatHistoryResponse with the session's messages.
        """
        messages = self._sessions.get(session_id, [])
        return ChatHistoryResponse(
            session_id=session_id,
            messages=messages,
        )

    # ── Private Helpers ───────────────────────────────────────────────

    @staticmethod
    def _build_sources(
        doc_results: list[dict[str, Any]],
        ticket_results: list[dict[str, Any]],
    ) -> list[SourceCitation]:
        """Build source citations from retrieval results."""
        sources: list[SourceCitation] = []

        for doc in doc_results:
            content = doc.get("content", "")
            preview = content[:200] + "..." if len(content) > 200 else content
            sources.append(
                SourceCitation(
                    type=SourceType.documentation,
                    title=doc.get("title", "Untitled"),
                    similarity=doc.get("similarity", 0.0),
                    content_preview=preview,
                    notebook=doc.get("notebook"),
                    section=doc.get("section"),
                )
            )

        for ticket in ticket_results:
            sources.append(
                SourceCitation(
                    type=SourceType.ticket,
                    title=ticket.get("id", "Unknown"),
                    similarity=ticket.get("similarity", 0.0),
                )
            )

        return sources

    @staticmethod
    def _build_context(
        doc_results: list[dict[str, Any]],
        ticket_results: list[dict[str, Any]],
    ) -> str:
        """Build the context string injected into the system prompt."""
        parts: list[str] = []

        if doc_results:
            parts.append("=== KNOWLEDGE BASE DOCUMENTATION ===")
            for i, doc in enumerate(doc_results, 1):
                title = doc.get("title", "Untitled")
                section = doc.get("section", "Unknown Section")
                notebook = doc.get("notebook", "unknown")
                content = doc.get("content", "")
                similarity = doc.get("similarity", 0.0)
                parts.append(
                    f"\n--- Document {i} (similarity: {similarity:.3f}) ---\n"
                    f"Notebook: {notebook} | Section: {section} | Title: {title}\n"
                    f"{content}"
                )

        if ticket_results:
            parts.append("\n=== SIMILAR HISTORICAL TICKETS ===")
            for i, ticket in enumerate(ticket_results, 1):
                ticket_id = ticket.get("id", "Unknown")
                similarity = ticket.get("similarity", 0.0)
                parts.append(f"- Ticket {ticket_id} (similarity: {similarity:.3f})")

        if not parts:
            return "No relevant documentation or similar tickets were found."

        return "\n".join(parts)

    def _build_llm_messages(
        self,
        session_id: str,
        context: str,
    ) -> list[dict[str, str]]:
        """
        Build the message list for the LLM call.

        Includes the system prompt with context, plus recent conversation history.
        """
        messages: list[dict[str, str]] = []

        # System prompt with retrieved context
        messages.append({
            "role": "system",
            "content": SYSTEM_PROMPT.format(context=context),
        })

        # Include recent conversation history (up to MAX_HISTORY_TURNS pairs)
        history = self._sessions.get(session_id, [])
        # Take the last N messages (user + assistant pairs)
        recent = history[-(MAX_HISTORY_TURNS * 2):]
        for msg in recent:
            if msg.role in (MessageRole.user, MessageRole.assistant):
                messages.append({
                    "role": msg.role.value,
                    "content": msg.content,
                })

        return messages