"""
FastAPI dependency injection wiring.
Creates and provides singleton client and service instances.
"""

from functools import lru_cache

from fastapi import Request
from fastapi.responses import RedirectResponse

from src.config import Settings, get_settings
from src.clients.athena_client import AthenaClient
from src.clients.databricks_client import DatabricksClient
from src.services.assignment import AssignmentService
from src.services.auth import AuthService, AuthUser
from src.services.chatbot import ChatbotService
from src.services.ticket_search import TicketSearchService
from src.services.turnover import TurnoverService


@lru_cache
def _get_settings() -> Settings:
    """Cached settings instance."""
    return get_settings()


@lru_cache
def get_auth_service() -> AuthService:
    """Provide a singleton AuthService instance."""
    settings = _get_settings()
    return AuthService(
        ldap_server=settings.ldap_server,
        ldap_domain=settings.ldap_domain,
        allowed_groups=[g.strip() for g in settings.allowed_ad_groups.split(",")],
        allowed_usernames=[u.strip() for u in settings.allowed_usernames.split(",")],
        session_secret=settings.session_secret_key,
        session_expire_hours=settings.session_expire_hours,
    )


SESSION_COOKIE = "sdh_session"


def get_current_user(request: Request) -> AuthUser | None:
    """Extract and validate the current user from the session cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return get_auth_service().validate_session_token(token)


def require_auth(request: Request) -> AuthUser:
    """
    FastAPI dependency that requires authentication.
    Returns the current user or raises a redirect to /login.
    """
    user = get_current_user(request)
    if user is None:
        # We can't raise a redirect from a dependency directly in FastAPI,
        # so we use a sentinel that middleware will catch.
        raise AuthRequired()
    return user


class AuthRequired(Exception):
    """Raised when authentication is required but not present."""
    pass


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
    )


def get_turnover_service() -> TurnoverService:
    """Provide a TurnoverService with injected AthenaClient."""
    return TurnoverService(
        athena_client=get_athena_client(),
    )
