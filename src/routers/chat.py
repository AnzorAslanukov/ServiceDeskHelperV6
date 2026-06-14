"""
Chat API Router — REST endpoints for Feature #2: Q&A Chatbot.

Endpoints:
    POST /chat              — Send a message and get an AI response
    POST /chat/stream       — Send a message and stream the AI response (SSE)
    POST /chat/reset        — Clear a session's conversation history
    GET  /chat/history/{id} — Retrieve conversation history for a session
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from src.dependencies import get_chatbot_service
from src.models.chat import (
    ChatHistoryResponse,
    ChatRequest,
    ChatResponse,
    ResetSessionRequest,
)
from src.services.chatbot import ChatbotService

logger = logging.getLogger(__name__)

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
    try:
        return await service.chat(
            message=request.message,
            session_id=request.session_id,
            top_k_docs=request.top_k_docs,
            top_k_tickets=request.top_k_tickets,
            max_tokens=request.max_tokens,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Chat service error for message: %s", request.message[:100])
        raise HTTPException(
            status_code=502,
            detail=f"Chat service error: {e}",
        )


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    service: ChatbotService = Depends(get_chatbot_service),
) -> StreamingResponse:
    """
    Send a message and stream the AI response via Server-Sent Events (SSE).

    Events:
        - event: sources  — source citations (sent first, before tokens)
        - event: token    — each text chunk as it arrives from the LLM
        - event: error    — error message if something goes wrong
        - event: done     — signals completion with session_id and full text
    """

    async def event_generator():
        try:
            async for event in service.chat_stream(
                message=request.message,
                session_id=request.session_id,
                top_k_docs=request.top_k_docs,
                top_k_tickets=request.top_k_tickets,
                max_tokens=request.max_tokens,
            ):
                event_type = event["event"]
                data = event.get("data", "")

                if event_type == "progress":
                    # Progress events send step info for the loading bar
                    yield f"event: progress\ndata: {json.dumps(data)}\n\n"
                elif event_type == "token":
                    # Token events send just the text chunk
                    yield f"event: token\ndata: {json.dumps(data)}\n\n"
                elif event_type == "ticket_data":
                    # Ticket data event sends referenced ticket details for UI card
                    yield f"event: ticket_data\ndata: {json.dumps(data)}\n\n"
                elif event_type == "sources":
                    # Sources event sends the full sources array + session_id
                    payload = {
                        "sources": data,
                        "session_id": event.get("session_id", ""),
                    }
                    yield f"event: sources\ndata: {json.dumps(payload)}\n\n"
                elif event_type == "done":
                    yield f"event: done\ndata: {json.dumps(data)}\n\n"
        except Exception as e:
            logger.exception("Chat stream error for message: %s", request.message[:100])
            error_payload = {"detail": str(e)}
            yield f"event: error\ndata: {json.dumps(error_payload)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
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