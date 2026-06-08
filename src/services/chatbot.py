"""
Chatbot Service — business logic for Feature #2: Q&A Chatbot.

Implements a Graph-First, Text-Fallback RAG pipeline:
1. Query the local knowledge graph for structured facts (escalations, procedures, priority rules)
2. If sufficient structured context is found → use it as primary context (skip text similarity)
3. If NOT sufficient → fall back to text similarity search against OneNote documentation
4. Always include similar historical tickets as supplementary context
5. Call the LLM with the assembled context + conversation history
6. Maintain conversation history per session
"""

import asyncio
import logging
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
from src.services.knowledge_graph import KnowledgeGraphService

logger = logging.getLogger(__name__)

# Maximum number of conversation turns to include in the LLM context
MAX_HISTORY_TURNS = 10

SYSTEM_PROMPT = """You are an AI assistant for the Penn Medicine / UPHS IT Service Desk. \
Your role is to help service desk analysts resolve IT issues by providing accurate, \
step-by-step troubleshooting guidance.

You have access to:
1. **Structured Knowledge Graph** — pre-extracted escalation paths, priority rules, \
troubleshooting procedures, and system dependencies from the service desk documentation
2. **Knowledge Base Documentation** — raw text from the UPHS and LGH OneNote service desk notebooks
3. **Historical Ticket Data** — similar incidents that have been resolved in the past

When answering:
- Provide clear, actionable steps
- If structured escalation paths or priority rules are provided, present them prominently
- If troubleshooting procedures with ordered steps are provided, present them in order
- Reference specific documentation or ticket IDs when relevant
- If the documentation contains an escalation path or support group, include it
- If you're unsure, say so rather than guessing
- Keep responses concise but thorough

Below is the retrieved context for the current question:

{context}"""


class ChatbotService:
    """Orchestrates the Graph-First RAG chatbot pipeline."""

    def __init__(
        self,
        databricks_client: DatabricksClient,
        knowledge_graph_service: KnowledgeGraphService | None = None,
    ) -> None:
        self._databricks = databricks_client
        self._knowledge_graph = knowledge_graph_service
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
        Process a user message through the Graph-First RAG pipeline.

        Strategy:
        1. Query knowledge graph for structured facts
        2. If graph has sufficient context → use structured facts only (no text search)
        3. If graph lacks context → fall back to text similarity search
        4. Always retrieve similar tickets for supplementary context
        5. Call LLM with assembled context

        Args:
            message: The user's question or message.
            session_id: Existing session ID, or None to create a new session.
            top_k_docs: Number of documentation articles to retrieve (fallback mode).
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

        # Step 1: Query knowledge graph for structured facts
        graph_result = self._query_knowledge_graph(message)
        graph_context = ""
        if graph_result and graph_result.get("facts"):
            graph_context = self._knowledge_graph.format_facts_for_llm(graph_result)
            logger.info(
                "Knowledge graph returned %d facts (sufficient=%s, systems=%s)",
                len(graph_result["facts"]),
                graph_result["has_sufficient_context"],
                graph_result.get("systems_matched", []),
            )

        # Step 2: Generate embedding for ticket similarity (always needed)
        query_embedding = await self._databricks.generate_embedding(message)

        # Step 3: Decide retrieval strategy
        use_text_fallback = not (graph_result and graph_result.get("has_sufficient_context"))

        if use_text_fallback:
            # Graph didn't have enough — do text similarity search too
            logger.info("Graph context insufficient, falling back to text similarity search")
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
                    "hive_metastore.embeddings_db.ticket_embeddings",
                    "embedding",
                    "Id",
                    top_k_tickets,
                ),
            )
        else:
            # Graph has sufficient context — only get similar tickets
            logger.info("Graph context sufficient, skipping text similarity search")
            doc_results = []
            loop = asyncio.get_event_loop()
            ticket_results = await loop.run_in_executor(
                None,
                self._databricks.find_similar_by_embedding,
                query_embedding,
                "hive_metastore.embeddings_db.ticket_embeddings",
                "embedding",
                "Id",
                top_k_tickets,
            )

        # Step 4: Build source citations
        sources = self._build_sources(graph_result, doc_results, ticket_results)

        # Step 5: Build the context string and LLM messages
        context = self._build_context(graph_context, doc_results, ticket_results)
        llm_messages = self._build_llm_messages(session_id, context)

        # Step 6: Call the LLM
        assistant_text = await self._databricks.call_llm(llm_messages, max_tokens=max_tokens)

        # Step 7: Record the assistant message
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

    def _query_knowledge_graph(self, message: str) -> dict[str, Any] | None:
        """Query the knowledge graph if available."""
        if self._knowledge_graph is None or not self._knowledge_graph.is_available:
            return None
        try:
            return self._knowledge_graph.query_for_chat(message)
        except Exception:
            logger.exception("Knowledge graph query failed, will use text fallback")
            return None

    @staticmethod
    def _build_sources(
        graph_result: dict[str, Any] | None,
        doc_results: list[dict[str, Any]],
        ticket_results: list[dict[str, Any]],
    ) -> list[SourceCitation]:
        """Build source citations from all retrieval results."""
        sources: list[SourceCitation] = []

        # Knowledge graph sources
        if graph_result and graph_result.get("facts"):
            # Add a single citation for the knowledge graph
            fact_types = set(f.get("type", "") for f in graph_result["facts"])
            kg_title = f"Knowledge Graph ({len(graph_result['facts'])} facts: {', '.join(sorted(fact_types))})"
            sources.append(
                SourceCitation(
                    type=SourceType.documentation,
                    title=kg_title,
                    similarity=1.0,  # Graph matches are exact
                    content_preview=f"Systems: {', '.join(graph_result.get('systems_matched', [])[:3])}"
                    if graph_result.get("systems_matched")
                    else "Text search results",
                )
            )

        # Documentation sources (from text similarity fallback)
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

        # Ticket sources
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
        graph_context: str,
        doc_results: list[dict[str, Any]],
        ticket_results: list[dict[str, Any]],
    ) -> str:
        """Build the context string injected into the system prompt."""
        parts: list[str] = []

        # Knowledge graph structured facts (primary)
        if graph_context:
            parts.append(graph_context)

        # Raw documentation (fallback — only present if graph was insufficient)
        if doc_results:
            parts.append("\n=== KNOWLEDGE BASE DOCUMENTATION ===")
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

        # Similar tickets (always included)
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