"""
Unit tests for WebSocket infrastructure — Feature #4: Bulk Assignment.

Tests:
- ConnectionManager (connect, disconnect, broadcast, send_to_user)
- WebSocket event helpers (lock, unlock, assign, state_sync)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from feature4.models import WSEventType
from feature4.websocket.events import (
    assign_event,
    lock_event,
    queue_loading_complete_event,
    queue_loading_start_event,
    queue_ticket_event,
    state_sync_event,
    unlock_event,
)
from feature4.websocket.manager import ConnectionManager


# ── Event Helpers ─────────────────────────────────────────────────────


def test_lock_event():
    """Should create a properly structured LOCK event."""
    event = lock_event("IR10001", "user_a")
    assert event["event"] == WSEventType.LOCK
    assert event["ticket_id"] == "IR10001"
    assert event["user_id"] == "user_a"


def test_unlock_event():
    """Should create a properly structured UNLOCK event."""
    event = unlock_event("IR10001", "user_a")
    assert event["event"] == WSEventType.UNLOCK
    assert event["ticket_id"] == "IR10001"
    assert event["user_id"] == "user_a"


def test_assign_event():
    """Should create a properly structured ASSIGN event."""
    event = assign_event("IR10001", "user_a")
    assert event["event"] == WSEventType.ASSIGN
    assert event["ticket_id"] == "IR10001"
    assert event["user_id"] == "user_a"


def test_state_sync_event():
    """Should create a STATE_SYNC event with lock state."""
    locks = {"IR10001": "user_a", "IR10002": "user_b"}
    event = state_sync_event(locks)
    assert event["event"] == WSEventType.STATE_SYNC
    assert event["locks"] == locks


def test_state_sync_event_empty():
    """Should handle empty lock state."""
    event = state_sync_event({})
    assert event["event"] == WSEventType.STATE_SYNC
    assert event["locks"] == {}


# ── ConnectionManager ─────────────────────────────────────────────────


@pytest.fixture
def manager():
    """Create a fresh ConnectionManager."""
    return ConnectionManager()


@pytest.fixture
def mock_websocket():
    """Create a mock WebSocket."""
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_connect(manager, mock_websocket):
    """Should accept the WebSocket and register the user."""
    await manager.connect(mock_websocket, "user_a")

    mock_websocket.accept.assert_called_once()
    assert "user_a" in manager.connected_user_ids


@pytest.mark.asyncio
async def test_connect_replaces_existing(manager):
    """Should replace an existing connection for the same user."""
    ws1 = AsyncMock()
    ws1.accept = AsyncMock()
    ws1.close = AsyncMock()

    ws2 = AsyncMock()
    ws2.accept = AsyncMock()

    await manager.connect(ws1, "user_a")
    await manager.connect(ws2, "user_a")

    # Old connection should be closed
    ws1.close.assert_called_once()
    # New connection should be active
    assert manager.active_connections["user_a"] == ws2


def test_disconnect(manager):
    """Should remove the user's connection."""
    # Manually add a connection
    manager._connections["user_a"] = MagicMock()

    manager.disconnect("user_a")

    assert "user_a" not in manager.connected_user_ids


def test_disconnect_nonexistent(manager):
    """Should handle disconnecting a non-existent user gracefully."""
    manager.disconnect("user_x")  # Should not raise


@pytest.mark.asyncio
async def test_send_to_user(manager, mock_websocket):
    """Should send a JSON message to a specific user."""
    await manager.connect(mock_websocket, "user_a")

    data = {"event": "test", "value": 42}
    await manager.send_to_user("user_a", data)

    mock_websocket.send_json.assert_called_once_with(data)


@pytest.mark.asyncio
async def test_send_to_nonexistent_user(manager):
    """Should silently ignore sends to non-existent users."""
    await manager.send_to_user("user_x", {"event": "test"})
    # Should not raise


@pytest.mark.asyncio
async def test_broadcast(manager):
    """Should send to all connected users."""
    ws_a = AsyncMock()
    ws_a.accept = AsyncMock()
    ws_a.send_json = AsyncMock()

    ws_b = AsyncMock()
    ws_b.accept = AsyncMock()
    ws_b.send_json = AsyncMock()

    await manager.connect(ws_a, "user_a")
    await manager.connect(ws_b, "user_b")

    data = {"event": "lock", "ticket_id": "IR10001"}
    await manager.broadcast(data)

    ws_a.send_json.assert_called_once_with(data)


def test_queue_loading_complete_event_empty():
    """Should handle empty queue and no locks."""
    event = queue_loading_complete_event(0, {}, "user_a")
    assert event["total"] == 0
    assert event["locks"] == {}


# ── Queue Streaming Event Helpers ─────────────────────────────────────


def test_queue_loading_start_event():
    """Should create a properly structured QUEUE_LOADING_START event."""
    event = queue_loading_start_event("user_a")
    assert event["event"] == "queue_loading_start"
    assert event["user_id"] == "user_a"


def test_queue_ticket_event():
    """Should create a properly structured QUEUE_TICKET event."""
    ticket_data = {"id": "IR10001", "title": "Test ticket"}
    event = queue_ticket_event(ticket_data, 5)
    assert event["event"] == "queue_ticket"
    assert event["ticket"] == ticket_data
    assert event["count"] == 5


def test_queue_ticket_event_count_one():
    """Should handle count of 1 (first ticket)."""
    event = queue_ticket_event({"id": "IR10001"}, 1)
    assert event["count"] == 1


def test_queue_loading_complete_event():
    """Should create a properly structured QUEUE_LOADING_COMPLETE event."""
    locks = {"IR10001": "user_a", "IR10002": "user_b"}
    event = queue_loading_complete_event(67, locks, "user_a")
    assert event["event"] == "queue_loading_complete"
    assert event["total"] == 67
    assert event["locks"] == locks
    assert event["user_id"] == "user_a"


@pytest.mark.asyncio
async def test_broadcast_with_exclude(manager):
    """Should exclude the specified user from broadcast."""
    ws_a = AsyncMock()
    ws_a.accept = AsyncMock()
    ws_a.send_json = AsyncMock()

    ws_b = AsyncMock()
    ws_b.accept = AsyncMock()
    ws_b.send_json = AsyncMock()

    await manager.connect(ws_a, "user_a")
    await manager.connect(ws_b, "user_b")

    data = {"event": "lock", "ticket_id": "IR10001"}
    await manager.broadcast(data, exclude_user="user_a")

    ws_a.send_json.assert_not_called()
    ws_b.send_json.assert_called_once_with(data)


@pytest.mark.asyncio
async def test_broadcast_cleans_up_failed_connections(manager):
    """Should remove connections that fail during broadcast."""
    ws_a = AsyncMock()
    ws_a.accept = AsyncMock()
    ws_a.send_json = AsyncMock(side_effect=Exception("Connection lost"))

    ws_b = AsyncMock()
    ws_b.accept = AsyncMock()
    ws_b.send_json = AsyncMock()

    await manager.connect(ws_a, "user_a")
    await manager.connect(ws_b, "user_b")

    await manager.broadcast({"event": "test"})

    # user_a should be disconnected due to failure
    assert "user_a" not in manager.connected_user_ids
    # user_b should still be connected
    assert "user_b" in manager.connected_user_ids


@pytest.mark.asyncio
async def test_connected_user_ids(manager):
    """Should return list of connected user IDs."""
    ws_a = AsyncMock()
    ws_a.accept = AsyncMock()
    ws_b = AsyncMock()
    ws_b.accept = AsyncMock()

    await manager.connect(ws_a, "user_a")
    await manager.connect(ws_b, "user_b")

    ids = manager.connected_user_ids
    assert set(ids) == {"user_a", "user_b"}


@pytest.mark.asyncio
async def test_broadcast_all(manager):
    """broadcast_all should send to all without exclusions."""
    ws_a = AsyncMock()
    ws_a.accept = AsyncMock()
    ws_a.send_json = AsyncMock()

    await manager.connect(ws_a, "user_a")

    data = {"event": "test"}
    await manager.broadcast_all(data)

    ws_a.send_json.assert_called_once_with(data)