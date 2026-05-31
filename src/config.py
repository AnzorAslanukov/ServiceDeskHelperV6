"""
Application configuration using Pydantic Settings.
Loads all environment variables from .env with validation.
"""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Athena API
    athena_client_id: str = Field(alias="ATHENA_CLIENT_ID")
    athena_username: str = Field(alias="ATHENA_USERNAME")
    athena_password: str = Field(alias="ATHENA_PASSWORD")
    athena_base_url: str = Field(alias="ATHENA_BASE_URL")
    athena_auth_url: str = Field(alias="ATHENA_AUTH_URL")
    athena_incident_view_url: str = Field(alias="ATHENA_INCIDENT_VIEW_URL")
    athena_servicerequest_view_url: str = Field(alias="ATHENA_SERVICEREQUEST_VIEW_URL")
    athena_incident_url: str = Field(alias="ATHENA_INCIDENT_URL")
    athena_servicerequest_url: str = Field(alias="ATHENA_SERVICEREQUEST_URL")
    athena_changerequest_url: str = Field(alias="ATHENA_CHANGEREQUEST_URL")
    athena_changerequest_view_url: str = Field(
        default="",
        alias="ATHENA_CHANGEREQUEST_VIEW_URL",
    )
    athena_ir_support_group_guid: str = Field(alias="ATHENA_IR_SUPPORT_GROUP_GUID")
    athena_sr_support_group_guid: str = Field(alias="ATHENA_SR_SUPPORT_GROUP_GUID")
    athena_json_template: str = Field(alias="ATHENA_JSON_TEMPLATE")

    # Databricks API
    databricks_api_key: str = Field(alias="DATABRICKS_API_KEY")
    databricks_sonnet_url: str = Field(alias="DATABRICKS_SONNET_4.5_URL")
    databricks_embedding_url: str = Field(alias="DATABRICKS_EMBEDDING_URL")
    databricks_server_hostname: str = Field(alias="DATABRICKS_SERVER_HOSTNAME")
    databricks_http_path: str = Field(alias="DATABRICKS_HTTP_PATH")

    # Authentication (LDAP)
    ldap_server: str = Field(default="ldap://uphs.pennhealth.prv", alias="LDAP_SERVER")
    ldap_domain: str = Field(default="UPHS", alias="LDAP_DOMAIN")
    allowed_ad_groups: str = Field(
        default="IS SD Team,Athena Users", alias="ALLOWED_AD_GROUPS"
    )
    allowed_usernames: str = Field(default="aslanuka", alias="ALLOWED_USERNAMES")
    session_secret_key: str = Field(alias="SESSION_SECRET_KEY")
    session_expire_hours: float = Field(default=12.0, alias="SESSION_EXPIRE_HOURS")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "populate_by_name": True,
    }


def get_settings() -> Settings:
    """Factory function for Settings. Used as a FastAPI dependency."""
    return Settings()