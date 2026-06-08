"""
Knowledge Graph Service for Feature #2 Chatbot
================================================
Provides structured knowledge retrieval from the local knowledge graph.
Used as the primary retrieval layer (graph-first), with text similarity as fallback.

The graph contains 81K+ nodes extracted from OneNote documentation:
- Escalations (conditions + target teams)
- PriorityRules (conditions + priority levels)
- TroubleshootingSteps (ordered steps within procedures)
- Procedures (named troubleshooting workflows)
- CallCapture (information gathering requirements)
- GeneralFacts (subject-predicate-object triples)
- Systems, Teams, Locations, Documents
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)

# Default path to the knowledge graph JSON file
DEFAULT_GRAPH_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "knowledge_graph",
    "output",
    "knowledge_graph.json",
)

# Minimum number of structured facts to consider graph results "sufficient"
MIN_FACTS_FOR_GRAPH_ONLY = 3


class KnowledgeGraphService:
    """
    Service that queries the local knowledge graph for structured facts.

    Loaded once at startup as a singleton. Provides fast in-memory queries
    via NetworkX (millisecond response times, no external service needed).
    """

    def __init__(self, graph_path: str | None = None) -> None:
        self._graph_path = graph_path or DEFAULT_GRAPH_PATH
        self._graph: nx.DiGraph = nx.DiGraph()
        self._system_names: list[str] = []
        self._procedure_names: list[str] = []
        self._team_names: list[str] = []
        self._loaded = False

    def load(self) -> None:
        """Load the knowledge graph from JSON. Call once at startup."""
        if self._loaded:
            return

        if not os.path.exists(self._graph_path):
            logger.warning(
                "Knowledge graph not found at %s — graph queries will return empty results",
                self._graph_path,
            )
            self._loaded = True
            return

        logger.info("Loading knowledge graph from %s ...", self._graph_path)
        with open(self._graph_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        for node in data.get("nodes", []):
            self._graph.add_node(node["id"], **node.get("attrs", {}))
        for edge in data.get("edges", []):
            self._graph.add_edge(edge["from"], edge["to"], **edge.get("attrs", {}))

        # Cache entity name lists for keyword matching
        self._system_names = sorted(self._list_all("System"), key=len, reverse=True)
        self._procedure_names = sorted(self._list_all("Procedure"), key=len, reverse=True)
        self._team_names = sorted(self._list_all("Team"), key=len, reverse=True)

        logger.info(
            "Knowledge graph loaded: %d nodes, %d edges, %d systems, %d procedures",
            self._graph.number_of_nodes(),
            self._graph.number_of_edges(),
            len(self._system_names),
            len(self._procedure_names),
        )
        self._loaded = True

    @property
    def is_loaded(self) -> bool:
        """Whether the graph has been loaded (even if empty)."""
        return self._loaded

    @property
    def is_available(self) -> bool:
        """Whether the graph has actual data."""
        return self._loaded and self._graph.number_of_nodes() > 0

    # ── Public Query API ──────────────────────────────────────────────

    def query_for_chat(self, message: str, max_results: int = 15) -> dict[str, Any]:
        """
        Query the knowledge graph for structured facts relevant to a user message.

        Returns a dict with:
            - "facts": list of structured fact dicts
            - "systems_matched": list of system names found in the query
            - "procedures_matched": list of procedure names found
            - "has_sufficient_context": bool — whether graph results are rich enough
              to serve as primary context (True = skip text fallback)

        Args:
            message: The user's chat message.
            max_results: Maximum total facts to return.

        Returns:
            Dict with structured query results.
        """
        if not self.is_available:
            return {
                "facts": [],
                "systems_matched": [],
                "procedures_matched": [],
                "has_sufficient_context": False,
            }

        message_lower = message.lower()

        # Step 1: Identify systems mentioned in the message
        matched_systems = self._match_entities(message_lower, self._system_names)

        # Step 2: Identify procedures mentioned in the message
        matched_procedures = self._match_entities(message_lower, self._procedure_names)

        # Step 3: Collect structured facts
        facts: list[dict[str, Any]] = []

        # Query by matched systems
        for system_name in matched_systems[:3]:  # Limit to top 3 systems
            system_facts = self._query_by_system(system_name)
            facts.extend(system_facts)

        # Query by matched procedures (get steps)
        for proc_name in matched_procedures[:2]:  # Limit to top 2 procedures
            proc_facts = self._get_procedure_with_steps(proc_name)
            facts.extend(proc_facts)

        # If no entity matches, do a text search across node attributes
        if not facts:
            search_facts = self._text_search(message_lower, limit=max_results)
            facts.extend(search_facts)

        # Deduplicate and limit
        seen_ids: set[str] = set()
        unique_facts: list[dict[str, Any]] = []
        for fact in facts:
            fact_id = fact.get("id", "")
            if fact_id not in seen_ids:
                seen_ids.add(fact_id)
                unique_facts.append(fact)
            if len(unique_facts) >= max_results:
                break

        # Determine if we have sufficient structured context
        # Sufficient = at least MIN_FACTS_FOR_GRAPH_ONLY actionable facts
        # (escalations, procedures, priority rules, troubleshooting steps)
        actionable_types = {"Escalation", "PriorityRule", "TroubleshootingStep", "Procedure", "CallCapture"}
        actionable_count = sum(
            1 for f in unique_facts if f.get("type") in actionable_types
        )
        has_sufficient = actionable_count >= MIN_FACTS_FOR_GRAPH_ONLY

        return {
            "facts": unique_facts,
            "systems_matched": matched_systems,
            "procedures_matched": matched_procedures,
            "has_sufficient_context": has_sufficient,
        }

    def format_facts_for_llm(self, query_result: dict[str, Any]) -> str:
        """
        Format knowledge graph query results into a string for the LLM context.

        Args:
            query_result: Output from query_for_chat().

        Returns:
            Formatted string ready for injection into the system prompt.
        """
        facts = query_result.get("facts", [])
        if not facts:
            return ""

        parts: list[str] = []
        parts.append("=== STRUCTURED KNOWLEDGE (from Knowledge Graph) ===")

        if query_result.get("systems_matched"):
            parts.append(f"Systems identified: {', '.join(query_result['systems_matched'])}")

        # Group facts by type for cleaner presentation
        escalations = [f for f in facts if f["type"] == "Escalation"]
        priority_rules = [f for f in facts if f["type"] == "PriorityRule"]
        procedures = [f for f in facts if f["type"] == "Procedure"]
        steps = [f for f in facts if f["type"] == "TroubleshootingStep"]
        call_captures = [f for f in facts if f["type"] == "CallCapture"]
        general = [f for f in facts if f["type"] == "GeneralFact"]

        if escalations:
            parts.append("\n--- Escalation Paths ---")
            for esc in escalations:
                parts.append(
                    f"• When: {esc.get('condition', 'N/A')}\n"
                    f"  Escalate to: {esc.get('target_team', 'N/A')}\n"
                    f"  Urgency: {esc.get('urgency', 'N/A')}"
                )

        if priority_rules:
            parts.append("\n--- Priority Rules ---")
            for pr in priority_rules:
                parts.append(
                    f"• Condition: {pr.get('condition', 'N/A')}\n"
                    f"  Priority: {pr.get('priority', 'N/A')}\n"
                    f"  Reason: {pr.get('reason', 'N/A')}"
                )

        if procedures or steps:
            parts.append("\n--- Troubleshooting Procedures ---")
            for proc in procedures:
                parts.append(f"\nProcedure: {proc.get('name', 'Unknown')}")
                proc_steps = proc.get("steps", [])
                for step in proc_steps:
                    action = step.get("action", "")
                    if_fails = step.get("if_fails", "")
                    parts.append(f"  Step {step.get('step_order', '?')}: {action}")
                    if if_fails:
                        parts.append(f"    → If fails: {if_fails}")
            # Standalone steps (not grouped under a procedure)
            standalone_steps = [s for s in steps if not any(
                s.get("id") in [st.get("id", "") for st in p.get("steps", [])]
                for p in procedures
            )]
            for step in standalone_steps:
                parts.append(
                    f"• Step: {step.get('action', 'N/A')}"
                    + (f"\n  If fails: {step.get('if_fails', '')}" if step.get("if_fails") else "")
                )

        if call_captures:
            parts.append("\n--- Information to Gather ---")
            for cc in call_captures:
                parts.append(
                    f"• Scenario: {cc.get('scenario', 'N/A')}\n"
                    f"  Required fields: {cc.get('required_fields', 'N/A')}\n"
                    f"  Route to: {cc.get('support_group', 'N/A')}"
                )

        if general:
            parts.append("\n--- Additional Facts ---")
            for gf in general[:5]:  # Limit general facts
                subject = gf.get("subject", "")
                predicate = gf.get("predicate", "")
                obj = gf.get("object", "")
                if subject and predicate and obj:
                    parts.append(f"• {subject} — {predicate} — {obj}")
                elif gf.get("context"):
                    parts.append(f"• {gf['context']}")

        return "\n".join(parts)

    # ── Private Helpers ───────────────────────────────────────────────

    def _match_entities(self, message_lower: str, entity_names: list[str]) -> list[str]:
        """
        Find entity names mentioned in the message using substring matching.
        Names are sorted longest-first to prefer more specific matches.
        """
        matched: list[str] = []
        for name in entity_names:
            if len(name) < 3:  # Skip very short names to avoid false positives
                continue
            if name.lower() in message_lower:
                matched.append(name)
                if len(matched) >= 5:
                    break
        return matched

    def _query_by_system(self, system_name: str) -> list[dict[str, Any]]:
        """Find escalations, priority rules, and other facts for a system."""
        sys_id = f"System::{system_name}"
        if sys_id not in self._graph:
            return []

        facts: list[dict[str, Any]] = []

        for pred in self._graph.predecessors(sys_id):
            node_attrs = dict(self._graph.nodes[pred])
            edge_attrs = dict(self._graph.edges[pred, sys_id])
            node_type = node_attrs.get("type", "")

            if node_type == "Escalation":
                # Find the target team
                target_team = ""
                for succ in self._graph.successors(pred):
                    succ_attrs = self._graph.nodes.get(succ, {})
                    if succ_attrs.get("type") == "Team":
                        target_team = succ_attrs.get("name", "")
                        break
                facts.append({
                    "id": pred,
                    "type": "Escalation",
                    "condition": node_attrs.get("condition", ""),
                    "urgency": node_attrs.get("urgency", ""),
                    "target_team": target_team,
                })

            elif node_type == "PriorityRule":
                facts.append({
                    "id": pred,
                    "type": "PriorityRule",
                    "condition": node_attrs.get("condition", ""),
                    "priority": node_attrs.get("priority", ""),
                    "reason": node_attrs.get("reason", ""),
                })

            elif node_type == "TroubleshootingStep":
                facts.append({
                    "id": pred,
                    "type": "TroubleshootingStep",
                    "action": node_attrs.get("action", ""),
                    "step_order": node_attrs.get("step_order", 0),
                    "if_fails": node_attrs.get("if_fails", ""),
                })

            elif node_type == "CallCapture":
                facts.append({
                    "id": pred,
                    "type": "CallCapture",
                    "scenario": node_attrs.get("scenario", ""),
                    "required_fields": node_attrs.get("required_fields", "[]"),
                    "support_group": self._get_routed_team(pred),
                })

            elif node_type == "GeneralFact":
                facts.append({
                    "id": pred,
                    "type": "GeneralFact",
                    "subject": node_attrs.get("subject", ""),
                    "predicate": node_attrs.get("predicate", ""),
                    "object": node_attrs.get("object", ""),
                    "context": node_attrs.get("context", ""),
                })

        return facts

    def _get_procedure_with_steps(self, procedure_name: str) -> list[dict[str, Any]]:
        """Get a procedure and its ordered steps."""
        proc_id = f"Procedure::{procedure_name}"
        if proc_id not in self._graph:
            return []

        steps: list[dict[str, Any]] = []
        for succ in self._graph.successors(proc_id):
            edge_attrs = dict(self._graph.edges[proc_id, succ])
            if edge_attrs.get("rel_type") == "HAS_STEP":
                node_attrs = dict(self._graph.nodes[succ])
                steps.append({
                    "id": succ,
                    "type": "TroubleshootingStep",
                    "action": node_attrs.get("action", ""),
                    "step_order": node_attrs.get("step_order", edge_attrs.get("order", 0)),
                    "if_fails": node_attrs.get("if_fails", ""),
                })

        steps.sort(key=lambda s: s["step_order"])

        return [{
            "id": proc_id,
            "type": "Procedure",
            "name": procedure_name,
            "steps": steps,
        }]

    def _get_routed_team(self, node_id: str) -> str:
        """Find the team a node routes to."""
        for succ in self._graph.successors(node_id):
            succ_attrs = self._graph.nodes.get(succ, {})
            edge_attrs = self._graph.edges.get((node_id, succ), {})
            if succ_attrs.get("type") == "Team" or edge_attrs.get("rel_type") == "ROUTES_TO":
                return succ_attrs.get("name", "")
        return ""

    def _text_search(self, query_lower: str, limit: int = 15) -> list[dict[str, Any]]:
        """
        Full-text search across node attributes.
        Used as fallback when no system/procedure entities are matched.
        """
        # Split query into keywords (ignore very short words)
        keywords = [w for w in query_lower.split() if len(w) >= 3]
        if not keywords:
            return []

        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        for node_id, attrs in self._graph.nodes(data=True):
            node_type = attrs.get("type", "")
            # Skip Document nodes (we want facts, not source docs)
            if node_type == "Document":
                continue

            # Check if any keyword matches any string attribute
            matched = False
            for _key, value in attrs.items():
                if isinstance(value, str) and any(kw in value.lower() for kw in keywords):
                    matched = True
                    break

            if matched and node_id not in seen:
                seen.add(node_id)
                fact = self._node_to_fact(node_id, attrs)
                if fact:
                    results.append(fact)
                if len(results) >= limit:
                    break

        return results

    def _node_to_fact(self, node_id: str, attrs: dict) -> dict[str, Any] | None:
        """Convert a graph node to a structured fact dict."""
        node_type = attrs.get("type", "")

        if node_type == "Escalation":
            target_team = ""
            for succ in self._graph.successors(node_id):
                succ_attrs = self._graph.nodes.get(succ, {})
                if succ_attrs.get("type") == "Team":
                    target_team = succ_attrs.get("name", "")
                    break
            return {
                "id": node_id,
                "type": "Escalation",
                "condition": attrs.get("condition", ""),
                "urgency": attrs.get("urgency", ""),
                "target_team": target_team,
            }

        elif node_type == "PriorityRule":
            return {
                "id": node_id,
                "type": "PriorityRule",
                "condition": attrs.get("condition", ""),
                "priority": attrs.get("priority", ""),
                "reason": attrs.get("reason", ""),
            }

        elif node_type == "TroubleshootingStep":
            return {
                "id": node_id,
                "type": "TroubleshootingStep",
                "action": attrs.get("action", ""),
                "step_order": attrs.get("step_order", 0),
                "if_fails": attrs.get("if_fails", ""),
            }

        elif node_type == "CallCapture":
            return {
                "id": node_id,
                "type": "CallCapture",
                "scenario": attrs.get("scenario", ""),
                "required_fields": attrs.get("required_fields", "[]"),
                "support_group": self._get_routed_team(node_id),
            }

        elif node_type == "GeneralFact":
            return {
                "id": node_id,
                "type": "GeneralFact",
                "subject": attrs.get("subject", ""),
                "predicate": attrs.get("predicate", ""),
                "object": attrs.get("object", ""),
                "context": attrs.get("context", ""),
            }

        elif node_type == "Procedure":
            # Get steps for this procedure
            steps = []
            for succ in self._graph.successors(node_id):
                edge_attrs = self._graph.edges.get((node_id, succ), {})
                if edge_attrs.get("rel_type") == "HAS_STEP":
                    succ_attrs = dict(self._graph.nodes[succ])
                    steps.append({
                        "id": succ,
                        "type": "TroubleshootingStep",
                        "action": succ_attrs.get("action", ""),
                        "step_order": succ_attrs.get("step_order", edge_attrs.get("order", 0)),
                        "if_fails": succ_attrs.get("if_fails", ""),
                    })
            steps.sort(key=lambda s: s["step_order"])
            return {
                "id": node_id,
                "type": "Procedure",
                "name": attrs.get("name", ""),
                "steps": steps[:10],  # Limit steps
            }

        return None

    def _list_all(self, node_type: str) -> list[str]:
        """List all node names of a given type."""
        return [
            attrs.get("name", "")
            for _n, attrs in self._graph.nodes(data=True)
            if attrs.get("type") == node_type and attrs.get("name")
        ]