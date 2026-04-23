"""
Unit tests for AssignmentService — Feature #3: Ticket Assignment Recommendation.

Tests the assignment pipeline with mocked clients:
- Ticket fetching (IR vs SR routing)
- Embedding generation from ticket content
- Semantic search for documentation and similar tickets
- LLM call with correct support group mappings per ticket type
- JSON response parsing (valid, malformed, code-fenced)
- Error handling (invalid prefix, ticket not found)
- Support group loading from JSON file (valid, missing, empty, malformed)
"""

import json
import tempfile
from pathlib import Path

import pytest

from src.models.chat import SourceType
from src.services.assignment import (
    AssignmentService,
    IR_SUPPORT_GROUPS,
    SR_SUPPORT_GROUPS,
    load_support_groups,
    _FALLBACK_IR_SUPPORT_GROUPS,
    _FALLBACK_SR_SUPPORT_GROUPS,
)


# ── Sample LLM Responses ─────────────────────────────────────────────

# Use EUS\HUP — an assignable group (not the disabled parent "EUS")
VALID_LLM_JSON = json.dumps(
    {
        "support_group_name": "EUS\\HUP",
        "support_group_guid": "d2ba6580-40d1-1574-7b41-89314f4b22c6",
        "priority": 3,
        "rationale": "This is a hardware issue at HUP that should be handled by the End User Support HUP team. Priority 3 is appropriate for a single-user printer issue.",
    }
)

VALID_SR_LLM_JSON = json.dumps(
    {
        "support_group_name": "PennChart\\User Provisioning",
        "support_group_guid": "2624881f-0303-7e3d-28e1-7fa1fb7f1198",
        "priority": "Medium",
        "rationale": "This is a PennChart access request that should be routed to User Provisioning.",
    }
)

CODE_FENCED_LLM_JSON = f"```json\n{VALID_LLM_JSON}\n```"

INVALID_LLM_RESPONSE = "I think this ticket should go to EUS because it's a printer issue."


# ── Support Group Loading ─────────────────────────────────────────────


def test_loaded_ir_groups_exclude_disabled_parents():
    """IR_SUPPORT_GROUPS should NOT contain disabled parent groups like 'EUS' or 'Applications'."""
    # These are disabled parent categories that should have been excluded
    disabled_parents = [
        "Applications",
        "EUS",
        "IS Operations",
        "Non-Corp IS",
        "PennChart",
        "Technology\\Infrastructure",
    ]
    for name in disabled_parents:
        assert name not in IR_SUPPORT_GROUPS, (
            f"Disabled parent group '{name}' should not be in IR_SUPPORT_GROUPS"
        )


def test_loaded_sr_groups_exclude_disabled_parents():
    """SR_SUPPORT_GROUPS should NOT contain disabled parent groups."""
    disabled_parents = [
        "Applications",
        "EUS",
        "IS Operations",
        "Non-Corp IS",
        "PennChart",
        "Technology\\Infrastructure",
    ]
    for name in disabled_parents:
        assert name not in SR_SUPPORT_GROUPS, (
            f"Disabled parent group '{name}' should not be in SR_SUPPORT_GROUPS"
        )


def test_loaded_ir_groups_contain_assignable_children():
    """IR_SUPPORT_GROUPS should contain assignable children of disabled parents."""
    # These are assignable children that replaced the disabled parents
    expected_children = [
        "EUS\\HUP",
        "EUS\\PPMC",
        "PennChart\\ED",
        "PennChart\\Ambulatory",
        "IS Operations\\Athena",
        "IS Operations\\ISAAC",
    ]
    for name in expected_children:
        assert name in IR_SUPPORT_GROUPS, (
            f"Assignable child group '{name}' should be in IR_SUPPORT_GROUPS"
        )


def test_loaded_sr_groups_contain_assignable_children():
    """SR_SUPPORT_GROUPS should contain assignable children of disabled parents."""
    expected_children = [
        "EUS\\HUP",
        "EUS\\PPMC",
        "PennChart\\ED",
        "PennChart\\Ambulatory",
        "IS Operations\\Athena",
        "IS Operations\\ISAAC",
    ]
    for name in expected_children:
        assert name in SR_SUPPORT_GROUPS, (
            f"Assignable child group '{name}' should be in SR_SUPPORT_GROUPS"
        )


def test_loaded_groups_have_different_guids_for_ir_and_sr():
    """IR and SR groups with the same name should have different GUIDs."""
    common_names = set(IR_SUPPORT_GROUPS.keys()) & set(SR_SUPPORT_GROUPS.keys())
    assert len(common_names) > 10, "Should have many common group names"
    for name in common_names:
        assert IR_SUPPORT_GROUPS[name] != SR_SUPPORT_GROUPS[name], (
            f"Group '{name}' should have different GUIDs for IR and SR"
        )


def test_loaded_groups_contain_service_desk():
    """Both IR and SR groups should contain 'Service Desk' (used as fallback)."""
    assert "Service Desk" in IR_SUPPORT_GROUPS
    assert "Service Desk" in SR_SUPPORT_GROUPS


def test_loaded_ir_groups_count():
    """Should have loaded ~309 IR assignable groups from JSON."""
    assert len(IR_SUPPORT_GROUPS) > 200, (
        f"Expected 300+ IR groups, got {len(IR_SUPPORT_GROUPS)}"
    )


def test_loaded_sr_groups_count():
    """Should have loaded ~310 SR assignable groups from JSON."""
    assert len(SR_SUPPORT_GROUPS) > 200, (
        f"Expected 300+ SR groups, got {len(SR_SUPPORT_GROUPS)}"
    )


def test_load_support_groups_from_valid_json():
    """load_support_groups should parse a valid JSON file correctly."""
    data = {
        "ir_assignable": [
            {"fullname": "Group A", "guid": "guid-a", "depth": 0, "has_children": False},
            {"fullname": "Group B", "guid": "guid-b", "depth": 1, "has_children": False},
        ],
        "sr_assignable": [
            {"fullname": "Group A", "guid": "sr-guid-a", "depth": 0, "has_children": False},
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        f.flush()
        ir, sr = load_support_groups(Path(f.name))

    assert ir == {"Group A": "guid-a", "Group B": "guid-b"}
    assert sr == {"Group A": "sr-guid-a"}


def test_load_support_groups_missing_file():
    """load_support_groups should return fallback when file doesn't exist."""
    ir, sr = load_support_groups(Path("/nonexistent/path/groups.json"))
    assert ir == _FALLBACK_IR_SUPPORT_GROUPS
    assert sr == _FALLBACK_SR_SUPPORT_GROUPS


def test_load_support_groups_empty_lists():
    """load_support_groups should return fallback when JSON has empty lists."""
    data = {"ir_assignable": [], "sr_assignable": []}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        f.flush()
        ir, sr = load_support_groups(Path(f.name))

    assert ir == _FALLBACK_IR_SUPPORT_GROUPS
    assert sr == _FALLBACK_SR_SUPPORT_GROUPS


def test_load_support_groups_malformed_json():
    """load_support_groups should return fallback for malformed JSON."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("not valid json {{{")
        f.flush()
        ir, sr = load_support_groups(Path(f.name))

    assert ir == _FALLBACK_IR_SUPPORT_GROUPS
    assert sr == _FALLBACK_SR_SUPPORT_GROUPS


def test_load_support_groups_validation_excluded():
    """Service Desk\\Validation should NOT be in loaded groups (it's disabled)."""
    assert "Service Desk\\Validation" not in IR_SUPPORT_GROUPS
    assert "Service Desk\\Validation" not in SR_SUPPORT_GROUPS


# ── Ticket Fetch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetches_incident_ticket(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """Should call get_ticket with the correct ticket ID for incidents."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    await assignment_service.recommend_assignment("IR1959493")

    mock_athena_client.get_ticket.assert_called_once_with("IR1959493")


@pytest.mark.asyncio
async def test_fetches_service_request_ticket(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
):
    """Should call get_ticket for SR tickets."""
    sr_ticket = {
        "id": "SR1959584",
        "title": "Request PennChart access",
        "description": "New hire needs PennChart access for ambulatory clinic.",
        "status": {"name": "New"},
        "priority": "Medium",
        "supportGroup": {"name": "Service Desk"},
        "affectedUser": {"displayName": "New User"},
        "location": {"name": "HUP"},
        "createdDate": "2024-01-20T08:00:00Z",
    }
    mock_athena_client.get_ticket.return_value = sr_ticket
    mock_databricks_client.call_llm.return_value = VALID_SR_LLM_JSON

    result = await assignment_service.recommend_assignment("SR1959584")

    mock_athena_client.get_ticket.assert_called_once_with("SR1959584")
    assert result.ticket.ticket_type == "servicerequest"


@pytest.mark.asyncio
async def test_invalid_ticket_prefix_raises_error(
    assignment_service: AssignmentService,
):
    """Should raise ValueError for unrecognized ticket prefixes."""
    with pytest.raises(ValueError, match="Unknown ticket type prefix"):
        await assignment_service.recommend_assignment("CR12345")


# ── Embedding Generation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generates_embedding_from_ticket_content(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """Should generate an embedding from the ticket's title + description."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    await assignment_service.recommend_assignment("IR1959493")

    mock_databricks_client.generate_embedding.assert_called_once()
    call_text = mock_databricks_client.generate_embedding.call_args[0][0]
    assert "Printer not working on 3rd floor" in call_text
    assert "HP LaserJet" in call_text


@pytest.mark.asyncio
async def test_generates_embedding_fallback_for_empty_ticket(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
):
    """Should use fallback text when ticket has no title or description."""
    mock_athena_client.get_ticket.return_value = {"id": "IR1959500"}
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    await assignment_service.recommend_assignment("IR1959500")

    call_text = mock_databricks_client.generate_embedding.call_args[0][0]
    assert call_text == "IT support ticket"


# ── Semantic Search ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_searches_documentation_and_tickets(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """Should search both documentation and ticket embeddings."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    await assignment_service.recommend_assignment(
        "IR1959493", top_k_docs=3, top_k_tickets=7
    )

    mock_databricks_client.find_similar_documentation.assert_called_once()
    doc_args = mock_databricks_client.find_similar_documentation.call_args[0]
    assert doc_args[1] == 3  # top_k_docs

    mock_databricks_client.find_similar_by_embedding.assert_called_once()
    ticket_args = mock_databricks_client.find_similar_by_embedding.call_args[0]
    assert ticket_args[4] == 7  # top_k_tickets


# ── LLM Call ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calls_llm_with_system_and_user_messages(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """Should call the LLM with a system prompt and user message containing ticket details."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    await assignment_service.recommend_assignment("IR1959493")

    mock_databricks_client.call_llm.assert_called_once()
    messages = mock_databricks_client.call_llm.call_args[0][0]

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


@pytest.mark.asyncio
async def test_llm_system_prompt_contains_support_groups(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """System prompt should contain assignable IR support group mappings for an IR ticket."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    await assignment_service.recommend_assignment("IR1959493")

    messages = mock_databricks_client.call_llm.call_args[0][0]
    system_content = messages[0]["content"]

    # Should contain assignable IR GUIDs (EUS\HUP, Service Desk)
    assert "d2ba6580-40d1-1574-7b41-89314f4b22c6" in system_content  # EUS\HUP IR GUID
    assert "ec749166-07c5-eba6-35ba-bd32fa8ed7d2" in system_content  # Service Desk IR GUID
    # Should NOT contain disabled parent GUIDs
    assert "ae9eb3ff-458a-206f-7815-129d50efa285" not in system_content  # EUS (disabled) IR GUID


@pytest.mark.asyncio
async def test_llm_system_prompt_uses_sr_groups_for_sr_ticket(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
):
    """System prompt should contain SR support group mappings for an SR ticket."""
    sr_ticket = {
        "id": "SR1959584",
        "title": "Access request",
        "description": "Need access to shared drive.",
        "status": {"name": "New"},
    }
    mock_athena_client.get_ticket.return_value = sr_ticket
    mock_databricks_client.call_llm.return_value = VALID_SR_LLM_JSON

    await assignment_service.recommend_assignment("SR1959584")

    messages = mock_databricks_client.call_llm.call_args[0][0]
    system_content = messages[0]["content"]

    # Should contain assignable SR GUIDs (EUS\HUP SR, Service Desk SR)
    assert "df0e37e7-2843-57ad-2e7b-5bc5d7b30634" in system_content  # EUS\HUP SR GUID
    assert "043871eb-f69c-2330-7cbb-155b04fe24ea" in system_content  # Service Desk SR GUID
    # Should NOT contain IR GUIDs
    assert "d2ba6580-40d1-1574-7b41-89314f4b22c6" not in system_content  # EUS\HUP IR GUID
    # Should NOT contain disabled parent GUIDs
    assert "bea228bb-7633-b24b-9295-099f61afc92c" not in system_content  # EUS (disabled) SR GUID


@pytest.mark.asyncio
async def test_llm_user_message_contains_ticket_details(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """User message should contain the ticket's details."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    await assignment_service.recommend_assignment("IR1959493")

    messages = mock_databricks_client.call_llm.call_args[0][0]
    user_content = messages[1]["content"]

    assert "IR1959493" in user_content
    assert "Printer not working on 3rd floor" in user_content
    assert "incident" in user_content


@pytest.mark.asyncio
async def test_respects_max_tokens(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """Should pass max_tokens to the LLM call."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    await assignment_service.recommend_assignment("IR1959493", max_tokens=512)

    call_kwargs = mock_databricks_client.call_llm.call_args[1]
    assert call_kwargs["max_tokens"] == 512


# ── Response Parsing ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parses_valid_json_response(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """Should correctly parse a valid JSON LLM response."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    result = await assignment_service.recommend_assignment("IR1959493")

    assert result.recommendation.support_group_name == "EUS\\HUP"
    assert result.recommendation.support_group_guid == "d2ba6580-40d1-1574-7b41-89314f4b22c6"
    assert result.recommendation.priority == 3
    assert "hardware issue" in result.recommendation.rationale


@pytest.mark.asyncio
async def test_parses_code_fenced_json_response(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """Should handle JSON wrapped in markdown code fences."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.call_llm.return_value = CODE_FENCED_LLM_JSON

    result = await assignment_service.recommend_assignment("IR1959493")

    assert result.recommendation.support_group_name == "EUS\\HUP"
    assert result.recommendation.support_group_guid == "d2ba6580-40d1-1574-7b41-89314f4b22c6"


@pytest.mark.asyncio
async def test_fallback_on_invalid_json_response(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """Should return a fallback recommendation when JSON parsing fails."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.call_llm.return_value = INVALID_LLM_RESPONSE

    result = await assignment_service.recommend_assignment("IR1959493")

    assert result.recommendation.support_group_name == "Service Desk"
    assert result.recommendation.support_group_guid == IR_SUPPORT_GROUPS["Service Desk"]
    assert "could not be parsed" in result.recommendation.rationale


# ── Ticket Info Extraction ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extracts_ticket_info_from_raw(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """Should correctly extract ticket info from the raw Athena response."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    result = await assignment_service.recommend_assignment("IR1959493")

    assert result.ticket.id == "IR1959493"
    assert result.ticket.ticket_type == "incident"
    assert result.ticket.title == "Printer not working on 3rd floor"
    assert result.ticket.status == "Active"
    assert result.ticket.priority == 3
    assert result.ticket.support_group == "EUS\\HUP"
    assert result.ticket.affected_user == "John Smith"
    assert result.ticket.location == "HUP"


@pytest.mark.asyncio
async def test_handles_missing_nested_fields(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
):
    """Should handle tickets with missing or non-dict nested fields."""
    minimal_ticket = {
        "id": "IR1959500",
        "title": "Test ticket",
        "status": "Active",  # string instead of dict
        "supportGroup": None,
        "affectedUser": None,
        "location": None,
    }
    mock_athena_client.get_ticket.return_value = minimal_ticket
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    result = await assignment_service.recommend_assignment("IR1959500")

    assert result.ticket.id == "IR1959500"
    assert result.ticket.status == "Active"
    assert result.ticket.support_group is None
    assert result.ticket.affected_user is None
    assert result.ticket.location is None


# ── Source Citations ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_documentation_sources(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
    sample_documentation_results,
):
    """Should return documentation source citations."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.find_similar_documentation.return_value = sample_documentation_results
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    result = await assignment_service.recommend_assignment("IR1959493")

    doc_sources = [s for s in result.sources if s.type == SourceType.documentation]
    assert len(doc_sources) == 2
    assert doc_sources[0].title == "HP LaserJet Troubleshooting"
    assert doc_sources[0].notebook == "uphs_notebook"


@pytest.mark.asyncio
async def test_returns_ticket_sources(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
    sample_similar_results,
):
    """Should return ticket source citations."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.find_similar_by_embedding.return_value = sample_similar_results
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    result = await assignment_service.recommend_assignment("IR1959493")

    ticket_sources = [s for s in result.sources if s.type == SourceType.ticket]
    assert len(ticket_sources) == 5
    assert ticket_sources[0].title == "IR1959100"
    assert ticket_sources[0].similarity == 0.95


@pytest.mark.asyncio
async def test_empty_retrieval_results(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
    sample_athena_ticket,
):
    """Should handle empty retrieval results gracefully."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket
    mock_databricks_client.find_similar_documentation.return_value = []
    mock_databricks_client.find_similar_by_embedding.return_value = []
    mock_databricks_client.call_llm.return_value = VALID_LLM_JSON

    result = await assignment_service.recommend_assignment("IR1959493")

    assert result.sources == []
    mock_databricks_client.call_llm.assert_called_once()


# ── Static Helper Methods ─────────────────────────────────────────────


def test_get_ticket_type_ir():
    """Should return 'incident' for IR prefix."""
    assert AssignmentService._get_ticket_type("IR1959493") == "incident"
    assert AssignmentService._get_ticket_type("ir1959493") == "incident"


def test_get_ticket_type_sr():
    """Should return 'servicerequest' for SR prefix."""
    assert AssignmentService._get_ticket_type("SR1959584") == "servicerequest"
    assert AssignmentService._get_ticket_type("sr1959584") == "servicerequest"


def test_get_ticket_type_invalid():
    """Should raise ValueError for unknown prefix."""
    with pytest.raises(ValueError, match="Unknown ticket type prefix"):
        AssignmentService._get_ticket_type("CR12345")


def test_build_search_text_with_title_and_description():
    """Should combine title and description."""
    from src.models.assignment import TicketInfo

    info = TicketInfo(
        id="IR1",
        ticket_type="incident",
        title="Printer broken",
        description="The printer on 3rd floor is not working.",
    )
    text = AssignmentService._build_search_text(info)
    assert "Printer broken" in text
    assert "printer on 3rd floor" in text


def test_build_search_text_fallback():
    """Should return fallback text when title and description are empty."""
    from src.models.assignment import TicketInfo

    info = TicketInfo(id="IR1", ticket_type="incident")
    text = AssignmentService._build_search_text(info)
    assert text == "IT support ticket"


def test_parse_recommendation_valid_json():
    """Should parse valid JSON into AssignmentRecommendation."""
    rec = AssignmentService._parse_recommendation(VALID_LLM_JSON, IR_SUPPORT_GROUPS)
    assert rec.support_group_name == "EUS\\HUP"
    assert rec.support_group_guid == "d2ba6580-40d1-1574-7b41-89314f4b22c6"
    assert rec.priority == 3


def test_parse_recommendation_code_fenced():
    """Should strip code fences and parse JSON."""
    rec = AssignmentService._parse_recommendation(CODE_FENCED_LLM_JSON, IR_SUPPORT_GROUPS)
    assert rec.support_group_name == "EUS\\HUP"


def test_parse_recommendation_invalid_json():
    """Should return fallback recommendation for invalid JSON."""
    rec = AssignmentService._parse_recommendation(INVALID_LLM_RESPONSE, IR_SUPPORT_GROUPS)
    assert rec.support_group_name == "Service Desk"
    assert rec.support_group_guid == IR_SUPPORT_GROUPS["Service Desk"]
    assert "could not be parsed" in rec.rationale


def test_parse_recommendation_sr_fallback():
    """Fallback should use SR Service Desk GUID when parsing fails for SR."""
    rec = AssignmentService._parse_recommendation(INVALID_LLM_RESPONSE, SR_SUPPORT_GROUPS)
    assert rec.support_group_guid == SR_SUPPORT_GROUPS["Service Desk"]


def test_build_context_with_docs_and_tickets(
    sample_documentation_results,
    sample_similar_results,
):
    """Should include both documentation and ticket sections."""
    context = AssignmentService._build_context(
        sample_documentation_results, sample_similar_results
    )
    assert "KNOWLEDGE BASE DOCUMENTATION" in context
    assert "SIMILAR HISTORICAL TICKETS" in context
    assert "IR1959100" in context


def test_build_context_empty():
    """Should return fallback message when no results."""
    context = AssignmentService._build_context([], [])
    assert "No relevant documentation" in context


def test_build_llm_messages_structure():
    """Should return system + user messages with correct content."""
    from src.models.assignment import TicketInfo

    info = TicketInfo(
        id="IR1959493",
        ticket_type="incident",
        title="Printer broken",
        description="Not printing",
        status="Active",
        priority=3,
    )
    messages = AssignmentService._build_llm_messages(
        ticket_info=info,
        support_groups=IR_SUPPORT_GROUPS,
        context="Some context here",
    )

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "IR1959493" in messages[1]["content"]
    assert "Printer broken" in messages[1]["content"]
    # Should contain an assignable group name (EUS\HUP, not disabled "EUS")
    assert "EUS\\HUP" in messages[0]["content"]
    assert "Some context here" in messages[0]["content"]


# ── Location-Aware Routing Tests ──────────────────────────────────────


def test_system_prompt_contains_location_routing_rules():
    """System prompt should contain the LOCATION-BASED ROUTING RULES section."""
    from src.services.assignment import ASSIGNMENT_SYSTEM_PROMPT

    assert "LOCATION-BASED ROUTING RULES" in ASSIGNMENT_SYSTEM_PROMPT


def test_system_prompt_contains_eus_location_mappings():
    """System prompt should contain EUS location-to-group mappings."""
    from src.services.assignment import ASSIGNMENT_SYSTEM_PROMPT

    # Check key location → EUS sub-group mappings are present
    assert "EUS\\Campus" in ASSIGNMENT_SYSTEM_PROMPT
    assert "EUS\\CCH" in ASSIGNMENT_SYSTEM_PROMPT
    assert "EUS\\HUP Cedar" in ASSIGNMENT_SYSTEM_PROMPT
    assert "EUS\\HUP" in ASSIGNMENT_SYSTEM_PROMPT
    assert "EUS\\MCP" in ASSIGNMENT_SYSTEM_PROMPT
    assert "EUS\\PaH" in ASSIGNMENT_SYSTEM_PROMPT
    assert "EUS\\PCAM" in ASSIGNMENT_SYSTEM_PROMPT
    assert "EUS\\PMUC" in ASSIGNMENT_SYSTEM_PROMPT
    assert "EUS\\PPMC" in ASSIGNMENT_SYSTEM_PROMPT
    assert "EUS\\RITT" in ASSIGNMENT_SYSTEM_PROMPT
    assert "EUS\\RSI" in ASSIGNMENT_SYSTEM_PROMPT


def test_system_prompt_contains_pmdh_routing():
    """System prompt should contain PMDH Dispatch routing for Doylestown."""
    from src.services.assignment import ASSIGNMENT_SYSTEM_PROMPT

    assert "PMDH Dispatch" in ASSIGNMENT_SYSTEM_PROMPT
    assert "Doylestown" in ASSIGNMENT_SYSTEM_PROMPT


def test_system_prompt_contains_lgh_routing():
    """System prompt should contain LGH routing for Lancaster locations."""
    from src.services.assignment import ASSIGNMENT_SYSTEM_PROMPT

    assert "LGH" in ASSIGNMENT_SYSTEM_PROMPT
    assert "LGHP" in ASSIGNMENT_SYSTEM_PROMPT


def test_system_prompt_warns_against_default_campus():
    """System prompt should explicitly warn against defaulting to EUS\\Campus."""
    from src.services.assignment import ASSIGNMENT_SYSTEM_PROMPT

    assert "Never default to EUS\\Campus" in ASSIGNMENT_SYSTEM_PROMPT


def test_llm_messages_include_location_routing_in_system_prompt():
    """The built LLM messages should include location routing rules in the system prompt."""
    from src.models.assignment import TicketInfo

    info = TicketInfo(
        id="IR2000001",
        ticket_type="incident",
        title="Printer not working",
        description="Printer at remote clinic is jammed.",
        location="Remote sites (RSI)",
    )
    messages = AssignmentService._build_llm_messages(
        ticket_info=info,
        support_groups=IR_SUPPORT_GROUPS,
        context="No context.",
    )

    system_content = messages[0]["content"]
    assert "LOCATION-BASED ROUTING RULES" in system_content
    assert "EUS\\RSI" in system_content


def test_llm_user_message_includes_rsi_location():
    """User message should include the RSI location for the LLM to use."""
    from src.models.assignment import TicketInfo

    info = TicketInfo(
        id="IR2000001",
        ticket_type="incident",
        title="Printer not working",
        description="Printer at remote clinic is jammed.",
        location="Remote sites (RSI)",
    )
    messages = AssignmentService._build_llm_messages(
        ticket_info=info,
        support_groups=IR_SUPPORT_GROUPS,
        context="No context.",
    )

    user_content = messages[1]["content"]
    assert "Remote sites (RSI)" in user_content


@pytest.mark.parametrize(
    "location,expected_in_user_msg",
    [
        ("HUP", "HUP"),
        ("CAMPUS", "CAMPUS"),
        ("CCH", "CCH"),
        ("Remote sites (RSI)", "Remote sites (RSI)"),
        ("PPMC", "PPMC"),
        ("PAH", "PAH"),
        ("PCAM", "PCAM"),
        ("LGH", "LGH"),
        ("Doylestown (PMDH)", "Doylestown (PMDH)"),
        ("HUP Cedar", "HUP Cedar"),
        ("RITT", "RITT"),
        ("Princeton (MCP)", "Princeton (MCP)"),
        (None, "N/A"),
    ],
)
def test_llm_user_message_includes_various_locations(location, expected_in_user_msg):
    """User message should include the ticket location for all Penn Medicine sites."""
    from src.models.assignment import TicketInfo

    info = TicketInfo(
        id="IR2000002",
        ticket_type="incident",
        title="Hardware issue",
        description="Something is broken.",
        location=location,
    )
    messages = AssignmentService._build_llm_messages(
        ticket_info=info,
        support_groups=IR_SUPPORT_GROUPS,
        context="No context.",
    )

    user_content = messages[1]["content"]
    assert expected_in_user_msg in user_content


@pytest.mark.asyncio
async def test_rsi_ticket_passes_location_to_llm(
    assignment_service: AssignmentService,
    mock_athena_client,
    mock_databricks_client,
):
    """An RSI-located ticket should pass 'Remote sites (RSI)' to the LLM prompt."""
    rsi_ticket = {
        "id": "IR2000010",
        "title": "Printer jam at remote clinic",
        "description": "Printer at RSI clinic is not working after paper jam.",
        "status": {"name": "Active"},
        "priority": 3,
        "supportGroup": {"name": "Service Desk"},
        "affectedUser": {"displayName": "Remote User"},
        "location": {"name": "Remote sites (RSI)"},
        "createdDate": "2026-04-20T09:00:00Z",
    }
    mock_athena_client.get_ticket.return_value = rsi_ticket
    mock_databricks_client.call_llm.return_value = json.dumps({
        "support_group_name": "EUS\\RSI",
        "support_group_guid": "81ee117d-9ac1-da98-8ee7-472a6bc12ed3",
        "priority": 3,
        "rationale": "Hardware issue at RSI location routed to EUS\\RSI.",
    })

    result = await assignment_service.recommend_assignment("IR2000010")

    # Verify location was extracted correctly
    assert result.ticket.location == "Remote sites (RSI)"

    # Verify the LLM received the location in the user message
    messages = mock_databricks_client.call_llm.call_args[0][0]
    user_content = messages[1]["content"]
    assert "Remote sites (RSI)" in user_content

    # Verify the system prompt has location routing rules
    system_content = messages[0]["content"]
    assert "LOCATION-BASED ROUTING RULES" in system_content
