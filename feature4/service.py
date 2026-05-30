"""
Bulk Assignment Service — business logic for Feature #4.

Orchestrates:
- Queue fetching (IR + SR from Validation tier queue)
- In-memory ticket locking
- Batch AI recommendations (reuses Feature #3 AssignmentService)
- Bulk ticket assignment via Athena PUT
- Support group list retrieval for manual assignment
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

# Read-only imports from core (never modify these source files)
from src.clients.athena_client import AthenaClient
from src.models.assignment import AssignmentRecommendation, TicketInfo
from src.services.assignment import AssignmentService

from feature4.models import (
    BulkAssignResponse,
    BulkRecommendResponse,
    QueueResponse,
    QueueTicketSummary,
    TicketAssignResult,
    TicketAssignment,
    TicketRecommendation,
)

logger = logging.getLogger(__name__)


class BulkAssignmentService:
    """
    Manages the bulk ticket recommendation and assignment workflow.

    Holds in-memory lock state for ticket-level locking across
    concurrent WebSocket-connected users.
    """

    # ── Queue GUID Mappings ───────────────────────────────────────────
    # IR and SR use different GUIDs for the same queue name.
    # The Athena view endpoint requires GUIDs (not names) for TierQueue
    # and supportGroup filter values.

    QUEUE_GUIDS: dict[str, dict[str, str]] = {
        "Validation": {
            "ir": "1a59b3b9-84a3-13ce-f50c-79b8a99f5531",  # Service Desk\Validation IR
            "sr": "c954d465-65a0-9e43-9b02-b353e87bdb37",  # Service Desk\Validation SR
        },
    }

    # ── Status GUID Mappings ──────────────────────────────────────────
    # The Athena view endpoint returns status as GUIDs and CANNOT combine
    # TierQueue/supportGroup with Status filters (returns 500 error).
    # Therefore, status filtering MUST be done client-side using these GUIDs.

    # IR statuses considered "open" (actionable in the Validation queue)
    IR_OPEN_STATUS_GUIDS: set[str] = {
        "5e2d3932-ca6d-1515-7310-6f58584df73e",  # Active
        "b6679968-e84e-96fa-1fec-8cd4ab39c3de",  # Pending (child of Active)
        "b7ba8903-66a1-485f-4418-00d06abf1235",  # Updated by Affected User (child of Active)
        "9accddda-fbf5-10d4-b402-69bdd276a69b",  # Work in Progress
    }

    # SR statuses considered "open" (actionable in the Validation queue)
    SR_OPEN_STATUS_GUIDS: set[str] = {
        "a52fbc7d-0ee3-c630-f820-37eae24d6e9b",  # New
        "72b55e17-1c7d-b34c-53ae-f61f8732e425",  # Submitted
        "59393f48-d85f-fa6d-2ebe-dcff395d7ed1",  # In Progress
        "05306bf5-a6b9-b5ad-326b-ba4e9724bf37",  # On Hold
    }

    # Combined mapping for lookup by ticket type
    OPEN_STATUS_GUIDS: dict[str, set[str]] = {
        "incident": IR_OPEN_STATUS_GUIDS,
        "servicerequest": SR_OPEN_STATUS_GUIDS,
    }

    # ── GUID-to-Name Resolution Maps ─────────────────────────────────
    # The Athena view endpoint returns some fields as plain GUID strings
    # instead of {name, id} dicts. These maps resolve GUIDs to human-
    # readable names for display in the queue table.

    STATUS_GUID_TO_NAME: dict[str, str] = {
        # IR statuses
        "5e2d3932-ca6d-1515-7310-6f58584df73e": "Active",
        "b6679968-e84e-96fa-1fec-8cd4ab39c3de": "Pending",
        "b7ba8903-66a1-485f-4418-00d06abf1235": "Updated by Affected User",
        "2b8830b6-59f0-f574-9c2a-f4b4682f1681": "Resolved",
        "bd0ae7c4-3315-2eb3-7933-82dfc482dbaf": "Closed",
        "9accddda-fbf5-10d4-b402-69bdd276a69b": "Work in Progress",
        # SR statuses
        "a52fbc7d-0ee3-c630-f820-37eae24d6e9b": "New",
        "72b55e17-1c7d-b34c-53ae-f61f8732e425": "Submitted",
        "59393f48-d85f-fa6d-2ebe-dcff395d7ed1": "In Progress",
        "05306bf5-a6b9-b5ad-326b-ba4e9724bf37": "On Hold",
        "b026fdfd-89bd-490b-e1fd-a599c78d440f": "Completed",
        "21dbfcb4-05f3-fcc0-a58e-a9c48cde3b0e": "Failed",
        "674e87e4-a58e-eab0-9a05-b48881de784c": "Cancelled",
        "c7b65747-f99e-c108-1e17-3c1062138fc4": "Closed",
    }

    SR_PRIORITY_GUID_TO_NAME: dict[str, str] = {
        "1e070214-693f-4a19-82bb-b88ee6362d98": "Low",
        "dd43a3a8-c640-2146-85a4-77978e3bb375": "Medium",
        "536beaf3-62a8-5dd0-248a-39c2bf86d3bc": "High",
        "d0a0fadd-7f17-c0a2-cb2f-00e15c51282c": "Immediate",
    }

    def __init__(
        self,
        athena_client: AthenaClient,
        assignment_service: AssignmentService,
    ) -> None:
        self._athena = athena_client
        self._assignment = assignment_service
        # In-memory lock state: ticket_id → user_id
        self._locks: dict[str, str] = {}
        # Last known ticket IDs for incremental diff refresh
        self._last_known_ticket_ids: set[str] = set()

    # ── Queue Management ──────────────────────────────────────────────

    @staticmethod
    def _build_ir_queue_filter(
        tier_queue_guid: str,
    ) -> list[dict[str, Any]]:
        """
        Build a queue filter for incidents.

        Uses 'TierQueue' property with GUID value. Status filtering is
        done client-side because the Athena view endpoint does not
        reliably support Status filters combined with TierQueue.
        """
        return [
            {
                "condition": "and",
                "filters": [
                    {
                        "condition": "and",
                        "property": "TierQueue",
                        "operator": "eq",
                        "value": tier_queue_guid,
                    },
                ],
            }
        ]

    @staticmethod
    def _build_sr_queue_filter(
        support_group_guid: str,
    ) -> list[dict[str, Any]]:
        """
        Build a queue filter for service requests.

        Uses 'supportGroup' property with GUID value. Status filtering
        is done client-side for consistency with IR behavior.
        """
        return [
            {
                "condition": "and",
                "filters": [
                    {
                        "condition": "and",
                        "property": "supportGroup",
                        "operator": "eq",
                        "value": support_group_guid,
                    },
                ],
            }
        ]

    @staticmethod
    def _extract_results(response: Any) -> list[dict[str, Any]]:
        """
        Extract the ticket list from an Athena search response.

        The search methods return a dict with a 'results' key containing
        the actual ticket list. This helper handles both dict and list
        responses for robustness.
        """
        if isinstance(response, dict):
            return response.get("results", [])
        if isinstance(response, list):
            return response
        return []

    async def fetch_queue(
        self,
        tier_queue_name: str = "Validation",
        statuses: list[str] | None = None,
    ) -> QueueResponse:
        """
        Fetch all IR and SR tickets from the specified tier queue.

        Queries both incident and service request view endpoints
        in parallel, merges results, and annotates with lock state.

        Note: IR and SR use different filter property names:
        - IR uses 'TierQueue'
        - SR uses 'supportGroup'

        Args:
            tier_queue_name: Tier queue name to query (default: Validation).
            statuses: Ticket statuses to include.

        Returns:
            QueueResponse with merged ticket list and lock state.
        """
        if statuses is None:
            statuses = ["Active", "Work in Progress"]

        # Resolve queue name → GUIDs (IR and SR use different GUIDs)
        guids = self.QUEUE_GUIDS.get(tier_queue_name)
        if not guids:
            raise ValueError(
                f"Unknown queue '{tier_queue_name}'. "
                f"Known queues: {list(self.QUEUE_GUIDS.keys())}"
            )

        ir_filters = self._build_ir_queue_filter(guids["ir"])
        sr_filters = self._build_sr_queue_filter(guids["sr"])

        # Fetch IR and SR in parallel (with separate filters)
        # Use $expand=affectedUser so the view endpoint returns the
        # affectedUser relationship data (displayName, userName).
        ir_results, sr_results = await asyncio.gather(
            self._athena.search_incidents(ir_filters, expand="affectedUser"),
            self._athena.search_service_requests(sr_filters, expand="affectedUser"),
            return_exceptions=True,
        )

        tickets: list[QueueTicketSummary] = []

        # Process IR results (search methods return dict with 'results' key)
        # Client-side status filtering is required because the Athena view
        # endpoint returns 500 when combining TierQueue with Status filters.
        # Without filtering, ALL historical tickets are returned (1000+).
        ir_open_guids = self.OPEN_STATUS_GUIDS.get("incident", set())
        sr_open_guids = self.OPEN_STATUS_GUIDS.get("servicerequest", set())

        if isinstance(ir_results, Exception):
            logger.warning("Failed to fetch IR queue: %s", ir_results)
        else:
            for raw in self._extract_results(ir_results):
                if not self._is_open_status(raw, ir_open_guids):
                    continue
                summary = self._raw_to_queue_summary(raw, "incident")
                if summary:
                    summary.locked_by = self._locks.get(summary.id)
                    tickets.append(summary)

        # Process SR results
        if isinstance(sr_results, Exception):
            logger.warning("Failed to fetch SR queue: %s", sr_results)
        else:
            for raw in self._extract_results(sr_results):
                if not self._is_open_status(raw, sr_open_guids):
                    continue
                summary = self._raw_to_queue_summary(raw, "servicerequest")
                if summary:
                    summary.locked_by = self._locks.get(summary.id)
                    tickets.append(summary)

        # Sort by created date (oldest first)
        tickets.sort(key=lambda t: t.created_date or "")

        return QueueResponse(
            tickets=tickets,
            total=len(tickets),
            locks=dict(self._locks),
        )

    async def fetch_queue_streaming(
        self,
        on_ticket: Callable[[QueueTicketSummary, int], Awaitable[None]],
        on_phase: Callable[[str], Awaitable[None]] | None = None,
        tier_queue_name: str = "Validation",
        statuses: list[str] | None = None,
    ) -> int:
        """
        Fetch queue tickets and stream them one-by-one via callbacks.

        Same logic as fetch_queue() but instead of collecting all tickets
        and returning them at once, each processed ticket is immediately
        passed to the on_ticket callback. This enables progressive UI
        rendering so users see tickets appearing as they are processed.

        Args:
            on_ticket: Async callback(ticket, running_count) called for
                each ticket as it is processed from the raw Athena response.
            on_phase: Optional async callback(phase_name) called when
                processing transitions between phases (e.g., 'fetching',
                'processing_ir', 'processing_sr', 'complete').
            tier_queue_name: Tier queue name to query (default: Validation).
            statuses: Ticket statuses to include.

        Returns:
            Total number of tickets streamed.
        """
        if statuses is None:
            statuses = ["Active", "Work in Progress"]

        guids = self.QUEUE_GUIDS.get(tier_queue_name)
        if not guids:
            raise ValueError(
                f"Unknown queue '{tier_queue_name}'. "
                f"Known queues: {list(self.QUEUE_GUIDS.keys())}"
            )

        ir_filters = self._build_ir_queue_filter(guids["ir"])
        sr_filters = self._build_sr_queue_filter(guids["sr"])

        if on_phase:
            await on_phase("fetching")

        # Fetch IR and SR in parallel from Athena
        ir_results, sr_results = await asyncio.gather(
            self._athena.search_incidents(ir_filters, expand="affectedUser"),
            self._athena.search_service_requests(sr_filters, expand="affectedUser"),
            return_exceptions=True,
        )

        count = 0
        ir_open_guids = self.OPEN_STATUS_GUIDS.get("incident", set())
        sr_open_guids = self.OPEN_STATUS_GUIDS.get("servicerequest", set())

        # Stream IR tickets
        if on_phase:
            await on_phase("processing_ir")

        if isinstance(ir_results, Exception):
            logger.warning("Failed to fetch IR queue: %s", ir_results)
        else:
            for raw in self._extract_results(ir_results):
                if not self._is_open_status(raw, ir_open_guids):
                    continue
                summary = self._raw_to_queue_summary(raw, "incident")
                if summary:
                    summary.locked_by = self._locks.get(summary.id)
                    count += 1
                    await on_ticket(summary, count)

        # Stream SR tickets
        if on_phase:
            await on_phase("processing_sr")

        if isinstance(sr_results, Exception):
            logger.warning("Failed to fetch SR queue: %s", sr_results)
        else:
            for raw in self._extract_results(sr_results):
                if not self._is_open_status(raw, sr_open_guids):
                    continue
                summary = self._raw_to_queue_summary(raw, "servicerequest")
                if summary:
                    summary.locked_by = self._locks.get(summary.id)
                    count += 1
                    await on_ticket(summary, count)

        if on_phase:
            await on_phase("complete")

        return count

    # ── Incremental Queue Refresh ─────────────────────────────────────

    async def compute_queue_diff(
        self,
        tier_queue_name: str = "Validation",
        statuses: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Fetch the current queue and compute an incremental diff against
        the last known ticket set.

        Returns only the tickets that were added and the IDs of tickets
        that were removed since the last fetch. Updates the internal
        ``_last_known_ticket_ids`` set so subsequent calls produce
        correct diffs.

        Args:
            tier_queue_name: Tier queue name to query (default: Validation).
            statuses: Ticket statuses to include.

        Returns:
            Dict with keys:
                added   – list[QueueTicketSummary] (new tickets, serialised)
                removed – list[str] (ticket IDs no longer in queue)
                total   – int (current queue size)
                locks   – dict[str, str] (current lock state)
        """
        queue_response = await self.fetch_queue(
            tier_queue_name=tier_queue_name,
            statuses=statuses,
        )

        current_ids = {t.id for t in queue_response.tickets}
        previous_ids = self._last_known_ticket_ids

        added_ids = current_ids - previous_ids
        removed_ids = previous_ids - current_ids

        added_tickets = [
            t for t in queue_response.tickets if t.id in added_ids
        ]

        # Update the last-known set for next diff
        self._last_known_ticket_ids = current_ids

        # Clean up locks for removed tickets (they no longer exist in queue)
        for tid in removed_ids:
            self._locks.pop(tid, None)

        return {
            "added": added_tickets,
            "removed": list(removed_ids),
            "total": queue_response.total,
            "locks": queue_response.locks,
        }

    def snapshot_ticket_ids(self, ticket_ids: set[str]) -> None:
        """
        Set the last-known ticket ID snapshot directly.

        Called after the initial streaming queue load completes so that
        the first ``compute_queue_diff`` call produces a correct diff
        rather than treating every ticket as "added".
        """
        self._last_known_ticket_ids = set(ticket_ids)

    # ── Lock Management ───────────────────────────────────────────────

    def lock_tickets(self, ticket_ids: list[str], user_id: str) -> list[str]:
        """
        Lock tickets for a user. Only locks tickets that are currently unlocked.

        Args:
            ticket_ids: Ticket IDs to lock.
            user_id: User requesting the locks.

        Returns:
            List of ticket IDs that were successfully locked.
        """
        locked: list[str] = []
        for tid in ticket_ids:
            if tid not in self._locks:
                self._locks[tid] = user_id
                locked.append(tid)
            elif self._locks[tid] == user_id:
                # Already locked by this user — count as success
                locked.append(tid)
        return locked

    def unlock_tickets(self, ticket_ids: list[str], user_id: str) -> list[str]:
        """
        Unlock tickets owned by a user.

        Args:
            ticket_ids: Ticket IDs to unlock.
            user_id: User requesting the unlock (must own the lock).

        Returns:
            List of ticket IDs that were successfully unlocked.
        """
        unlocked: list[str] = []
        for tid in ticket_ids:
            if self._locks.get(tid) == user_id:
                del self._locks[tid]
                unlocked.append(tid)
        return unlocked

    def release_user_locks(self, user_id: str) -> list[str]:
        """
        Release all locks held by a user (called on WebSocket disconnect).

        Args:
            user_id: User whose locks should be released.

        Returns:
            List of ticket IDs that were unlocked.
        """
        released: list[str] = []
        for tid in list(self._locks.keys()):
            if self._locks[tid] == user_id:
                del self._locks[tid]
                released.append(tid)
        return released

    def get_locks(self) -> dict[str, str]:
        """Return a copy of the current lock state."""
        return dict(self._locks)

    def claim_batch(self, user_id: str, batch_size: int, queue_ticket_ids: list[str]) -> list[str]:
        """
        Auto-claim the next N unlocked tickets from the queue.

        Args:
            user_id: User claiming the batch.
            batch_size: Maximum number of tickets to claim.
            queue_ticket_ids: Ordered list of ticket IDs currently in the queue.

        Returns:
            List of ticket IDs that were claimed/locked.
        """
        claimed: list[str] = []
        for tid in queue_ticket_ids:
            if len(claimed) >= batch_size:
                break
            if tid not in self._locks:
                self._locks[tid] = user_id
                claimed.append(tid)
            elif self._locks[tid] == user_id:
                claimed.append(tid)
        return claimed

    # ── Batch Recommendations ─────────────────────────────────────────

    async def batch_recommend(
        self,
        ticket_ids: list[str],
        on_processing: Callable[[str, int, int], Awaitable[None]] | None = None,
        on_result: Callable[[str, bool, int, int], Awaitable[None]] | None = None,
    ) -> BulkRecommendResponse:
        """
        Generate classifier recommendations for a batch of tickets.

        Processes tickets sequentially, reusing the Feature #3
        AssignmentService (TF-IDF classifier) for each ticket.

        Args:
            ticket_ids: List of ticket IDs to generate recommendations for.
            on_processing: Optional async callback(ticket_id, current, total)
                called before each ticket starts processing.
            on_result: Optional async callback(ticket_id, success, current, total)
                called after each ticket finishes processing.

        Returns:
            BulkRecommendResponse with per-ticket recommendations.
        """
        recommendations: list[TicketRecommendation] = []
        failed_count = 0
        total = len(ticket_ids)

        for i, ticket_id in enumerate(ticket_ids):
            current = i + 1

            # Notify that this ticket is starting
            if on_processing:
                await on_processing(ticket_id, current, total)

            try:
                result = await self._assignment.recommend_assignment(
                    ticket_id=ticket_id,
                )
                recommendations.append(
                    TicketRecommendation(
                        ticket_id=ticket_id,
                        ticket_info=result.ticket,
                        recommendation=result.recommendation,
                        success=True,
                    )
                )

                # Notify that this ticket succeeded
                if on_result:
                    await on_result(ticket_id, True, current, total)

            except Exception as exc:
                logger.warning("Recommendation failed for %s: %s", ticket_id, exc)
                failed_count += 1
                recommendations.append(
                    TicketRecommendation(
                        ticket_id=ticket_id,
                        ticket_info=TicketInfo(id=ticket_id, ticket_type="unknown"),
                        recommendation=_fallback_recommendation(),
                        success=False,
                        error=str(exc),
                    )
                )

                # Notify that this ticket failed
                if on_result:
                    await on_result(ticket_id, False, current, total)

        return BulkRecommendResponse(
            recommendations=recommendations,
            total=len(recommendations),
            failed=failed_count,
        )

    # ── Bulk Assignment ───────────────────────────────────────────────

    async def assign_tickets(
        self,
        assignments: list[TicketAssignment],
    ) -> BulkAssignResponse:
        """
        Assign a batch of tickets by updating them in Athena.

        For each assignment, calls AthenaClient.update_ticket() with
        the tier queue GUID and optional priority.

        Args:
            assignments: List of TicketAssignment objects with ticket details.

        Returns:
            BulkAssignResponse with per-ticket results.
        """
        results: list[TicketAssignResult] = []
        assigned_count = 0
        failed_count = 0

        for assignment in assignments:
            try:
                updated = await self._athena.update_ticket(
                    ticket_id=assignment.ticket_id,
                    entity_id=assignment.entity_id,
                    tier_queue_guid=assignment.tier_queue_guid,
                    priority=assignment.priority,
                )

                # Extract updated values from response
                tier_queue = updated.get("tierQueue")
                tq_name = tier_queue.get("name") if isinstance(tier_queue, dict) else None
                updated_priority = updated.get("priority")

                results.append(
                    TicketAssignResult(
                        ticket_id=assignment.ticket_id,
                        success=True,
                        updated_tier_queue=tq_name,
                        updated_priority=updated_priority,
                    )
                )
                assigned_count += 1

                # Remove lock after successful assignment
                self._locks.pop(assignment.ticket_id, None)

            except Exception as exc:
                logger.warning(
                    "Assignment failed for %s: %s", assignment.ticket_id, exc
                )
                results.append(
                    TicketAssignResult(
                        ticket_id=assignment.ticket_id,
                        success=False,
                        error=str(exc),
                    )
                )
                failed_count += 1

        return BulkAssignResponse(
            results=results,
            total_assigned=assigned_count,
            total_failed=failed_count,
        )

    # ── Support Group Lists (for Manual Assignment) ────────────────────

    # In-memory cache: ticket_type → list[{name, guid}]
    _support_group_cache: dict[str, list[dict[str, str]]] = {}

    async def get_support_groups(
        self,
        ticket_type: str,
    ) -> list[dict[str, str]]:
        """
        Get the list of assignable support groups for a ticket type.

        Loads from the pre-generated JSON file (exploration/output/
        assignable_support_groups.json) which contains only groups with
        disabled=false. Falls back to fetching from Athena enum tree
        endpoints if the JSON file is unavailable.

        Args:
            ticket_type: 'incident' or 'servicerequest'.

        Returns:
            List of {name, guid} dicts sorted by name.
        """
        if ticket_type in self._support_group_cache:
            return self._support_group_cache[ticket_type]

        groups = self._load_groups_from_json(ticket_type)

        if not groups:
            # Fallback: fetch from Athena enum tree endpoint
            groups = await self._fetch_groups_from_athena(ticket_type)

        self._support_group_cache[ticket_type] = groups
        return groups

    @staticmethod
    def _load_groups_from_json(ticket_type: str) -> list[dict[str, str]]:
        """Load support groups from the pre-generated JSON file."""
        json_path = (
            Path(__file__).resolve().parent.parent
            / "exploration"
            / "output"
            / "assignable_support_groups.json"
        )
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            key = "ir_assignable" if ticket_type == "incident" else "sr_assignable"
            raw_groups = data.get(key, [])
            # Normalize field names: the JSON uses 'fullname' but we return 'name'
            normalized = [
                {"name": g.get("fullname", g.get("name", "")), "guid": g.get("guid", "")}
                for g in raw_groups
            ]
            # Sort by name for consistent autocomplete ordering
            return sorted(normalized, key=lambda g: g.get("name", ""))
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load support groups from JSON: %s", exc)
            return []

    async def _fetch_groups_from_athena(
        self, ticket_type: str
    ) -> list[dict[str, str]]:
        """
        Fetch support groups from Athena enum tree as a fallback.

        Flattens the hierarchical tree into a flat list of {name, guid}
        pairs, excluding disabled groups.
        """
        enum_id = (
            "c3264527-a501-029f-6872-31300080b3bf"  # IR IncidentTierQueuesEnum
            if ticket_type == "incident"
            else "23c243f6-9365-d46f-dff2-03826e24d228"  # SR ServiceRequestSupportGroupEnum
        )

        try:
            tree = await self._athena.get_enum_tree(enum_id)
            groups: list[dict[str, str]] = []
            self._flatten_enum_tree(tree, groups, prefix="")
            return sorted(groups, key=lambda g: g.get("name", ""))
        except Exception as exc:
            logger.warning("Failed to fetch support groups from Athena: %s", exc)
            return []

    @staticmethod
    def _flatten_enum_tree(
        nodes: list[dict[str, Any]],
        result: list[dict[str, str]],
        prefix: str,
    ) -> None:
        """Recursively flatten an Athena enum tree into {name, guid} pairs."""
        for node in nodes:
            name = node.get("name", "")
            full_name = f"{prefix}\\{name}" if prefix else name
            disabled = node.get("disabled", False)

            if not disabled and name:
                result.append({"name": full_name, "guid": node.get("id", "")})

            children = node.get("children", [])
            if children:
                BulkAssignmentService._flatten_enum_tree(children, result, full_name)

    # ── Private Helpers ───────────────────────────────────────────────

    @staticmethod
    def _is_open_status(raw: dict[str, Any], open_guids: set[str]) -> bool:
        """
        Check if a raw ticket's status is in the set of open status GUIDs.

        The Athena view endpoint returns status as either:
        - A plain GUID string (e.g., '5e2d3932-ca6d-1515-7310-6f58584df73e')
        - A dict with 'id' and/or 'name' keys

        Args:
            raw: Raw ticket dict from Athena.
            open_guids: Set of status GUIDs considered "open".

        Returns:
            True if the ticket's status is in the open set.
        """
        status = raw.get("status")
        if status is None:
            return False
        if isinstance(status, dict):
            status_guid = status.get("id", "")
        else:
            status_guid = str(status)
        return status_guid in open_guids

    @staticmethod
    def _resolve_status(raw_status: Any) -> str | None:
        """
        Resolve a status value to a human-readable name.

        The Athena view endpoint may return status as:
        - A dict with 'name' and 'id' keys
        - A plain GUID string
        - None

        Args:
            raw_status: Raw status value from Athena.

        Returns:
            Human-readable status name, or the original value if unresolvable.
        """
        if raw_status is None:
            return None
        if isinstance(raw_status, dict):
            return raw_status.get("name")
        status_str = str(raw_status)
        return BulkAssignmentService.STATUS_GUID_TO_NAME.get(status_str, status_str)

    @staticmethod
    def _resolve_priority(raw_priority: Any, ticket_type: str) -> str | int | None:
        """
        Resolve a priority value to a human-readable form.

        IR priorities are numeric (pass through as-is).
        SR priorities may be GUID strings that need resolution.

        Args:
            raw_priority: Raw priority value from Athena.
            ticket_type: 'incident' or 'servicerequest'.

        Returns:
            Human-readable priority, or the original value if unresolvable.
        """
        if raw_priority is None:
            return None
        # IR priorities are numeric — pass through
        if isinstance(raw_priority, (int, float)):
            return raw_priority
        priority_str = str(raw_priority)
        # Only resolve GUIDs for SR tickets (IR uses numeric values)
        if ticket_type == "servicerequest":
            return BulkAssignmentService.SR_PRIORITY_GUID_TO_NAME.get(
                priority_str, priority_str
            )
        return raw_priority

    @staticmethod
    def _raw_to_queue_summary(
        raw: dict[str, Any],
        ticket_type: str,
    ) -> QueueTicketSummary | None:
        """Convert a raw Athena ticket dict to a QueueTicketSummary."""
        ticket_id = raw.get("id")
        entity_id = raw.get("entityId")
        if not ticket_id or not entity_id:
            return None

        # Extract nested fields safely, resolving GUIDs to names
        status = BulkAssignmentService._resolve_status(raw.get("status"))

        priority = BulkAssignmentService._resolve_priority(
            raw.get("priority"), ticket_type
        )

        tier_queue = raw.get("tierQueue")
        if isinstance(tier_queue, dict):
            tier_queue = tier_queue.get("name")

        # The Athena view endpoint with $expand=affectedUser returns flattened
        # fields (e.g., 'affectedUser_DisplayName', 'affectedUser_UserName')
        # instead of a nested 'affectedUser' dict. Check both formats.
        affected_user = raw.get("affectedUser")
        if isinstance(affected_user, dict):
            affected_user = affected_user.get("displayName") or affected_user.get("userName")
        if not affected_user:
            affected_user = (
                raw.get("affectedUser_DisplayName")
                or raw.get("affectedUser_UserName")
            )

        description = raw.get("description")
        if description and len(description) > 200:
            description = description[:200] + "..."

        # Extract assigned user (view endpoint may use flat or nested format)
        assigned_user = raw.get("assignedTo_DisplayName") or raw.get("assignedTo_UserName")
        if not assigned_user:
            assigned_to = raw.get("assignedToUser")
            if isinstance(assigned_to, dict):
                assigned_user = assigned_to.get("displayName") or assigned_to.get("userName")

        # Extract location (view endpoint returns locationValue as a string)
        location = raw.get("locationValue")
        if not location:
            loc = raw.get("location")
            if isinstance(loc, dict):
                location = loc.get("name")
            elif isinstance(loc, str):
                location = loc

        return QueueTicketSummary(
            id=ticket_id,
            entity_id=entity_id,
            ticket_type=ticket_type,
            title=raw.get("title"),
            description=description,
            status=status,
            priority=priority,
            tier_queue=tier_queue,
            affected_user=affected_user,
            assigned_user=assigned_user,
            location=location,
            created_date=raw.get("createdDate"),
        )


def _fallback_recommendation() -> AssignmentRecommendation:
    """Create a fallback recommendation when classifier fails."""
    return AssignmentRecommendation(
        support_group_name="Service Desk",
        support_group_guid="ec749166-07c5-eba6-35ba-bd32fa8ed7d2",
        confidence=0.0,
        method="classifier",
        rationale="Recommendation generation failed. Defaulting to Service Desk.",
        alternatives=[],
    )
