"""
Chatbot Service — business logic for Feature #2: Q&A Chatbot.

Implements a Graph-First, Text-Fallback RAG pipeline:
1. Query the local knowledge graph for structured facts (escalations, procedures, priority rules)
2. If sufficient structured context is found → use it as primary context (skip text similarity)
3. If NOT sufficient → fall back to text similarity search against OneNote documentation
4. Always include similar historical tickets as supplementary context
5. For referenced tickets: run the TF-IDF classifier to predict support group assignment
6. Call the LLM with the assembled context + conversation history
7. Maintain conversation history per session
"""

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from src.clients.athena_client import AthenaClient
from src.clients.databricks_client import DatabricksClient
from src.models.chat import (
    ChatHistoryResponse,
    ChatMessage,
    ChatResponse,
    MessageRole,
    SourceCitation,
    SourceType,
)
from src.services.assignment import (
    IR_SUPPORT_GROUPS,
    SR_SUPPORT_GROUPS,
    check_service_desk_triage,
    check_specific_triage,
    resolve_group_guid,
)
from src.services.knowledge_graph import KnowledgeGraphService
from src.services.ticket_classifier import TicketClassifier, get_ticket_classifier

logger = logging.getLogger(__name__)

# Maximum number of conversation turns to include in the LLM context
MAX_HISTORY_TURNS = 10

# Regex to detect ticket IDs (IR or SR followed by 5+ digits)
TICKET_ID_PATTERN = re.compile(r"\b(IR|SR)\d{5,}\b", re.IGNORECASE)

# Maximum number of tickets to fetch per message
MAX_TICKET_FETCHES = 3

SYSTEM_PROMPT = """You are an AI assistant for the Penn Medicine / UPHS IT Service Desk. \
Your role is to help service desk analysts resolve IT issues by providing accurate, \
step-by-step troubleshooting guidance. You are advisory only — the analyst makes all final decisions.

## Your Data Sources (in priority order)
1. **Triage Rules** — high-confidence rule-based routing (100% confidence when matched)
2. **Classifier Predictions** — a trained ML model (80.7% accuracy, 226 support groups) that predicts \
the best support group based on ticket title, description, location, and classification
3. **Structured Knowledge Graph** — pre-extracted escalation paths, priority rules, \
troubleshooting procedures, and system dependencies from service desk documentation
4. **Knowledge Base Documentation** — raw text from the UPHS and LGH OneNote service desk notebooks
5. **Historical Ticket Data** — similar incidents that have been resolved in the past
6. **Referenced Ticket Data** — full details of any specific ticket (IR/SR) mentioned in the conversation

## Response Guidelines

**Format:**
- Use headers, bullet points, and numbered steps for clarity
- Keep responses concise — analysts are busy and need quick, actionable answers
- For troubleshooting: use numbered steps. For routing: lead with the recommendation
- Bold the most critical information (recommended group, priority, escalation target)

**When a ticket is referenced:**
- Analyze its current state (status, priority, support group, description)
- If it appears to already be assigned to the correct group, confirm that
- If priority seems mismatched with the issue severity, flag it
- Note if the ticket appears stale (very old with no recent updates)
- Report comment details EXACTLY as provided — include the author name, date, and text verbatim
- NEVER fabricate or paraphrase comment content; only report what is explicitly in the data

**Classifier predictions:**
- Confidence >80%: Present confidently as "Recommended: [group] (confidence%)"
- Confidence 50-80%: Present with alternatives, suggest analyst verify
- Confidence <50%: Note uncertainty, recommend manual review of alternatives
- Method "triage_rule": Present as a high-confidence rule-based match (no alternatives needed)

**When information conflicts:**
- Triage rules override all other sources
- Classifier predictions take priority over documentation for routing decisions
- Knowledge graph procedures take priority for troubleshooting steps
- Historical tickets are supplementary context, not authoritative

**When you don't have enough information:**
- Say so clearly — do not guess or fabricate
- Suggest what additional information the analyst could gather
- Recommend escalation paths if available in the knowledge graph

**Critical accuracy rules:**
- ONLY state facts that are explicitly present in the provided ticket data or documentation
- NEVER invent, hallucinate, or assume ticket details (comments, dates, names, actions taken)
- If a field is not in the data, say "not available in the data" — do not guess
- The ticketing system is called **Athena** (not ServiceNow, Remedy, or any other name)
- Dates and timestamps must be reported exactly as they appear in the data

**Boundaries — do NOT:**
- Provide medical advice or clinical guidance
- Share or request passwords, credentials, or PII
- Make promises about resolution times or SLAs
- Perform any actions — you advise, the analyst acts

---

{context}"""


class ChatbotService:
    """Orchestrates the Graph-First RAG chatbot pipeline."""

    def __init__(
        self,
        databricks_client: DatabricksClient,
        knowledge_graph_service: KnowledgeGraphService | None = None,
        athena_client: AthenaClient | None = None,
        ticket_classifier: TicketClassifier | None = None,
    ) -> None:
        self._databricks = databricks_client
        self._knowledge_graph = knowledge_graph_service
        self._athena = athena_client
        self._classifier = ticket_classifier
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

        # Step 0: Detect and fetch referenced tickets
        referenced_tickets = await self._fetch_referenced_tickets(message)

        # Step 1: Query knowledge graph for structured facts
        # If we have a referenced ticket, also query using its title for better matches
        kg_query = message
        if referenced_tickets:
            # Enrich the KG query with ticket title/description for better matching
            first_ticket = referenced_tickets[0]
            title = first_ticket.get("title", "")
            if title:
                kg_query = f"{message} {title}"

        graph_result = self._query_knowledge_graph(kg_query)
        graph_context = ""
        if graph_result and graph_result.get("facts"):
            graph_context = self._knowledge_graph.format_facts_for_llm(graph_result)
            logger.info(
                "Knowledge graph returned %d facts (sufficient=%s, systems=%s)",
                len(graph_result["facts"]),
                graph_result["has_sufficient_context"],
                graph_result.get("systems_matched", []),
            )

        # Step 2: Decide retrieval strategy based on available context
        has_graph_context = bool(graph_result and graph_result.get("has_sufficient_context"))
        has_referenced_ticket = bool(referenced_tickets and any(
            not t.get("_not_found") and not t.get("_error") for t in referenced_tickets
        ))

        # Determine what SQL searches to skip
        skip_doc_search = has_graph_context  # KG provides structured docs
        skip_ticket_search = has_graph_context or has_referenced_ticket  # Already have ticket context

        if skip_doc_search and skip_ticket_search:
            # We have enough context — skip all slow SQL queries entirely
            logger.info(
                "Sufficient context available (graph=%s, referenced_ticket=%s) — skipping SQL similarity searches",
                has_graph_context, has_referenced_ticket,
            )
            query_embedding = None
            doc_results = []
            ticket_results = []
        elif skip_doc_search and not skip_ticket_search:
            # KG provides docs but we still need ticket similarity
            logger.info("Graph context sufficient — skipping doc search, running ticket similarity only")
            query_embedding = await self._databricks.generate_embedding(message)
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
        elif skip_ticket_search and not skip_doc_search:
            # Referenced ticket provides ticket context but we still need doc search
            logger.info("Referenced ticket found — skipping ticket search, running doc search only")
            query_embedding = await self._databricks.generate_embedding(message)
            ticket_results = []
            loop = asyncio.get_event_loop()
            doc_results = await loop.run_in_executor(
                None,
                self._databricks.find_similar_documentation,
                query_embedding,
                top_k_docs,
            )
        else:
            # Full fallback — need both doc and ticket similarity
            logger.info("Insufficient context — running full text similarity search")
            query_embedding = await self._databricks.generate_embedding(message)
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

        # Step 3.5: Run classifier on referenced tickets (conditional — only when tickets detected)
        classifier_results = self._classify_referenced_tickets(referenced_tickets)

        # Step 4: Build source citations
        sources = self._build_sources(graph_result, doc_results, ticket_results, referenced_tickets)

        # Step 5: Build the context string and LLM messages
        context = self._build_context(
            graph_context, doc_results, ticket_results, referenced_tickets, classifier_results
        )
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

    async def _fetch_referenced_tickets(self, message: str) -> list[dict[str, Any]]:
        """
        Detect ticket IDs in the message and fetch their data from Athena.

        Args:
            message: The user's chat message.

        Returns:
            List of ticket data dicts (may be empty if no tickets found or Athena unavailable).
        """
        if self._athena is None:
            return []

        # Detect ticket IDs via regex
        matches = TICKET_ID_PATTERN.findall(message)
        if not matches:
            return []

        # Extract full ticket IDs (the regex captures the prefix group, reconstruct full IDs)
        ticket_ids = []
        for match in re.finditer(TICKET_ID_PATTERN, message):
            ticket_id = match.group(0).upper()
            if ticket_id not in ticket_ids:
                ticket_ids.append(ticket_id)
            if len(ticket_ids) >= MAX_TICKET_FETCHES:
                break

        logger.info("Detected ticket IDs in message: %s", ticket_ids)

        # Fetch tickets from Athena in parallel
        fetched: list[dict[str, Any]] = []
        for ticket_id in ticket_ids:
            try:
                ticket_data = await self._athena.get_ticket(ticket_id)
                if ticket_data:
                    fetched.append(ticket_data)
                    logger.info("Fetched ticket %s from Athena", ticket_id)
                else:
                    logger.warning("Ticket %s not found in Athena", ticket_id)
                    fetched.append({"id": ticket_id, "_not_found": True})
            except Exception:
                logger.exception("Failed to fetch ticket %s from Athena", ticket_id)
                fetched.append({"id": ticket_id, "_error": True})

        return fetched

    def _query_knowledge_graph(self, message: str) -> dict[str, Any] | None:
        """Query the knowledge graph if available."""
        if self._knowledge_graph is None or not self._knowledge_graph.is_available:
            return None
        try:
            return self._knowledge_graph.query_for_chat(message)
        except Exception:
            logger.exception("Knowledge graph query failed, will use text fallback")
            return None

    def _classify_referenced_tickets(
        self, referenced_tickets: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Run the TF-IDF classifier on each valid referenced ticket.

        For each ticket, runs the full Feature #3 pipeline:
        1. Check specific triage rules (12 data-driven patterns)
        2. Check Service Desk triage rules
        3. Run TF-IDF classifier for support group prediction
        4. Resolve GUID from predicted group name

        Args:
            referenced_tickets: List of ticket data dicts fetched from Athena.

        Returns:
            List of classifier result dicts, one per valid ticket. Each contains:
                - ticket_id: str
                - method: "triage_rule" or "classifier"
                - support_group: str (predicted group name)
                - support_group_guid: str
                - confidence: float (0.0-1.0)
                - alternatives: list of {support_group, confidence} dicts
        """
        if self._classifier is None:
            return []

        results: list[dict[str, Any]] = []

        for ticket in referenced_tickets:
            if ticket.get("_not_found") or ticket.get("_error"):
                continue

            ticket_id = ticket.get("id", "Unknown")
            prefix = ticket_id[:2].upper() if len(ticket_id) >= 2 else ""

            # Determine support groups dict based on ticket type
            if prefix == "SR":
                support_groups = SR_SUPPORT_GROUPS
                ticket_type_str = "Service Request"
            else:
                support_groups = IR_SUPPORT_GROUPS
                ticket_type_str = "Incident"

            # Extract fields for classification
            def _extract_str(field_data: Any) -> str:
                if isinstance(field_data, dict):
                    return field_data.get("name", field_data.get("displayName", ""))
                if field_data is None:
                    return ""
                return str(field_data)

            title = _extract_str(ticket.get("title") or ticket.get("shortDescription", ""))
            description = _extract_str(ticket.get("description", ""))
            location = _extract_str(ticket.get("location", ""))
            classification = _extract_str(
                ticket.get("classificationPath") or ticket.get("classification", "")
            )
            source = _extract_str(ticket.get("source", ""))

            try:
                # Step 1: Check specific triage rules
                specific_match = check_specific_triage(
                    title, description, location, support_groups
                )
                if specific_match:
                    group_name, group_guid = specific_match
                    results.append({
                        "ticket_id": ticket_id,
                        "method": "triage_rule",
                        "support_group": group_name,
                        "support_group_guid": group_guid,
                        "confidence": 1.0,
                        "alternatives": [],
                    })
                    logger.info(
                        "Classifier (triage rule) for %s: %s", ticket_id, group_name
                    )
                    continue

                # Step 2: Check Service Desk triage rules
                if check_service_desk_triage(title, description):
                    sd_guid = support_groups.get("Service Desk", "")
                    results.append({
                        "ticket_id": ticket_id,
                        "method": "triage_rule",
                        "support_group": "Service Desk",
                        "support_group_guid": sd_guid,
                        "confidence": 1.0,
                        "alternatives": [],
                    })
                    logger.info(
                        "Classifier (SD triage) for %s: Service Desk", ticket_id
                    )
                    continue

                # Step 3: Run TF-IDF classifier
                predictions = self._classifier.predict(
                    title=title,
                    description=description,
                    ticket_type=ticket_type_str,
                    location=location,
                    classification=classification,
                    source=source,
                    top_k=5,
                )

                if predictions:
                    top = predictions[0]
                    top_group = top["support_group"]
                    top_confidence = top["confidence"]
                    top_guid = resolve_group_guid(top_group, support_groups)

                    alternatives = [
                        {"support_group": p["support_group"], "confidence": p["confidence"]}
                        for p in predictions[1:]
                        if p["confidence"] > 0.001
                    ]

                    results.append({
                        "ticket_id": ticket_id,
                        "method": "classifier",
                        "support_group": top_group,
                        "support_group_guid": top_guid,
                        "confidence": top_confidence,
                        "alternatives": alternatives,
                    })
                    logger.info(
                        "Classifier for %s: %s (confidence=%.3f)",
                        ticket_id, top_group, top_confidence,
                    )
                else:
                    logger.warning("Classifier returned no predictions for %s", ticket_id)

            except Exception:
                logger.exception("Classifier failed for ticket %s", ticket_id)

        return results

    @staticmethod
    def _format_classifier_results_for_context(
        classifier_results: list[dict[str, Any]],
    ) -> str:
        """
        Format classifier predictions into a context block for the LLM.

        Args:
            classifier_results: Output from _classify_referenced_tickets().

        Returns:
            Formatted string for injection into the LLM context.
        """
        if not classifier_results:
            return ""

        lines: list[str] = []
        lines.append("=== CLASSIFIER PREDICTIONS (Support Group Recommendation) ===")

        for result in classifier_results:
            ticket_id = result["ticket_id"]
            method = result["method"]
            group = result["support_group"]
            confidence = result["confidence"]
            guid = result["support_group_guid"]
            alternatives = result.get("alternatives", [])

            lines.append(f"\nTicket: {ticket_id}")
            lines.append(f"  Recommended Group: {group}")
            lines.append(f"  Confidence: {confidence:.1%}")
            lines.append(f"  Method: {method}")
            if guid:
                lines.append(f"  GUID: {guid}")

            if alternatives:
                lines.append("  Alternatives:")
                for alt in alternatives[:4]:
                    lines.append(
                        f"    - {alt['support_group']} ({alt['confidence']:.1%})"
                    )

        return "\n".join(lines)

    @staticmethod
    def _build_sources(
        graph_result: dict[str, Any] | None,
        doc_results: list[dict[str, Any]],
        ticket_results: list[dict[str, Any]],
        referenced_tickets: list[dict[str, Any]] | None = None,
    ) -> list[SourceCitation]:
        """Build source citations from all retrieval results."""
        sources: list[SourceCitation] = []

        # Referenced ticket sources (fetched from Athena — exact matches)
        if referenced_tickets:
            for ticket in referenced_tickets:
                if ticket.get("_not_found") or ticket.get("_error"):
                    continue
                ticket_id = ticket.get("id", "Unknown")
                title = ticket.get("title", "")
                preview = title[:200] if title else "Ticket details fetched from Athena"
                sources.append(
                    SourceCitation(
                        type=SourceType.ticket,
                        title=f"{ticket_id} (referenced)",
                        similarity=1.0,  # Exact match — user explicitly mentioned it
                        content_preview=preview,
                    )
                )

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

        # Ticket sources (from similarity search)
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
    def _format_ticket_for_context(ticket: dict[str, Any]) -> str:
        """Format a single fetched ticket into a readable context block."""
        ticket_id = ticket.get("id", "Unknown")

        if ticket.get("_not_found"):
            return f"Ticket {ticket_id}: NOT FOUND in Athena"
        if ticket.get("_error"):
            return f"Ticket {ticket_id}: ERROR fetching from Athena"

        # Extract fields, handling both nested dict and flat formats
        def _extract(field_data: Any) -> str:
            if isinstance(field_data, dict):
                return field_data.get("name", field_data.get("displayName", str(field_data)))
            if field_data is None:
                return "N/A"
            return str(field_data)

        lines = [f"Ticket: {ticket_id}"]
        if ticket.get("title"):
            lines.append(f"Title: {ticket['title']}")
        if ticket.get("status"):
            lines.append(f"Status: {_extract(ticket['status'])}")
        if ticket.get("priority"):
            lines.append(f"Priority: {_extract(ticket['priority'])}")
        if ticket.get("supportGroup") or ticket.get("tierQueue"):
            sg = ticket.get("supportGroup") or ticket.get("tierQueue")
            lines.append(f"Support Group: {_extract(sg)}")
        if ticket.get("affectedUser"):
            lines.append(f"Affected User: {_extract(ticket['affectedUser'])}")
        if ticket.get("assignedToUser"):
            lines.append(f"Assigned To: {_extract(ticket['assignedToUser'])}")
        if ticket.get("location"):
            lines.append(f"Location: {_extract(ticket['location'])}")
        if ticket.get("createdDate"):
            lines.append(f"Created: {ticket['createdDate']}")
        if ticket.get("description"):
            desc = ticket["description"]
            # Truncate very long descriptions
            if len(desc) > 500:
                desc = desc[:500] + "..."
            lines.append(f"Description: {desc}")

        # Include analyst and user comments with author and timestamp
        if ticket.get("analystComments") or ticket.get("userComments"):
            lines.append("Comments (newest first):")
            # Combine and sort all comments by date (newest first)
            all_comments = []
            for comment_obj in ticket.get("analystComments") or []:
                comment_text = comment_obj.get("comment", "")
                if comment_text:
                    author = comment_obj.get("enteredBy", "Unknown")
                    date = comment_obj.get("enteredDate", "")
                    all_comments.append((date, author, comment_text, "Analyst"))
            for comment_obj in ticket.get("userComments") or []:
                comment_text = comment_obj.get("comment", "")
                if comment_text:
                    author = comment_obj.get("enteredBy", "Unknown")
                    date = comment_obj.get("enteredDate", "")
                    all_comments.append((date, author, comment_text, "User"))
            # Sort by date descending (newest first)
            all_comments.sort(key=lambda x: x[0], reverse=True)
            for date, author, text, role in all_comments:
                # Format date for readability (strip timezone offset details)
                date_display = date[:19].replace("T", " ") if date else "Unknown date"
                prefix = f"[{role}]" if role == "User" else "[Analyst]"
                lines.append(f"  {prefix} {author} ({date_display}): {text}")

        return "\n".join(lines)

    @staticmethod
    def _build_context(
        graph_context: str,
        doc_results: list[dict[str, Any]],
        ticket_results: list[dict[str, Any]],
        referenced_tickets: list[dict[str, Any]] | None = None,
        classifier_results: list[dict[str, Any]] | None = None,
    ) -> str:
        """Build the context string injected into the system prompt."""
        parts: list[str] = []

        # Referenced ticket data (highest priority — user explicitly asked about these)
        if referenced_tickets:
            parts.append("=== REFERENCED TICKET DATA ===")
            for ticket in referenced_tickets:
                parts.append(ChatbotService._format_ticket_for_context(ticket))
                parts.append("")  # blank line between tickets

        # Classifier predictions (injected right after ticket data for prominence)
        if classifier_results:
            parts.append(ChatbotService._format_classifier_results_for_context(classifier_results))

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

    async def chat_stream(
        self,
        message: str,
        session_id: str | None = None,
        top_k_docs: int = 5,
        top_k_tickets: int = 5,
        max_tokens: int = 2048,
    ):
        """
        Process a user message and stream the LLM response tokens.

        Performs all retrieval steps (KG, embedding, similarity) first,
        then streams the LLM output token by token.

        Yields dicts with event types:
            {"event": "progress", "data": {"step": N, "total": 5, "label": "...", "status": "running|done|skipped"}}
            {"event": "sources", "data": [...]}  — source citations
            {"event": "token", "data": "..."}    — each text chunk
            {"event": "done", "data": {"session_id": "...", "full_text": "..."}}

        Args:
            message: The user's question or message.
            session_id: Existing session ID, or None to create a new session.
            top_k_docs: Number of documentation articles to retrieve (fallback mode).
            top_k_tickets: Number of similar tickets to retrieve.
            max_tokens: Maximum tokens in the LLM response.
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

        # --- Progress Step 1: Analyzing your question ---
        yield {"event": "progress", "data": {"step": 1, "total": 5, "label": "Analyzing your question", "status": "running"}}

        # Step 0: Detect and fetch referenced tickets
        referenced_tickets = await self._fetch_referenced_tickets(message)

        # Step 1: Query knowledge graph
        kg_query = message
        if referenced_tickets:
            first_ticket = referenced_tickets[0]
            title = first_ticket.get("title", "")
            if title:
                kg_query = f"{message} {title}"

        graph_result = self._query_knowledge_graph(kg_query)
        graph_context = ""
        if graph_result and graph_result.get("facts"):
            graph_context = self._knowledge_graph.format_facts_for_llm(graph_result)

        # Step 2: Decide retrieval strategy
        has_graph_context = bool(graph_result and graph_result.get("has_sufficient_context"))
        has_referenced_ticket = bool(referenced_tickets and any(
            not t.get("_not_found") and not t.get("_error") for t in referenced_tickets
        ))

        skip_doc_search = has_graph_context
        skip_ticket_search = has_graph_context or has_referenced_ticket

        # Determine which steps will be skipped
        will_fetch_tickets = bool(referenced_tickets and any(
            not t.get("_not_found") and not t.get("_error") for t in referenced_tickets
        )) or bool(self._athena and TICKET_ID_PATTERN.search(message))

        yield {"event": "progress", "data": {"step": 1, "total": 5, "label": "Analyzing your question", "status": "done"}}

        # --- Progress Step 2: Searching knowledge base ---
        if skip_doc_search:
            yield {"event": "progress", "data": {"step": 2, "total": 5, "label": "Searching knowledge base", "status": "skipped"}}
        else:
            yield {"event": "progress", "data": {"step": 2, "total": 5, "label": "Searching knowledge base", "status": "running"}}

        # --- Progress Step 3: Finding similar tickets ---
        if skip_ticket_search:
            yield {"event": "progress", "data": {"step": 3, "total": 5, "label": "Finding similar tickets", "status": "skipped"}}
        else:
            yield {"event": "progress", "data": {"step": 3, "total": 5, "label": "Finding similar tickets", "status": "running"}}

        # Execute the searches
        if skip_doc_search and skip_ticket_search:
            doc_results = []
            ticket_results = []
        elif skip_doc_search:
            query_embedding = await self._databricks.generate_embedding(message)
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
        elif skip_ticket_search:
            query_embedding = await self._databricks.generate_embedding(message)
            ticket_results = []
            loop = asyncio.get_event_loop()
            doc_results = await loop.run_in_executor(
                None,
                self._databricks.find_similar_documentation,
                query_embedding,
                top_k_docs,
            )
        else:
            query_embedding = await self._databricks.generate_embedding(message)
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

        # Mark search steps as done
        if not skip_doc_search:
            yield {"event": "progress", "data": {"step": 2, "total": 5, "label": "Searching knowledge base", "status": "done"}}
        if not skip_ticket_search:
            yield {"event": "progress", "data": {"step": 3, "total": 5, "label": "Finding similar tickets", "status": "done"}}

        # --- Progress Step 4: Fetching ticket details ---
        has_fetched_tickets = bool(referenced_tickets and any(
            not t.get("_not_found") and not t.get("_error") for t in referenced_tickets
        ))
        if has_fetched_tickets:
            # Already fetched during step 1 — mark as done immediately
            yield {"event": "progress", "data": {"step": 4, "total": 5, "label": "Fetching ticket details", "status": "done"}}
            # Send ticket data to frontend for collapsible card display
            valid_tickets = [
                t for t in referenced_tickets
                if not t.get("_not_found") and not t.get("_error")
            ]
            if valid_tickets:
                yield {"event": "ticket_data", "data": valid_tickets}
        else:
            yield {"event": "progress", "data": {"step": 4, "total": 5, "label": "Fetching ticket details", "status": "skipped"}}

        # --- Progress Step 5: Generating response ---
        yield {"event": "progress", "data": {"step": 5, "total": 5, "label": "Generating response", "status": "running"}}

        # Run classifier on referenced tickets
        classifier_results = self._classify_referenced_tickets(referenced_tickets)

        # Build sources and context
        sources = self._build_sources(graph_result, doc_results, ticket_results, referenced_tickets)
        context = self._build_context(
            graph_context, doc_results, ticket_results, referenced_tickets, classifier_results
        )
        llm_messages = self._build_llm_messages(session_id, context)

        # Yield sources so frontend can display them
        yield {
            "event": "sources",
            "data": [s.model_dump() for s in sources],
            "session_id": session_id,
        }

        # Stream LLM tokens
        full_text = ""
        async for chunk in self._databricks.call_llm_stream(llm_messages, max_tokens=max_tokens):
            full_text += chunk
            yield {"event": "token", "data": chunk}

        # Record the assistant message
        assistant_msg = ChatMessage(
            role=MessageRole.assistant,
            content=full_text,
            sources=sources,
            timestamp=datetime.now(UTC),
        )
        self._sessions[session_id].append(assistant_msg)

        # Signal completion
        yield {
            "event": "done",
            "data": {"session_id": session_id, "full_text": full_text},
        }

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

        # Inject current timestamp so the LLM knows "now"
        eastern = timezone(timedelta(hours=-4))
        now_et = datetime.now(eastern)
        time_header = f"Current Date/Time: {now_et.strftime('%A, %B %d, %Y at %I:%M %p ET')}\n\n"

        # System prompt with retrieved context
        messages.append({
            "role": "system",
            "content": SYSTEM_PROMPT.format(context=time_header + context),
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