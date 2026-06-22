"""
LLM Advisor Service for Feature #4: Bulk Ticket Assignment.

Provides RAG-based ticket routing recommendations using:
  1. Knowledge graph structured facts
  2. Similar documentation (vector search)
  3. Similar historical tickets (vector search)
  4. Claude Sonnet 4.5 via Databricks

Runs alongside the TF-IDF classifier as an optional "backup advisor"
that provides rationale explanations for its recommendations.
"""

import json
import logging
import re
import time

from src.clients.databricks_client import DatabricksClient
from src.models.assignment import TicketInfo
from src.services.assignment import (
    IR_SUPPORT_GROUPS,
    SR_SUPPORT_GROUPS,
    resolve_group_guid,
)
from src.services.knowledge_graph import KnowledgeGraphService
from src.services.local_vector_store import LocalVectorStore

logger = logging.getLogger(__name__)

# ── System Prompt ──────────────────────────────────────────────────────

LLM_ADVISOR_SYSTEM_PROMPT = """You are an expert IT ticket routing advisor for Penn Medicine (UPHS).
Your job is to analyze a support ticket and recommend the correct support group to handle it.

You will be given:
1. The ticket details (title, description, location, affected user)
2. Structured knowledge from the knowledge graph (escalation paths, procedures, priority rules)
3. Similar documentation from the OneNote knowledge base
4. Similar historical tickets and their assigned support groups

Based on ALL available context, recommend the single best support group from the CANDIDATE GROUPS list.

RESPOND WITH ONLY A JSON OBJECT — no other text before or after:
{
  "support_group": "<exact group name from CANDIDATE GROUPS>",
  "priority": <number 1-3 for IR tickets>,
  "rationale": "<1-2 sentence explanation>"
}

CRITICAL RULES:
- support_group MUST be one of the exact names from the CANDIDATE GROUPS list below
- Do NOT invent group names or modify the names in any way
- If the knowledge graph mentions a specific escalation target, prefer that group
- If similar tickets were consistently assigned to one group, that's a strong signal
- For hardware/device issues, consider the ticket's physical location for EUS sub-group routing
- Password resets, account lockouts, and basic access issues typically stay at Service Desk
- If truly unsure, pick "Service Desk" as the safe default
"""


# ── Result Model ───────────────────────────────────────────────────────


class LLMAdvisorResult:
    """Result from the LLM advisor pipeline."""

    def __init__(
        self,
        support_group_name: str,
        support_group_guid: str,
        priority: int | None,
        rationale: str,
        confidence_signal: str,
        latency_ms: float,
        kg_facts_used: int,
        docs_used: int,
        similar_tickets_used: int,
    ):
        self.support_group_name = support_group_name
        self.support_group_guid = support_group_guid
        self.priority = priority
        self.rationale = rationale
        self.confidence_signal = confidence_signal
        self.latency_ms = latency_ms
        self.kg_facts_used = kg_facts_used
        self.docs_used = docs_used
        self.similar_tickets_used = similar_tickets_used

    def to_dict(self) -> dict:
        """Serialize to dict for Pydantic model conversion."""
        return {
            "support_group_name": self.support_group_name,
            "support_group_guid": self.support_group_guid,
            "priority": self.priority,
            "rationale": self.rationale,
            "confidence_signal": self.confidence_signal,
            "latency_ms": self.latency_ms,
            "kg_facts_used": self.kg_facts_used,
            "docs_used": self.docs_used,
            "similar_tickets_used": self.similar_tickets_used,
        }


# ── Service ────────────────────────────────────────────────────────────


class LLMAdvisorService:
    """
    RAG-based LLM advisor for ticket routing.

    Uses knowledge graph + vector store + Claude Sonnet 4.5 to generate
    support group recommendations with human-readable rationale.
    """

    def __init__(
        self,
        databricks_client: DatabricksClient,
        vector_store: LocalVectorStore,
        kg_service: KnowledgeGraphService,
    ):
        self._databricks = databricks_client
        self._vector_store = vector_store
        self._kg_service = kg_service

    @property
    def is_available(self) -> bool:
        """Check if the LLM advisor has all required services."""
        return self._vector_store.documentation_count > 0

    async def advise(self, ticket_info: TicketInfo) -> LLMAdvisorResult:
        """
        Run the full RAG pipeline for a single ticket.

        Args:
            ticket_info: Ticket information from Athena.

        Returns:
            LLMAdvisorResult with recommendation, rationale, and metadata.
        """
        start_time = time.time()

        ticket_type_short = "IR" if ticket_info.ticket_type == "incident" else "SR"
        support_groups = (
            IR_SUPPORT_GROUPS if ticket_type_short == "IR" else SR_SUPPORT_GROUPS
        )

        search_text = f"{ticket_info.title or ''} {ticket_info.description or ''}"

        # ── Step 1: Knowledge Graph query ──
        kg_context = ""
        kg_facts_count = 0
        if self._kg_service.is_available:
            try:
                kg_result = self._kg_service.query_for_chat(search_text)
                kg_facts_count = len(kg_result.get("facts", []))
                if kg_facts_count > 0:
                    kg_context = self._kg_service.format_facts_for_llm(kg_result)
            except Exception as e:
                logger.warning("KG query failed: %s", e)

        # ── Step 2: Generate embedding ──
        embedding = await self._databricks.generate_embedding(search_text[:2000])

        # ── Step 3: Search similar documentation ──
        docs = self._vector_store.find_similar_documentation(embedding, top_k=5)
        doc_context = ""
        docs_used = 0
        if docs:
            doc_parts = []
            for i, doc in enumerate(docs[:3], 1):
                content_preview = doc["content"][:300] if doc.get("content") else ""
                doc_parts.append(
                    f"  {i}. [{doc.get('section', '')}] {doc.get('title', '')} "
                    f"(similarity: {doc['similarity']:.3f})\n     {content_preview}"
                )
                docs_used += 1
            doc_context = "\n".join(doc_parts)

        # ── Step 4: Search similar tickets ──
        similar_tickets = self._vector_store.find_similar_by_embedding(
            embedding, top_k=10
        )
        ticket_context = ""
        similar_count = 0
        if similar_tickets:
            ticket_parts = []
            for t in similar_tickets[:5]:
                ticket_parts.append(
                    f"  - {t['id']} (similarity: {t['similarity']:.3f})"
                )
                similar_count += 1
            ticket_context = "\n".join(ticket_parts)

        # ── Step 5: Build candidate groups ──
        candidates = list(support_groups.keys())

        # ── Step 6: Build prompt and call LLM ──
        messages = self._build_prompt(
            ticket_info, kg_context, doc_context, ticket_context, candidates
        )

        try:
            llm_response = await self._databricks.call_llm(messages, max_tokens=512)
        except Exception as e:
            elapsed = (time.time() - start_time) * 1000
            logger.warning("LLM call failed for %s: %s", ticket_info.id, e)
            return LLMAdvisorResult(
                support_group_name="Service Desk",
                support_group_guid=resolve_group_guid(
                    "Service Desk", support_groups
                ),
                priority=None,
                rationale=f"LLM call failed: {e}",
                confidence_signal="low",
                latency_ms=elapsed,
                kg_facts_used=kg_facts_count,
                docs_used=docs_used,
                similar_tickets_used=similar_count,
            )

        # ── Step 7: Parse response ──
        group_name, priority, rationale = self._parse_response(llm_response)

        # Resolve GUID
        guid = resolve_group_guid(group_name, support_groups)
        if not guid:
            # Fallback: try to find a partial match
            guid = resolve_group_guid("Service Desk", support_groups)
            if group_name not in support_groups:
                logger.warning(
                    "LLM returned unknown group '%s' for %s, falling back",
                    group_name,
                    ticket_info.id,
                )

        # Determine confidence signal based on context richness
        confidence_signal = self._assess_confidence(
            kg_facts_count, docs_used, similar_count, docs
        )

        elapsed = (time.time() - start_time) * 1000
        return LLMAdvisorResult(
            support_group_name=group_name,
            support_group_guid=guid or "",
            priority=priority,
            rationale=rationale,
            confidence_signal=confidence_signal,
            latency_ms=elapsed,
            kg_facts_used=kg_facts_count,
            docs_used=docs_used,
            similar_tickets_used=similar_count,
        )

    def _build_prompt(
        self,
        ticket_info: TicketInfo,
        kg_context: str,
        doc_context: str,
        ticket_context: str,
        candidate_groups: list[str],
    ) -> list[dict[str, str]]:
        """Build the messages array for the LLM call."""
        candidates_text = "\n".join(f"  - {g}" for g in sorted(candidate_groups))

        ticket_text = f"""TICKET DETAILS:
- ID: {ticket_info.id}
- Type: {ticket_info.ticket_type}
- Title: {ticket_info.title or 'N/A'}
- Description: {ticket_info.description or 'N/A'}
- Location: {ticket_info.location or 'N/A'}
- Affected User: {ticket_info.affected_user or 'N/A'}
- User Job Title: {ticket_info.affected_user_title or 'N/A'}"""

        user_parts = [ticket_text]

        if kg_context:
            user_parts.append(f"\n{kg_context}")

        if doc_context:
            user_parts.append(f"\nRELEVANT DOCUMENTATION:\n{doc_context}")

        if ticket_context:
            user_parts.append(f"\nSIMILAR HISTORICAL TICKETS:\n{ticket_context}")

        user_parts.append(
            f"\nCANDIDATE GROUPS (you MUST pick from this list):\n{candidates_text}"
        )

        return [
            {"role": "system", "content": LLM_ADVISOR_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(user_parts)},
        ]

    def _parse_response(self, response: str) -> tuple[str, int | None, str]:
        """Parse the LLM JSON response. Returns (group, priority, rationale)."""
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        # Fix unescaped backslashes in group paths
        cleaned = re.sub(r'(?<!\\)\\(?!["\\/bfnrtu])', r"\\\\", cleaned)

        try:
            data = json.loads(cleaned)
            group = data.get("support_group", "Service Desk")
            priority = data.get("priority")
            rationale = data.get("rationale", "")

            if isinstance(priority, str):
                priority_map = {
                    "low": 4, "medium": 3, "high": 2, "critical": 1, "urgent": 1,
                }
                priority = priority_map.get(priority.lower(), 3)

            return group, priority, rationale
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse LLM response: %s", e)
            return "Service Desk", None, f"Parse error: {str(e)[:100]}"

    def _assess_confidence(
        self,
        kg_facts: int,
        docs_used: int,
        similar_tickets: int,
        docs: list[dict] | None,
    ) -> str:
        """Assess confidence based on context richness."""
        score = 0
        if kg_facts >= 3:
            score += 2
        elif kg_facts >= 1:
            score += 1

        if docs and docs[0].get("similarity", 0) > 0.75:
            score += 2
        elif docs_used > 0:
            score += 1

        if similar_tickets >= 3:
            score += 1

        if score >= 4:
            return "high"
        elif score >= 2:
            return "medium"
        return "low"