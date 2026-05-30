"""
Assignment API Router — REST endpoint for Feature #3: Ticket Assignment Recommendation.

Endpoints:
    POST /assignment/{ticket_id} — Get classifier-recommended support group for a ticket
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
    Analyze a ticket and recommend the best support group assignment.

    The system will:
    1. Fetch the ticket from Athena
    2. Check triage rules for known routing patterns
    3. Use the TF-IDF classifier to predict the optimal support group
    """
    try:
        return await service.recommend_assignment(
            ticket_id=ticket_id,
            top_k=request.top_k,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        error_msg = str(e)
        normalized = ticket_id.strip().upper() if ticket_id else ticket_id
        if "404" in error_msg or "not found" in error_msg.lower():
            raise HTTPException(
                status_code=404,
                detail=f"Ticket '{normalized}' not found in Athena. Please verify the ticket ID exists.",
            )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to generate recommendation for '{normalized}': {error_msg}",
        )