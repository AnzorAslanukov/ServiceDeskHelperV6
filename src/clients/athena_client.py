"""
Async HTTP client for the Athena ticketing system REST API.
Handles OAuth2 authentication, token caching, and ticket operations.
"""

import time
from typing import Any

import httpx

from src.config import Settings


class AthenaClient:
    """Async client for Athena ITSM API with automatic token management."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._http_client: httpx.AsyncClient | None = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the async HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def _authenticate(self) -> str:
        """Acquire a new OAuth2 JWT token from Athena."""
        client = await self._get_http_client()
        response = await client.post(
            self._settings.athena_auth_url,
            data={
                "username": self._settings.athena_username,
                "password": self._settings.athena_password,
                "grant_type": "password",
                "client_id": self._settings.athena_client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        token_data = response.json()
        self._token = token_data["access_token"]
        # Cache token for slightly less than its lifetime (default 50 min buffer)
        expires_in = token_data.get("expires_in", 3600)
        self._token_expiry = time.time() + expires_in - 60
        return self._token

    async def _get_token(self) -> str:
        """Return a valid token, refreshing if expired."""
        if self._token is None or time.time() >= self._token_expiry:
            await self._authenticate()
        return self._token  # type: ignore[return-value]

    async def _auth_headers(self) -> dict[str, str]:
        """Build authorization headers with a valid token."""
        token = await self._get_token()
        return {
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
        }

    # ── Ticket Retrieval ──────────────────────────────────────────────

    async def get_incident(self, ticket_id: str) -> dict[str, Any]:
        """Retrieve a single incident by ticket ID (e.g., 'IR1959493')."""
        client = await self._get_http_client()
        headers = await self._auth_headers()
        url = f"{self._settings.athena_incident_url}{ticket_id}"
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    async def get_service_request(self, ticket_id: str) -> dict[str, Any]:
        """Retrieve a single service request by ticket ID (e.g., 'SR1959584')."""
        client = await self._get_http_client()
        headers = await self._auth_headers()
        url = f"{self._settings.athena_servicerequest_url}{ticket_id}"
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Retrieve a ticket by ID, auto-detecting type from prefix."""
        if ticket_id.upper().startswith("IR"):
            return await self.get_incident(ticket_id)
        elif ticket_id.upper().startswith("SR"):
            return await self.get_service_request(ticket_id)
        else:
            raise ValueError(f"Unknown ticket type prefix in '{ticket_id}'. Expected IR or SR.")

    # ── View Filter Queries ───────────────────────────────────────────

    async def search_incidents(
        self,
        filters: list[dict[str, Any]],
        page: int = 1,
        page_size: int = 50,
        expand: str | None = None,
    ) -> dict[str, Any]:
        """
        Search incidents using the advanced view filter endpoint.
        
        Args:
            filters: JSON filter array for POST /v1/view/workitem?type=incident
            page: Page number (1-based).
            page_size: Number of results per page.
            expand: Comma-separated relationships to include (e.g., 'affectedUser').
            
        Returns:
            Dict with 'results', 'total', 'page', 'page_size', 'has_more'.
        """
        client = await self._get_http_client()
        headers = await self._auth_headers()
        # Build URL with pagination query params
        url = self._settings.athena_incident_view_url
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}$skip={( page - 1) * page_size}&$top={page_size}"
        if expand:
            url = f"{url}&$expand={expand}"
        response = await client.post(
            url,
            headers=headers,
            json=filters,
        )
        response.raise_for_status()
        data = response.json()
        return self._parse_paged_response(data, page, page_size)

    async def search_service_requests(
        self,
        filters: list[dict[str, Any]],
        page: int = 1,
        page_size: int = 50,
        expand: str | None = None,
    ) -> dict[str, Any]:
        """
        Search service requests using the advanced view filter endpoint.
        
        Args:
            filters: JSON filter array for POST /v1/view/workitem?type=servicerequest
            page: Page number (1-based).
            page_size: Number of results per page.
            expand: Comma-separated relationships to include (e.g., 'affectedUser').
            
        Returns:
            Dict with 'results', 'total', 'page', 'page_size', 'has_more'.
        """
        client = await self._get_http_client()
        headers = await self._auth_headers()
        url = self._settings.athena_servicerequest_view_url
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}$skip={(page - 1) * page_size}&$top={page_size}"
        if expand:
            url = f"{url}&$expand={expand}"
        response = await client.post(
            url,
            headers=headers,
            json=filters,
        )
        response.raise_for_status()
        data = response.json()
        return self._parse_paged_response(data, page, page_size)

    async def search_tickets(
        self,
        filters: list[dict[str, Any]],
        ticket_type: str = "incident",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """
        Search tickets by type using the view filter endpoint.
        
        Args:
            filters: JSON filter array.
            ticket_type: 'incident' or 'servicerequest'.
            page: Page number (1-based).
            page_size: Number of results per page.
            
        Returns:
            Dict with 'results', 'total', 'page', 'page_size', 'has_more'.
        """
        if ticket_type == "incident":
            return await self.search_incidents(filters, page, page_size)
        elif ticket_type == "servicerequest":
            return await self.search_service_requests(filters, page, page_size)
        else:
            raise ValueError(f"Unsupported ticket_type: '{ticket_type}'. Use 'incident' or 'servicerequest'.")

    # ── Change Request Queries ────────────────────────────────────────

    async def search_change_requests(self, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Search change requests using the advanced view filter endpoint.

        Args:
            filters: JSON filter array for POST /v1/view/workitem?type=changeRequest

        Returns:
            List of matching change request records.
        """
        client = await self._get_http_client()
        headers = await self._auth_headers()
        # Build the CR view URL from base URL if not configured
        cr_view_url = self._settings.athena_changerequest_view_url
        if not cr_view_url:
            cr_view_url = f"{self._settings.athena_base_url}v1/view/workitem?type=changeRequest"
        response = await client.post(
            cr_view_url,
            headers=headers,
            json=filters,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data if isinstance(data, list) else []

    # ── Filter Builders ───────────────────────────────────────────────

    @staticmethod
    def build_sev_filter(
        statuses: list[str],
        priorities: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build a JSON filter for P1/P2 SEV incidents with given statuses.

        Uses nested 'or' for priority values and 'ne' for status exclusion,
        which is the syntax confirmed to work with the Athena view endpoint.

        Args:
            statuses: List of status values to match (e.g., ['Active', 'Work in Progress']).
            priorities: List of priority values (default: ['1', '2'] for P1/P2).

        Returns:
            Filter array suitable for POST /v1/view/workitem?type=incident.
        """
        if priorities is None:
            priorities = ["1", "2"]

        # Build priority OR conditions
        priority_filters: list[dict[str, Any]] = [
            {
                "condition": "and",
                "property": "Priority",
                "operator": "eq",
                "value": p,
            }
            for p in priorities
        ]

        # Build status conditions
        status_filters: list[dict[str, Any]] = [
            {
                "condition": "and",
                "property": "Status",
                "operator": "eq",
                "value": s,
            }
            for s in statuses
        ]

        # Combine: (P1 OR P2) AND (status1 OR status2 ...)
        filters: list[dict[str, Any]] = [
            {
                "condition": "and",
                "filters": [
                    {"condition": "or", "filters": priority_filters},
                    {"condition": "or", "filters": status_filters},
                ],
            }
        ]

        return filters

    @staticmethod
    def build_upcoming_cr_filter(hours_ahead: int = 24) -> list[dict[str, Any]]:
        """
        Build a JSON filter for upcoming change requests.

        Args:
            hours_ahead: How many hours ahead to look for scheduled CRs.

        Returns:
            Filter array suitable for POST /v1/view/workitem?type=changeRequest.
        """
        return [
            {
                "condition": "and",
                "filters": [
                    {
                        "condition": "and",
                        "property": "ScheduledStartDate",
                        "operator": "gt",
                        "value": "[now]",
                    },
                    {
                        "condition": "and",
                        "property": "ScheduledStartDate",
                        "operator": "lt",
                        "value": f"[now]+{hours_ahead}h",
                    },
                    {
                        "condition": "and",
                        "property": "Status",
                        "operator": "ne",
                        "value": "Completed",
                    },
                    {
                        "condition": "and",
                        "property": "Status",
                        "operator": "ne",
                        "value": "Failed",
                    },
                    {
                        "condition": "and",
                        "property": "Status",
                        "operator": "ne",
                        "value": "Cancelled",
                    },
                    {
                        "condition": "and",
                        "property": "Status",
                        "operator": "ne",
                        "value": "Closed",
                    },
                ],
            }
        ]

    @staticmethod
    def build_field_filter(field: str, value: str, operator: str = "eq") -> list[dict[str, Any]]:
        """
        Build a JSON filter array for a single field condition.
        
        Args:
            field: Property name (e.g., 'contactMethod', 'supportGroup').
            value: Value to match.
            operator: Filter operator ('eq', 'contains', 'like', etc.).
            
        Returns:
            Filter array suitable for POST /v1/view/workitem.
        """
        return [
            {
                "condition": "and",
                "filters": [
                    {
                        "condition": "and",
                        "property": field,
                        "operator": operator,
                        "value": value,
                    }
                ],
            }
        ]

    @staticmethod
    def build_description_filter(text: str) -> list[dict[str, Any]]:
        """
        Build a JSON filter to search for text within the description field.

        Splits multi-word input into individual words and creates a 'contains'
        condition for each word, joined with AND. This means the description
        must contain ALL words, but they don't need to be adjacent.

        Single-word input produces a single 'contains' condition (unchanged behavior).

        Args:
            text: One or more words to search for in ticket descriptions.

        Returns:
            Filter array using 'contains' operators on description (one per word).
        """
        words = text.strip().split()
        word_filters: list[dict[str, Any]] = [
            {
                "condition": "and",
                "property": "Description",
                "operator": "contains",
                "value": word,
            }
            for word in words
        ]
        return [
            {
                "condition": "and",
                "filters": word_filters,
            }
        ]

    # ── Ticket Updates ─────────────────────────────────────────────────

    async def update_incident(
        self,
        entity_id: str,
        tier_queue_guid: str | None = None,
        priority: int | str | None = None,
    ) -> dict[str, Any]:
        """
        Update an incident via PUT /v1/incident/.

        The Athena API requires entityId in the body and uses 'tierQueue'
        (not 'supportGroup') for the support group assignment.

        Args:
            entity_id: The ticket's entityId GUID (required by Athena).
            tier_queue_guid: GUID of the target support group/tier queue.
            priority: Priority level (int for IR, e.g., 3).

        Returns:
            Updated incident data from Athena.
        """
        client = await self._get_http_client()
        headers = await self._auth_headers()

        payload: dict[str, Any] = {"entityId": entity_id}
        if tier_queue_guid is not None:
            payload["tierQueue"] = {"id": tier_queue_guid}
        if priority is not None:
            payload["priority"] = priority

        response = await client.put(
            self._settings.athena_incident_url,
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def update_service_request(
        self,
        entity_id: str,
        tier_queue_guid: str | None = None,
        priority: int | str | None = None,
    ) -> dict[str, Any]:
        """
        Update a service request via PUT /v1/servicerequest/.

        Args:
            entity_id: The ticket's entityId GUID (required by Athena).
            tier_queue_guid: GUID of the target support group/tier queue.
            priority: Priority level (string for SR, e.g., 'Medium').

        Returns:
            Updated service request data from Athena.
        """
        client = await self._get_http_client()
        headers = await self._auth_headers()

        payload: dict[str, Any] = {"entityId": entity_id}
        if tier_queue_guid is not None:
            payload["supportGroup"] = {"id": tier_queue_guid}
        if priority is not None:
            payload["priority"] = priority

        response = await client.put(
            self._settings.athena_servicerequest_url,
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def update_ticket(
        self,
        ticket_id: str,
        entity_id: str,
        tier_queue_guid: str | None = None,
        priority: int | str | None = None,
    ) -> dict[str, Any]:
        """
        Update a ticket, auto-detecting type from the ID prefix.

        Args:
            ticket_id: Ticket ID (e.g., 'IR1959493' or 'SR1959584') for type detection.
            entity_id: The ticket's entityId GUID (required by Athena PUT).
            tier_queue_guid: GUID of the target support group/tier queue.
            priority: Priority level.

        Returns:
            Updated ticket data from Athena.
        """
        if ticket_id.upper().startswith("IR"):
            return await self.update_incident(entity_id, tier_queue_guid, priority)
        elif ticket_id.upper().startswith("SR"):
            return await self.update_service_request(entity_id, tier_queue_guid, priority)
        else:
            raise ValueError(f"Unknown ticket type prefix in '{ticket_id}'. Expected IR or SR.")

    # ── Filter Builders (Queue) ────────────────────────────────────────

    @staticmethod
    def build_queue_filter(
        tier_queue_name: str,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build a JSON filter to fetch tickets from a specific tier queue.

        Args:
            tier_queue_name: Support group / tier queue name (e.g., 'Validation').
            statuses: Optional list of statuses to include. If None, fetches
                      Active and Work in Progress tickets.

        Returns:
            Filter array suitable for POST /v1/view/workitem.
        """
        if statuses is None:
            statuses = ["Active", "Work in Progress"]

        status_filters: list[dict[str, Any]] = [
            {
                "condition": "and",
                "property": "Status",
                "operator": "eq",
                "value": s,
            }
            for s in statuses
        ]

        return [
            {
                "condition": "and",
                "filters": [
                    {
                        "condition": "and",
                        "property": "TierQueue",
                        "operator": "eq",
                        "value": tier_queue_name,
                    },
                    {"condition": "or", "filters": status_filters},
                ],
            }
        ]

    # ── Enum Lookups ──────────────────────────────────────────────────

    async def get_enum_tree(self, enum_id: str) -> list[dict[str, Any]]:
        """
        Fetch an enum tree from Athena.

        Args:
            enum_id: The GUID of the enum to fetch (e.g., support group enum).

        Returns:
            List of enum tree nodes, each with 'id', 'name', and optional 'children'.
        """
        client = await self._get_http_client()
        headers = await self._auth_headers()
        url = f"{self._settings.athena_base_url}v1/enums/tree/{enum_id}"
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    async def resolve_support_group_guid(
        self,
        group_name: str,
        ticket_type: str,
    ) -> str | None:
        """
        Resolve a support group name to its GUID by searching the enum tree.

        Performs a case-insensitive search through the hierarchical support
        group enum. Matches against full path names (e.g., 'Service Desk\\Validation')
        as well as leaf names (e.g., 'Validation').

        Args:
            group_name: Support group name to look up.
            ticket_type: 'incident' or 'servicerequest' (determines which enum to query).

        Returns:
            The GUID of the matching support group, or None if not found.
        """
        if ticket_type == "incident":
            enum_id = self._settings.athena_ir_support_group_guid
        elif ticket_type == "servicerequest":
            enum_id = self._settings.athena_sr_support_group_guid
        else:
            raise ValueError(f"Unsupported ticket_type: '{ticket_type}'")

        tree = await self.get_enum_tree(enum_id)
        return self._search_enum_tree(tree, group_name.lower())

    @staticmethod
    def _search_enum_tree(
        nodes: list[dict[str, Any]],
        target_lower: str,
        parent_path: str = "",
    ) -> str | None:
        """
        Recursively search an enum tree for a node matching the target name.

        Matches against:
        - Full path (e.g., 'service desk\\validation')
        - Node name alone (e.g., 'validation')

        Args:
            nodes: List of enum tree nodes.
            target_lower: Lowercased target name to match.
            parent_path: Accumulated path from parent nodes.

        Returns:
            The GUID of the first matching node, or None.
        """
        for node in nodes:
            name = node.get("name", "")
            node_id = node.get("id", "")
            full_path = f"{parent_path}\\{name}" if parent_path else name

            # Match against full path or just the node name
            if name.lower() == target_lower or full_path.lower() == target_lower:
                return node_id

            # Recurse into children
            children = node.get("children") or node.get("childEnumValues") or []
            if children:
                result = AthenaClient._search_enum_tree(children, target_lower, full_path)
                if result:
                    return result

        return None

    # ── Response Parsing ──────────────────────────────────────────────

    @staticmethod
    def _parse_paged_response(
        data: Any,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        """
        Parse a paged response from the Athena view endpoint.

        The Athena view endpoint returns:
            {currentPage, resultCount, pageSize, hasMoreResults, result}

        Args:
            data: Raw JSON response from Athena.
            page: Requested page number.
            page_size: Requested page size.

        Returns:
            Normalized dict with 'results', 'total', 'page', 'page_size', 'has_more'.
        """
        if isinstance(data, dict) and "result" in data:
            return {
                "results": data.get("result", []),
                "total": data.get("resultCount", len(data.get("result", []))),
                "page": page,
                "page_size": page_size,
                "has_more": data.get("hasMoreResults", False),
            }
        # Fallback for unexpected response shapes
        results = data if isinstance(data, list) else []
        return {
            "results": results,
            "total": len(results),
            "page": page,
            "page_size": page_size,
            "has_more": False,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None