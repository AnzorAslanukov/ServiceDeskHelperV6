"""
Unit tests for ChatbotService — Feature #2: Q&A Chatbot.

Tests the RAG pipeline with mocked Databricks client:
- Chat flow (embedding → retrieval → LLM call → response)
- Session management (create, continue, reset, history)
- Context building and source citations
- Conversation history in LLM messages
"""

import pytest

from src.models.chat import MessageRole, SourceType
from src.services.chatbot import ChatbotService


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
    context = ChatbotService._build_context(sample_documentation_results, [])

    assert "KNOWLEDGE BASE DOCUMENTATION" in context
    assert "SIMILAR HISTORICAL TICKETS" not in context


def test_build_context_tickets_only(sample_similar_results):
    """_build_context with only tickets should not include docs section."""
    context = ChatbotService._build_context([], sample_similar_results)

    assert "KNOWLEDGE BASE DOCUMENTATION" not in context
    assert "SIMILAR HISTORICAL TICKETS" in context


def test_build_context_empty():
    """_build_context with no results should return a fallback message."""
    context = ChatbotService._build_context([], [])

    assert "No relevant documentation" in context


# ── Source Building ───────────────────────────────────────────────────


def test_build_sources_documentation(sample_documentation_results):
    """_build_sources should create documentation citations with previews."""
    sources = ChatbotService._build_sources(sample_documentation_results, [])

    assert len(sources) == 2
    assert sources[0].type == SourceType.documentation
    assert sources[0].title == "HP LaserJet Troubleshooting"
    assert sources[0].notebook == "uphs_notebook"
    assert sources[0].section == "Helpdesk Printer Issues"
    assert sources[0].content_preview is not None


def test_build_sources_tickets(sample_similar_results):
    """_build_sources should create ticket citations."""
    sources = ChatbotService._build_sources([], sample_similar_results)

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

    sources = ChatbotService._build_sources(docs, [])

    assert len(sources[0].content_preview) == 203  # 200 + "..."
    assert sources[0].content_preview.endswith("...")


def test_build_sources_empty():
    """_build_sources with no results should return empty list."""
    sources = ChatbotService._build_sources([], [])
    assert sources == []


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