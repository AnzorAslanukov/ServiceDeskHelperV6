"""
Shared test fixtures for Feature #4 tests.

Self-contained — does not depend on the core tests/conftest.py.
Uses mock clients from core (read-only imports).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.clients.athena_client import AthenaClient
from src.services.assignment import AssignmentService
from src.services.ticket_classifier import TicketClassifier

from feature4.service import BulkAssignmentService


@pytest.fixture
def mock_athena_client() -> AsyncMock:
    """Create a mock AthenaClient with async methods."""
    client = AsyncMock(spec=AthenaClient)
    # Default: return empty paged results
    empty_paged = {"results": [], "total": 0, "page": 1, "page_size": 50, "has_more": False}
    client.search_tickets.return_value = empty_paged
    client.search_incidents.return_value = empty_paged
    client.search_service_requests.return_value = empty_paged
    client.search_change_requests.return_value = []
    client.get_incident.return_value = {}
    client.get_service_request.return_value = {}
    client.get_ticket.return_value = {}
    # Update methods
    client.update_incident.return_value = {}
    client.update_service_request.return_value = {}
    client.update_ticket.return_value = {}
    return client


@pytest.fixture
def mock_classifier() -> MagicMock:
    """Create a mock TicketClassifier."""
    classifier = MagicMock()
    classifier.predict.return_value = [
        {"support_group": "HUP", "confidence": 0.75},
        {"support_group": "User Provisioning", "confidence": 0.15},
        {"support_group": "Account Provisioning", "confidence": 0.05},
    ]
    return classifier


@pytest.fixture
def bulk_assignment_service(mock_athena_client, mock_classifier) -> BulkAssignmentService:
    """Create a BulkAssignmentService with mocked clients."""
    assignment_svc = AssignmentService(
        athena_client=mock_athena_client,
        classifier=mock_classifier,
    )
    return BulkAssignmentService(
        athena_client=mock_athena_client,
        assignment_service=assignment_svc,
    )


# ── Sample Data Fixtures ─────────────────────────────────────────────


@pytest.fixture
def sample_athena_ticket() -> dict:
    """A sample raw Athena incident ticket as returned by the API."""
    return {
        "id": "IR1959493",
        "entityId": "abc123-def456",
        "title": "Printer not working on 3rd floor",
        "description": "User reports that the HP LaserJet on 3rd floor Ravdin is not printing. Paper jam cleared but still not working.",
        "status": {"name": "Active", "id": "5e2d3932-ca6d-1515-7310-6f58584df73e"},
        "priority": 3,
        "supportGroup": {"name": "EUS\\HUP", "id": "some-guid"},
        "affectedUser": {"displayName": "John Smith", "userName": "smithjoh"},
        "assignedToUser": {"displayName": "Jane Doe", "userName": "doejane"},
        "createdDate": "2024-01-15T10:30:00Z",
        "contactMethod": "215-555-1234",
        "location": {"name": "HUP"},
        "floor": {"name": "3rd"},
    }