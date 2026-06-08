"""
Knowledge Graph Store (File-based)
===================================
Lightweight graph store using NetworkX + JSON persistence.
No Docker/Neo4j required — stores the knowledge graph as a JSON file.

Usage:
    from knowledge_graph_store import KnowledgeGraphStore
    store = KnowledgeGraphStore("output/knowledge_graph.json")
    store.store_fact(fact_dict)
    stats = store.get_stats()
    store.save()
"""

import os
import json
import networkx as nx
from typing import Optional


class KnowledgeGraphStore:
    """File-based knowledge graph using NetworkX for in-memory graph operations."""

    def __init__(self, filepath: str = "output/knowledge_graph.json"):
        self.filepath = filepath
        self.graph = nx.DiGraph()
        self._load()

    def _load(self):
        """Load graph from JSON file if it exists."""
        if os.path.exists(self.filepath):
            with open(self.filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # Reconstruct graph from serialized format
            for node in data.get("nodes", []):
                self.graph.add_node(node["id"], **node.get("attrs", {}))
            for edge in data.get("edges", []):
                self.graph.add_edge(edge["from"], edge["to"], **edge.get("attrs", {}))

    def save(self):
        """Persist graph to JSON file."""
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        data = {
            "nodes": [
                {"id": n, "attrs": dict(self.graph.nodes[n])}
                for n in self.graph.nodes
            ],
            "edges": [
                {"from": u, "to": v, "attrs": dict(self.graph.edges[u, v])}
                for u, v in self.graph.edges
            ]
        }
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _node_id(self, node_type: str, name: str) -> str:
        """Create a unique node ID from type and name."""
        return f"{node_type}::{name}"

    def _ensure_node(self, node_type: str, name: str, **attrs) -> str:
        """Create node if it doesn't exist, update attrs if it does."""
        node_id = self._node_id(node_type, name)
        if node_id not in self.graph:
            self.graph.add_node(node_id, type=node_type, name=name, **attrs)
        else:
            # Update attributes
            for k, v in attrs.items():
                if v:  # Only update non-empty values
                    self.graph.nodes[node_id][k] = v
        return node_id

    def _add_edge(self, from_id: str, to_id: str, rel_type: str, **attrs):
        """Add an edge (relationship) between two nodes."""
        self.graph.add_edge(from_id, to_id, rel_type=rel_type, **attrs)

    def store_fact(self, fact: dict):
        """Store a single extracted fact with appropriate nodes and relationships."""
        fact_type = fact.get("type", "general_fact")

        # Create source document node
        doc_id = self._ensure_node(
            "Document",
            fact["_source_title"],
            section=fact["_source_section"],
            notebook=fact["_source_notebook"]
        )

        if fact_type == "priority_rule":
            self._store_priority_rule(fact, doc_id)
        elif fact_type == "escalation":
            self._store_escalation(fact, doc_id)
        elif fact_type == "troubleshooting_step":
            self._store_troubleshooting_step(fact, doc_id)
        elif fact_type == "system_dependency":
            self._store_system_dependency(fact, doc_id)
        elif fact_type == "call_capture":
            self._store_call_capture(fact, doc_id)
        elif fact_type == "general_fact":
            self._store_general_fact(fact, doc_id)

    def _store_priority_rule(self, fact: dict, doc_id: str):
        """Store a priority rule."""
        fact_id = self._ensure_node(
            "PriorityRule",
            fact["_id"],
            condition=fact.get("condition", ""),
            reason=fact.get("reason", ""),
            priority=str(fact.get("priority", "3"))
        )

        # Link to priority level
        priority_id = self._ensure_node("Priority", str(fact.get("priority", "3")))
        self._add_edge(fact_id, priority_id, "REQUIRES_PRIORITY")

        # Link to source document
        self._add_edge(fact_id, doc_id, "EXTRACTED_FROM")

        # Link to systems
        for system in fact.get("systems", []):
            if system:
                sys_id = self._ensure_node("System", system)
                self._add_edge(fact_id, sys_id, "INVOLVES_SYSTEM")

        # Link to locations
        for location in fact.get("locations", []):
            if location:
                loc_id = self._ensure_node("Location", location)
                self._add_edge(fact_id, loc_id, "AT_LOCATION")

    def _store_escalation(self, fact: dict, doc_id: str):
        """Store an escalation path."""
        fact_id = self._ensure_node(
            "Escalation",
            fact["_id"],
            condition=fact.get("condition", ""),
            urgency=fact.get("urgency", "")
        )

        # Link to target team
        team_id = self._ensure_node("Team", fact.get("target_group", "Unknown"))
        self._add_edge(fact_id, team_id, "ESCALATES_TO")

        # Link to source document
        self._add_edge(fact_id, doc_id, "EXTRACTED_FROM")

        # Link to systems
        for system in fact.get("systems", []):
            if system:
                sys_id = self._ensure_node("System", system)
                self._add_edge(fact_id, sys_id, "INVOLVES_SYSTEM")

    def _store_troubleshooting_step(self, fact: dict, doc_id: str):
        """Store a troubleshooting step."""
        fact_id = self._ensure_node(
            "TroubleshootingStep",
            fact["_id"],
            action=fact.get("action", ""),
            step_order=fact.get("step_order", 0),
            if_fails=fact.get("if_fails", "")
        )

        # Link to procedure
        proc_id = self._ensure_node("Procedure", fact.get("procedure_name", "Unknown"))
        self._add_edge(proc_id, fact_id, "HAS_STEP", order=fact.get("step_order", 0))

        # Link to source document
        self._add_edge(fact_id, doc_id, "EXTRACTED_FROM")

        # Link to systems
        for system in fact.get("systems", []):
            if system:
                sys_id = self._ensure_node("System", system)
                self._add_edge(fact_id, sys_id, "INVOLVES_SYSTEM")

    def _store_system_dependency(self, fact: dict, doc_id: str):
        """Store a system dependency."""
        sys1_id = self._ensure_node("System", fact.get("system", "Unknown"))
        sys2_id = self._ensure_node("System", fact.get("depends_on", "Unknown"))
        self._add_edge(sys1_id, sys2_id, "DEPENDS_ON",
                       relationship=fact.get("relationship", ""),
                       fact_id=fact["_id"])
        self._add_edge(doc_id, sys1_id, "DOCUMENTS")

    def _store_call_capture(self, fact: dict, doc_id: str):
        """Store call capture requirements."""
        fact_id = self._ensure_node(
            "CallCapture",
            fact["_id"],
            scenario=fact.get("scenario", ""),
            required_fields=json.dumps(fact.get("required_fields", [])),
            ticket_type=fact.get("ticket_type", "")
        )

        # Link to source document
        self._add_edge(fact_id, doc_id, "EXTRACTED_FROM")

        # Link to target team
        target = fact.get("support_group", "")
        if target:
            team_id = self._ensure_node("Team", target)
            self._add_edge(fact_id, team_id, "ROUTES_TO")

    def _store_general_fact(self, fact: dict, doc_id: str):
        """Store a general fact."""
        fact_id = self._ensure_node(
            "GeneralFact",
            fact["_id"],
            subject=fact.get("subject", ""),
            predicate=fact.get("predicate", ""),
            object=fact.get("object", ""),
            context=fact.get("context", "")
        )

        # Link to source document
        self._add_edge(fact_id, doc_id, "EXTRACTED_FROM")

    def get_stats(self) -> dict:
        """Get graph statistics."""
        node_types = {}
        for n, attrs in self.graph.nodes(data=True):
            t = attrs.get("type", "Unknown")
            node_types[t] = node_types.get(t, 0) + 1

        edge_types = {}
        for u, v, attrs in self.graph.edges(data=True):
            t = attrs.get("rel_type", "Unknown")
            edge_types[t] = edge_types.get(t, 0) + 1

        return {
            "total_nodes": self.graph.number_of_nodes(),
            "total_edges": self.graph.number_of_edges(),
            "node_types": node_types,
            "edge_types": edge_types
        }

    def query_by_system(self, system_name: str) -> list[dict]:
        """Find all facts related to a system."""
        sys_id = self._node_id("System", system_name)
        if sys_id not in self.graph:
            return []

        results = []
        # Find all nodes that have an edge TO this system
        for pred in self.graph.predecessors(sys_id):
            node_attrs = dict(self.graph.nodes[pred])
            edge_attrs = dict(self.graph.edges[pred, sys_id])
            results.append({
                "node_id": pred,
                "node_attrs": node_attrs,
                "relationship": edge_attrs.get("rel_type", "")
            })
        return results

    def query_by_team(self, team_name: str) -> list[dict]:
        """Find all facts that escalate/route to a team."""
        team_id = self._node_id("Team", team_name)
        if team_id not in self.graph:
            return []

        results = []
        for pred in self.graph.predecessors(team_id):
            node_attrs = dict(self.graph.nodes[pred])
            edge_attrs = dict(self.graph.edges[pred, team_id])
            results.append({
                "node_id": pred,
                "node_attrs": node_attrs,
                "relationship": edge_attrs.get("rel_type", "")
            })
        return results

    def query_by_priority(self, level: str) -> list[dict]:
        """Find all facts that require a specific priority level."""
        priority_id = self._node_id("Priority", level)
        if priority_id not in self.graph:
            return []

        results = []
        for pred in self.graph.predecessors(priority_id):
            node_attrs = dict(self.graph.nodes[pred])
            results.append({
                "node_id": pred,
                "node_attrs": node_attrs
            })
        return results

    def get_procedure_steps(self, procedure_name: str) -> list[dict]:
        """Get all steps for a procedure, ordered by step_order."""
        proc_id = self._node_id("Procedure", procedure_name)
        if proc_id not in self.graph:
            return []

        steps = []
        for succ in self.graph.successors(proc_id):
            edge_attrs = dict(self.graph.edges[proc_id, succ])
            if edge_attrs.get("rel_type") == "HAS_STEP":
                node_attrs = dict(self.graph.nodes[succ])
                steps.append({
                    "step_order": node_attrs.get("step_order", edge_attrs.get("order", 0)),
                    "action": node_attrs.get("action", ""),
                    "if_fails": node_attrs.get("if_fails", "")
                })

        return sorted(steps, key=lambda s: s["step_order"])

    def list_all(self, node_type: str) -> list[str]:
        """List all node names of a given type."""
        return [
            attrs.get("name", n)
            for n, attrs in self.graph.nodes(data=True)
            if attrs.get("type") == node_type
        ]