"""
Unit tests for TurnoverService — Feature #5: Turnover Email Draft Generator.

Tests the turnover pipeline with mocked AthenaClient:
- Filter building (SEV combined, CR upcoming)
- Ticket extraction and categorization (parent/active/pended)
- CR extraction
- Shift time calculation and rounding
- Greeting selection (morning/afternoon/evening/night)
- Email formatting (subject, body sections, empty sections)
- Edge cases (no tickets, no CRs, missing fields)
- Full generate_turnover workflow
"""

from datetime import datetime

import pytest

from src.clients.athena_client import AthenaClient
from src.models.turnover import (
    ChangeRequestSummary,
    TurnoverRequest,
    TurnoverTicketSummary,
)
from src.services.turnover import (
    ACTIVE_STATUSES,
    EMAIL_CC,
    EMAIL_TO,
    PENDED_STATUSES,
    TurnoverService,
)


# ── Filter Building ──────────────────────────────────────────────────


def test_build_sev_filter_default_priorities():
    """Should build a filter for P1/P2 with given statuses."""
    result = AthenaClient.build_sev_filter(statuses=["Active", "Work in Progress"])
    assert len(result) == 1
    outer = result[0]
    assert outer["condition"] == "and"
    inner_filters = outer["filters"]
    assert len(inner_filters) == 2

    # Priority OR group
    priority_group = inner_filters[0]
    assert priority_group["condition"] == "or"
    assert len(priority_group["filters"]) == 2
    assert priority_group["filters"][0]["value"] == "1"
    assert priority_group["filters"][1]["value"] == "2"

    # Status OR group
    status_group = inner_filters[1]
    assert status_group["condition"] == "or"
    assert len(status_group["filters"]) == 2
    assert status_group["filters"][0]["value"] == "Active"
    assert status_group["filters"][1]["value"] == "Work in Progress"


def test_build_sev_filter_custom_priorities():
    """Should accept custom priority values."""
    result = AthenaClient.build_sev_filter(statuses=["Pending"], priorities=["1"])
    priority_group = result[0]["filters"][0]
    assert len(priority_group["filters"]) == 1
    assert priority_group["filters"][0]["value"] == "1"


def test_build_sev_filter_single_status():
    """Should work with a single status."""
    result = AthenaClient.build_sev_filter(statuses=["Pending"])
    status_group = result[0]["filters"][1]
    assert len(status_group["filters"]) == 1
    assert status_group["filters"][0]["value"] == "Pending"


def test_build_upcoming_cr_filter_default():
    """Should build a CR filter with default 24h lookahead."""
    result = AthenaClient.build_upcoming_cr_filter()
    assert len(result) == 1
    filters = result[0]["filters"]
    # Should have: start > [now], start < [now]+24h, 4x status ne
    assert len(filters) == 6
    assert filters[0]["property"] == "ScheduledStartDate"
    assert filters[0]["value"] == "[now]"
    assert filters[1]["value"] == "[now]+24h"
    assert filters[2]["value"] == "Completed"
    assert filters[3]["value"] == "Failed"


def test_build_upcoming_cr_filter_custom_hours():
    """Should use custom hours_ahead value."""
    result = AthenaClient.build_upcoming_cr_filter(hours_ahead=48)
    filters = result[0]["filters"]
    assert filters[1]["value"] == "[now]+48h"


# ── Ticket Extraction ────────────────────────────────────────────────


def test_extract_ticket_summary_full(sample_sev_ticket):
    """Should extract all fields from a full ticket."""
    summary = TurnoverService._extract_ticket_summary(sample_sev_ticket)
    assert summary.id == "IR10371854"
    assert summary.title == "HUP Pavilion — Network outage affecting 5th floor"
    assert summary.status == "Active"
    assert summary.priority == 1
    assert summary.support_group == "Technology\\Infrastructure"
    assert summary.affected_user == "John Smith"
    assert summary.assigned_user == "Jane Doe"
    assert summary.is_parent is True


def test_extract_ticket_summary_minimal():
    """Should handle a ticket with minimal fields."""
    raw = {"id": "IR9999999"}
    summary = TurnoverService._extract_ticket_summary(raw)
    assert summary.id == "IR9999999"
    assert summary.title is None
    assert summary.status is None
    assert summary.priority is None
    assert summary.support_group is None
    assert summary.affected_user is None
    assert summary.assigned_user is None
    assert summary.is_parent is False


def test_extract_ticket_summary_string_fields():
    """Should handle string (non-dict) nested fields."""
    raw = {
        "id": "IR1111111",
        "status": "Active",
        "tierQueue": "Service Desk",
        "affectedUser": "someuser",
        "assignedToUser": "analyst1",
    }
    summary = TurnoverService._extract_ticket_summary(raw)
    assert summary.status == "Active"
    assert summary.support_group == "Service Desk"
    assert summary.affected_user == "someuser"
    assert summary.assigned_user == "analyst1"


def test_extract_ticket_summary_missing_id():
    """Should default to 'Unknown' when id is missing."""
    summary = TurnoverService._extract_ticket_summary({})
    assert summary.id == "Unknown"


def test_extract_ticket_summary_uses_tierqueue_over_supportgroup():
    """Should prefer tierQueue over supportGroup."""
    raw = {
        "id": "IR1",
        "tierQueue": {"name": "PennChart\\ED"},
        "supportGroup": {"name": "Service Desk"},
    }
    summary = TurnoverService._extract_ticket_summary(raw)
    assert summary.support_group == "PennChart\\ED"


# ── CR Extraction ─────────────────────────────────────────────────────


def test_extract_cr_summary_full(sample_change_request):
    """Should extract all fields from a full CR."""
    summary = TurnoverService._extract_cr_summary(sample_change_request)
    assert summary.id == "CR10312956"
    assert summary.title == "PennChart March 2026 Update — Database Maintenance"
    assert summary.status == "In Progress"
    assert summary.scheduled_start == "2026-04-14T02:00:00Z"
    assert summary.scheduled_end == "2026-04-14T06:00:00Z"
    assert summary.downtime == "Yes"
    assert summary.category == "Standard"


def test_extract_cr_summary_minimal():
    """Should handle a CR with minimal fields."""
    raw = {"id": "CR9999999"}
    summary = TurnoverService._extract_cr_summary(raw)
    assert summary.id == "CR9999999"
    assert summary.title is None
    assert summary.status is None
    assert summary.downtime is None


def test_extract_cr_summary_string_fields():
    """Should handle string (non-dict) nested fields."""
    raw = {
        "id": "CR1",
        "status": "Submitted",
        "category": "Minor",
        "downtime": "No",
    }
    summary = TurnoverService._extract_cr_summary(raw)
    assert summary.status == "Submitted"
    assert summary.category == "Minor"
    assert summary.downtime == "No"


# ── Shift Time Calculation ────────────────────────────────────────────


def test_shift_times_exact_half_hour():
    """Should not change time when already on a half hour."""
    now = datetime(2026, 4, 13, 14, 30, 0)
    start, end = TurnoverService._calculate_shift_times(now)
    assert end == datetime(2026, 4, 13, 14, 30, 0)
    assert start == datetime(2026, 4, 13, 6, 30, 0)


def test_shift_times_rounds_down():
    """Should round down to nearest 30 minutes."""
    now = datetime(2026, 4, 13, 14, 45, 23)
    start, end = TurnoverService._calculate_shift_times(now)
    assert end == datetime(2026, 4, 13, 14, 30, 0)
    assert start == datetime(2026, 4, 13, 6, 30, 0)


def test_shift_times_rounds_down_before_half():
    """Should round down when minutes < 30."""
    now = datetime(2026, 4, 13, 14, 15, 0)
    start, end = TurnoverService._calculate_shift_times(now)
    assert end == datetime(2026, 4, 13, 14, 0, 0)
    assert start == datetime(2026, 4, 13, 6, 0, 0)


def test_shift_times_exact_hour():
    """Should handle exact hour correctly."""
    now = datetime(2026, 4, 13, 8, 0, 0)
    start, end = TurnoverService._calculate_shift_times(now)
    assert end == datetime(2026, 4, 13, 8, 0, 0)
    assert start == datetime(2026, 4, 13, 0, 0, 0)


def test_shift_times_crosses_midnight():
    """Should handle shift start crossing midnight into previous day."""
    now = datetime(2026, 4, 13, 3, 30, 0)
    start, end = TurnoverService._calculate_shift_times(now)
    assert end == datetime(2026, 4, 13, 3, 30, 0)
    assert start == datetime(2026, 4, 12, 19, 30, 0)


# ── Greeting Selection ────────────────────────────────────────────────


def test_greeting_morning():
    """Should return 'Good morning' for 5:00–11:59."""
    assert TurnoverService._get_greeting(datetime(2026, 4, 13, 5, 0)) == "Good morning"
    assert TurnoverService._get_greeting(datetime(2026, 4, 13, 11, 59)) == "Good morning"


def test_greeting_afternoon():
    """Should return 'Good afternoon' for 12:00–16:59."""
    assert TurnoverService._get_greeting(datetime(2026, 4, 13, 12, 0)) == "Good afternoon"
    assert TurnoverService._get_greeting(datetime(2026, 4, 13, 16, 59)) == "Good afternoon"


def test_greeting_evening():
    """Should return 'Good evening' for 17:00–20:59."""
    assert TurnoverService._get_greeting(datetime(2026, 4, 13, 17, 0)) == "Good evening"
    assert TurnoverService._get_greeting(datetime(2026, 4, 13, 20, 59)) == "Good evening"


def test_greeting_night():
    """Should return 'Good night' for 21:00–4:59."""
    assert TurnoverService._get_greeting(datetime(2026, 4, 13, 21, 0)) == "Good night"
    assert TurnoverService._get_greeting(datetime(2026, 4, 13, 0, 0)) == "Good night"
    assert TurnoverService._get_greeting(datetime(2026, 4, 13, 4, 59)) == "Good night"


# ── Email Subject Formatting ─────────────────────────────────────────


def test_format_subject():
    """Should format subject with shift times and date."""
    start = datetime(2026, 4, 13, 6, 30)
    end = datetime(2026, 4, 13, 14, 30)
    now = datetime(2026, 4, 13, 14, 45)
    subject = TurnoverService._format_subject(start, end, now)
    assert subject == "SEV Turnover for 06:30 - 14:30 04/13/2026"


# ── Ticket/CR Line Formatting ────────────────────────────────────────


def test_format_ticket_line_full():
    """Should format a ticket line with all fields."""
    ticket = TurnoverTicketSummary(
        id="IR10371854",
        title="Network outage",
        priority=1,
        assigned_user="Jane Doe",
        support_group="Technology\\Infrastructure",
    )
    line = TurnoverService._format_ticket_line(ticket)
    assert "IR10371854" in line
    assert "(P1)" in line
    assert "Network outage" in line
    assert "Jane Doe" in line
    assert "Technology\\Infrastructure" in line


def test_format_ticket_line_minimal():
    """Should format a ticket line with only ID."""
    ticket = TurnoverTicketSummary(id="IR9999999")
    line = TurnoverService._format_ticket_line(ticket)
    assert line == "  - IR9999999"


def test_format_cr_line_full():
    """Should format a CR line with all fields."""
    cr = ChangeRequestSummary(
        id="CR10312956",
        title="DB Maintenance",
        scheduled_start="2026-04-14T02:00:00Z",
        scheduled_end="2026-04-14T06:00:00Z",
        downtime="Yes",
    )
    line = TurnoverService._format_cr_line(cr)
    assert "CR10312956" in line
    assert "DB Maintenance" in line
    assert "2026-04-14T02:00:00Z" in line
    assert "Yes" in line


def test_format_cr_line_minimal():
    """Should format a CR line with only ID."""
    cr = ChangeRequestSummary(id="CR9999999")
    line = TurnoverService._format_cr_line(cr)
    assert line == "  - CR9999999"


# ── Email Body Formatting ────────────────────────────────────────────


def test_format_email_body_empty_sections():
    """Should show 'None at this time' for empty sections."""
    body = TurnoverService._format_email_body(
        greeting="Good afternoon",
        turnover_agent_name="Bob",
        sender_name="Alice",
        upcoming_crs=[],
        parent_tickets=[],
        active_sevs=[],
        pended_sevs=[],
        escalation_notes="",
        voicemail_notes="",
        notes="",
    )
    assert "Good afternoon team," in body
    assert "from Alice to Bob" in body
    assert body.count("None at this time.") == 6
    assert "No additional notes." in body
    assert "Thank you,\nAlice" in body


def test_format_email_body_with_tickets():
    """Should include ticket lines in the correct sections."""
    active = [TurnoverTicketSummary(id="IR111", title="Active issue", priority=2)]
    pended = [TurnoverTicketSummary(id="IR222", title="Pended issue", priority=1)]
    parent = [TurnoverTicketSummary(id="IR333", title="Parent incident", priority=1, is_parent=True)]
    crs = [ChangeRequestSummary(id="CR444", title="Maintenance window")]

    body = TurnoverService._format_email_body(
        greeting="Good morning",
        turnover_agent_name="Bob",
        sender_name="Alice",
        upcoming_crs=crs,
        parent_tickets=parent,
        active_sevs=active,
        pended_sevs=pended,
        escalation_notes="Escalated IR111 to ISOD",
        voicemail_notes="Left VM for on-call DBA",
        notes="Monitoring IR111 closely",
    )
    assert "IR111" in body
    assert "IR222" in body
    assert "IR333" in body
    assert "CR444" in body
    assert "Escalated IR111 to ISOD" in body
    assert "Left VM for on-call DBA" in body
    assert "Monitoring IR111 closely" in body


def test_format_email_body_sections_order():
    """Should have sections in the correct order."""
    body = TurnoverService._format_email_body(
        greeting="Good morning",
        turnover_agent_name="Bob",
        sender_name="Alice",
        upcoming_crs=[],
        parent_tickets=[],
        active_sevs=[],
        pended_sevs=[],
        escalation_notes="",
        voicemail_notes="",
        notes="",
    )
    sections = [
        "UPCOMING OUTAGES / DOWNTIMES",
        "PARENT TICKETS",
        "ACTIVE SEVS",
        "PENDED SEVS",
        "ESCALATED TO MANAGER / ISOD / ISMT",
        "ON-CALL ANALYST VOICEMAILS LEFT",
        "VERBAL TURNOVER",
    ]
    positions = [body.index(s) for s in sections]
    assert positions == sorted(positions), "Sections are not in the correct order"


# ── Full Workflow (generate_turnover) ─────────────────────────────────


@pytest.mark.asyncio
async def test_generate_turnover_returns_email_fields(
    turnover_service: TurnoverService,
    mock_athena_client,
):
    """Should return a TurnoverResponse with email fields."""
    mock_athena_client.search_incidents.return_value = []
    mock_athena_client.search_change_requests.return_value = []

    request = TurnoverRequest(
        turnover_agent_name="Bob",
        sender_name="Alice",
    )
    result = await turnover_service.generate_turnover(request)

    assert result.email_to == EMAIL_TO
    assert result.email_cc == EMAIL_CC
    assert "SEV Turnover for" in result.email_subject
    assert "Alice" in result.email_body
    assert "Bob" in result.email_body
    assert result.total_tickets == 0


@pytest.mark.asyncio
async def test_generate_turnover_categorizes_tickets(
    turnover_service: TurnoverService,
    mock_athena_client,
    sample_sev_ticket,
):
    """Should categorize tickets into parent, active, and pended."""
    active_ticket = {**sample_sev_ticket, "isParent": False, "id": "IR001"}
    parent_ticket = {**sample_sev_ticket, "isParent": True, "id": "IR002"}
    pended_ticket = {
        "id": "IR003",
        "title": "Pended issue",
        "status": {"name": "Pending"},
        "priority": 2,
        "isParent": False,
    }

    # First call returns active tickets, second returns pended
    mock_athena_client.search_incidents.side_effect = [
        [active_ticket, parent_ticket],
        [pended_ticket],
    ]
    mock_athena_client.search_change_requests.return_value = []

    request = TurnoverRequest(turnover_agent_name="Bob", sender_name="Alice")
    result = await turnover_service.generate_turnover(request)

    assert len(result.active_sevs) == 1
    assert result.active_sevs[0].id == "IR001"
    assert len(result.parent_tickets) == 1
    assert result.parent_tickets[0].id == "IR002"
    assert len(result.pended_sevs) == 1
    assert result.pended_sevs[0].id == "IR003"
    assert result.total_tickets == 3


@pytest.mark.asyncio
async def test_generate_turnover_includes_crs(
    turnover_service: TurnoverService,
    mock_athena_client,
    sample_change_request,
):
    """Should include upcoming change requests."""
    mock_athena_client.search_incidents.return_value = []
    mock_athena_client.search_change_requests.return_value = [sample_change_request]

    request = TurnoverRequest(turnover_agent_name="Bob", sender_name="Alice")
    result = await turnover_service.generate_turnover(request)

    assert len(result.upcoming_outages) == 1
    assert result.upcoming_outages[0].id == "CR10312956"
    assert "CR10312956" in result.email_body


@pytest.mark.asyncio
async def test_generate_turnover_calls_athena_three_times(
    turnover_service: TurnoverService,
    mock_athena_client,
):
    """Should make 3 Athena queries: active SEVs, pended SEVs, upcoming CRs."""
    mock_athena_client.search_incidents.return_value = []
    mock_athena_client.search_change_requests.return_value = []

    request = TurnoverRequest(turnover_agent_name="Bob", sender_name="Alice")
    await turnover_service.generate_turnover(request)

    assert mock_athena_client.search_incidents.call_count == 2
    assert mock_athena_client.search_change_requests.call_count == 1


@pytest.mark.asyncio
async def test_generate_turnover_handles_athena_errors(
    turnover_service: TurnoverService,
    mock_athena_client,
):
    """Should handle Athena query failures gracefully."""
    mock_athena_client.search_incidents.side_effect = Exception("Connection timeout")
    mock_athena_client.search_change_requests.side_effect = Exception("Connection timeout")

    request = TurnoverRequest(turnover_agent_name="Bob", sender_name="Alice")
    result = await turnover_service.generate_turnover(request)

    # Should still return a valid response with empty lists
    assert result.total_tickets == 0
    assert result.active_sevs == []
    assert result.pended_sevs == []
    assert result.upcoming_outages == []
    assert "SEV Turnover for" in result.email_subject


@pytest.mark.asyncio
async def test_generate_turnover_passes_lookahead_hours(
    turnover_service: TurnoverService,
    mock_athena_client,
):
    """Should use the hours_lookahead from the request."""
    mock_athena_client.search_incidents.return_value = []
    mock_athena_client.search_change_requests.return_value = []

    request = TurnoverRequest(
        turnover_agent_name="Bob",
        sender_name="Alice",
        hours_lookahead=48,
    )
    await turnover_service.generate_turnover(request)

    # Check the CR filter was built with 48h
    cr_call = mock_athena_client.search_change_requests.call_args[0][0]
    # Find the ScheduledStartDate lt filter
    cr_filters = cr_call[0]["filters"]
    lt_filter = [f for f in cr_filters if f.get("operator") == "lt"]
    assert len(lt_filter) == 1
    assert lt_filter[0]["value"] == "[now]+48h"


@pytest.mark.asyncio
async def test_generate_turnover_includes_notes_in_body(
    turnover_service: TurnoverService,
    mock_athena_client,
):
    """Should include all notes in the email body."""
    mock_athena_client.search_incidents.return_value = []
    mock_athena_client.search_change_requests.return_value = []

    request = TurnoverRequest(
        turnover_agent_name="Bob",
        sender_name="Alice",
        notes="Watch IR999 closely",
        escalation_notes="Escalated IR888 to ISOD at 14:00",
        voicemail_notes="Left VM for on-call network engineer",
    )
    result = await turnover_service.generate_turnover(request)

    assert "Watch IR999 closely" in result.email_body
    assert "Escalated IR888 to ISOD at 14:00" in result.email_body
    assert "Left VM for on-call network engineer" in result.email_body