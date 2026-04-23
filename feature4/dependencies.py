"""
FastAPI dependency injection wiring for Feature #4.

Provides singleton instances of BulkAssignmentService and ConnectionManager.
Imports core clients/services read-only from src/.
"""

# Read-only imports from core (never modify these source files)
from src.clients.athena_client import AthenaClient
from src.clients.databricks_client import DatabricksClient
from src.config import get_settings
from src.services.assignment import AssignmentService

from feature4.service import BulkAssignmentService
from feature4.websocket.manager import ConnectionManager


def _get_athena_client() -> AthenaClient:
    """Provide an AthenaClient instance."""
    return AthenaClient(get_settings())


def _get_databricks_client() -> DatabricksClient:
    """Provide a DatabricksClient instance."""
    return DatabricksClient(get_settings())


def _get_assignment_service() -> AssignmentService:
    """Provide an AssignmentService with injected clients."""
    return AssignmentService(
        athena_client=_get_athena_client(),
        databricks_client=_get_databricks_client(),
    )


# Singleton bulk assignment service (must be a singleton to preserve lock state)
_bulk_assignment_service: BulkAssignmentService | None = None


def get_bulk_assignment_service() -> BulkAssignmentService:
    """Provide a BulkAssignmentService singleton with injected clients."""
    global _bulk_assignment_service
    if _bulk_assignment_service is None:
        _bulk_assignment_service = BulkAssignmentService(
            athena_client=_get_athena_client(),
            assignment_service=_get_assignment_service(),
        )
    return _bulk_assignment_service


# Singleton WebSocket connection manager
_ws_manager: ConnectionManager | None = None


def get_ws_manager() -> ConnectionManager:
    """Provide a singleton ConnectionManager for WebSocket connections."""
    global _ws_manager
    if _ws_manager is None:
        _ws_manager = ConnectionManager()
    return _ws_manager