"""
Turnover API Router — REST endpoint for Feature #5: Turnover Email Draft Generator.

Endpoints:
    POST /turnover/generate — Generate a SEV turnover email draft
"""

from fastapi import APIRouter, Depends, HTTPException

from src.dependencies import get_turnover_service
from src.models.turnover import TurnoverRequest, TurnoverResponse
from src.services.turnover import TurnoverService

router = APIRouter(prefix="/turnover", tags=["turnover"])


@router.post("/generate", response_model=TurnoverResponse)
async def generate_turnover(
    request: TurnoverRequest,
    service: TurnoverService = Depends(get_turnover_service),
) -> TurnoverResponse:
    """
    Generate a SEV turnover email draft.

    The system will:
    1. Query Athena for active and pended P1/P2 severity incidents
    2. Query Athena for upcoming change requests with scheduled outages
    3. Categorize tickets (parent incidents, active SEVs, pended SEVs)
    4. Format a copy-paste-ready email with all sections

    The response includes the email To/CC/Subject/Body fields ready for Outlook,
    plus structured data for each ticket category.
    """
    try:
        return await service.generate_turnover(request)
    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg or "403" in error_msg:
            raise HTTPException(
                status_code=502,
                detail="Athena authentication failed. Check credentials.",
            )
        raise HTTPException(status_code=502, detail=f"Upstream error: {error_msg}")