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
    extract_location_path,
    resolve_group_guid,
)
from src.services.knowledge_graph import KnowledgeGraphService
from src.services.local_vector_store import LocalVectorStore
from src.services.site_routing import (
    detect_site,
    group_site,
    is_holding_queue,
    notebook_for_site,
    site_for_location_path,
    sites_conflict,
)
from src.services.ticket_classifier import TicketClassifier, get_ticket_classifier

logger = logging.getLogger(__name__)

# Maximum number of conversation turns to include in the LLM context
MAX_HISTORY_TURNS = 10

# Regex to detect ticket IDs (IR or SR followed by 5+ digits)
TICKET_ID_PATTERN = re.compile(r"\b(IR|SR)\d{5,}\b", re.IGNORECASE)

# Maximum number of tickets to fetch per message
MAX_TICKET_FETCHES = 3

# Classifier predictions at or below this confidence are treated as
# low-confidence: the LLM must lean on documentation + matching tickets, and
# retrieval is widened to pull more corroborating evidence.
CLASSIFIER_LOW_CONFIDENCE = 0.50

# How much to widen retrieval (multiplier + cap) when confidence is low.
LOW_CONFIDENCE_RETRIEVAL_MULTIPLIER = 2
LOW_CONFIDENCE_MAX_DOCS = 12
LOW_CONFIDENCE_MAX_TICKETS = 12

SYSTEM_PROMPT = """You are an AI assistant for the Penn Medicine / UPHS IT Service Desk. \
Your role is to help service desk analysts resolve IT issues by providing accurate, \
step-by-step troubleshooting guidance. You are advisory only — the analyst makes all final decisions.

## Your Data Sources (in priority order for ROUTING decisions)
1. **Triage Rules** — high-confidence rule-based routing (100% confidence when matched)
2. **Referenced Ticket Data** — full details of any specific ticket (IR/SR) mentioned, including \
its current support group and resolved location
3. **Matching Historical Tickets** — similar past incidents WITH the support group they were actually \
assigned to. When several similar tickets went to the same group, that is strong corroborating evidence
4. **Knowledge Base Documentation** — troubleshooting/routing guidance from the UPHS and LGH OneNote \
service desk notebooks
5. **Structured Knowledge Graph** — pre-extracted escalation paths, priority rules, \
troubleshooting procedures, and system dependencies
6. **Classifier Predictions** — a trained ML model that SUGGESTS a support group. Treat this as ONE \
advisory hint only, NOT an authority — it is frequently wrong and must be corroborated (see below)

## Response Guidelines

**Format:**
- Use headers, bullet points, and numbered steps for clarity
- Keep responses concise — analysts are busy and need quick, actionable answers
- For troubleshooting: use numbered steps. For routing: lead with the recommendation
- Bold the most critical information (recommended group, priority, escalation target)

**When a ticket is referenced:**
- Analyze its current state (status, priority, support group, description)
- If it appears to already be assigned to the correct REAL support group, confirm that
- **Validation / Service Desk\\Validation is the intake queue where UNASSIGNED tickets \
wait for triage — it is NEVER a valid recommendation.** If a referenced ticket is currently \
in Validation (or any queue flagged as an INTAKE/HOLDING QUEUE), do NOT confirm it as correct: \
the ticket still needs routing. Recommend a real support group based on documentation, matching \
historical tickets, and the site. If you cannot confidently determine one, say so and ask for the \
specific detail you need (just as you would for an unknown site) — do NOT fall back to Validation.
- If priority seems mismatched with the issue severity, flag it
- Note if the ticket appears stale (very old with no recent updates)
- Report comment details EXACTLY as provided — include the author name, date, and text verbatim
- NEVER fabricate or paraphrase comment content; only report what is explicitly in the data

**How to treat the Classifier (IMPORTANT — be skeptical):**
- The classifier is a suggestion, not a decision. Do NOT lead with it or present it as "the answer."
- ALWAYS corroborate the classifier's group against: (a) the groups that similar historical tickets \
were actually assigned to, (b) the routing guidance in documentation, and (c) the ticket's location/site.
- If the classifier AGREES with the matching tickets and/or documentation, you may present that group \
with normal confidence (still cite the corroborating evidence, not the classifier alone).
- If the classifier CONFLICTS with the matching tickets or documentation, TRUST the documentation and \
the historical-ticket evidence over the classifier, and say why.
- **Confidence < 50% (low):** treat the classifier prediction as barely more than a guess. Lead your \
routing reasoning with documentation and the support groups seen on similar tickets. Present the \
classifier only as "a low-confidence hint" and explicitly recommend the analyst verify.
- **Confidence ≥ 50%:** you may mention it, but still require corroboration before endorsing it.
- Method "triage_rule": this is a deterministic rule match (not the ML classifier) — it IS reliable.

**When information conflicts (routing):**
- Triage rules override all other sources.
- Documentation + the support groups actually used on matching historical tickets outrank the classifier.
- The classifier is the LOWEST-priority routing signal and must never override documentation or \
consistent historical-ticket evidence.
- Knowledge graph procedures take priority for troubleshooting steps.

## Location & Site Routing (UPHS vs LGH) — CRITICAL
Penn Medicine spans TWO separate organizations with NON-interchangeable support groups:
- **UPHS** (University of Pennsylvania Health System): HUP, PAH, PCAM, PPMC, CCH, Presbyterian, \
Perelman, PennChart, MyPennMedicine, Penn Medicine at Home, PMDH/Doylestown, etc.
- **LGH** (Lancaster General Health): LGH, LGHP, Lancaster, MyLGHealth, Women & Babies, lha.org, \
lgh.org, groups under the "LGH\\..." hierarchy.

Rules:
- NEVER recommend an LGH support group for a UPHS ticket/query, or a UPHS group for an LGH ticket/query.
- Use the ticket's **resolved location** and the **Detected Site** provided in the context to determine \
the organization. If the user query itself names a site/system, use that.
- If a "SITE MISMATCH" warning appears in the context (the classifier's group belongs to the other \
organization), you MUST reject that group and instead choose a same-site group supported by \
documentation or matching tickets — or state that you cannot confidently route and recommend the \
analyst confirm the site.
- If the site is genuinely unknown/ambiguous, say so and ask the analyst to confirm before routing.

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
        vector_store: LocalVectorStore | None = None,
        ticket_classifier: TicketClassifier | None = None,
    ) -> None:
        self._databricks = databricks_client
        self._knowledge_graph = knowledge_graph_service
        self._athena = athena_client
        self._vector_store = vector_store
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

        # Step 0.5: Detect the organization/site (UPHS vs LGH) for this request
        # and derive the documentation notebook filter. Done BEFORE formatting the
        # knowledge graph so escalation targets can be flagged for cross-site
        # mismatch (e.g. an LGH-only 'PC Techs' escalation on a UPHS ticket).
        detected_site = self._compute_detected_site(message, referenced_tickets)
        notebook = notebook_for_site(detected_site)

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
            graph_context = self._knowledge_graph.format_facts_for_llm(
                graph_result, detected_site=detected_site
            )
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
            ticket_results = self._vector_store.find_similar_by_embedding(
                query_embedding, top_k=top_k_tickets
            )
        elif skip_ticket_search and not skip_doc_search:
            # Referenced ticket provides ticket context but we still need doc search
            logger.info("Referenced ticket found — skipping ticket search, running doc search only")
            query_embedding = await self._databricks.generate_embedding(message)
            ticket_results = []
            doc_results = self._vector_store.find_similar_documentation(
                query_embedding, top_k=top_k_docs, notebook=notebook
            )
        else:
            # Full fallback — need both doc and ticket similarity
            logger.info("Insufficient context — running full text similarity search")
            query_embedding = await self._databricks.generate_embedding(message)
            doc_results = self._vector_store.find_similar_documentation(
                query_embedding, top_k=top_k_docs, notebook=notebook
            )
            ticket_results = self._vector_store.find_similar_by_embedding(
                query_embedding, top_k=top_k_tickets
            )

        # Step 3.5: Run classifier on referenced tickets (conditional — only when tickets detected)
        classifier_results = self._classify_referenced_tickets(referenced_tickets)

        # Step 3.6: If the classifier is low-confidence, widen retrieval and
        # emphasize documentation (site-filtered) over the weak classifier hint.
        doc_results, ticket_results, _widened = await self._widen_retrieval_if_low_confidence(
            message, classifier_results, query_embedding,
            doc_results, ticket_results, top_k_docs, top_k_tickets, notebook,
        )

        # Step 4: Build source citations
        sources = self._build_sources(graph_result, doc_results, ticket_results, referenced_tickets)

        # Step 5: Build the context string and LLM messages
        context = self._build_context(
            graph_context, doc_results, ticket_results, referenced_tickets,
            classifier_results, detected_site,
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

    @staticmethod
    def _compute_detected_site(
        message: str,
        referenced_tickets: list[dict[str, Any]] | None,
    ) -> str | None:
        """
        Determine the organization/site (UPHS vs LGH) for the whole request.

        Combines the user's query text with the resolved location + title of any
        referenced tickets. Referenced-ticket location is the strongest signal,
        so it is included first.

        The resolved location path is checked FIRST via ``site_for_location_path``
        (top-level campus segment, e.g. 'PPMC\\MUTCH' -> UPHS), which is far more
        reliable than substring keyword matching. Only if the location yields no
        site do we fall back to keyword detection across location + title + query.
        """
        fields: list[str] = []
        if referenced_tickets:
            for t in referenced_tickets:
                if t.get("_not_found") or t.get("_error"):
                    continue
                loc = extract_location_path(t) or ""
                # Prefer deterministic campus-based detection on the location path.
                loc_site = site_for_location_path(loc)
                if loc_site is not None:
                    return loc_site
                fields.append(loc)
                fields.append(str(t.get("title", "")))
        fields.append(message)
        return detect_site(*fields)

    @staticmethod
    def _is_low_confidence(classifier_results: list[dict[str, Any]]) -> bool:
        """
        True if any referenced ticket got a low-confidence *classifier* result.

        A 'triage_rule' match is deterministic/reliable and does NOT count as
        low confidence.
        """
        return any(
            r.get("method") == "classifier"
            and r.get("confidence", 1.0) <= CLASSIFIER_LOW_CONFIDENCE
            for r in classifier_results
        )

    async def _widen_retrieval_if_low_confidence(
        self,
        message: str,
        classifier_results: list[dict[str, Any]],
        query_embedding: list[float] | None,
        doc_results: list[dict[str, Any]],
        ticket_results: list[dict[str, Any]],
        top_k_docs: int,
        top_k_tickets: int,
        notebook: str | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        """
        When the classifier is low-confidence, pull MORE documentation and
        similar tickets so the LLM can rely on that evidence instead of the weak
        classifier hint. Documentation is (re)fetched even if it was skipped
        earlier, honoring the notebook/site filter.

        Returns (doc_results, ticket_results, widened).
        """
        if not self._is_low_confidence(classifier_results):
            return doc_results, ticket_results, False

        widened_docs = min(
            top_k_docs * LOW_CONFIDENCE_RETRIEVAL_MULTIPLIER, LOW_CONFIDENCE_MAX_DOCS
        )
        widened_tickets = min(
            top_k_tickets * LOW_CONFIDENCE_RETRIEVAL_MULTIPLIER, LOW_CONFIDENCE_MAX_TICKETS
        )
        logger.info(
            "Low classifier confidence — widening retrieval to %d docs / %d tickets "
            "and emphasizing documentation.",
            widened_docs, widened_tickets,
        )

        if query_embedding is None:
            query_embedding = await self._databricks.generate_embedding(message)

        # Always (re)fetch documentation on the low-confidence path — this is the
        # evidence we want the LLM to lean on.
        doc_results = self._vector_store.find_similar_documentation(
            query_embedding, top_k=widened_docs, notebook=notebook
        )
        # Fetch more similar tickets only if we don't already have a fuller set.
        if len(ticket_results) < widened_tickets:
            ticket_results = self._vector_store.find_similar_by_embedding(
                query_embedding, top_k=widened_tickets
            )

        return doc_results, ticket_results, True

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
            # Resolve the location to its full parent\child path (GUID → fullname)
            # using the same resolver as Feature #3, falling back to the leaf.
            location = extract_location_path(ticket) or _extract_str(ticket.get("location", ""))
            classification = _extract_str(
                ticket.get("classificationPath") or ticket.get("classification", "")
            )
            source = _extract_str(ticket.get("source", ""))

            # Detect the ticket's organization/site (UPHS vs LGH) for the
            # cross-site routing guardrail. Prefer the deterministic campus-based
            # detection on the resolved location path (e.g. 'PPMC\\MUTCH' -> UPHS),
            # falling back to keyword detection over location + title + description.
            ticket_site = site_for_location_path(location) or detect_site(
                location, title, description
            )

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
                        "location": location,
                        "ticket_site": ticket_site,
                        "predicted_group_site": group_site(group_name),
                        "site_mismatch": sites_conflict(ticket_site, group_site(group_name)),
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
                        "location": location,
                        "ticket_site": ticket_site,
                        "predicted_group_site": None,  # Service Desk is site-neutral
                        "site_mismatch": False,
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
                    # Site-aware selection: if the top pick belongs to the other
                    # organization, prefer the best same-site / site-neutral
                    # prediction (mirrors Feature #3's guardrail).
                    chosen, site_adjusted = self._select_site_aware_prediction(
                        predictions, ticket_site
                    )
                    top_group = chosen["support_group"]
                    top_confidence = chosen["confidence"]
                    top_guid = resolve_group_guid(top_group, support_groups)
                    predicted_site = group_site(top_group)

                    alternatives = [
                        {"support_group": p["support_group"], "confidence": p["confidence"]}
                        for p in predictions
                        if p is not chosen and p["confidence"] > 0.001
                    ]

                    results.append({
                        "ticket_id": ticket_id,
                        "method": "classifier",
                        "support_group": top_group,
                        "support_group_guid": top_guid,
                        "confidence": top_confidence,
                        "alternatives": alternatives,
                        "location": location,
                        "ticket_site": ticket_site,
                        "predicted_group_site": predicted_site,
                        "site_mismatch": sites_conflict(ticket_site, predicted_site),
                        "site_adjusted": site_adjusted,
                    })
                    logger.info(
                        "Classifier for %s: %s (confidence=%.3f, site=%s, adjusted=%s)",
                        ticket_id, top_group, top_confidence, ticket_site, site_adjusted,
                    )
                else:
                    logger.warning("Classifier returned no predictions for %s", ticket_id)

            except Exception:
                logger.exception("Classifier failed for ticket %s", ticket_id)

        return results

    @staticmethod
    def _select_site_aware_prediction(
        predictions: list[dict[str, Any]],
        ticket_site: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """
        Pick the classifier prediction to surface, avoiding UPHS/LGH mismatch.

        Returns (chosen_prediction, site_adjusted). Holding/intake queues
        (Validation) are never surfaced — the best non-holding prediction is
        promoted instead. If the ticket site is unknown or the chosen
        prediction is same-site/site-neutral, it is returned unchanged. If it
        conflicts with the ticket site, the highest-ranked non-conflicting,
        non-holding prediction is promoted; if none exists, the original
        non-holding top is kept (site_adjusted=False).
        """
        # Never let the intake/holding queue (Validation) be a suggestion.
        assignable = [
            p for p in predictions if not is_holding_queue(p["support_group"])
        ]
        if not assignable:
            # Every prediction was a holding queue — keep the raw top so the
            # caller/LLM at least sees the (annotated) data, but this is rare.
            return predictions[0], False

        top = assignable[0]
        adjusted = top is not predictions[0]
        if ticket_site is None:
            return top, adjusted
        if not sites_conflict(ticket_site, group_site(top["support_group"])):
            return top, adjusted
        for pred in assignable:
            if not sites_conflict(ticket_site, group_site(pred["support_group"])):
                return pred, True
        return top, adjusted

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
        lines.append("=== CLASSIFIER HINTS (ADVISORY — corroborate before using) ===")
        lines.append(
            "The following are ML classifier suggestions, NOT decisions. A prediction "
            "with method 'classifier' is only a hint and is often wrong; corroborate it "
            "against the documentation and the groups used on matching historical tickets "
            "before endorsing it. A prediction with method 'triage_rule' is a reliable "
            "deterministic rule match."
        )

        for result in classifier_results:
            ticket_id = result["ticket_id"]
            method = result["method"]
            group = result["support_group"]
            confidence = result["confidence"]
            guid = result["support_group_guid"]
            alternatives = result.get("alternatives", [])
            ticket_site = result.get("ticket_site")
            predicted_group_site = result.get("predicted_group_site")
            site_mismatch = result.get("site_mismatch", False)
            site_adjusted = result.get("site_adjusted", False)

            lines.append(f"\nTicket: {ticket_id}")
            if ticket_site:
                lines.append(f"  Detected Site: {ticket_site}")

            # Defensive: if any path produced a holding/intake queue (Validation),
            # never present it as a routing suggestion.
            if is_holding_queue(group):
                lines.append(
                    f"  ⚠ NOT A VALID TARGET: '{group}' is an intake/holding queue "
                    f"(unassigned), not a real support group. Do NOT recommend it — "
                    f"route based on documentation and matching historical tickets."
                )
                if guid:
                    lines.append(f"  GUID: {guid}")
                if alternatives:
                    lines.append("  Alternatives (also advisory):")
                    for alt in alternatives[:4]:
                        lines.append(
                            f"    - {alt['support_group']} ({alt['confidence']:.1%})"
                        )
                continue

            if method == "triage_rule":
                lines.append(f"  Rule-based routing (RELIABLE): {group}")
            else:
                low = confidence <= CLASSIFIER_LOW_CONFIDENCE
                label = "LOW-CONFIDENCE hint" if low else "Suggested group (advisory)"
                lines.append(f"  {label}: {group}")
                if low:
                    lines.append(
                        "  ⚠ Confidence is below 50% — treat this as barely a guess. "
                        "Base your routing on documentation and matching historical tickets."
                    )
            lines.append(f"  Confidence: {confidence:.1%}")
            lines.append(f"  Method: {method}")
            if predicted_group_site:
                lines.append(f"  Predicted Group Site: {predicted_group_site}")
            if guid:
                lines.append(f"  GUID: {guid}")

            if site_adjusted:
                lines.append(
                    "  ↪ SITE-ADJUSTED: the classifier's top pick belonged to the other "
                    "organization; a same-site option was surfaced instead."
                )
            if site_mismatch:
                lines.append(
                    f"  ⚠ SITE MISMATCH: this group appears to belong to a different "
                    f"organization than the {ticket_site or 'ticket'}. Do NOT route "
                    f"cross-site — reject this group and use a same-site option or defer "
                    f"to documentation."
                )

            if alternatives:
                lines.append("  Alternatives (also advisory):")
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

        # Ticket sources (from similarity search) — now enriched with the
        # historical ticket's title, actually-assigned support group, and
        # location so the analyst can see corroborating routing evidence.
        for ticket in ticket_results:
            t_title = ticket.get("title", "")
            t_group = ticket.get("support_group", "")
            t_location = ticket.get("location", "")
            preview_bits = []
            if t_group:
                preview_bits.append(f"Assigned: {t_group}")
            if t_location:
                preview_bits.append(f"Location: {t_location}")
            if t_title:
                preview_bits.append(f'"{t_title[:120]}"')
            preview = " | ".join(preview_bits) if preview_bits else None
            sources.append(
                SourceCitation(
                    type=SourceType.ticket,
                    title=ticket.get("id", "Unknown"),
                    similarity=ticket.get("similarity", 0.0),
                    content_preview=preview,
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
            sg_name = _extract(sg)
            if is_holding_queue(sg_name):
                # Validation / Service Desk\Validation is the intake queue for
                # UNASSIGNED tickets — it is NOT a real support group. Annotate
                # it so the LLM never "confirms" it as the correct routing.
                lines.append(
                    f"Support Group: {sg_name}  "
                    f"⚠ (INTAKE/HOLDING QUEUE — this ticket is UNASSIGNED and "
                    f"awaiting triage. Validation is NOT a valid routing target; "
                    f"the ticket still needs to be routed to a real support group.)"
                )
            else:
                lines.append(f"Support Group: {sg_name}")
        if ticket.get("affectedUser"):
            lines.append(f"Affected User: {_extract(ticket['affectedUser'])}")
        if ticket.get("assignedToUser"):
            lines.append(f"Assigned To: {_extract(ticket['assignedToUser'])}")
        if ticket.get("location"):
            # Show the resolved parent\child path (e.g. 'PPMC\\MUTCH') so the LLM
            # sees the campus for site routing — not just the bare leaf ('MUTCH').
            resolved_location = extract_location_path(ticket) or _extract(ticket["location"])
            lines.append(f"Location: {resolved_location}")
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
        detected_site: str | None = None,
    ) -> str:
        """Build the context string injected into the system prompt."""
        parts: list[str] = []

        # Detected organization/site for the whole request (UPHS vs LGH). Helps
        # the LLM avoid cross-site routing when no ticket is referenced.
        if detected_site:
            parts.append(
                f"=== DETECTED SITE: {detected_site} ===\n"
                f"Route only to {detected_site} support groups. Do NOT recommend a "
                f"group from the other organization."
            )
        else:
            parts.append(
                "=== DETECTED SITE: UNKNOWN ===\n"
                "The organization (UPHS vs LGH) could not be determined from the "
                "request. Do NOT commit to a site-specific support group. Instead, "
                "state that the site is unconfirmed, ask the analyst to confirm the "
                "campus/site, and only then route. If you must suggest a group, "
                "prefer a site-neutral one (e.g. Service Desk) and clearly flag the "
                "site as unverified."
            )

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

        # Similar tickets (always included) — now enriched with each ticket's
        # title, the support group it was ACTUALLY assigned to, and its location.
        # The assigned group is strong corroborating routing evidence and lets
        # the LLM cross-check (or override) the classifier hint.
        if ticket_results:
            parts.append("\n=== MATCHING HISTORICAL TICKETS (routing evidence) ===")
            parts.append(
                "Each line shows a similar past ticket and the support group it was "
                "actually assigned to. Consistent groups here are strong evidence — "
                "weigh them ABOVE the classifier hint."
            )
            for i, ticket in enumerate(ticket_results, 1):
                ticket_id = ticket.get("id", "Unknown")
                similarity = ticket.get("similarity", 0.0)
                t_group = ticket.get("support_group", "")
                t_location = ticket.get("location", "")
                t_title = ticket.get("title", "")
                line = f"- {ticket_id} (similarity: {similarity:.3f})"
                if t_group:
                    line += f" | Assigned Group: {t_group}"
                if t_location:
                    line += f" | Location: {t_location}"
                if t_title:
                    line += f' | "{t_title[:120]}"'
                parts.append(line)

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

        # Step 0.5: Detect site (UPHS vs LGH) and derive the doc notebook filter.
        # Done BEFORE formatting the knowledge graph so escalation targets can be
        # flagged for cross-site mismatch (e.g. LGH-only 'PC Techs' on a UPHS ticket).
        detected_site = self._compute_detected_site(message, referenced_tickets)
        notebook = notebook_for_site(detected_site)

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
            graph_context = self._knowledge_graph.format_facts_for_llm(
                graph_result, detected_site=detected_site
            )

        # Step 2: Decide retrieval strategy
        has_graph_context = bool(graph_result and graph_result.get("has_sufficient_context"))
        has_referenced_ticket = bool(referenced_tickets and any(
            not t.get("_not_found") and not t.get("_error") for t in referenced_tickets
        ))

        skip_doc_search = has_graph_context
        skip_ticket_search = has_graph_context or has_referenced_ticket
        query_embedding: list[float] | None = None

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
            ticket_results = self._vector_store.find_similar_by_embedding(
                query_embedding, top_k=top_k_tickets
            )
        elif skip_ticket_search:
            query_embedding = await self._databricks.generate_embedding(message)
            ticket_results = []
            doc_results = self._vector_store.find_similar_documentation(
                query_embedding, top_k=top_k_docs, notebook=notebook
            )
        else:
            query_embedding = await self._databricks.generate_embedding(message)
            doc_results = self._vector_store.find_similar_documentation(
                query_embedding, top_k=top_k_docs, notebook=notebook
            )
            ticket_results = self._vector_store.find_similar_by_embedding(
                query_embedding, top_k=top_k_tickets
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

        # If the classifier is low-confidence, widen retrieval and emphasize
        # documentation (site-filtered) over the weak classifier hint.
        doc_results, ticket_results, _widened = await self._widen_retrieval_if_low_confidence(
            message, classifier_results, query_embedding,
            doc_results, ticket_results, top_k_docs, top_k_tickets, notebook,
        )

        # Build sources and context
        sources = self._build_sources(graph_result, doc_results, ticket_results, referenced_tickets)
        context = self._build_context(
            graph_context, doc_results, ticket_results, referenced_tickets,
            classifier_results, detected_site,
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