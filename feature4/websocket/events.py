"""
WebSocket event helpers for Feature #4: Bulk Assignment.

Provides factory functions for creating typed WebSocket event payloads
that are broadcast to all connected clients.
"""

from feature4.models import (
    WSAssignEvent,
    WSEventType,
    WSLockEvent,
    WSStateSyncEvent,
    WSUnlockEvent,
)


def lock_event(ticket_id: str, user_id: str) -> dict:
    """Create a serialized LOCK event."""
    return WSLockEvent(ticket_id=ticket_id, user_id=user_id).model_dump()


def unlock_event(ticket_id: str, user_id: str) -> dict:
    """Create a serialized UNLOCK event."""
    return WSUnlockEvent(ticket_id=ticket_id, user_id=user_id).model_dump()


def assign_event(ticket_id: str, user_id: str) -> dict:
    """Create a serialized ASSIGN event (ticket removed from queue)."""
    return WSAssignEvent(ticket_id=ticket_id, user_id=user_id).model_dump()


def state_sync_event(locks: dict[str, str]) -> dict:
    """Create a serialized STATE_SYNC event with full lock state."""
    return WSStateSyncEvent(locks=locks).model_dump()


# ── Recommendation Progress Events ────────────────────────────────────


def rec_start_event(ticket_ids: list[str], user_id: str) -> dict:
    """Create a REC_START event when a recommendation batch begins."""
    return {
        "event": "rec_start",
        "ticket_ids": ticket_ids,
        "total": len(ticket_ids),
        "user_id": user_id,
    }


def rec_processing_event(
    ticket_id: str, current: int, total: int, user_id: str
) -> dict:
    """Create a REC_PROCESSING event when a specific ticket starts processing."""
    return {
        "event": "rec_processing",
        "ticket_id": ticket_id,
        "current": current,
        "total": total,
        "user_id": user_id,
    }


def rec_result_event(
    ticket_id: str, success: bool, current: int, total: int, user_id: str
) -> dict:
    """Create a REC_RESULT event when a specific ticket's recommendation is ready."""
    return {
        "event": "rec_result",
        "ticket_id": ticket_id,
        "success": success,
        "current": current,
        "total": total,
        "user_id": user_id,
    }


def rec_complete_event(total: int, failed: int, user_id: str) -> dict:
    """Create a REC_COMPLETE event when the entire batch is done."""
    return {
        "event": "rec_complete",
        "total": total,
        "failed": failed,
        "user_id": user_id,
    }


# ── Queue Streaming Events ────────────────────────────────────────────


def queue_loading_start_event(user_id: str) -> dict:
    """Create a QUEUE_LOADING_START event when queue fetch begins."""
    return {
        "event": "queue_loading_start",
        "user_id": user_id,
    }


def queue_ticket_event(ticket_data: dict, count: int) -> dict:
    """
    Create a QUEUE_TICKET event for a single ticket streamed from the queue.

    Args:
        ticket_data: Serialized QueueTicketSummary dict.
        count: Running count of tickets streamed so far.
    """
    return {
        "event": "queue_ticket",
        "ticket": ticket_data,
        "count": count,
    }


def queue_loading_complete_event(total: int, locks: dict[str, str], user_id: str) -> dict:
    """Create a QUEUE_LOADING_COMPLETE event when all tickets have been streamed."""
    return {
        "event": "queue_loading_complete",
        "total": total,
        "locks": locks,
        "user_id": user_id,
    }
