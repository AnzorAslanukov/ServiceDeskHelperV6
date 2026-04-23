"""
Assignment API Router — REST endpoint for Feature #3: Ticket Assignment Recommendation.

Endpoints:
    POST /assignment/{ticket_id} — Get AI-recommended support group and priority for a ticket
"""

from fastapi import APIRouter, Depends, HTTPException

from src.dependencies import get_assignment_service
from src.models.assignment import AssignmentRequest, AssignmentResponse
from src.services.assignment import AssignmentService

router = APIRouter(prefix="/assignment", tags=["assignment"])


@router.post("/{ticket_id}", response_model=AssignmentResponse)
async def recommend_assignment(
    ticket_id: str,
    request: AssignmentRequest = AssignmentRequest(),
    service: AssignmentService = Depends(get_assignment_service),
) -> AssignmentResponse:
    """
    Analyze a ticket and recommend the best support group assignment and priority.

    The system will:
    1. Fetch the ticket from Athena
    2. Search the knowledge base and historical tickets for context
    3. Use AI to recommend the optimal support group (with correct GUID) and priority

    The recommended support group GUID is specific to the ticket type
    (IR and SR use different GUIDs for the same group names).
    """
    try:
        return await service.recommend_assignment(
            ticket_id=ticket_id,
            top_k_docs=request.top_k_docs,
            top_k_tickets=request.top_k_tickets,
            max_tokens=request.max_tokens,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Catch Athena 404s or other HTTP errors
        error_msg = str(e)
        if "404" in error_msg or "not found" in error_msg.lower():
            raise HTTPException(
                status_code=404,
                detail=f"Ticket '{ticket_id}' not found in Athena.",
            )
        raise HTTPException(status_code=502, detail=f"Upstream error: {error_msg}")