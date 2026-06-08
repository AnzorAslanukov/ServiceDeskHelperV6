"""
Unit tests for KnowledgeGraphService — Feature #2 enhancement.

Tests the graph-first retrieval layer:
- Loading behavior (missing file, empty graph)
- Entity matching (systems, procedures)
- Query methods (by system, by procedure, text search)
- LLM context formatting
- Integration with ChatbotService (graph-first, text-fallback logic)
"""

import json
import os
import tempfile

import pytest

from src.services.knowledge_graph import KnowledgeGraphService


# ── Test Graph Data ───────────────────────────────────────────────────


def _build_test_graph() -> dict:
    """Build a small test knowledge graph for unit testing."""
    return {
        "nodes": [
            {"id": "Document::PennChart Password Reset", "attrs": {"type": "Document", "name": "PennChart Password Reset", "section": "PennChart", "notebook": "uphs_notebook"}},
            {"id": "System::PennChart", "attrs": {"type": "System", "name": "PennChart"}},
            {"id": "System::Active Directory", "attrs": {"type": "System", "name": "Active Directory"}},
            {"id": "Team::PennChart\\User Provisioning", "attrs": {"type": "Team", "name": "PennChart\\User Provisioning"}},
            {"id": "Team::Service Desk", "attrs": {"type": "Team", "name": "Service Desk"}},
            {"id": "Escalation::esc-001", "attrs": {"type": "Escalation", "name": "esc-001", "condition": "User cannot reset PennChart password after 3 attempts", "urgency": "High"}},
            {"id": "Escalation::esc-002", "attrs": {"type": "Escalation", "name": "esc-002", "condition": "PennChart account locked out", "urgency": "Medium"}},
            {"id": "PriorityRule::pr-001", "attrs": {"type": "PriorityRule", "name": "pr-001", "condition": "PennChart is DOWN for all users", "priority": "1", "reason": "Enterprise-wide outage"}},
            {"id": "Priority::1", "attrs": {"type": "Priority", "name": "1"}},
            {"id": "Procedure::PennChart Password Reset Steps", "attrs": {"type": "Procedure", "name": "PennChart Password Reset Steps"}},
            {"id": "TroubleshootingStep::ts-001", "attrs": {"type": "TroubleshootingStep", "name": "ts-001", "action": "Verify user identity via security questions", "step_order": 1, "if_fails": ""}},
            {"id": "TroubleshootingStep::ts-002", "attrs": {"type": "TroubleshootingStep", "name": "ts-002", "action": "Navigate to PennChart Admin > User Management", "step_order": 2, "if_fails": "Check if you have admin access"}},
            {"id": "TroubleshootingStep::ts-003", "attrs": {"type": "TroubleshootingStep", "name": "ts-003", "action": "Reset password and notify user", "step_order": 3, "if_fails": "Escalate to PennChart User Provisioning"}},
            {"id": "CallCapture::cc-001", "attrs": {"type": "CallCapture", "name": "cc-001", "scenario": "User reports PennChart login failure", "required_fields": "[\"username\", \"error message\", \"last successful login\"]", "ticket_type": "IR"}},
            {"id": "GeneralFact::gf-001", "attrs": {"type": "GeneralFact", "name": "gf-001", "subject": "PennChart", "predicate": "uses", "object": "Active Directory for authentication", "context": ""}},
        ],
        "edges": [
            # Escalations → Team
            {"from": "Escalation::esc-001", "to": "Team::PennChart\\User Provisioning", "attrs": {"rel_type": "ESCALATES_TO"}},
            {"from": "Escalation::esc-002", "to": "Team::Service Desk", "attrs": {"rel_type": "ESCALATES_TO"}},
            # Escalations → System
            {"from": "Escalation::esc-001", "to": "System::PennChart", "attrs": {"rel_type": "INVOLVES_SYSTEM"}},
            {"from": "Escalation::esc-002", "to": "System::PennChart", "attrs": {"rel_type": "INVOLVES_SYSTEM"}},
            # PriorityRule → System, Priority
            {"from": "PriorityRule::pr-001", "to": "System::PennChart", "attrs": {"rel_type": "INVOLVES_SYSTEM"}},
            {"from": "PriorityRule::pr-001", "to": "Priority::1", "attrs": {"rel_type": "REQUIRES_PRIORITY"}},
            # Procedure → Steps
            {"from": "Procedure::PennChart Password Reset Steps", "to": "TroubleshootingStep::ts-001", "attrs": {"rel_type": "HAS_STEP", "order": 1}},
            {"from": "Procedure::PennChart Password Reset Steps", "to": "TroubleshootingStep::ts-002", "attrs": {"rel_type": "HAS_STEP", "order": 2}},
            {"from": "Procedure::PennChart Password Reset Steps", "to": "TroubleshootingStep::ts-003", "attrs": {"rel_type": "HAS_STEP", "order": 3}},
            # Steps → System
            {"from": "TroubleshootingStep::ts-001", "to": "System::PennChart", "attrs": {"rel_type": "INVOLVES_SYSTEM"}},
            {"from": "TroubleshootingStep::ts-002", "to": "System::PennChart", "attrs": {"rel_type": "INVOLVES_SYSTEM"}},
            {"from": "TroubleshootingStep::ts-003", "to": "System::PennChart", "attrs": {"rel_type": "INVOLVES_SYSTEM"}},
            # CallCapture → Team
            {"from": "CallCapture::cc-001", "to": "Team::Service Desk", "attrs": {"rel_type": "ROUTES_TO"}},
            # CallCapture → System
            {"from": "CallCapture::cc-001", "to": "System::PennChart", "attrs": {"rel_type": "INVOLVES_SYSTEM"}},
            # GeneralFact → System
            {"from": "GeneralFact::gf-001", "to": "System::PennChart", "attrs": {"rel_type": "INVOLVES_SYSTEM"}},
            # Document edges
            {"from": "Escalation::esc-001", "to": "Document::PennChart Password Reset", "attrs": {"rel_type": "EXTRACTED_FROM"}},
            {"from": "Escalation::esc-002", "to": "Document::PennChart Password Reset", "attrs": {"rel_type": "EXTRACTED_FROM"}},
            # System dependency
            {"from": "System::PennChart", "to": "System::Active Directory", "attrs": {"rel_type": "DEPENDS_ON"}},
        ],
    }


@pytest.fixture
def test_graph_path(tmp_path) -> str:
    """Create a temporary knowledge graph JSON file for testing."""
    graph_file = tmp_path / "test_knowledge_graph.json"
    graph_file.write_text(json.dumps(_build_test_graph()), encoding="utf-8")
    return str(graph_file)


@pytest.fixture
def kg_service(test_graph_path) -> KnowledgeGraphService:
    """Create a KnowledgeGraphService loaded with test data."""
    service = KnowledgeGraphService(graph_path=test_graph_path)
    service.load()
    return service


# ── Loading Tests ─────────────────────────────────────────────────────


def test_load_graph_from_file(kg_service):
    """Service should load graph data from JSON file."""
    assert kg_service.is_loaded is True
    assert kg_service.is_available is True


def test_load_missing_file():
    """Service should handle missing graph file gracefully."""
    service = KnowledgeGraphService(graph_path="/nonexistent/path.json")
    service.load()

    assert service.is_loaded is True
    assert service.is_available is False


def test_load_only_once(kg_service, test_graph_path):
    """Loading should be idempotent — second call is a no-op."""
    kg_service.load()  # Second call
    assert kg_service.is_available is True


# ── Entity Matching Tests ─────────────────────────────────────────────


def test_query_matches_system_name(kg_service):
    """Should match system names in user messages."""
    result = kg_service.query_for_chat("How do I reset a PennChart password?")

    assert "PennChart" in result["systems_matched"]


def test_query_matches_procedure_name(kg_service):
    """Should match procedure names in user messages."""
    result = kg_service.query_for_chat("What are the PennChart Password Reset Steps?")

    assert "PennChart Password Reset Steps" in result["procedures_matched"]


def test_query_no_match_returns_text_search(kg_service):
    """When no entities match, should fall back to text search."""
    result = kg_service.query_for_chat("How do I handle a locked account?")

    # Should still find results via text search (keyword "locked" matches esc-002 condition)
    assert len(result["facts"]) > 0


def test_query_empty_message(kg_service):
    """Empty or very short messages should return no results."""
    result = kg_service.query_for_chat("")

    assert result["facts"] == []
    assert result["has_sufficient_context"] is False


# ── System Query Tests ────────────────────────────────────────────────


def test_query_by_system_returns_escalations(kg_service):
    """Querying by system should return related escalation paths."""
    result = kg_service.query_for_chat("PennChart is not working")

    escalations = [f for f in result["facts"] if f["type"] == "Escalation"]
    assert len(escalations) >= 1
    # Check that target team is resolved
    assert any(e.get("target_team") for e in escalations)


def test_query_by_system_returns_priority_rules(kg_service):
    """Querying by system should return related priority rules."""
    result = kg_service.query_for_chat("PennChart is completely down")

    priority_rules = [f for f in result["facts"] if f["type"] == "PriorityRule"]
    assert len(priority_rules) >= 1
    assert priority_rules[0]["priority"] == "1"
    assert "outage" in priority_rules[0]["reason"].lower()


def test_query_by_system_returns_call_capture(kg_service):
    """Querying by system should return call capture requirements."""
    result = kg_service.query_for_chat("PennChart login issue")

    call_captures = [f for f in result["facts"] if f["type"] == "CallCapture"]
    assert len(call_captures) >= 1
    assert "username" in call_captures[0]["required_fields"]


# ── Procedure Query Tests ─────────────────────────────────────────────


def test_query_procedure_returns_ordered_steps(kg_service):
    """Querying a procedure should return steps in order."""
    result = kg_service.query_for_chat("PennChart Password Reset Steps")

    procedures = [f for f in result["facts"] if f["type"] == "Procedure"]
    assert len(procedures) == 1
    steps = procedures[0]["steps"]
    assert len(steps) == 3
    assert steps[0]["step_order"] <= steps[1]["step_order"] <= steps[2]["step_order"]
    assert "Verify user identity" in steps[0]["action"]
    assert "Reset password" in steps[2]["action"]


# ── Sufficient Context Detection ─────────────────────────────────────


def test_sufficient_context_with_multiple_actionable_facts(kg_service):
    """Should report sufficient context when enough actionable facts are found."""
    result = kg_service.query_for_chat("PennChart password reset")

    # PennChart system has escalations + priority rules + call captures + steps
    assert result["has_sufficient_context"] is True


def test_insufficient_context_for_unknown_topic(kg_service):
    """Should report insufficient context for topics not in the graph."""
    result = kg_service.query_for_chat("How do I order a new laptop?")

    assert result["has_sufficient_context"] is False


# ── LLM Formatting Tests ─────────────────────────────────────────────


def test_format_facts_includes_escalation_paths(kg_service):
    """Formatted output should include escalation paths."""
    result = kg_service.query_for_chat("PennChart is not working")
    formatted = kg_service.format_facts_for_llm(result)

    assert "Escalation Paths" in formatted
    assert "Escalate to:" in formatted


def test_format_facts_includes_priority_rules(kg_service):
    """Formatted output should include priority rules."""
    result = kg_service.query_for_chat("PennChart is down for everyone")
    formatted = kg_service.format_facts_for_llm(result)

    assert "Priority Rules" in formatted
    assert "Priority:" in formatted


def test_format_facts_includes_procedures(kg_service):
    """Formatted output should include troubleshooting procedures with steps."""
    result = kg_service.query_for_chat("PennChart Password Reset Steps")
    formatted = kg_service.format_facts_for_llm(result)

    assert "Troubleshooting Procedures" in formatted
    assert "Step" in formatted


def test_format_facts_empty_result(kg_service):
    """Formatting empty results should return empty string."""
    empty_result = {"facts": [], "systems_matched": [], "procedures_matched": [], "has_sufficient_context": False}
    formatted = kg_service.format_facts_for_llm(empty_result)

    assert formatted == ""


def test_format_facts_includes_systems_header(kg_service):
    """Formatted output should mention matched systems."""
    result = kg_service.query_for_chat("PennChart login failure")
    formatted = kg_service.format_facts_for_llm(result)

    assert "STRUCTURED KNOWLEDGE" in formatted
    assert "PennChart" in formatted