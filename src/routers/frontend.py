"""
Frontend Router — Serves HTML pages and HTMX partial responses.

This router handles:
    - Full page renders (GET requests returning complete HTML pages)
    - HTMX partial renders (POST requests returning HTML fragments for search results)

The HTMX partials call the existing backend services directly,
avoiding an extra HTTP hop through the API endpoints.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.dependencies import get_assignment_service, get_athena_client, get_chatbot_service, get_search_service
from src.services.assignment import AssignmentService
from src.services.chatbot import ChatbotService
from src.services.ticket_search import TicketSearchService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["frontend"])

# Templates directory
TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Page Routes ────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Redirect root UI to the search page."""
    return RedirectResponse(url="/ui/search", status_code=302)


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    """Render the Enhanced Ticket Search page."""
    return templates.TemplateResponse(
        "search/index.html",
        {"request": request, "active_page": "search"},
    )


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """Render the Q&A Chatbot page."""
    return templates.TemplateResponse(
        "chat/index.html",
        {"request": request, "active_page": "chat"},
    )


@router.get("/assignment", response_class=HTMLResponse)
async def assignment_page(request: Request):
    """Render the Ticket Assignment Recommendation page."""
    return templates.TemplateResponse(
        "assignment/index.html",
        {"request": request, "active_page": "assignment"},
    )


# ── HTMX Partial Routes (Search Feature) ──────────────────────────────


@router.post("/search/field", response_class=HTMLResponse)
async def search_field_partial(
    request: Request,
    field: str = Form(...),
    value: str = Form(...),
    operator: str = Form("eq"),
    ticket_type: str = Form("incident"),
    page: int = Form(1),
    page_size: int = Form(50),
    service: TicketSearchService = Depends(get_search_service),
):
    """HTMX partial: Execute field search and return results HTML."""
    try:
        result = await service.search_by_field(
            field=field,
            value=value,
            ticket_type=ticket_type,
            operator=operator,
            page=page,
            page_size=page_size,
        )
        return templates.TemplateResponse(
            "search/partials/field_results.html",
            {
                "request": request,
                "tickets": [t.model_dump() for t in result.tickets],
                "total": result.total,
                "page": result.page,
                "page_size": result.page_size,
                "has_more": result.has_more,
                # Pass form values for pagination re-submission
                "form_field": field,
                "form_value": value,
                "form_operator": operator,
                "form_ticket_type": ticket_type,
            },
        )
    except Exception as e:
        logger.exception("Field search failed")
        return templates.TemplateResponse(
            "search/partials/field_results.html",
            {"request": request, "error": str(e), "tickets": [], "total": 0},
        )


@router.post("/search/description", response_class=HTMLResponse)
async def search_description_partial(
    request: Request,
    text: str = Form(...),
    ticket_type: str = Form("incident"),
    page: int = Form(1),
    page_size: int = Form(50),
    service: TicketSearchService = Depends(get_search_service),
):
    """HTMX partial: Execute description search and return results HTML."""
    try:
        result = await service.search_by_description(
            text=text,
            ticket_type=ticket_type,
            page=page,
            page_size=page_size,
        )
        return templates.TemplateResponse(
            "search/partials/description_results.html",
            {
                "request": request,
                "tickets": [t.model_dump() for t in result.tickets],
                "total": result.total,
                "page": result.page,
                "page_size": result.page_size,
                "has_more": result.has_more,
                # Pass form values for pagination re-submission
                "form_text": text,
                "form_ticket_type": ticket_type,
            },
        )
    except Exception as e:
        logger.exception("Description search failed")
        return templates.TemplateResponse(
            "search/partials/description_results.html",
            {"request": request, "error": str(e), "tickets": [], "total": 0},
        )


@router.post("/search/semantic", response_class=HTMLResponse)
async def search_semantic_partial(
    request: Request,
    query: str = Form(...),
    top_k: int = Form(10),
    service: TicketSearchService = Depends(get_search_service),
):
    """HTMX partial: Execute semantic search and return results HTML."""
    try:
        result = await service.semantic_search(
            query=query,
            top_k=top_k,
        )
        return templates.TemplateResponse(
            "search/partials/semantic_results.html",
            {
                "request": request,
                "similar_tickets": [t.model_dump() for t in result.similar_tickets],
                "documentation": [d.model_dump() for d in result.documentation],
            },
        )
    except Exception as e:
        logger.exception("Semantic search failed")
        return templates.TemplateResponse(
            "search/partials/semantic_results.html",
            {
                "request": request,
                "error": str(e),
                "similar_tickets": [],
                "documentation": [],
            },
        )


@router.get("/ticket/{ticket_id}/details", response_class=HTMLResponse)
async def ticket_detail_partial(
    request: Request,
    ticket_id: str,
    athena=Depends(get_athena_client),
):
    """HTMX partial: Fetch a single ticket from Athena and return detail HTML."""
    try:
        raw = await athena.get_ticket(ticket_id)
        ticket = TicketSearchService._map_ticket(raw)
        return templates.TemplateResponse(
            "search/partials/ticket_detail.html",
            {"request": request, "ticket": ticket.model_dump()},
        )
    except Exception as e:
        logger.exception("Ticket detail fetch failed for %s", ticket_id)
        return HTMLResponse(
            f'<div class="alert alert-error"><span>⚠️</span><span>Could not load details for {ticket_id}: {e}</span></div>'
        )


# ── HTMX Partial Routes (Assignment Feature) ──────────────────────────


@router.post("/assignment/recommend", response_class=HTMLResponse)
async def assignment_recommend_partial(
    request: Request,
    ticket_id: str = Form(...),
    top_k_docs: int = Form(5),
    top_k_tickets: int = Form(5),
    max_tokens: int = Form(2048),
    service: AssignmentService = Depends(get_assignment_service),
):
    """HTMX partial: Get assignment recommendation and return results HTML."""
    try:
        result = await service.recommend_assignment(
            ticket_id=ticket_id.strip().upper(),
            top_k_docs=top_k_docs,
            top_k_tickets=top_k_tickets,
            max_tokens=max_tokens,
        )
        return templates.TemplateResponse(
            "assignment/partials/recommendation.html",
            {
                "request": request,
                "ticket": result.ticket.model_dump(),
                "recommendation": result.recommendation.model_dump(),
                "sources": [s.model_dump() for s in result.sources],
            },
        )
    except ValueError as e:
        return templates.TemplateResponse(
            "assignment/partials/recommendation.html",
            {"request": request, "error": str(e)},
        )
    except Exception as e:
        logger.exception("Assignment recommendation failed for %s", ticket_id)
        error_msg = str(e)
        if "404" in error_msg or "not found" in error_msg.lower():
            error_msg = f"Ticket '{ticket_id.strip().upper()}' not found in Athena."
        else:
            error_msg = f"Failed to generate recommendation: {error_msg}"
        return templates.TemplateResponse(
            "assignment/partials/recommendation.html",
            {"request": request, "error": error_msg},
        )


# ── HTMX Partial Routes (Search Feature — continued) ──────────────────


@router.post("/search/similar", response_class=HTMLResponse)
async def search_similar_partial(
    request: Request,
    ticket_id: str = Form(...),
    top_k: int = Form(10),
    service: TicketSearchService = Depends(get_search_service),
):
    """HTMX partial: Find similar tickets and return results HTML."""
    try:
        result = await service.find_similar_tickets(
            ticket_id=ticket_id,
            top_k=top_k,
        )
        return templates.TemplateResponse(
            "search/partials/similar_results.html",
            {
                "request": request,
                "source_ticket_id": result.source_ticket_id,
                "similar_tickets": [t.model_dump() for t in result.similar_tickets],
            },
        )
    except ValueError as e:
        return templates.TemplateResponse(
            "search/partials/similar_results.html",
            {
                "request": request,
                "error": str(e),
                "source_ticket_id": ticket_id,
                "similar_tickets": [],
            },
        )
    except Exception as e:
        logger.exception("Similar ticket search failed")
        return templates.TemplateResponse(
            "search/partials/similar_results.html",
            {
                "request": request,
                "error": str(e),
                "source_ticket_id": ticket_id,
                "similar_tickets": [],
            },
        )