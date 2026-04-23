"""
Turnover Service — business logic for Feature #5: Turnover Email Draft Generator.

Workflow:
1. Calculate shift times from current time
2. Query Athena for active/pended P1/P2 SEV incidents and upcoming CRs (in parallel)
3. Categorize tickets (parent, active, pended)
4. Format a copy-paste-ready turnover email
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from src.clients.athena_client import AthenaClient
from src.models.turnover import (
    ChangeRequestSummary,
    TurnoverRequest,
    TurnoverResponse,
    TurnoverTicketSummary,
)

logger = logging.getLogger(__name__)

# ── Email Constants ───────────────────────────────────────────────────

EMAIL_TO = "issdteam@uphs.upenn.edu"
EMAIL_CC = (
    "oscar.thodde@pennmedicine.upenn.edu; "
    "michael.albano@pennmedicine.upenn.edu; "
    "andrew.cruz@pennmedicine.upenn.edu; "
    "emmanuel.edu@pennmedicine.upenn.edu; "
    "adam.maselli@pennmedicine.upenn.edu"
)

# Statuses for active SEV tickets
ACTIVE_STATUSES = ["Active", "Work in Progress"]
# Statuses for pended SEV tickets
PENDED_STATUSES = ["Pending"]


class TurnoverService:
    """Orchestrates the turnover email generation pipeline."""

    def __init__(self, athena_client: AthenaClient) -> None:
        self._athena = athena_client

    async def generate_turnover(self, request: TurnoverRequest) -> TurnoverResponse:
        """
        Generate a turnover email draft.

        Args:
            request: Turnover request with agent names, notes, and lookahead hours.

        Returns:
            TurnoverResponse with email fields and categorized ticket data.
        """
        now = datetime.now()

        # Step 1: Build filters
        active_filter = AthenaClient.build_sev_filter(statuses=ACTIVE_STATUSES)
        pended_filter = AthenaClient.build_sev_filter(statuses=PENDED_STATUSES)
        cr_filter = AthenaClient.build_upcoming_cr_filter(hours_ahead=request.hours_lookahead)

        # Step 2: Query Athena in parallel
        active_results, pended_results, cr_results = await asyncio.gather(
            self._athena.search_incidents(active_filter),
            self._athena.search_incidents(pended_filter),
            self._athena.search_change_requests(cr_filter),
            return_exceptions=True,
        )

        # Handle exceptions from parallel queries gracefully
        if isinstance(active_results, BaseException):
            logger.error("Failed to query active SEV incidents: %s", active_results)
            active_results = []
        if isinstance(pended_results, BaseException):
            logger.error("Failed to query pended SEV incidents: %s", pended_results)
            pended_results = []
        if isinstance(cr_results, BaseException):
            logger.error("Failed to query upcoming change requests: %s", cr_results)
            cr_results = []

        # Step 3: Extract and categorize
        active_tickets = [self._extract_ticket_summary(t) for t in active_results]
        pended_tickets = [self._extract_ticket_summary(t) for t in pended_results]
        upcoming_crs = [self._extract_cr_summary(cr) for cr in cr_results]

        # Separate parent tickets from active SEVs
        parent_tickets = [t for t in active_tickets if t.is_parent]
        active_sevs = [t for t in active_tickets if not t.is_parent]

        total_tickets = len(parent_tickets) + len(active_sevs) + len(pended_tickets)

        # Step 4: Calculate shift times and format email
        shift_start, shift_end = self._calculate_shift_times(now)
        greeting = self._get_greeting(now)
        subject = self._format_subject(shift_start, shift_end, now)
        body = self._format_email_body(
            greeting=greeting,
            turnover_agent_name=request.turnover_agent_name,
            sender_name=request.sender_name,
            upcoming_crs=upcoming_crs,
            parent_tickets=parent_tickets,
            active_sevs=active_sevs,
            pended_sevs=pended_tickets,
            escalation_notes=request.escalation_notes,
            voicemail_notes=request.voicemail_notes,
            notes=request.notes,
        )

        return TurnoverResponse(
            email_to=EMAIL_TO,
            email_cc=EMAIL_CC,
            email_subject=subject,
            email_body=body,
            upcoming_outages=upcoming_crs,
            parent_tickets=parent_tickets,
            active_sevs=active_sevs,
            pended_sevs=pended_tickets,
            total_tickets=total_tickets,
        )

    # ── Extraction Helpers ────────────────────────────────────────────

    @staticmethod
    def _extract_ticket_summary(raw: dict[str, Any]) -> TurnoverTicketSummary:
        """Extract a TurnoverTicketSummary from a raw Athena incident."""
        status = raw.get("status")
        if isinstance(status, dict):
            status = status.get("name") or status.get("displayName")

        support_group = raw.get("tierQueue") or raw.get("supportGroup")
        if isinstance(support_group, dict):
            support_group = support_group.get("name") or support_group.get("displayName")

        affected_user = raw.get("affectedUser")
        if isinstance(affected_user, dict):
            affected_user = affected_user.get("displayName") or affected_user.get("userName")

        assigned_user = raw.get("assignedToUser")
        if isinstance(assigned_user, dict):
            assigned_user = assigned_user.get("displayName") or assigned_user.get("userName")

        return TurnoverTicketSummary(
            id=raw.get("id", "Unknown"),
            title=raw.get("title"),
            status=status,
            priority=raw.get("priority"),
            support_group=support_group,
            affected_user=affected_user,
            assigned_user=assigned_user,
            created_date=raw.get("createdDate"),
            is_parent=bool(raw.get("isParent", False)),
        )

    @staticmethod
    def _extract_cr_summary(raw: dict[str, Any]) -> ChangeRequestSummary:
        """Extract a ChangeRequestSummary from a raw Athena change request."""
        status = raw.get("status")
        if isinstance(status, dict):
            status = status.get("name") or status.get("displayName")

        category = raw.get("category")
        if isinstance(category, dict):
            category = category.get("name") or category.get("displayName")

        downtime = raw.get("downtime")
        if isinstance(downtime, dict):
            downtime = downtime.get("name") or downtime.get("displayName")

        return ChangeRequestSummary(
            id=raw.get("id", "Unknown"),
            title=raw.get("title"),
            status=status,
            scheduled_start=raw.get("scheduledStartDate"),
            scheduled_end=raw.get("scheduledEndDate"),
            downtime=str(downtime) if downtime is not None else None,
            category=category,
        )

    # ── Time Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _calculate_shift_times(now: datetime) -> tuple[datetime, datetime]:
        """
        Calculate shift start and end times.

        Shift end = current time rounded down to the nearest 30 minutes.
        Shift start = shift end minus 8 hours.

        Args:
            now: Current datetime.

        Returns:
            Tuple of (shift_start, shift_end).
        """
        # Round down to nearest 30 minutes
        minute = now.minute
        rounded_minute = (minute // 30) * 30
        shift_end = now.replace(minute=rounded_minute, second=0, microsecond=0)
        shift_start = shift_end - timedelta(hours=8)
        return shift_start, shift_end

    @staticmethod
    def _get_greeting(now: datetime) -> str:
        """
        Get a time-of-day based greeting.

        - morning: 5:00–11:59
        - afternoon: 12:00–16:59
        - evening: 17:00–20:59
        - night: 21:00–4:59

        Args:
            now: Current datetime.

        Returns:
            Greeting string (e.g., 'Good morning').
        """
        hour = now.hour
        if 5 <= hour < 12:
            return "Good morning"
        elif 12 <= hour < 17:
            return "Good afternoon"
        elif 17 <= hour < 21:
            return "Good evening"
        else:
            return "Good night"

    # ── Formatting Helpers ────────────────────────────────────────────

    @staticmethod
    def _format_subject(shift_start: datetime, shift_end: datetime, now: datetime) -> str:
        """
        Format the email subject line.

        Format: 'SEV Turnover for HH:MM - HH:MM MM/DD/YYYY'

        Args:
            shift_start: Shift start time.
            shift_end: Shift end time.
            now: Current datetime (for the date).

        Returns:
            Formatted subject string.
        """
        start_str = shift_start.strftime("%H:%M")
        end_str = shift_end.strftime("%H:%M")
        date_str = now.strftime("%m/%d/%Y")
        return f"SEV Turnover for {start_str} - {end_str} {date_str}"

    @staticmethod
    def _format_ticket_line(ticket: TurnoverTicketSummary) -> str:
        """Format a single ticket as a line for the email body."""
        parts = [f"  - {ticket.id}"]
        if ticket.priority is not None:
            parts.append(f"(P{ticket.priority})")
        if ticket.title:
            parts.append(f"— {ticket.title}")
        if ticket.assigned_user:
            parts.append(f"[Assigned: {ticket.assigned_user}]")
        if ticket.support_group:
            parts.append(f"[Queue: {ticket.support_group}]")
        return " ".join(parts)

    @staticmethod
    def _format_cr_line(cr: ChangeRequestSummary) -> str:
        """Format a single change request as a line for the email body."""
        parts = [f"  - {cr.id}"]
        if cr.title:
            parts.append(f"— {cr.title}")
        if cr.scheduled_start:
            parts.append(f"[Start: {cr.scheduled_start}]")
        if cr.scheduled_end:
            parts.append(f"[End: {cr.scheduled_end}]")
        if cr.downtime:
            parts.append(f"[Downtime: {cr.downtime}]")
        return " ".join(parts)

    @staticmethod
    def _format_email_body(
        greeting: str,
        turnover_agent_name: str,
        sender_name: str,
        upcoming_crs: list[ChangeRequestSummary],
        parent_tickets: list[TurnoverTicketSummary],
        active_sevs: list[TurnoverTicketSummary],
        pended_sevs: list[TurnoverTicketSummary],
        escalation_notes: str,
        voicemail_notes: str,
        notes: str,
    ) -> str:
        """
        Format the full email body with all sections.

        Args:
            greeting: Time-of-day greeting.
            turnover_agent_name: Name of the agent taking over.
            sender_name: Name of the sender.
            upcoming_crs: List of upcoming change requests.
            parent_tickets: List of parent (major) incidents.
            active_sevs: List of active SEV tickets.
            pended_sevs: List of pended SEV tickets.
            escalation_notes: Notes for escalation section.
            voicemail_notes: Notes for voicemail section.
            notes: Notes for verbal turnover section.

        Returns:
            Complete email body string.
        """
        lines: list[str] = []

        # Greeting
        lines.append(f"{greeting} team,")
        lines.append("")
        lines.append(f"Please see below for the SEV turnover from {sender_name} to {turnover_agent_name}.")
        lines.append("")

        # Upcoming Outages/Downtimes
        lines.append("=" * 50)
        lines.append("UPCOMING OUTAGES / DOWNTIMES")
        lines.append("=" * 50)
        if upcoming_crs:
            for cr in upcoming_crs:
                lines.append(TurnoverService._format_cr_line(cr))
        else:
            lines.append("  None at this time.")
        lines.append("")

        # Parent Tickets
        lines.append("=" * 50)
        lines.append("PARENT TICKETS")
        lines.append("=" * 50)
        if parent_tickets:
            for ticket in parent_tickets:
                lines.append(TurnoverService._format_ticket_line(ticket))
        else:
            lines.append("  None at this time.")
        lines.append("")

        # Active Sevs
        lines.append("=" * 50)
        lines.append("ACTIVE SEVS")
        lines.append("=" * 50)
        if active_sevs:
            for ticket in active_sevs:
                lines.append(TurnoverService._format_ticket_line(ticket))
        else:
            lines.append("  None at this time.")
        lines.append("")

        # Pended Sevs
        lines.append("=" * 50)
        lines.append("PENDED SEVS")
        lines.append("=" * 50)
        if pended_sevs:
            for ticket in pended_sevs:
                lines.append(TurnoverService._format_ticket_line(ticket))
        else:
            lines.append("  None at this time.")
        lines.append("")

        # Escalated to Manager/ISOD/ISMT
        lines.append("=" * 50)
        lines.append("ESCALATED TO MANAGER / ISOD / ISMT")
        lines.append("=" * 50)
        if escalation_notes:
            lines.append(f"  {escalation_notes}")
        else:
            lines.append("  None at this time.")
        lines.append("")

        # On-Call Analyst Voicemails Left
        lines.append("=" * 50)
        lines.append("ON-CALL ANALYST VOICEMAILS LEFT")
        lines.append("=" * 50)
        if voicemail_notes:
            lines.append(f"  {voicemail_notes}")
        else:
            lines.append("  None at this time.")
        lines.append("")

        # Verbal Turnover
        lines.append("=" * 50)
        lines.append("VERBAL TURNOVER")
        lines.append("=" * 50)
        if notes:
            lines.append(f"  {notes}")
        else:
            lines.append("  No additional notes.")
        lines.append("")

        # Sign-off
        lines.append("Thank you,")
        lines.append(sender_name)

        return "\n".join(lines)