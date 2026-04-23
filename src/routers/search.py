"""
Search API Router — REST endpoints for Feature #1: Enhanced Ticket Search.

Endpoints:
    POST /search/field          — Search by field value
    POST /search/description    — Search by description substring
    POST /search/semantic       — Semantic natural-language search
    POST /search/similar/{id}   — Find tickets similar to a given ticket
"""

from fastapi import APIRouter, Depends, HTTPException

from src.dependencies import get_search_service
from src.models.search import (
    DescriptionSearchRequest,
    FieldSearchRequest,
    FieldSearchResponse,
    SemanticSearchRequest,
    SemanticSearchResponse,
    SimilarTicketRequest,
    SimilarTicketResponse,
)
from src.services.ticket_search import TicketSearchService

router = APIRouter(prefix="/search", tags=["search"])


@router.post("/field", response_model=FieldSearchResponse)
async def search_by_field(
    request: FieldSearchRequest,
    service: TicketSearchService = Depends(get_search_service),
) -> FieldSearchResponse:
    """
    Search tickets by a specific field value.

    Example: Find all incidents where contactMethod = '215-555-1234'.
    Supports pagination via page and page_size parameters.
    """
    return await service.search_by_field(
        field=request.field,
        value=request.value,
        ticket_type=request.ticket_type.value,
        operator=request.operator,
        page=request.page,
        page_size=request.page_size,
    )


@router.post("/description", response_model=FieldSearchResponse)
async def search_by_description(
    request: DescriptionSearchRequest,
    service: TicketSearchService = Depends(get_search_service),
) -> FieldSearchResponse:
    """
    Search tickets by substring match in the description field.

    Example: Find all incidents whose description contains 'printer not printing'.
    Supports pagination via page and page_size parameters.
    """
    return await service.search_by_description(
        text=request.text,
        ticket_type=request.ticket_type.value,
        page=request.page,
        page_size=request.page_size,
    )


@router.post("/semantic", response_model=SemanticSearchResponse)
async def semantic_search(
    request: SemanticSearchRequest,
    service: TicketSearchService = Depends(get_search_service),
) -> SemanticSearchResponse:
    """
    Perform natural-language semantic search across historical tickets
    and knowledge base documentation.

    Uses AI embeddings to find conceptually similar content even without
    exact keyword matches.
    """
    return await service.semantic_search(
        query=request.query,
        top_k=request.top_k,
    )


@router.post("/similar/{ticket_id}", response_model=SimilarTicketResponse)
async def find_similar_tickets(
    ticket_id: str,
    request: SimilarTicketRequest = SimilarTicketRequest(),
    service: TicketSearchService = Depends(get_search_service),
) -> SimilarTicketResponse:
    """
    Find tickets similar to a given ticket ID.

    Uses the ticket's pre-computed embedding to find historically similar incidents.
    """
    try:
        return await service.find_similar_tickets(
            ticket_id=ticket_id,
            top_k=request.top_k,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))