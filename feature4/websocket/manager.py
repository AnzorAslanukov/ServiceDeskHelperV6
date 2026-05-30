"""
WebSocket Connection Manager for Feature #4: Bulk Assignment.

Manages WebSocket connections, broadcasts events to all connected clients,
and handles connection lifecycle (connect, disconnect, auto-release locks).
"""

import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


# Red-shade palette for distinguishing other users' locks.
# The current user always sees blue (#4A90D9) for their own locks.
# Other users each get a unique shade of red/warm color.
USER_COLOR_PALETTE: list[str] = [
    "#DC143C",  # Crimson
    "#FF6347",  # Tomato
    "#CD5C5C",  # Indian Red
    "#8B0000",  # Dark Red
    "#FF7F50",  # Coral
    "#FA8072",  # Salmon
    "#B22222",  # Firebrick
    "#FF69B4",  # Hot Pink
    "#800000",  # Maroon
    "#E8505B",  # Rose
]

# Blue color for the current user (Penn Medicine accent)
USER_COLOR_SELF = "#4A90D9"


class ConnectionManager:
    """
    Manages WebSocket connections for real-time queue synchronization.

    Each connected client is identified by a user_id. The manager tracks
    active connections and provides broadcast capabilities for lock/unlock/assign events.
    Assigns a unique color from the red-shade palette to each connected user.
    """

    def __init__(self) -> None:
        # user_id → WebSocket connection
        self._connections: dict[str, WebSocket] = {}
        # user_id → color hex string (from USER_COLOR_PALETTE)
        self._user_colors: dict[str, str] = {}

    @property
    def active_connections(self) -> dict[str, WebSocket]:
        """Return the current active connections (read-only view)."""
        return dict(self._connections)

    @property
    def connected_user_ids(self) -> list[str]:
        """Return list of currently connected user IDs."""
        return list(self._connections.keys())

    @property
    def user_colors(self) -> dict[str, str]:
        """Return the current user_id → color_hex mapping."""
        return dict(self._user_colors)

    def assign_color(self, user_id: str) -> str:
        """
        Assign a color to a user from the red-shade palette.

        If the user already has a color, return it.
        Otherwise, pick the first unused color from the palette.
        If all colors are taken, wrap around using modulo.

        Returns:
            The hex color string assigned to this user.
        """
        if user_id in self._user_colors:
            return self._user_colors[user_id]

        used_colors = set(self._user_colors.values())
        # Find first unused color
        for color in USER_COLOR_PALETTE:
            if color not in used_colors:
                self._user_colors[user_id] = color
                return color

        # All colors taken — wrap around
        idx = len(self._user_colors) % len(USER_COLOR_PALETTE)
        color = USER_COLOR_PALETTE[idx]
        self._user_colors[user_id] = color
        return color

    def release_color(self, user_id: str) -> None:
        """Release a user's color assignment so it can be reused."""
        self._user_colors.pop(user_id, None)

    async def connect(self, websocket: WebSocket, user_id: str) -> None:
        """
        Accept a WebSocket connection and register the user.

        If the user already has an active connection, the old one is
        replaced (single connection per user).
        """
        await websocket.accept()

        # If user already connected, close old connection gracefully
        if user_id in self._connections:
            old_ws = self._connections[user_id]
            try:
                await old_ws.close(code=1000, reason="Replaced by new connection")
            except Exception:
                pass  # Old connection may already be closed

        self._connections[user_id] = websocket
        logger.info("WebSocket connected: user_id=%s (total: %d)", user_id, len(self._connections))

    def disconnect(self, user_id: str) -> None:
        """
        Remove a user's WebSocket connection and release their color.

        Does NOT close the WebSocket — call this after the connection
        is already closed or in an exception handler.
        """
        if user_id in self._connections:
            del self._connections[user_id]
            self.release_color(user_id)
            logger.info("WebSocket disconnected: user_id=%s (total: %d)", user_id, len(self._connections))

    async def send_to_user(self, user_id: str, data: dict[str, Any]) -> None:
        """Send a JSON message to a specific user."""
        ws = self._connections.get(user_id)
        if ws is not None:
            try:
                await ws.send_json(data)
            except Exception as exc:
                logger.warning("Failed to send to user %s: %s", user_id, exc)
                self.disconnect(user_id)

    async def broadcast(self, data: dict[str, Any], exclude_user: str | None = None) -> None:
        """
        Broadcast a JSON message to all connected clients.

        Args:
            data: JSON-serializable dict to send.
            exclude_user: Optional user_id to exclude from the broadcast
                          (e.g., the user who triggered the event).
        """
        disconnected: list[str] = []

        for user_id, ws in self._connections.items():
            if exclude_user and user_id == exclude_user:
                continue
            try:
                await ws.send_json(data)
            except Exception as exc:
                logger.warning("Broadcast failed for user %s: %s", user_id, exc)
                disconnected.append(user_id)

        # Clean up failed connections
        for user_id in disconnected:
            self.disconnect(user_id)

    async def broadcast_all(self, data: dict[str, Any]) -> None:
        """Broadcast a JSON message to ALL connected clients (no exclusions)."""
        await self.broadcast(data, exclude_user=None)