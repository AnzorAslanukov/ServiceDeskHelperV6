"""
Shared test fixtures for the Service Desk Helper test suite.
Provides mock clients and service instances for unit testing.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.clients.athena_client import AthenaClient
from src.clients.databricks_client import DatabricksClient
from src.services.assignment import AssignmentService
from src.services.chatbot import ChatbotService
from src.services.ticket_search import TicketSearchService
from src.services.turnover import TurnoverService


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
def mock_databricks_client() -> MagicMock:
    """
    Create a mock DatabricksClient.
    
    Note: SQL methods are synchronous, embedding methods are async.
    """
    client = MagicMock(spec=DatabricksClient)
    # Async methods need AsyncMock
    client.generate_embedding = AsyncMock(return_value=[0.1] * 1024)
    client.generate_embeddings = AsyncMock(return_value=[[0.1] * 1024])
    client.call_llm = AsyncMock(return_value="This is a mock LLM response.")
    client.close = AsyncMock()
    # Sync SQL methods
    client.find_similar_by_embedding.return_value = []
    client.find_similar_documentation.return_value = []
    client.get_ticket_embedding.return_value = None
    client.execute_query.return_value = []
    return client


@pytest.fixture
def search_service(mock_athena_client, mock_databricks_client) -> TicketSearchService:
    """Create a TicketSearchService with mocked clients."""
    return TicketSearchService(
        athena_client=mock_athena_client,
        databricks_client=mock_databricks_client,
    )


@pytest.fixture
def chatbot_service(mock_databricks_client) -> ChatbotService:
    """Create a ChatbotService with a mocked Databricks client."""
    return ChatbotService(databricks_client=mock_databricks_client)


@pytest.fixture
def assignment_service(mock_athena_client, mock_databricks_client) -> AssignmentService:
    """Create an AssignmentService with mocked clients."""
    return AssignmentService(
        athena_client=mock_athena_client,
        databricks_client=mock_databricks_client,
    )


@pytest.fixture
def turnover_service(mock_athena_client) -> TurnoverService:
    """Create a TurnoverService with a mocked AthenaClient."""
    return TurnoverService(athena_client=mock_athena_client)


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


@pytest.fixture
def sample_athena_tickets(sample_athena_ticket) -> list[dict]:
    """A list of sample Athena tickets."""
    ticket2 = {
        **sample_athena_ticket,
        "id": "IR1959494",
        "title": "Monitor flickering",
        "description": "Monitor on desk keeps flickering intermittently.",
        "status": {"name": "Resolved", "id": "2b8830b6-59f0-f574-9c2a-f4b4682f1681"},
    }
    return [sample_athena_ticket, ticket2]


@pytest.fixture
def sample_similar_results() -> list[dict]:
    """Sample results from a cosine similarity search."""
    return [
        {"id": "IR1959100", "similarity": 0.95},
        {"id": "IR1959101", "similarity": 0.91},
        {"id": "IR1959102", "similarity": 0.88},
        {"id": "IR1959103", "similarity": 0.85},
        {"id": "IR1959104", "similarity": 0.82},
    ]


@pytest.fixture
def sample_sev_ticket() -> dict:
    """A sample raw Athena SEV incident ticket for turnover tests."""
    return {
        "id": "IR10371854",
        "entityId": "sev-123-456",
        "title": "HUP Pavilion — Network outage affecting 5th floor",
        "description": "Multiple users reporting network connectivity issues on 5th floor HUP Pavilion.",
        "status": {"name": "Active", "id": "5e2d3932-ca6d-1515-7310-6f58584df73e"},
        "priority": 1,
        "urgency": {"name": "Urgent"},
        "impact": {"name": "Local Outage"},
        "tierQueue": {"name": "Technology\\Infrastructure"},
        "supportGroup": {"name": "Technology\\Infrastructure"},
        "affectedUser": {"displayName": "John Smith", "userName": "smithjoh"},
        "assignedToUser": {"displayName": "Jane Doe", "userName": "doejane"},
        "createdDate": "2026-04-13T10:30:00Z",
        "isParent": True,
        "escalated": False,
        "location": {"name": "HUP Pavilion"},
        "floor": {"name": "5th"},
    }


@pytest.fixture
def sample_change_request() -> dict:
    """A sample raw Athena change request for turnover tests."""
    return {
        "id": "CR10312956",
        "title": "PennChart March 2026 Update — Database Maintenance",
        "status": {"name": "In Progress", "id": "6d6c64dd-07ac-aaf5-f812-6a7cceb5154d"},
        "category": {"name": "Standard"},
        "risk": {"name": "Low"},
        "scheduledStartDate": "2026-04-14T02:00:00Z",
        "scheduledEndDate": "2026-04-14T06:00:00Z",
        "downtime": {"name": "Yes"},
        "scheduledDowntimeStartDate": "2026-04-14T02:00:00Z",
        "scheduledDowntimeEndDate": "2026-04-14T04:00:00Z",
        "description": "Scheduled database maintenance for PennChart update.",
    }


@pytest.fixture
def sample_documentation_results() -> list[dict]:
    """Sample results from documentation similarity search."""
    return [
        {
            "content": "## Printer Troubleshooting\n\n1. Check paper tray\n2. Clear paper jam\n3. Restart printer\n4. Check network connection",
            "notebook": "uphs_notebook",
            "section": "Helpdesk Printer Issues",
            "title": "HP LaserJet Troubleshooting",
            "similarity": 0.92,
        },
        {
            "content": "## Escalation Path\n\nIf printer issue persists after basic troubleshooting, escalate to EUS team.",
            "notebook": "uphs_notebook",
            "section": "Helpdesk Printer Issues",
            "title": "Printer Escalation Guide",
            "similarity": 0.87,
        },
    ]