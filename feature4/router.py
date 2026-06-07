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

import asyncio
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
    presence_join_event,
    presence_leave_event,
    queue_loading_complete_event,
    queue_loading_start_event,
    queue_refresh_event,
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

# ── Eager Cache Population on Startup ─────────────────────────────────

_cache_startup_task: asyncio.Task | None = None


async def _populate_cache_on_startup() -> None:
    """
    Background task that eagerly populates the queue cache on server startup.

    Runs once after the event loop starts. If it fails (e.g., Athena
    unreachable), logs a warning — the cache will be populated on the
    first auto-refresh cycle or user connection instead.
    """
    from feature4.dependencies import get_bulk_assignment_service

    try:
        service = get_bulk_assignment_service()
        count = await service.refresh_cache()
        logger.info("Startup cache population complete: %d tickets cached", count)
    except Exception as exc:
        logger.warning("Startup cache population failed (will retry on first refresh): %s", exc)


@router.on_event("startup")
async def _on_startup() -> None:
    """Schedule eager cache population when the app starts."""
    global _cache_startup_task
    _cache_startup_task = asyncio.create_task(
        _populate_cache_on_startup(),
        name="bulk_cache_startup",
    )


# ── Background Auto-Refresh Task ──────────────────────────────────────

_refresh_task: asyncio.Task | None = None
_REFRESH_INTERVAL_SECONDS = 30


async def _auto_refresh_loop(
    service: BulkAssignmentService,
    manager: ConnectionManager,
) -> None:
    """
    Background polling loop that periodically checks Athena for queue
    changes and broadcasts incremental diffs to all connected clients.

    Runs as long as there is at least one WebSocket client connected.
    Cancelled automatically when the last client disconnects.
    """
    logger.info("Auto-refresh loop started (interval=%ds)", _REFRESH_INTERVAL_SECONDS)
    try:
        while True:
            await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)

            # Only poll if there are connected clients
            if not manager.connected_user_ids:
                logger.debug("No connected clients — skipping refresh")
                continue

            try:
                diff = await service.compute_queue_diff()
                added = diff["added"]
                removed = diff["removed"]

                if not added and not removed:
                    logger.debug("Auto-refresh: no queue changes detected")
                    continue

                # Serialise added tickets for the wire
                added_dicts = [t.model_dump() for t in added]

                event = queue_refresh_event(
                    added=added_dicts,
                    removed=removed,
                    total=diff["total"],
                    locks=diff["locks"],
                )
                await manager.broadcast_all(event)

                logger.info(
                    "Auto-refresh broadcast: +%d added, -%d removed (total=%d)",
                    len(added),
                    len(removed),
                    diff["total"],
                )
            except Exception as exc:
                logger.warning("Auto-refresh poll failed: %s", exc)

    except asyncio.CancelledError:
        logger.info("Auto-refresh loop cancelled")
        raise


def _start_refresh_task(
    service: BulkAssignmentService,
    manager: ConnectionManager,
) -> None:
    """Start the background auto-refresh task if not already running."""
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(
            _auto_refresh_loop(service, manager),
            name="bulk_auto_refresh",
        )


def _stop_refresh_task_if_idle(manager: ConnectionManager) -> None:
    """Cancel the background auto-refresh task if no clients remain."""
    global _refresh_task
    if not manager.connected_user_ids and _refresh_task and not _refresh_task.done():
        _refresh_task.cancel()
        _refresh_task = None
        logger.info("Auto-refresh loop stopped (no connected clients)")

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
    # Get the authenticated user's display name from the session (set by AuthMiddleware)
    user = getattr(request.state, "user", None)
    bulk_user_id = user.display_name if user else "Unknown"

    return templates.TemplateResponse(
        "bulk/index.html",
        {"request": request, "active_page": "bulk", "bulk_user_id": bulk_user_id},
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

    If the client provides ticket_ids (from its local queue view), those
    are used directly — avoiding a costly Athena re-fetch.  Otherwise
    falls back to fetching the queue from Athena.
    """
    if request.ticket_ids is not None:
        queue_ticket_ids = request.ticket_ids
    else:
        # Fallback: fetch current queue from Athena (slow path)
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

    # Assign a unique color to this user
    manager.assign_color(user_id)

    # Start background auto-refresh if this is the first client
    _start_refresh_task(service, manager)

    try:
        # Send current lock state + online users + user colors on connect
        sync = state_sync_event(service.get_locks())
        sync["users"] = manager.connected_user_ids
        sync["user_colors"] = manager.user_colors
        await manager.send_to_user(user_id, sync)

        # Broadcast presence_join to all OTHER clients (with user_colors)
        await manager.broadcast(
            presence_join_event(user_id, manager.connected_user_ids, user_colors=manager.user_colors),
            exclude_user=user_id,
        )

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

                # Track streamed ticket IDs for snapshot
                _streamed_ids: list[str] = []

                async def _on_ticket_with_tracking(ticket, count):
                    _streamed_ids.append(ticket.id)
                    await _on_ticket(ticket, count)

                try:
                    total = await service.fetch_queue_streaming(
                        on_ticket=_on_ticket_with_tracking,
                    )
                    # Snapshot ticket IDs so auto-refresh diffs are correct
                    service.snapshot_ticket_ids(set(_streamed_ids))

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

        # Stop auto-refresh if no clients remain
        _stop_refresh_task_if_idle(manager)

        # Broadcast unlock events for released tickets
        for tid in released:
            await manager.broadcast_all(unlock_event(tid, user_id))

        # Broadcast presence_leave to remaining clients (with updated user_colors)
        await manager.broadcast_all(
            presence_leave_event(user_id, manager.connected_user_ids, user_colors=manager.user_colors)
        )
