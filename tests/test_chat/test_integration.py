"""
Integration tests for Feature #2: Q&A Chatbot.

These tests call REAL external APIs (Databricks).
They verify that:
- Embedding generation works for chat queries
- Documentation and ticket retrieval produce results
- LLM inference returns coherent responses
- End-to-end RAG pipeline produces valid chatbot responses
- Multi-turn conversation maintains context

Run with: pytest tests/test_chat/test_integration.py -v -m integration

These tests are marked with @pytest.mark.integration and are SKIPPED
by default. To run them, use: pytest -m integration
"""

import pytest

from src.config import get_settings
from src.clients.databricks_client import DatabricksClient
from src.services.chatbot import ChatbotService


# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def settings():
    """Load real settings from .env."""
    return get_settings()


@pytest.fixture
def databricks_client(settings) -> DatabricksClient:
    """Create a real DatabricksClient."""
    return DatabricksClient(settings)


@pytest.fixture
def chatbot_service(databricks_client) -> ChatbotService:
    """Create a real ChatbotService with real Databricks client."""
    return ChatbotService(databricks_client=databricks_client)


# ── LLM Inference ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_databricks_llm_call(databricks_client):
    """Should call Claude Sonnet 4.5 and get a text response."""
    messages = [
        {"role": "user", "content": "What is 2 + 2? Reply with just the number."}
    ]
    response = await databricks_client.call_llm(messages, max_tokens=50)

    assert isinstance(response, str)
    assert len(response) > 0
    assert "4" in response


@pytest.mark.asyncio
async def test_databricks_llm_with_system_prompt(databricks_client):
    """Should respect system prompt instructions."""
    messages = [
        {"role": "system", "content": "You are a helpful IT support assistant. Always respond in exactly one sentence."},
        {"role": "user", "content": "What is a VPN?"},
    ]
    response = await databricks_client.call_llm(messages, max_tokens=200)

    assert isinstance(response, str)
    assert len(response) > 0


# ── End-to-End Chatbot ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_chat_single_message(chatbot_service):
    """End-to-end: single chat message through the full RAG pipeline."""
    result = await chatbot_service.chat(
        message="How do I troubleshoot a printer that is not printing?",
        top_k_docs=3,
        top_k_tickets=3,
        max_tokens=500,
    )

    # Should get a response
    assert result.message is not None
    assert len(result.message) > 0

    # Should get a session ID
    assert result.session_id is not None

    # Should have sources (given 6K+ docs and 42K+ tickets)
    assert len(result.sources) > 0


@pytest.mark.asyncio
async def test_e2e_chat_returns_documentation_sources(chatbot_service):
    """End-to-end: chat should return documentation source citations."""
    result = await chatbot_service.chat(
        message="How do I reset a PennChart password?",
        top_k_docs=3,
        top_k_tickets=0,
    )

    doc_sources = [s for s in result.sources if s.type.value == "documentation"]
    # Should find relevant documentation
    assert len(doc_sources) > 0
    assert doc_sources[0].title is not None
    assert doc_sources[0].similarity > 0


@pytest.mark.asyncio
async def test_e2e_chat_returns_ticket_sources(chatbot_service):
    """End-to-end: chat should return ticket source citations."""
    result = await chatbot_service.chat(
        message="VPN not connecting for remote user",
        top_k_docs=0,
        top_k_tickets=3,
    )

    ticket_sources = [s for s in result.sources if s.type.value == "ticket"]
    assert len(ticket_sources) > 0
    assert ticket_sources[0].title.startswith("IR") or ticket_sources[0].title.isdigit()
    assert ticket_sources[0].similarity > 0


@pytest.mark.asyncio
async def test_e2e_multi_turn_conversation(chatbot_service):
    """End-to-end: multi-turn conversation should maintain context."""
    # First message
    result1 = await chatbot_service.chat(
        message="What is PennChart?",
        max_tokens=300,
    )
    session_id = result1.session_id
    assert len(result1.message) > 0

    # Follow-up message in same session
    result2 = await chatbot_service.chat(
        message="How do I reset a password in it?",
        session_id=session_id,
        max_tokens=300,
    )
    assert result2.session_id == session_id
    assert len(result2.message) > 0

    # Verify history has all messages
    history = chatbot_service.get_history(session_id)
    assert len(history.messages) == 4  # user1 + assistant1 + user2 + assistant2


@pytest.mark.asyncio
async def test_e2e_session_reset(chatbot_service):
    """End-to-end: session reset should clear conversation history."""
    # Create a session with a message
    result = await chatbot_service.chat(
        message="Hello",
        session_id="integration-reset-test",
        max_tokens=100,
    )
    assert result.session_id == "integration-reset-test"

    # Reset the session
    found = chatbot_service.reset_session("integration-reset-test")
    assert found is True

    # History should be empty
    history = chatbot_service.get_history("integration-reset-test")
    assert history.messages == []