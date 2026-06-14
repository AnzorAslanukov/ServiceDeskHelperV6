"""
Unit tests for ChatbotService — Feature #2: Q&A Chatbot.

Tests the RAG pipeline with mocked Databricks client:
- Chat flow (embedding → retrieval → LLM call → response)
- Session management (create, continue, reset, history)
- Context building and source citations
- Conversation history in LLM messages
- Ticket detection and fetching
"""

import pytest
from unittest.mock import AsyncMock

from src.models.chat import MessageRole, SourceType
from src.services.chatbot import ChatbotService, TICKET_ID_PATTERN


# ── Chat Flow ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_generates_embedding(
    chatbot_service: ChatbotService,
    mock_databricks_client,
):
    """Chat should generate an embedding for the user's message."""
    await chatbot_service.chat(message="How do I reset a password?")

    mock_databricks_client.generate_embedding.assert_called_once_with(
        "How do I reset a password?"
    )


@pytest.mark.asyncio
async def test_chat_retrieves_documentation_and_tickets(
    chatbot_service: ChatbotService,
    mock_databricks_client,
):
    """Chat should search both documentation and ticket embeddings."""
    await chatbot_service.chat(
        message="printer not working",
        top_k_docs=3,
        top_k_tickets=7,
    )

    mock_databricks_client.find_similar_documentation.assert_called_once()
    doc_call_args = mock_databricks_client.find_similar_documentation.call_args
    assert doc_call_args[0][1] == 3  # top_k_docs

    mock_databricks_client.find_similar_by_embedding.assert_called_once()
    ticket_call_args = mock_databricks_client.find_similar_by_embedding.call_args
    assert ticket_call_args[0][4] == 7  # top_k_tickets


@pytest.mark.asyncio
async def test_chat_calls_llm(
    chatbot_service: ChatbotService,
    mock_databricks_client,
):
    """Chat should call the LLM with messages including system prompt and user message."""
    await chatbot_service.chat(message="How do I fix a printer?")

    mock_databricks_client.call_llm.assert_called_once()
    call_args = mock_databricks_client.call_llm.call_args
    messages = call_args[0][0]

    # First message should be system prompt
    assert messages[0]["role"] == "system"
    assert "Penn Medicine" in messages[0]["content"]

    # Last message should be the user's message
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "How do I fix a printer?"


@pytest.mark.asyncio
async def test_chat_returns_llm_response(
    chatbot_service: ChatbotService,
    mock_databricks_client,
):
    """Chat should return the LLM's response text."""
    mock_databricks_client.call_llm.return_value = "Try restarting the printer."

    result = await chatbot_service.chat(message="Printer not working")

    assert result.message == "Try restarting the printer."


@pytest.mark.asyncio
async def test_chat_respects_max_tokens(
    chatbot_service: ChatbotService,
    mock_databricks_client,
):
    """Chat should pass max_tokens to the LLM call."""
    await chatbot_service.chat(message="test", max_tokens=512)

    call_kwargs = mock_databricks_client.call_llm.call_args[1]
    assert call_kwargs["max_tokens"] == 512


# ── Source Citations ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_returns_documentation_sources(
    chatbot_service: ChatbotService,
    mock_databricks_client,
    sample_documentation_results,
):
    """Chat should return documentation sources with correct fields."""
    mock_databricks_client.find_similar_documentation.return_value = (
        sample_documentation_results
    )

    result = await chatbot_service.chat(message="printer issue")

    doc_sources = [s for s in result.sources if s.type == SourceType.documentation]
    assert len(doc_sources) == 2
    assert doc_sources[0].title == "HP LaserJet Troubleshooting"
    assert doc_sources[0].notebook == "uphs_notebook"
    assert doc_sources[0].section == "Helpdesk Printer Issues"
    assert doc_sources[0].similarity == 0.92
    assert doc_sources[0].content_preview is not None


@pytest.mark.asyncio
async def test_chat_returns_ticket_sources(
    chatbot_service: ChatbotService,
    mock_databricks_client,
    sample_similar_results,
):
    """Chat should return ticket sources with IDs and similarity scores."""
    mock_databricks_client.find_similar_by_embedding.return_value = (
        sample_similar_results
    )

    result = await chatbot_service.chat(message="printer issue")

    ticket_sources = [s for s in result.sources if s.type == SourceType.ticket]
    assert len(ticket_sources) == 5
    assert ticket_sources[0].title == "IR1959100"
    assert ticket_sources[0].similarity == 0.95


@pytest.mark.asyncio
async def test_chat_empty_retrieval_results(
    chatbot_service: ChatbotService,
    mock_databricks_client,
):
    """Chat should handle empty retrieval results gracefully."""
    mock_databricks_client.find_similar_documentation.return_value = []
    mock_databricks_client.find_similar_by_embedding.return_value = []

    result = await chatbot_service.chat(message="something unusual")

    assert result.sources == []
    # LLM should still be called
    mock_databricks_client.call_llm.assert_called_once()


# ── Session Management ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_creates_new_session(
    chatbot_service: ChatbotService,
):
    """Chat without session_id should create a new session."""
    result = await chatbot_service.chat(message="Hello")

    assert result.session_id is not None
    assert len(result.session_id) > 0


@pytest.mark.asyncio
async def test_chat_uses_provided_session_id(
    chatbot_service: ChatbotService,
):
    """Chat with a session_id should use that session."""
    result = await chatbot_service.chat(
        message="Hello",
        session_id="my-session-123",
    )

    assert result.session_id == "my-session-123"


@pytest.mark.asyncio
async def test_chat_continues_existing_session(
    chatbot_service: ChatbotService,
    mock_databricks_client,
):
    """Multiple messages with the same session_id should share history."""
    # First message
    result1 = await chatbot_service.chat(
        message="What is PennChart?",
        session_id="session-abc",
    )

    # Second message in same session
    mock_databricks_client.call_llm.return_value = "Follow-up response."
    result2 = await chatbot_service.chat(
        message="How do I reset a password in it?",
        session_id="session-abc",
    )

    assert result2.session_id == "session-abc"

    # The LLM call for the second message should include history
    second_call_messages = mock_databricks_client.call_llm.call_args[0][0]
    # Should have: system + user1 + assistant1 + user2
    user_messages = [m for m in second_call_messages if m["role"] == "user"]
    assert len(user_messages) == 2
    assert user_messages[0]["content"] == "What is PennChart?"
    assert user_messages[1]["content"] == "How do I reset a password in it?"


@pytest.mark.asyncio
async def test_get_history_returns_messages(
    chatbot_service: ChatbotService,
):
    """get_history should return all messages in a session."""
    await chatbot_service.chat(message="Hello", session_id="hist-session")

    history = chatbot_service.get_history("hist-session")

    assert history.session_id == "hist-session"
    assert len(history.messages) == 2  # user + assistant
    assert history.messages[0].role == MessageRole.user
    assert history.messages[0].content == "Hello"
    assert history.messages[1].role == MessageRole.assistant


@pytest.mark.asyncio
async def test_get_history_empty_session(
    chatbot_service: ChatbotService,
):
    """get_history for a non-existent session should return empty messages."""
    history = chatbot_service.get_history("nonexistent")

    assert history.session_id == "nonexistent"
    assert history.messages == []


@pytest.mark.asyncio
async def test_reset_session_clears_history(
    chatbot_service: ChatbotService,
):
    """reset_session should clear the conversation history."""
    await chatbot_service.chat(message="Hello", session_id="reset-me")

    # Verify history exists
    history = chatbot_service.get_history("reset-me")
    assert len(history.messages) == 2

    # Reset
    found = chatbot_service.reset_session("reset-me")
    assert found is True

    # Verify history is cleared
    history = chatbot_service.get_history("reset-me")
    assert history.messages == []


def test_reset_session_nonexistent(
    chatbot_service: ChatbotService,
):
    """reset_session for a non-existent session should return False."""
    found = chatbot_service.reset_session("does-not-exist")
    assert found is False


# ── Context Building ──────────────────────────────────────────────────


def test_build_context_with_docs_and_tickets(
    sample_documentation_results,
    sample_similar_results,
):
    """_build_context should include both documentation and ticket sections."""
    context = ChatbotService._build_context(
        "",  # no graph context
        sample_documentation_results,
        sample_similar_results,
    )

    assert "KNOWLEDGE BASE DOCUMENTATION" in context
    assert "HP LaserJet Troubleshooting" in context
    assert "Printer Escalation Guide" in context
    assert "SIMILAR HISTORICAL TICKETS" in context
    assert "IR1959100" in context
    assert "0.950" in context  # similarity formatted to 3 decimals


def test_build_context_docs_only(sample_documentation_results):
    """_build_context with only docs should not include ticket section."""
    context = ChatbotService._build_context("", sample_documentation_results, [])

    assert "KNOWLEDGE BASE DOCUMENTATION" in context
    assert "SIMILAR HISTORICAL TICKETS" not in context


def test_build_context_tickets_only(sample_similar_results):
    """_build_context with only tickets should not include docs section."""
    context = ChatbotService._build_context("", [], sample_similar_results)

    assert "KNOWLEDGE BASE DOCUMENTATION" not in context
    assert "SIMILAR HISTORICAL TICKETS" in context


def test_build_context_empty():
    """_build_context with no results should return a fallback message."""
    context = ChatbotService._build_context("", [], [])

    assert "No relevant documentation" in context


def test_build_context_with_graph_context(sample_similar_results):
    """_build_context should include graph context when provided."""
    graph_ctx = "=== STRUCTURED KNOWLEDGE ===\n• Escalate to: EUS\\HUP"
    context = ChatbotService._build_context(graph_ctx, [], sample_similar_results)

    assert "STRUCTURED KNOWLEDGE" in context
    assert "Escalate to: EUS\\HUP" in context
    assert "SIMILAR HISTORICAL TICKETS" in context
    assert "KNOWLEDGE BASE DOCUMENTATION" not in context


# ── Source Building ───────────────────────────────────────────────────


def test_build_sources_documentation(sample_documentation_results):
    """_build_sources should create documentation citations with previews."""
    sources = ChatbotService._build_sources(None, sample_documentation_results, [])

    assert len(sources) == 2
    assert sources[0].type == SourceType.documentation
    assert sources[0].title == "HP LaserJet Troubleshooting"
    assert sources[0].notebook == "uphs_notebook"
    assert sources[0].section == "Helpdesk Printer Issues"
    assert sources[0].content_preview is not None


def test_build_sources_tickets(sample_similar_results):
    """_build_sources should create ticket citations."""
    sources = ChatbotService._build_sources(None, [], sample_similar_results)

    assert len(sources) == 5
    assert sources[0].type == SourceType.ticket
    assert sources[0].title == "IR1959100"
    assert sources[0].similarity == 0.95


def test_build_sources_truncates_long_content():
    """_build_sources should truncate long documentation content previews."""
    long_content = "A" * 300
    docs = [
        {
            "content": long_content,
            "title": "Long Doc",
            "notebook": "test",
            "section": "test",
            "similarity": 0.9,
        }
    ]

    sources = ChatbotService._build_sources(None, docs, [])

    assert len(sources[0].content_preview) == 203  # 200 + "..."
    assert sources[0].content_preview.endswith("...")


def test_build_sources_empty():
    """_build_sources with no results should return empty list."""
    sources = ChatbotService._build_sources(None, [], [])
    assert sources == []


def test_build_sources_with_graph_result():
    """_build_sources should include a knowledge graph citation when graph results exist."""
    graph_result = {
        "facts": [
            {"type": "Escalation", "condition": "System down", "target_team": "EUS"},
            {"type": "PriorityRule", "condition": "Outage", "priority": "1"},
        ],
        "systems_matched": ["PennChart"],
        "procedures_matched": [],
        "has_sufficient_context": True,
    }
    sources = ChatbotService._build_sources(graph_result, [], [])

    assert len(sources) == 1
    assert sources[0].type == SourceType.documentation
    assert "Knowledge Graph" in sources[0].title
    assert "2 facts" in sources[0].title
    assert sources[0].similarity == 1.0
    assert "PennChart" in sources[0].content_preview


# ── LLM Message Building ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_messages_include_system_prompt(
    chatbot_service: ChatbotService,
    mock_databricks_client,
):
    """LLM messages should start with a system prompt containing context."""
    await chatbot_service.chat(message="test", session_id="msg-test")

    messages = mock_databricks_client.call_llm.call_args[0][0]
    assert messages[0]["role"] == "system"
    assert "Penn Medicine" in messages[0]["content"]


@pytest.mark.asyncio
async def test_llm_messages_include_context(
    chatbot_service: ChatbotService,
    mock_databricks_client,
    sample_documentation_results,
):
    """LLM system prompt should include retrieved documentation context."""
    mock_databricks_client.find_similar_documentation.return_value = (
        sample_documentation_results
    )

    await chatbot_service.chat(message="printer help", session_id="ctx-test")

    messages = mock_databricks_client.call_llm.call_args[0][0]
    system_content = messages[0]["content"]
    assert "HP LaserJet Troubleshooting" in system_content
    assert "KNOWLEDGE BASE DOCUMENTATION" in system_content


# ── Ticket Detection & Fetching ───────────────────────────────────────


def test_ticket_id_regex_matches_ir():
    """Regex should match IR ticket IDs."""
    matches = TICKET_ID_PATTERN.findall("What about IR10419292?")
    assert len(matches) == 1


def test_ticket_id_regex_matches_sr():
    """Regex should match SR ticket IDs."""
    matches = TICKET_ID_PATTERN.findall("Check SR1234567 please")
    assert len(matches) == 1


def test_ticket_id_regex_matches_multiple():
    """Regex should match multiple ticket IDs in one message."""
    import re
    matches = list(re.finditer(TICKET_ID_PATTERN, "Compare IR10419292 and SR9876543"))
    assert len(matches) == 2
    assert matches[0].group(0).upper() == "IR10419292"
    assert matches[1].group(0).upper() == "SR9876543"


def test_ticket_id_regex_case_insensitive():
    """Regex should match regardless of case."""
    matches = TICKET_ID_PATTERN.findall("look at ir10419292")
    assert len(matches) == 1


def test_ticket_id_regex_no_match_short():
    """Regex should NOT match IDs with fewer than 5 digits."""
    matches = TICKET_ID_PATTERN.findall("IR1234 is too short")
    assert len(matches) == 0


def test_ticket_id_regex_no_match_no_prefix():
    """Regex should NOT match bare numbers without IR/SR prefix."""
    matches = TICKET_ID_PATTERN.findall("ticket 10419292")
    assert len(matches) == 0


@pytest.mark.asyncio
async def test_chat_fetches_referenced_ticket(
    mock_databricks_client,
    mock_athena_client,
    sample_athena_ticket,
):
    """Chat should fetch ticket data when a ticket ID is mentioned."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket

    service = ChatbotService(
        databricks_client=mock_databricks_client,
        athena_client=mock_athena_client,
    )

    result = await service.chat(message="What should I do with IR1959493?")

    mock_athena_client.get_ticket.assert_called_once_with("IR1959493")

    # LLM context should include the ticket data
    llm_messages = mock_databricks_client.call_llm.call_args[0][0]
    system_content = llm_messages[0]["content"]
    assert "REFERENCED TICKET DATA" in system_content
    assert "IR1959493" in system_content
    assert "Printer not working on 3rd floor" in system_content


@pytest.mark.asyncio
async def test_chat_referenced_ticket_in_sources(
    mock_databricks_client,
    mock_athena_client,
    sample_athena_ticket,
):
    """Referenced tickets should appear in source citations."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket

    service = ChatbotService(
        databricks_client=mock_databricks_client,
        athena_client=mock_athena_client,
    )

    result = await service.chat(message="Help with IR1959493")

    ref_sources = [s for s in result.sources if "(referenced)" in s.title]
    assert len(ref_sources) == 1
    assert "IR1959493" in ref_sources[0].title
    assert ref_sources[0].similarity == 1.0


@pytest.mark.asyncio
async def test_chat_ticket_not_found_handled_gracefully(
    mock_databricks_client,
    mock_athena_client,
):
    """Chat should handle ticket not found gracefully."""
    mock_athena_client.get_ticket.return_value = None

    service = ChatbotService(
        databricks_client=mock_databricks_client,
        athena_client=mock_athena_client,
    )

    result = await service.chat(message="What about IR9999999?")

    # Should still get a response (LLM still called)
    mock_databricks_client.call_llm.assert_called_once()

    # Context should mention not found
    llm_messages = mock_databricks_client.call_llm.call_args[0][0]
    system_content = llm_messages[0]["content"]
    assert "NOT FOUND" in system_content


@pytest.mark.asyncio
async def test_chat_no_athena_client_skips_fetch(
    mock_databricks_client,
):
    """Without an Athena client, ticket detection is skipped."""
    service = ChatbotService(
        databricks_client=mock_databricks_client,
        athena_client=None,
    )

    result = await service.chat(message="What about IR1959493?")

    # Should still work, just no ticket data in context
    mock_databricks_client.call_llm.assert_called_once()
    llm_messages = mock_databricks_client.call_llm.call_args[0][0]
    system_content = llm_messages[0]["content"]
    assert "REFERENCED TICKET DATA" not in system_content


@pytest.mark.asyncio
async def test_chat_no_ticket_in_message_skips_fetch(
    mock_databricks_client,
    mock_athena_client,
):
    """Messages without ticket IDs should not trigger Athena fetch."""
    service = ChatbotService(
        databricks_client=mock_databricks_client,
        athena_client=mock_athena_client,
    )

    await service.chat(message="How do I reset a password?")

    mock_athena_client.get_ticket.assert_not_called()


def test_build_context_with_referenced_ticket(sample_athena_ticket):
    """_build_context should include referenced ticket data at the top."""
    context = ChatbotService._build_context("", [], [], [sample_athena_ticket])

    assert "REFERENCED TICKET DATA" in context
    assert "IR1959493" in context
    assert "Printer not working on 3rd floor" in context
    assert "Active" in context


def test_build_context_referenced_ticket_not_found():
    """_build_context should handle not-found tickets."""
    not_found = {"id": "IR9999999", "_not_found": True}
    context = ChatbotService._build_context("", [], [], [not_found])

    assert "NOT FOUND" in context
    assert "IR9999999" in context


def test_format_ticket_truncates_long_description():
    """Long descriptions should be truncated to 500 chars."""
    ticket = {
        "id": "IR1234567",
        "title": "Test",
        "description": "A" * 600,
    }
    formatted = ChatbotService._format_ticket_for_context(ticket)

    assert "..." in formatted
    # Description line should be truncated
    desc_line = [l for l in formatted.split("\n") if l.startswith("Description:")][0]
    assert len(desc_line) < 520  # "Description: " + 500 + "..."


# ── Smart Skip Logic ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_skips_sql_when_graph_has_context(
    mock_databricks_client,
):
    """When KG has sufficient context, SQL similarity searches should be skipped entirely."""
    from unittest.mock import MagicMock, patch
    from src.services.knowledge_graph import KnowledgeGraphService

    mock_kg = MagicMock(spec=KnowledgeGraphService)
    mock_kg.is_available = True
    mock_kg.query_for_chat.return_value = {
        "facts": [
            {"type": "Escalation", "condition": "test", "target_team": "EUS"},
            {"type": "PriorityRule", "condition": "test", "priority": "2"},
            {"type": "CallCapture", "scenario": "test", "required_fields": "[]"},
        ],
        "systems_matched": ["PennChart"],
        "procedures_matched": [],
        "has_sufficient_context": True,
    }
    mock_kg.format_facts_for_llm.return_value = "=== STRUCTURED KNOWLEDGE ===\nTest facts"

    service = ChatbotService(
        databricks_client=mock_databricks_client,
        knowledge_graph_service=mock_kg,
    )

    await service.chat(message="How do I handle PennChart issues?")

    # Embedding should NOT be generated (skipped entirely)
    mock_databricks_client.generate_embedding.assert_not_called()
    # SQL searches should NOT be called
    mock_databricks_client.find_similar_documentation.assert_not_called()
    mock_databricks_client.find_similar_by_embedding.assert_not_called()
    # LLM should still be called
    mock_databricks_client.call_llm.assert_called_once()


@pytest.mark.asyncio
async def test_chat_skips_sql_when_referenced_ticket_found(
    mock_databricks_client,
    mock_athena_client,
    sample_athena_ticket,
):
    """When a referenced ticket is fetched, SQL ticket search should be skipped."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket

    service = ChatbotService(
        databricks_client=mock_databricks_client,
        athena_client=mock_athena_client,
    )

    await service.chat(message="What should I do with IR1959493?")

    # Ticket similarity search should be skipped (we have the referenced ticket)
    mock_databricks_client.find_similar_by_embedding.assert_not_called()
    # But doc search still runs (no KG context)
    mock_databricks_client.find_similar_documentation.assert_called_once()


@pytest.mark.asyncio
async def test_chat_runs_full_search_when_no_context(
    mock_databricks_client,
):
    """Without KG context or referenced tickets, full SQL search should run."""
    service = ChatbotService(
        databricks_client=mock_databricks_client,
        athena_client=None,
    )

    await service.chat(message="random question with no context")

    # Both searches should run
    mock_databricks_client.generate_embedding.assert_called_once()
    mock_databricks_client.find_similar_documentation.assert_called_once()
    mock_databricks_client.find_similar_by_embedding.assert_called_once()


# ── Streaming ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_stream_yields_events(
    mock_databricks_client,
):
    """chat_stream should yield sources, tokens, and done events."""

    # Mock the streaming LLM to yield chunks
    async def mock_stream(*args, **kwargs):
        for chunk in ["Hello", " world", "!"]:
            yield chunk

    mock_databricks_client.call_llm_stream = mock_stream

    service = ChatbotService(
        databricks_client=mock_databricks_client,
        athena_client=None,
    )

    events = []
    async for event in service.chat_stream(message="test streaming"):
        events.append(event)

    # Separate progress events from non-progress events
    progress_events = [e for e in events if e["event"] == "progress"]
    non_progress_events = [e for e in events if e["event"] != "progress"]

    # Should have progress events (at least step 1 running/done + steps 2-5)
    assert len(progress_events) >= 5  # At minimum: step1 running, step1 done, step2-4 skipped/done, step5 running

    # Non-progress events: sources + 3 tokens + done = 5 events
    assert len(non_progress_events) == 5

    # First non-progress event is sources
    assert non_progress_events[0]["event"] == "sources"
    assert "session_id" in non_progress_events[0]

    # Middle events are tokens
    assert non_progress_events[1]["event"] == "token"
    assert non_progress_events[1]["data"] == "Hello"
    assert non_progress_events[2]["event"] == "token"
    assert non_progress_events[2]["data"] == " world"
    assert non_progress_events[3]["event"] == "token"
    assert non_progress_events[3]["data"] == "!"

    # Last event is done
    assert non_progress_events[4]["event"] == "done"
    assert non_progress_events[4]["data"]["full_text"] == "Hello world!"
    assert "session_id" in non_progress_events[4]["data"]

    # Verify progress events have correct structure
    for pe in progress_events:
        assert "step" in pe["data"]
        assert "total" in pe["data"]
        assert "label" in pe["data"]
        assert "status" in pe["data"]
        assert pe["data"]["status"] in ("running", "done", "skipped")


@pytest.mark.asyncio
async def test_chat_stream_records_session_history(
    mock_databricks_client,
):
    """chat_stream should record messages in session history."""

    async def mock_stream(*args, **kwargs):
        yield "Response text"

    mock_databricks_client.call_llm_stream = mock_stream

    service = ChatbotService(
        databricks_client=mock_databricks_client,
    )

    session_id = "stream-session"
    async for _ in service.chat_stream(message="Hello", session_id=session_id):
        pass

    history = service.get_history(session_id)
    assert len(history.messages) == 2
    assert history.messages[0].role.value == "user"
    assert history.messages[0].content == "Hello"
    assert history.messages[1].role.value == "assistant"
    assert history.messages[1].content == "Response text"


# ── Classifier Integration Tests ──────────────────────────────────────


class TestClassifierIntegration:
    """Tests for the TF-IDF classifier integration in the chatbot."""

    def _make_mock_classifier(self):
        """Create a mock TicketClassifier."""
        from unittest.mock import MagicMock
        classifier = MagicMock()
        classifier.predict.return_value = [
            {"support_group": "EUS\\HUP", "confidence": 0.75},
            {"support_group": "EUS\\Campus", "confidence": 0.12},
            {"support_group": "Service Desk", "confidence": 0.05},
        ]
        return classifier

    @pytest.mark.asyncio
    async def test_classifier_runs_when_ticket_referenced(
        self, mock_databricks_client, mock_athena_client
    ):
        """Classifier should run when a ticket ID is detected and fetched."""
        mock_athena_client.get_ticket.return_value = {
            "id": "IR1959493",
            "title": "Printer not working",
            "description": "HP LaserJet on 3rd floor not printing",
            "location": {"name": "HUP"},
            "classificationPath": "Hardware",
            "source": "Phone",
        }

        classifier = self._make_mock_classifier()
        service = ChatbotService(
            databricks_client=mock_databricks_client,
            athena_client=mock_athena_client,
            ticket_classifier=classifier,
        )

        await service.chat(message="What group should IR1959493 be assigned to?")

        classifier.predict.assert_called_once()
        call_kwargs = classifier.predict.call_args[1]
        assert call_kwargs["title"] == "Printer not working"
        assert call_kwargs["ticket_type"] == "Incident"

    @pytest.mark.asyncio
    async def test_classifier_not_called_without_ticket(
        self, mock_databricks_client
    ):
        """Classifier should NOT run when no ticket IDs are in the message."""
        classifier = self._make_mock_classifier()
        service = ChatbotService(
            databricks_client=mock_databricks_client,
            ticket_classifier=classifier,
        )

        await service.chat(message="How do I reset a password?")

        classifier.predict.assert_not_called()

    @pytest.mark.asyncio
    async def test_classifier_results_in_context(
        self, mock_databricks_client, mock_athena_client
    ):
        """Classifier predictions should appear in the LLM context."""
        mock_athena_client.get_ticket.return_value = {
            "id": "IR1959493",
            "title": "Printer not working",
            "description": "HP LaserJet on 3rd floor not printing",
            "location": {"name": "HUP"},
            "classificationPath": "Hardware",
            "source": "Phone",
        }

        classifier = self._make_mock_classifier()
        service = ChatbotService(
            databricks_client=mock_databricks_client,
            athena_client=mock_athena_client,
            ticket_classifier=classifier,
        )

        await service.chat(message="What group should IR1959493 be assigned to?")

        # Check that the LLM was called with context containing classifier predictions
        llm_call_args = mock_databricks_client.call_llm.call_args[0][0]
        system_msg = llm_call_args[0]["content"]
        assert "CLASSIFIER PREDICTIONS" in system_msg
        assert "EUS\\HUP" in system_msg
        assert "75.0%" in system_msg

    @pytest.mark.asyncio
    async def test_classifier_handles_sr_tickets(
        self, mock_databricks_client, mock_athena_client
    ):
        """Classifier should use SR support groups for SR tickets."""
        mock_athena_client.get_ticket.return_value = {
            "id": "SR2045678",
            "title": "New laptop request",
            "description": "User needs a new laptop for remote work",
            "location": {"name": "Campus"},
            "classificationPath": "Hardware Request",
            "source": "Web Portal",
        }

        classifier = self._make_mock_classifier()
        service = ChatbotService(
            databricks_client=mock_databricks_client,
            athena_client=mock_athena_client,
            ticket_classifier=classifier,
        )

        await service.chat(message="Where should SR2045678 go?")

        call_kwargs = classifier.predict.call_args[1]
        assert call_kwargs["ticket_type"] == "Service Request"

    @pytest.mark.asyncio
    async def test_classifier_skips_not_found_tickets(
        self, mock_databricks_client, mock_athena_client
    ):
        """Classifier should skip tickets that weren't found in Athena."""
        mock_athena_client.get_ticket.return_value = None

        classifier = self._make_mock_classifier()
        service = ChatbotService(
            databricks_client=mock_databricks_client,
            athena_client=mock_athena_client,
            ticket_classifier=classifier,
        )

        await service.chat(message="What about IR9999999?")

        classifier.predict.assert_not_called()

    @pytest.mark.asyncio
    async def test_classifier_triage_rule_takes_priority(
        self, mock_databricks_client, mock_athena_client
    ):
        """Triage rules should take priority over the classifier."""
        mock_athena_client.get_ticket.return_value = {
            "id": "IR1959493",
            "title": "Password reset needed",
            "description": "User account locked, needs password reset",
            "location": {"name": "HUP"},
            "classificationPath": "Access",
            "source": "Phone",
        }

        classifier = self._make_mock_classifier()
        service = ChatbotService(
            databricks_client=mock_databricks_client,
            athena_client=mock_athena_client,
            ticket_classifier=classifier,
        )

        await service.chat(message="Assign IR1959493")

        # Classifier predict should NOT be called because triage rule matches first
        classifier.predict.assert_not_called()

        # But the context should still have a prediction (from triage rule)
        llm_call_args = mock_databricks_client.call_llm.call_args[0][0]
        system_msg = llm_call_args[0]["content"]
        assert "CLASSIFIER PREDICTIONS" in system_msg
        assert "Service Desk" in system_msg
        assert "triage_rule" in system_msg

    @pytest.mark.asyncio
    async def test_classifier_no_classifier_instance(
        self, mock_databricks_client, mock_athena_client
    ):
        """Service should work fine without a classifier (backward compatible)."""
        mock_athena_client.get_ticket.return_value = {
            "id": "IR1959493",
            "title": "Printer not working",
            "description": "HP LaserJet on 3rd floor not printing",
            "location": {"name": "HUP"},
        }

        service = ChatbotService(
            databricks_client=mock_databricks_client,
            athena_client=mock_athena_client,
            ticket_classifier=None,  # No classifier
        )

        response = await service.chat(message="What about IR1959493?")

        # Should still work, just without classifier predictions
        assert response.message is not None
        llm_call_args = mock_databricks_client.call_llm.call_args[0][0]
        system_msg = llm_call_args[0]["content"]
        assert "CLASSIFIER PREDICTIONS" not in system_msg

    def test_format_classifier_results_empty(self):
        """Formatting empty results should return empty string."""
        result = ChatbotService._format_classifier_results_for_context([])
        assert result == ""

    def test_format_classifier_results_with_data(self):
        """Formatting should produce readable context block."""
        results = [
            {
                "ticket_id": "IR1959493",
                "method": "classifier",
                "support_group": "EUS\\HUP",
                "support_group_guid": "some-guid-123",
                "confidence": 0.85,
                "alternatives": [
                    {"support_group": "EUS\\Campus", "confidence": 0.08},
                ],
            }
        ]
        formatted = ChatbotService._format_classifier_results_for_context(results)

        assert "CLASSIFIER PREDICTIONS" in formatted
        assert "IR1959493" in formatted
        assert "EUS\\HUP" in formatted
        assert "85.0%" in formatted
        assert "some-guid-123" in formatted
        assert "EUS\\Campus" in formatted

    def test_format_classifier_results_triage_rule(self):
        """Triage rule results should show method correctly."""
        results = [
            {
                "ticket_id": "IR1959493",
                "method": "triage_rule",
                "support_group": "Service Desk",
                "support_group_guid": "sd-guid",
                "confidence": 1.0,
                "alternatives": [],
            }
        ]
        formatted = ChatbotService._format_classifier_results_for_context(results)

        assert "triage_rule" in formatted
        assert "100.0%" in formatted
        assert "Service Desk" in formatted

    @pytest.mark.asyncio
    async def test_classifier_multiple_tickets(
        self, mock_databricks_client, mock_athena_client
    ):
        """Classifier should run for each unique ticket ID in the message."""
        call_count = 0

        async def mock_get_ticket(ticket_id):
            return {
                "id": ticket_id,
                "title": f"Issue for {ticket_id}",
                "description": "Some issue",
                "location": {"name": "HUP"},
                "classificationPath": "Hardware",
                "source": "Phone",
            }

        mock_athena_client.get_ticket = mock_get_ticket

        classifier = self._make_mock_classifier()
        service = ChatbotService(
            databricks_client=mock_databricks_client,
            athena_client=mock_athena_client,
            ticket_classifier=classifier,
        )

        await service.chat(message="Compare IR1959493 and IR2045678")

        # Classifier should be called twice (once per ticket)
        assert classifier.predict.call_count == 2
