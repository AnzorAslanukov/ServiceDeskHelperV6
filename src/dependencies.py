"""
FastAPI dependency injection wiring.
Creates and provides singleton client and service instances.
"""

from functools import lru_cache

from src.config import Settings, get_settings
from src.clients.athena_client import AthenaClient
from src.clients.databricks_client import DatabricksClient
from src.services.assignment import AssignmentService
from src.services.chatbot import ChatbotService
from src.services.ticket_search import TicketSearchService
from src.services.turnover import TurnoverService


@lru_cache
def _get_settings() -> Settings:
    """Cached settings instance."""
    return get_settings()


def get_athena_client() -> AthenaClient:
    """Provide an AthenaClient instance."""
    return AthenaClient(_get_settings())


def get_databricks_client() -> DatabricksClient:
    """Provide a DatabricksClient instance."""
    return DatabricksClient(_get_settings())


def get_search_service() -> TicketSearchService:
    """Provide a TicketSearchService with injected clients."""
    return TicketSearchService(
        athena_client=get_athena_client(),
        databricks_client=get_databricks_client(),
    )


# Singleton chatbot service (must be a singleton to preserve session state)
_chatbot_service: ChatbotService | None = None


def get_chatbot_service() -> ChatbotService:
    """Provide a ChatbotService singleton with injected Databricks client."""
    global _chatbot_service
    if _chatbot_service is None:
        _chatbot_service = ChatbotService(
            databricks_client=get_databricks_client(),
        )
    return _chatbot_service


def get_assignment_service() -> AssignmentService:
    """Provide an AssignmentService with injected clients."""
    return AssignmentService(
        athena_client=get_athena_client(),
        databricks_client=get_databricks_client(),
    )


def get_turnover_service() -> TurnoverService:
    """Provide a TurnoverService with injected AthenaClient."""
    return TurnoverService(
        athena_client=get_athena_client(),
    )
