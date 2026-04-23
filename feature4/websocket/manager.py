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


class ConnectionManager:
    """
    Manages WebSocket connections for real-time queue synchronization.

    Each connected client is identified by a user_id. The manager tracks
    active connections and provides broadcast capabilities for lock/unlock/assign events.
    """

    def __init__(self) -> None:
        # user_id → WebSocket connection
        self._connections: dict[str, WebSocket] = {}

    @property
    def active_connections(self) -> dict[str, WebSocket]:
        """Return the current active connections (read-only view)."""
        return dict(self._connections)

    @property
    def connected_user_ids(self) -> list[str]:
        """Return list of currently connected user IDs."""
        return list(self._connections.keys())

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
        Remove a user's WebSocket connection.

        Does NOT close the WebSocket — call this after the connection
        is already closed or in an exception handler.
        """
        if user_id in self._connections:
            del self._connections[user_id]
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