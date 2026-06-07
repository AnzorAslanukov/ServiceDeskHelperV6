"""
Ticket Search Service — business logic for Feature #1: Enhanced Ticket Search.

Provides four search modes:
1. Field match — search by any Athena ticket field
2. Description match — substring search in description
3. Semantic search — natural language query via embeddings
4. Ticket similarity — find tickets similar to a given ticket ID
"""

import asyncio
import re
from datetime import datetime
from typing import Any

from src.clients.athena_client import AthenaClient
from src.clients.databricks_client import DatabricksClient
from src.models.search import (
    DocumentationResult,
    FieldSearchResponse,
    SemanticSearchResponse,
    SimilarTicketResponse,
    SimilarTicketResult,
    TicketSummary,
)


class TicketSearchService:
    """Orchestrates search operations across Athena and Databricks."""

    def __init__(self, athena_client: AthenaClient, databricks_client: DatabricksClient) -> None:
        self._athena = athena_client
        self._databricks = databricks_client

    # ── Mode 1: Field Match ───────────────────────────────────────────

    async def search_by_field(
        self,
        field: str,
        value: str,
        ticket_type: str = "incident",
        operator: str = "eq",
        page: int = 1,
        page_size: int = 50,
    ) -> FieldSearchResponse:
        """
        Search tickets by a specific field value using Athena view filters.

        Args:
            field: Athena property name (e.g., 'contactMethod', 'supportGroup').
            value: Value to match.
            ticket_type: 'incident' or 'servicerequest'.
            operator: Filter operator ('eq', 'contains', 'like', etc.).
            page: Page number (1-based).
            page_size: Number of results per page.

        Returns:
            FieldSearchResponse with matching tickets and pagination metadata.
        """
        filters = AthenaClient.build_field_filter(field, value, operator)
        paged = await self._athena.search_tickets(filters, ticket_type, page, page_size)
        tickets = [self._map_ticket(t) for t in paged["results"]]
        return FieldSearchResponse(
            tickets=tickets,
            total=paged["total"],
            page=paged["page"],
            page_size=paged["page_size"],
            has_more=paged["has_more"],
        )

    # ── Mode 2: Description Match ─────────────────────────────────────

    async def search_by_description(
        self,
        text: str,
        ticket_type: str = "incident",
        page: int = 1,
        page_size: int = 50,
    ) -> FieldSearchResponse:
        """
        Search tickets by substring match in the description field.

        Args:
            text: Substring to search for in descriptions.
            ticket_type: 'incident' or 'servicerequest'.
            page: Page number (1-based).
            page_size: Number of results per page.

        Returns:
            FieldSearchResponse with matching tickets and pagination metadata.
        """
        filters = AthenaClient.build_description_filter(text)
        paged = await self._athena.search_tickets(filters, ticket_type, page, page_size)
        tickets = [self._map_ticket(t) for t in paged["results"]]
        return FieldSearchResponse(
            tickets=tickets,
            total=paged["total"],
            page=paged["page"],
            page_size=paged["page_size"],
            has_more=paged["has_more"],
        )

    # ── Mode 3: Semantic Search ───────────────────────────────────────

    async def semantic_search(
        self,
        query: str,
        top_k: int = 10,
    ) -> SemanticSearchResponse:
        """
        Perform natural-language semantic search across historical tickets
        and knowledge base documentation.

        1. Generate embedding for the query text
        2. Search ir_embeddings for similar tickets (cosine similarity)
        3. Search onenote_documentation for relevant KB articles

        Args:
            query: Natural language description of the issue.
            top_k: Number of similar tickets to return.

        Returns:
            SemanticSearchResponse with similar tickets and documentation matches.
        """
        # Step 1: Generate embedding
        query_embedding = await self._databricks.generate_embedding(query)

        # Step 2 & 3: Run both similarity searches in parallel via thread executor
        # (databricks SQL connector is synchronous)
        loop = asyncio.get_event_loop()

        ticket_results, doc_results = await asyncio.gather(
            loop.run_in_executor(
                None,
                self._databricks.find_similar_by_embedding,
                query_embedding,
                "hive_metastore.embeddings_db.ticket_embeddings",
                "embedding",
                "Id",
                top_k,
            ),
            loop.run_in_executor(
                None,
                self._databricks.find_similar_documentation,
                query_embedding,
                5,
            ),
        )

        similar_tickets = [
            SimilarTicketResult(id=r["id"], similarity=r["similarity"])
            for r in ticket_results
        ]

        # Fetch titles for the similar tickets in parallel
        similar_tickets = await self._enrich_similar_tickets_with_titles(similar_tickets)

        documentation = [
            DocumentationResult(
                content=r["content"],
                notebook=r["notebook"],
                section=r["section"],
                title=r["title"],
                similarity=r["similarity"],
            )
            for r in doc_results
        ]

        return SemanticSearchResponse(
            similar_tickets=similar_tickets,
            documentation=documentation,
        )

    # ── Mode 4: Ticket Similarity ─────────────────────────────────────

    async def find_similar_tickets(
        self,
        ticket_id: str,
        top_k: int = 10,
    ) -> SimilarTicketResponse:
        """
        Find tickets similar to a given ticket ID.

        1. Fetch the ticket from Athena (auto-detects IR vs SR from prefix)
        2. Build search text from title + description
        3. Generate an embedding on-the-fly via Databricks GTE-Large-EN
        4. Run cosine similarity against all ticket embeddings in ir_embeddings

        Args:
            ticket_id: Ticket ID (e.g., 'IR1959493' or 'SR10393291').
            top_k: Number of similar tickets to return.

        Returns:
            SimilarTicketResponse with the source ticket ID and similar matches.

        Raises:
            ValueError: If the ticket is not found in Athena or has no usable content.
        """
        # Step 1: Fetch ticket from Athena (handles IR vs SR via prefix)
        ticket = await self._athena.get_ticket(ticket_id)

        if not ticket:
            raise ValueError(f"Ticket '{ticket_id}' not found in Athena.")

        # Step 2: Build search text from title and description
        title = ticket.get("title") or ""
        description = ticket.get("description") or ""
        search_text = f"{title} {description}".strip()

        if not search_text:
            raise ValueError(
                f"Ticket '{ticket_id}' has no title or description to generate an embedding."
            )

        # Step 3: Generate embedding on-the-fly
        embedding = await self._databricks.generate_embedding(search_text)

        # Step 4: Find similar tickets (request top_k + 1 to exclude self)
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            self._databricks.find_similar_by_embedding,
            embedding,
            "hive_metastore.embeddings_db.ticket_embeddings",
            "embedding",
            "Id",
            top_k + 1,
        )

        # Filter out the source ticket itself
        similar_tickets = [
            SimilarTicketResult(id=r["id"], similarity=r["similarity"])
            for r in results
            if r["id"] != ticket_id
        ][:top_k]

        # Fetch titles for the similar tickets in parallel
        similar_tickets = await self._enrich_similar_tickets_with_titles(similar_tickets)

        return SimilarTicketResponse(
            source_ticket_id=ticket_id,
            similar_tickets=similar_tickets,
        )

    # ── Helpers ───────────────────────────────────────────────────────

    async def _enrich_similar_tickets_with_titles(
        self, tickets: list[SimilarTicketResult]
    ) -> list[SimilarTicketResult]:
        """Fetch titles from Athena for a list of similar ticket results.

        Fetches tickets in parallel and populates the title field.
        If a fetch fails for a ticket, its title remains None.
        """
        if not tickets:
            return tickets

        async def _fetch_title(ticket_id: str) -> str | None:
            try:
                raw = await self._athena.get_ticket(ticket_id)
                if raw:
                    return raw.get("title")
            except Exception:
                pass
            return None

        titles = await asyncio.gather(
            *[_fetch_title(t.id) for t in tickets]
        )

        return [
            SimilarTicketResult(id=t.id, title=title, similarity=t.similarity)
            for t, title in zip(tickets, titles)
        ]

    @staticmethod
    def _extract_name(value: Any) -> str | None:
        """Extract a human-readable name from an Athena field value.

        Handles:
        - dict with ``name`` or ``displayName`` key
        - plain non-GUID string
        - Returns None for bare GUIDs or non-string/non-dict values
        """
        if value is None:
            return None
        if isinstance(value, dict):
            return value.get("name") or value.get("displayName")
        if isinstance(value, str):
            if TicketSearchService._is_guid(value):
                return None
            return value
        return str(value)

    @staticmethod
    def _format_date(raw_date: str | None) -> str | None:
        """Format an Athena ISO date string to HH:MM MM/DD/YYYY.

        Handles formats:
        - ``2024-01-15T10:30:00Z``
        - ``2024-01-15T10:30:00.000Z``
        - ``2024-01-15T10:30:00``

        Returns the original string if parsing fails.
        """
        if not raw_date:
            return None

        # Try common Athena date formats
        for fmt in (
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                dt = datetime.strptime(raw_date, fmt)
                return dt.strftime("%H:%M %m/%d/%Y")
            except ValueError:
                continue

        # Return raw string as fallback (don't lose data)
        return raw_date

    @staticmethod
    def _is_guid(value: str) -> bool:
        """Check if a string looks like a GUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)."""
        return bool(re.match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
            value,
        ))

    @staticmethod
    def _extract_field(raw: dict[str, Any], field: str, value_suffix: str = "Value") -> Any:
        """
        Extract a human-readable value for a field, handling both response formats.

        The Athena API returns different formats depending on the endpoint:
        - **View endpoint** (POST /v1/view/workitem): flat format with GUID in the
          field and a companion ``*Value`` field with the human-readable name.
          E.g. ``status = "9acc..."`` and ``statusValue = "Work in Progress"``.
        - **Object/detail endpoint** (GET /v1/incident/{id}): nested dict format.
          E.g. ``status = {"id": "9acc...", "name": "Work in Progress"}``.

        Resolution order:
        1. ``<field>Value`` companion field (view endpoint flat format)
        2. Dict with ``name`` or ``displayName`` key (object endpoint nested format)
        3. Raw string value, but only if it's NOT a GUID
        4. ``None`` if the value is a bare GUID or missing
        """
        # 1. Check for *Value companion field (view endpoint flat format)
        value_key = f"{field}{value_suffix}"
        companion = raw.get(value_key)
        if companion is not None:
            return companion

        # 2. Check the field itself
        val = raw.get(field)
        if val is None:
            return None

        # 3. If it's a dict, extract name/displayName
        if isinstance(val, dict):
            return val.get("name") or val.get("displayName")

        # 4. If it's a string, reject bare GUIDs
        if isinstance(val, str) and TicketSearchService._is_guid(val):
            return None

        return val

    @staticmethod
    def _map_ticket(raw: dict[str, Any]) -> TicketSummary:
        """Map a raw Athena ticket dict to a TicketSummary model.

        Handles both the view endpoint flat format (GUID + *Value fields)
        and the object endpoint nested dict format.
        """
        # Status: view endpoint has statusValue, object endpoint has status dict
        status = TicketSearchService._extract_field(raw, "status")

        # Support group: view endpoint has supportGroupValue, object endpoint has dict.
        # Also fall back to tierQueue which is used for incidents.
        support_group = TicketSearchService._extract_field(raw, "supportGroup")
        if support_group is None:
            support_group = TicketSearchService._extract_field(raw, "tierQueue")

        # Affected user: view endpoint uses affectedUser_DisplayName (flat),
        # object endpoint uses affectedUser dict with displayName/userName.
        affected_user = raw.get("affectedUser_DisplayName")
        if affected_user is None:
            au = raw.get("affectedUser")
            if isinstance(au, dict):
                affected_user = au.get("displayName") or au.get("userName")
            elif isinstance(au, str) and not TicketSearchService._is_guid(au):
                affected_user = au

        # Priority: IR uses numeric, SR view endpoint has GUID + priorityValue companion
        priority = TicketSearchService._extract_field(raw, "priority")

        # Location: view endpoint has locationValue, object endpoint has dict
        location = TicketSearchService._extract_field(raw, "location")

        # Description: truncate if too long
        description = raw.get("description") or None
        if description and len(description) > 500:
            description = description[:500] + "..."

        # Format created date to MM-DD-YYYY HH:MM
        created_date = TicketSearchService._format_date(raw.get("createdDate"))

        return TicketSummary(
            id=raw.get("id", raw.get("name", "unknown")),
            title=raw.get("title"),
            status=status,
            priority=priority,
            support_group=support_group,
            affected_user=affected_user,
            created_date=created_date,
            description=description,
            location=location,
        )
