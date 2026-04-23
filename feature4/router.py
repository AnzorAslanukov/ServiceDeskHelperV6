"""
Bulk Assignment API Router — REST + WebSocket + Frontend endpoints for Feature #4.

REST Endpoints:
    POST /bulk/queue       — Fetch the Validation queue (IR + SR tickets)
    POST /bulk/recommend   — Generate AI recommendations for a batch of tickets
    POST /bulk/assign      — Assign tickets (PUT to Athena) and broadcast events
    POST /bulk/lock        — Lock tickets for a user
    POST /bulk/unlock      — Unlock tickets for a user
    POST /bulk/claim       — Auto-claim next N unlocked tickets

Frontend Endpoint:
    GET /bulk/ui           — Serve the Bulk Assignment web page

WebSocket Endpoint:
    WS /bulk/ws?user_id=xxx — Real-time lock/unlock/assign event stream
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from feature4.dependencies import get_bulk_assignment_service, get_ws_manager
from feature4.models import (
    BulkAssignRequest,
    BulkAssignResponse,
    BulkRecommendRequest,
    BulkRecommendResponse,
    ClaimBatchRequest,
    ClaimBatchResponse,
    LockRequest,
    QueueRequest,
    QueueResponse,
)
from feature4.service import BulkAssignmentService
from feature4.websocket.events import (
    assign_event,
    lock_event,
    queue_loading_complete_event,
    queue_loading_start_event,
    queue_ticket_event,
    rec_complete_event,
    rec_processing_event,
    rec_result_event,
    rec_start_event,
    state_sync_event,
    unlock_event,
)
from feature4.websocket.manager import ConnectionManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bulk", tags=["bulk"])

# Templates directory for Feature #4 (uses shared base.html from frontend/templates)
_FEATURE4_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIRS = [
    str(_FEATURE4_DIR / "templates"),
    str(_FEATURE4_DIR.parent / "frontend" / "templates"),
]
templates = Jinja2Templates(directory=_TEMPLATES_DIRS)


# ── Frontend Page Route ───────────────────────────────────────────────


@router.get("/ui", response_class=HTMLResponse)
async def bulk_ui_page(request: Request):
    """Serve the Bulk Assignment web page."""
    return templates.TemplateResponse(
        "bulk/index.html",
        {"request": request, "active_page": "bulk"},
    )


# ── REST Endpoints ────────────────────────────────────────────────────


@router.post("/queue", response_model=QueueResponse)
async def fetch_queue(
    request: QueueRequest = QueueRequest(),
    service: BulkAssignmentService = Depends(get_bulk_assignment_service),
) -> QueueResponse:
    """
    Fetch the current Validation queue.

    Returns all IR and SR tickets from the specified tier queue,
    merged and annotated with current lock state.
    """
    try:
        return await service.fetch_queue(
            tier_queue_name=request.tier_queue_name,
            statuses=request.statuses,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch queue: {e}")


@router.post("/lock")
async def lock_tickets(
    request: LockRequest,
    service: BulkAssignmentService = Depends(get_bulk_assignment_service),
    manager: ConnectionManager = Depends(get_ws_manager),
) -> dict:
    """Lock tickets for a user and broadcast LOCK events."""
    locked = service.lock_tickets(request.ticket_ids, request.user_id)

    # Broadcast lock events
    for tid in locked:
        await manager.broadcast_all(lock_event(tid, request.user_id))

    return {
        "locked": locked,
        "total_locked": len(locked),
        "requested": len(request.ticket_ids),
    }


@router.post("/unlock")
async def unlock_tickets(
    request: LockRequest,
    service: BulkAssignmentService = Depends(get_bulk_assignment_service),
    manager: ConnectionManager = Depends(get_ws_manager),
) -> dict:
    """Unlock tickets for a user and broadcast UNLOCK events."""
    unlocked = service.unlock_tickets(request.ticket_ids, request.user_id)

    # Broadcast unlock events
    for tid in unlocked:
        await manager.broadcast_all(unlock_event(tid, request.user_id))

    return {
        "unlocked": unlocked,
        "total_unlocked": len(unlocked),
        "requested": len(request.ticket_ids),
    }


@router.post("/claim", response_model=ClaimBatchResponse)
async def claim_batch(
    request: ClaimBatchRequest,
    service: BulkAssignmentService = Depends(get_bulk_assignment_service),
    manager: ConnectionManager = Depends(get_ws_manager),
) -> ClaimBatchResponse:
    """
    Auto-claim the next N unlocked tickets from the queue.

    Fetches the current queue, then locks the first N unlocked tickets
    for the requesting user.
    """
    # Fetch current queue to get ordered ticket IDs
    queue = await service.fetch_queue()
    queue_ticket_ids = [t.id for t in queue.tickets]

    claimed = service.claim_batch(request.user_id, request.batch_size, queue_ticket_ids)

    # Broadcast lock events for claimed tickets
    for tid in claimed:
        await manager.broadcast_all(lock_event(tid, request.user_id))

    return ClaimBatchResponse(
        claimed_ticket_ids=claimed,
        total_claimed=len(claimed),
    )


@router.post("/recommend", response_model=BulkRecommendResponse)
async def bulk_recommend(
    request: BulkRecommendRequest,
    service: BulkAssignmentService = Depends(get_bulk_assignment_service),
    manager: ConnectionManager = Depends(get_ws_manager),
) -> BulkRecommendResponse:
    """
    Generate AI recommendations for a batch of tickets.

    Each ticket is analyzed independently using the same pipeline
    as the single-ticket assignment recommendation (Feature #3).
    Broadcasts real-time progress events via WebSocket so all
    connected clients see per-ticket visual feedback.
    """
    user_id = request.user_id or "unknown"

    # Broadcast rec_start so all clients mark these tickets as pending
    await manager.broadcast_all(rec_start_event(request.ticket_ids, user_id))

    async def on_processing(ticket_id: str, current: int, total: int) -> None:
        await manager.broadcast_all(
            rec_processing_event(ticket_id, current, total, user_id)
        )

    async def on_result(ticket_id: str, success: bool, current: int, total: int) -> None:
        await manager.broadcast_all(
            rec_result_event(ticket_id, success, current, total, user_id)
        )

    try:
        result = await service.batch_recommend(
            ticket_ids=request.ticket_ids,
            top_k_docs=request.top_k_docs,
            top_k_tickets=request.top_k_tickets,
            max_tokens=request.max_tokens,
            on_processing=on_processing,
            on_result=on_result,
        )

        # Broadcast rec_complete so all clients clear progress states
        await manager.broadcast_all(
            rec_complete_event(result.total, result.failed, user_id)
        )

        return result
    except Exception as e:
        # Broadcast rec_complete on error too, so UI clears progress states
        await manager.broadcast_all(
            rec_complete_event(len(request.ticket_ids), len(request.ticket_ids), user_id)
        )
        raise HTTPException(status_code=502, detail=f"Recommendation failed: {e}")


@router.get("/support-groups")
async def get_support_groups(
    ticket_type: str = Query(
        ...,
        description="Ticket type: 'incident' or 'servicerequest'",
        pattern="^(incident|servicerequest)$",
    ),
    service: BulkAssignmentService = Depends(get_bulk_assignment_service),
) -> list[dict[str, str]]:
    """
    Get assignable support groups for a ticket type.

    Returns a list of {name, guid} pairs for use in the manual
    assignment autocomplete dropdown.
    """
    try:
        return await service.get_support_groups(ticket_type)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch support groups: {e}")


@router.post("/assign", response_model=BulkAssignResponse)
async def bulk_assign(
    request: BulkAssignRequest,
    service: BulkAssignmentService = Depends(get_bulk_assignment_service),
    manager: ConnectionManager = Depends(get_ws_manager),
) -> BulkAssignResponse:
    """
    Assign a batch of tickets by updating them in Athena.

    For each successful assignment, broadcasts an ASSIGN event
    so all connected clients know the ticket has been removed from the queue.
    """
    result = await service.assign_tickets(request.assignments)

    # Broadcast assign events for successful assignments
    for r in result.results:
        if r.success:
            await manager.broadcast_all(assign_event(r.ticket_id, request.user_id))

    return result


# ── WebSocket Endpoint ────────────────────────────────────────────────


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: str = Query(...),
    service: BulkAssignmentService = Depends(get_bulk_assignment_service),
    manager: ConnectionManager = Depends(get_ws_manager),
):
    """
    WebSocket endpoint for real-time queue synchronization.

    On connect:
        - Sends STATE_SYNC with current lock state

    Client messages (JSON):
        - {"action": "lock", "ticket_ids": ["IR123", ...]}
        - {"action": "unlock", "ticket_ids": ["IR123", ...]}

    Server broadcasts:
        - LOCK events when tickets are locked
        - UNLOCK events when tickets are unlocked
        - ASSIGN events when tickets are assigned (via REST)

    On disconnect:
        - Auto-releases all locks held by the user
        - Broadcasts UNLOCK events for released tickets
    """
    await manager.connect(websocket, user_id)

    try:
        # Send current lock state on connect
        await manager.send_to_user(user_id, state_sync_event(service.get_locks()))

        # Listen for client messages
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "lock":
                ticket_ids = data.get("ticket_ids", [])
                locked = service.lock_tickets(ticket_ids, user_id)
                for tid in locked:
                    await manager.broadcast_all(lock_event(tid, user_id))

            elif action == "unlock":
                ticket_ids = data.get("ticket_ids", [])
                unlocked = service.unlock_tickets(ticket_ids, user_id)
                for tid in unlocked:
                    await manager.broadcast_all(unlock_event(tid, user_id))

            elif action == "load_queue":
                # Stream queue tickets one-by-one via WebSocket
                await manager.send_to_user(
                    user_id, queue_loading_start_event(user_id)
                )

                async def _on_ticket(ticket, count):
                    await manager.send_to_user(
                        user_id,
                        queue_ticket_event(ticket.model_dump(), count),
                    )

                try:
                    total = await service.fetch_queue_streaming(
                        on_ticket=_on_ticket,
                    )
                    await manager.send_to_user(
                        user_id,
                        queue_loading_complete_event(
                            total, service.get_locks(), user_id
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "Streaming queue load failed for %s: %s", user_id, exc
                    )
                    await manager.send_to_user(
                        user_id,
                        {
                            "event": "queue_loading_error",
                            "message": str(exc),
                        },
                    )

            else:
                # Unknown action — send error back to client
                await manager.send_to_user(
                    user_id,
                    {"event": "error", "message": f"Unknown action: {action}"},
                )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: user_id=%s", user_id)
    except Exception as exc:
        logger.warning("WebSocket error for user %s: %s", user_id, exc)
    finally:
        # Auto-release all locks on disconnect
        released = service.release_user_locks(user_id)
        manager.disconnect(user_id)

        # Broadcast unlock events for released tickets
        for tid in released:
            await manager.broadcast_all(unlock_event(tid, user_id))