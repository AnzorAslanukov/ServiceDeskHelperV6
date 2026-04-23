"""
Chat API Router — REST endpoints for Feature #2: Q&A Chatbot.

Endpoints:
    POST /chat              — Send a message and get an AI response
    POST /chat/reset        — Clear a session's conversation history
    GET  /chat/history/{id} — Retrieve conversation history for a session
"""

from fastapi import APIRouter, Depends, HTTPException

from src.dependencies import get_chatbot_service
from src.models.chat import (
    ChatHistoryResponse,
    ChatRequest,
    ChatResponse,
    ResetSessionRequest,
)
from src.services.chatbot import ChatbotService

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    service: ChatbotService = Depends(get_chatbot_service),
) -> ChatResponse:
    """
    Send a message to the Q&A chatbot and receive an AI-generated response.

    The chatbot uses a RAG pipeline to:
    1. Search the knowledge base documentation for relevant articles
    2. Find historically similar tickets
    3. Generate a contextual response using Claude Sonnet 4.5

    Provide a `session_id` to continue an existing conversation,
    or omit it to start a new session.
    """
    return await service.chat(
        message=request.message,
        session_id=request.session_id,
        top_k_docs=request.top_k_docs,
        top_k_tickets=request.top_k_tickets,
        max_tokens=request.max_tokens,
    )


@router.post("/reset")
async def reset_session(
    request: ResetSessionRequest,
    service: ChatbotService = Depends(get_chatbot_service),
) -> dict:
    """
    Clear the conversation history for a chat session.

    Returns a confirmation message indicating whether the session was found and reset.
    """
    found = service.reset_session(request.session_id)
    if not found:
        raise HTTPException(status_code=404, detail=f"Session '{request.session_id}' not found.")
    return {"status": "ok", "message": f"Session '{request.session_id}' has been reset."}


@router.get("/history/{session_id}", response_model=ChatHistoryResponse)
async def get_history(
    session_id: str,
    service: ChatbotService = Depends(get_chatbot_service),
) -> ChatHistoryResponse:
    """
    Retrieve the conversation history for a chat session.

    Returns all messages exchanged in the session, including source citations
    on assistant messages.
    """
    return service.get_history(session_id)