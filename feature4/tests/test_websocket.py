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
    presence_join_event,
    presence_leave_event,
    queue_loading_complete_event,
    queue_loading_start_event,
    queue_refresh_event,
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


# ── Queue Auto-Refresh Event Tests ────────────────────────────────────


def test_queue_refresh_event_with_changes():
    """Should create a properly structured queue_refresh event with added and removed."""
    added = [
        {"id": "IR10003", "entity_id": "eid-3", "ticket_type": "incident", "title": "New ticket"},
    ]
    removed = ["IR10001", "SR20001"]
    locks = {"IR10002": "user_a"}

    event = queue_refresh_event(
        added=added,
        removed=removed,
        total=5,
        locks=locks,
    )

    assert event["event"] == "queue_refresh"
    assert len(event["added"]) == 1
    assert event["added"][0]["id"] == "IR10003"
    assert event["removed"] == ["IR10001", "SR20001"]
    assert event["total"] == 5
    assert event["locks"] == {"IR10002": "user_a"}


def test_queue_refresh_event_empty_diff():
    """Should create a valid event even when no changes occurred."""
    event = queue_refresh_event(
        added=[],
        removed=[],
        total=10,
        locks={},
    )

    assert event["event"] == "queue_refresh"
    assert event["added"] == []
    assert event["removed"] == []
    assert event["total"] == 10
    assert event["locks"] == {}


def test_queue_refresh_event_only_additions():
    """Should handle additions without removals."""
    added = [
        {"id": "IR10003", "entity_id": "eid-3", "ticket_type": "incident"},
        {"id": "SR20002", "entity_id": "eid-4", "ticket_type": "servicerequest"},
    ]

    event = queue_refresh_event(
        added=added,
        removed=[],
        total=12,
        locks={"IR10001": "user_a"},
    )

    assert len(event["added"]) == 2
    assert event["removed"] == []
    assert event["total"] == 12


def test_queue_refresh_event_only_removals():
    """Should handle removals without additions."""
    event = queue_refresh_event(
        added=[],
        removed=["IR10001", "IR10002"],
        total=8,
        locks={},
    )

    assert event["added"] == []
    assert len(event["removed"]) == 2
    assert event["total"] == 8


# ── Presence Event Helpers ────────────────────────────────────────────


def test_presence_join_event():
    """Should create a properly structured PRESENCE_JOIN event."""
    event = presence_join_event("user_a", ["user_a", "user_b"])
    assert event["event"] == "presence_join"
    assert event["user_id"] == "user_a"
    assert event["users"] == ["user_a", "user_b"]


def test_presence_leave_event():
    """Should create a properly structured PRESENCE_LEAVE event."""
    event = presence_leave_event("user_a", ["user_b"])
    assert event["event"] == "presence_leave"
    assert event["user_id"] == "user_a"
    assert event["users"] == ["user_b"]


def test_presence_join_event_single_user():
    """Should handle a single user joining (first user)."""
    event = presence_join_event("user_a", ["user_a"])
    assert event["event"] == "presence_join"
    assert event["user_id"] == "user_a"
    assert len(event["users"]) == 1


def test_presence_leave_event_empty_users():
    """Should handle the last user leaving (empty user list)."""
    event = presence_leave_event("user_a", [])
    assert event["event"] == "presence_leave"
    assert event["user_id"] == "user_a"
    assert event["users"] == []


# ── Color Assignment Tests ─────────────────────────────────────────────

from feature4.websocket.manager import USER_COLOR_PALETTE, USER_COLOR_SELF


class TestColorAssignment:
    """Tests for per-user color assignment in ConnectionManager."""

    def test_assign_color_returns_first_palette_color(self):
        """First user should get the first color in the palette."""
        mgr = ConnectionManager()
        color = mgr.assign_color("user_a")
        assert color == USER_COLOR_PALETTE[0]

    def test_assign_color_unique_per_user(self):
        """Each user should get a unique color."""
        mgr = ConnectionManager()
        c1 = mgr.assign_color("user_a")
        c2 = mgr.assign_color("user_b")
        c3 = mgr.assign_color("user_c")
        assert c1 != c2
        assert c2 != c3
        assert c1 != c3

    def test_assign_color_idempotent(self):
        """Assigning a color to the same user twice should return the same color."""
        mgr = ConnectionManager()
        c1 = mgr.assign_color("user_a")
        c2 = mgr.assign_color("user_a")
        assert c1 == c2

    def test_assign_color_sequential(self):
        """Colors should be assigned in palette order."""
        mgr = ConnectionManager()
        for i in range(len(USER_COLOR_PALETTE)):
            color = mgr.assign_color(f"user_{i}")
            assert color == USER_COLOR_PALETTE[i]

    def test_assign_color_wraps_around(self):
        """When all palette colors are used, should wrap around with modulo."""
        mgr = ConnectionManager()
        # Fill all palette slots
        for i in range(len(USER_COLOR_PALETTE)):
            mgr.assign_color(f"user_{i}")
        # Next user wraps around
        extra_color = mgr.assign_color("user_extra")
        expected_idx = len(USER_COLOR_PALETTE) % len(USER_COLOR_PALETTE)
        assert extra_color == USER_COLOR_PALETTE[expected_idx]

    def test_release_color_frees_slot(self):
        """Releasing a color should make it available for the next user."""
        mgr = ConnectionManager()
        c1 = mgr.assign_color("user_a")
        c2 = mgr.assign_color("user_b")
        mgr.release_color("user_a")
        # user_c should get user_a's freed color (first unused in palette)
        c3 = mgr.assign_color("user_c")
        assert c3 == c1

    def test_release_color_nonexistent_user(self):
        """Releasing a color for a user that has none should not raise."""
        mgr = ConnectionManager()
        mgr.release_color("nonexistent")  # Should not raise

    def test_user_colors_property(self):
        """user_colors property should return a copy of the color map."""
        mgr = ConnectionManager()
        mgr.assign_color("user_a")
        mgr.assign_color("user_b")
        colors = mgr.user_colors
        assert len(colors) == 2
        assert "user_a" in colors
        assert "user_b" in colors
        # Should be a copy, not the internal dict
        colors["user_c"] = "#000000"
        assert "user_c" not in mgr.user_colors

    @pytest.mark.asyncio
    async def test_disconnect_releases_color(self):
        """Disconnecting a user should release their color."""
        mgr = ConnectionManager()
        ws = AsyncMock()
        ws.accept = AsyncMock()

        await mgr.connect(ws, "user_a")
        mgr.assign_color("user_a")
        assert "user_a" in mgr.user_colors

        mgr.disconnect("user_a")
        assert "user_a" not in mgr.user_colors

    def test_user_color_self_constant(self):
        """USER_COLOR_SELF should be the Penn Medicine blue."""
        assert USER_COLOR_SELF == "#4A90D9"

    def test_palette_has_at_least_10_colors(self):
        """Palette should have at least 10 colors for reasonable multi-user support."""
        assert len(USER_COLOR_PALETTE) >= 10


class TestPresenceEventsWithColors:
    """Tests for presence events including user_colors."""

    def test_presence_join_with_colors(self):
        """presence_join_event should include user_colors when provided."""
        colors = {"user_a": "#DC143C", "user_b": "#FF6347"}
        event = presence_join_event("user_b", ["user_a", "user_b"], user_colors=colors)
        assert event["event"] == "presence_join"
        assert event["user_colors"] == colors

    def test_presence_join_without_colors(self):
        """presence_join_event should not include user_colors when not provided."""
        event = presence_join_event("user_a", ["user_a"])
        assert "user_colors" not in event

    def test_presence_leave_with_colors(self):
        """presence_leave_event should include user_colors when provided."""
        colors = {"user_b": "#FF6347"}
        event = presence_leave_event("user_a", ["user_b"], user_colors=colors)
        assert event["event"] == "presence_leave"
        assert event["user_colors"] == colors

    def test_presence_leave_without_colors(self):
        """presence_leave_event should not include user_colors when not provided."""
        event = presence_leave_event("user_a", [])
        assert "user_colors" not in event
