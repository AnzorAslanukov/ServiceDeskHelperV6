"""
Unit tests for BulkAssignmentService — Feature #4: Bulk Assignment.

Tests the bulk assignment pipeline with mocked clients:
- Queue fetching (IR + SR merge, sorting, lock annotation)
- Lock management (lock, unlock, release, claim batch)
- Batch recommendations (sequential processing, error handling)
- Bulk assignment (Athena PUT, lock cleanup, error handling)
- Raw ticket to queue summary conversion
"""

import pytest

from feature4.models import TicketAssignment
from feature4.service import BulkAssignmentService


# ── Sample Queue Tickets ──────────────────────────────────────────────

# Raw ticket dicts as returned inside the Athena response
# Use real Athena status GUIDs so client-side filtering works correctly
IR_ACTIVE_GUID = "5e2d3932-ca6d-1515-7310-6f58584df73e"
IR_CLOSED_GUID = "bd0ae7c4-3315-2eb3-7933-82dfc482dbaf"
IR_RESOLVED_GUID = "2b8830b6-59f0-f574-9c2a-f4b4682f1681"
SR_SUBMITTED_GUID = "72b55e17-1c7d-b34c-53ae-f61f8732e425"
SR_CLOSED_GUID = "c7b65747-f99e-c108-1e17-3c1062138fc4"
SR_CANCELLED_GUID = "674e87e4-a58e-eab0-9a05-b48881de784c"

SAMPLE_IR_TICKETS = [
    {
        "id": "IR10001",
        "entityId": "eid-ir-10001",
        "title": "Printer jam on 2nd floor",
        "description": "Paper jam in HP LaserJet.",
        "status": {"name": "Active", "id": IR_ACTIVE_GUID},
        "priority": 3,
        "tierQueue": {"name": "Validation", "id": "tq-guid-1"},
        "affectedUser": {"displayName": "Alice Smith", "userName": "smitha"},
        "createdDate": "2026-04-14T10:00:00Z",
    },
    {
        "id": "IR10002",
        "entityId": "eid-ir-10002",
        "title": "VPN not connecting",
        "description": "User cannot connect to VPN from home.",
        "status": {"name": "Active", "id": IR_ACTIVE_GUID},
        "priority": 3,
        "tierQueue": {"name": "Validation", "id": "tq-guid-1"},
        "affectedUser": {"displayName": "Bob Jones", "userName": "jonesb"},
        "createdDate": "2026-04-14T09:00:00Z",
    },
]

SAMPLE_SR_TICKETS = [
    {
        "id": "SR20001",
        "entityId": "eid-sr-20001",
        "title": "Request PennChart access",
        "description": "New hire needs PennChart access.",
        "status": {"name": "Submitted", "id": SR_SUBMITTED_GUID},
        "priority": "Medium",
        "tierQueue": {"name": "Validation", "id": "tq-guid-2"},
        "affectedUser": {"displayName": "Carol White"},
        "createdDate": "2026-04-14T08:00:00Z",
    },
]

# Tickets with closed/resolved statuses (should be filtered out)
SAMPLE_CLOSED_IR_TICKETS = [
    {
        "id": "IR10003",
        "entityId": "eid-ir-10003",
        "title": "Old closed ticket",
        "description": "This was resolved months ago.",
        "status": IR_CLOSED_GUID,  # Plain GUID string (as returned by Athena view endpoint)
        "priority": 3,
        "tierQueue": "1a59b3b9-84a3-13ce-f50c-79b8a99f5531",
        "createdDate": "2025-01-01T10:00:00Z",
    },
    {
        "id": "IR10004",
        "entityId": "eid-ir-10004",
        "title": "Another resolved ticket",
        "description": "Resolved last week.",
        "status": IR_RESOLVED_GUID,
        "priority": 3,
        "tierQueue": "1a59b3b9-84a3-13ce-f50c-79b8a99f5531",
        "createdDate": "2026-04-10T10:00:00Z",
    },
]

SAMPLE_CLOSED_SR_TICKETS = [
    {
        "id": "SR20002",
        "entityId": "eid-sr-20002",
        "title": "Old cancelled SR",
        "description": "Cancelled request.",
        "status": SR_CANCELLED_GUID,
        "priority": "Medium",
        "createdDate": "2025-06-01T10:00:00Z",
    },
]


def _paged(tickets: list) -> dict:
    """Wrap a ticket list in the paged response dict that AthenaClient returns."""
    return {
        "results": tickets,
        "total": len(tickets),
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }


# Convenience: paged responses matching what the real AthenaClient returns
SAMPLE_IR_QUEUE = _paged(SAMPLE_IR_TICKETS)
SAMPLE_SR_QUEUE = _paged(SAMPLE_SR_TICKETS)
EMPTY_QUEUE = _paged([])



# ── Queue Fetching ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_queue_merges_ir_and_sr(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should merge IR and SR results into a single queue."""
    mock_athena_client.search_incidents.return_value = SAMPLE_IR_QUEUE
    mock_athena_client.search_service_requests.return_value = SAMPLE_SR_QUEUE

    result = await bulk_assignment_service.fetch_queue()

    assert result.total == 3
    ticket_ids = [t.id for t in result.tickets]
    assert "IR10001" in ticket_ids
    assert "IR10002" in ticket_ids
    assert "SR20001" in ticket_ids


@pytest.mark.asyncio
async def test_fetch_queue_sorts_by_created_date(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should sort tickets by created date (oldest first)."""
    mock_athena_client.search_incidents.return_value = SAMPLE_IR_QUEUE
    mock_athena_client.search_service_requests.return_value = SAMPLE_SR_QUEUE

    result = await bulk_assignment_service.fetch_queue()

    dates = [t.created_date for t in result.tickets]
    assert dates == sorted(dates)
    # SR20001 (08:00) should be first, IR10002 (09:00) second, IR10001 (10:00) third
    assert result.tickets[0].id == "SR20001"
    assert result.tickets[1].id == "IR10002"
    assert result.tickets[2].id == "IR10001"


@pytest.mark.asyncio
async def test_fetch_queue_annotates_locks(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should annotate tickets with lock state."""
    mock_athena_client.search_incidents.return_value = SAMPLE_IR_QUEUE
    mock_athena_client.search_service_requests.return_value = EMPTY_QUEUE

    # Lock one ticket
    bulk_assignment_service.lock_tickets(["IR10001"], "user_a")

    result = await bulk_assignment_service.fetch_queue()

    ir10001 = next(t for t in result.tickets if t.id == "IR10001")
    ir10002 = next(t for t in result.tickets if t.id == "IR10002")
    assert ir10001.locked_by == "user_a"
    assert ir10002.locked_by is None
    assert result.locks == {"IR10001": "user_a"}


@pytest.mark.asyncio
async def test_fetch_queue_handles_ir_failure(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should still return SR results if IR fetch fails."""
    mock_athena_client.search_incidents.side_effect = Exception("IR fetch failed")
    mock_athena_client.search_service_requests.return_value = SAMPLE_SR_QUEUE

    result = await bulk_assignment_service.fetch_queue()

    assert result.total == 1
    assert result.tickets[0].id == "SR20001"


@pytest.mark.asyncio
async def test_fetch_queue_handles_sr_failure(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should still return IR results if SR fetch fails."""
    mock_athena_client.search_incidents.return_value = SAMPLE_IR_QUEUE
    mock_athena_client.search_service_requests.side_effect = Exception("SR fetch failed")

    result = await bulk_assignment_service.fetch_queue()

    assert result.total == 2


@pytest.mark.asyncio
async def test_fetch_queue_extracts_ticket_type(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should correctly set ticket_type for IR and SR tickets."""
    mock_athena_client.search_incidents.return_value = _paged(SAMPLE_IR_TICKETS[:1])
    mock_athena_client.search_service_requests.return_value = SAMPLE_SR_QUEUE

    result = await bulk_assignment_service.fetch_queue()

    ir_ticket = next(t for t in result.tickets if t.id == "IR10001")
    sr_ticket = next(t for t in result.tickets if t.id == "SR20001")
    assert ir_ticket.ticket_type == "incident"
    assert sr_ticket.ticket_type == "servicerequest"


@pytest.mark.asyncio
async def test_fetch_queue_extracts_entity_id(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should extract entityId for each ticket (needed for PUT)."""
    mock_athena_client.search_incidents.return_value = _paged(SAMPLE_IR_TICKETS[:1])
    mock_athena_client.search_service_requests.return_value = EMPTY_QUEUE

    result = await bulk_assignment_service.fetch_queue()

    assert result.tickets[0].entity_id == "eid-ir-10001"


# ── Lock Management ───────────────────────────────────────────────────


def test_lock_tickets(bulk_assignment_service: BulkAssignmentService):
    """Should lock unlocked tickets for a user."""
    locked = bulk_assignment_service.lock_tickets(["IR10001", "IR10002"], "user_a")
    assert locked == ["IR10001", "IR10002"]
    assert bulk_assignment_service.get_locks() == {
        "IR10001": "user_a",
        "IR10002": "user_a",
    }


def test_lock_already_locked_by_other(bulk_assignment_service: BulkAssignmentService):
    """Should not lock tickets already locked by another user."""
    bulk_assignment_service.lock_tickets(["IR10001"], "user_a")
    locked = bulk_assignment_service.lock_tickets(["IR10001", "IR10002"], "user_b")
    assert locked == ["IR10002"]
    assert bulk_assignment_service.get_locks()["IR10001"] == "user_a"


def test_lock_already_locked_by_same_user(bulk_assignment_service: BulkAssignmentService):
    """Should count already-owned locks as success."""
    bulk_assignment_service.lock_tickets(["IR10001"], "user_a")
    locked = bulk_assignment_service.lock_tickets(["IR10001"], "user_a")
    assert locked == ["IR10001"]


def test_unlock_tickets(bulk_assignment_service: BulkAssignmentService):
    """Should unlock tickets owned by the user."""
    bulk_assignment_service.lock_tickets(["IR10001", "IR10002"], "user_a")
    unlocked = bulk_assignment_service.unlock_tickets(["IR10001"], "user_a")
    assert unlocked == ["IR10001"]
    assert "IR10001" not in bulk_assignment_service.get_locks()
    assert "IR10002" in bulk_assignment_service.get_locks()


def test_unlock_not_owned(bulk_assignment_service: BulkAssignmentService):
    """Should not unlock tickets owned by another user."""
    bulk_assignment_service.lock_tickets(["IR10001"], "user_a")
    unlocked = bulk_assignment_service.unlock_tickets(["IR10001"], "user_b")
    assert unlocked == []
    assert bulk_assignment_service.get_locks()["IR10001"] == "user_a"


def test_release_user_locks(bulk_assignment_service: BulkAssignmentService):
    """Should release all locks for a user."""
    bulk_assignment_service.lock_tickets(["IR10001", "IR10002"], "user_a")
    bulk_assignment_service.lock_tickets(["IR10003"], "user_b")

    released = bulk_assignment_service.release_user_locks("user_a")

    assert set(released) == {"IR10001", "IR10002"}
    assert bulk_assignment_service.get_locks() == {"IR10003": "user_b"}


def test_release_user_locks_empty(bulk_assignment_service: BulkAssignmentService):
    """Should return empty list if user has no locks."""
    released = bulk_assignment_service.release_user_locks("user_x")
    assert released == []


def test_claim_batch(bulk_assignment_service: BulkAssignmentService):
    """Should claim the first N unlocked tickets."""
    queue_ids = ["IR10001", "IR10002", "IR10003", "IR10004"]
    claimed = bulk_assignment_service.claim_batch("user_a", 2, queue_ids)
    assert claimed == ["IR10001", "IR10002"]
    assert bulk_assignment_service.get_locks() == {
        "IR10001": "user_a",
        "IR10002": "user_a",
    }


def test_claim_batch_skips_locked(bulk_assignment_service: BulkAssignmentService):
    """Should skip tickets locked by other users."""
    bulk_assignment_service.lock_tickets(["IR10001"], "user_b")
    queue_ids = ["IR10001", "IR10002", "IR10003"]
    claimed = bulk_assignment_service.claim_batch("user_a", 2, queue_ids)
    assert claimed == ["IR10002", "IR10003"]


def test_claim_batch_includes_own_locks(bulk_assignment_service: BulkAssignmentService):
    """Should include tickets already locked by the claiming user."""
    bulk_assignment_service.lock_tickets(["IR10001"], "user_a")
    queue_ids = ["IR10001", "IR10002", "IR10003"]
    claimed = bulk_assignment_service.claim_batch("user_a", 2, queue_ids)
    assert claimed == ["IR10001", "IR10002"]


def test_claim_batch_with_client_provided_ids(bulk_assignment_service: BulkAssignmentService):
    """Should work with client-provided ticket IDs (fast path — no Athena fetch)."""
    # Simulate client sending its local queue order
    client_ids = ["SR20001", "IR10002", "IR10001"]
    claimed = bulk_assignment_service.claim_batch("user_a", 2, client_ids)
    assert claimed == ["SR20001", "IR10002"]
    assert bulk_assignment_service.get_locks() == {
        "SR20001": "user_a",
        "IR10002": "user_a",
    }


def test_claim_batch_with_empty_client_ids(bulk_assignment_service: BulkAssignmentService):
    """Should return empty list when client provides empty ticket_ids."""
    claimed = bulk_assignment_service.claim_batch("user_a", 5, [])
    assert claimed == []
    assert bulk_assignment_service.get_locks() == {}


def test_claim_batch_request_model_with_ticket_ids():
    """ClaimBatchRequest should accept optional ticket_ids field."""
    from feature4.models import ClaimBatchRequest

    # With ticket_ids
    req = ClaimBatchRequest(
        user_id="user_a",
        batch_size=5,
        ticket_ids=["IR10001", "IR10002"],
    )
    assert req.ticket_ids == ["IR10001", "IR10002"]
    assert req.batch_size == 5

    # Without ticket_ids (backward compatible)
    req2 = ClaimBatchRequest(user_id="user_b", batch_size=3)
    assert req2.ticket_ids is None
    assert req2.batch_size == 3


def test_claim_batch_request_model_ticket_ids_none():
    """ClaimBatchRequest ticket_ids defaults to None when omitted."""
    from feature4.models import ClaimBatchRequest

    req = ClaimBatchRequest(user_id="user_a")
    assert req.ticket_ids is None
    assert req.batch_size == 10  # default


# ── Batch Recommendations ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_recommend_success(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
    sample_athena_ticket,
):
    """Should generate recommendations for each ticket."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket

    result = await bulk_assignment_service.batch_recommend(["IR1959493"])

    assert result.total == 1
    assert result.failed == 0
    assert result.recommendations[0].success is True
    assert result.recommendations[0].recommendation.support_group_name == "HUP"


@pytest.mark.asyncio
async def test_batch_recommend_handles_failure(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should handle individual ticket failures gracefully."""
    mock_athena_client.get_ticket.side_effect = Exception("Ticket not found")

    result = await bulk_assignment_service.batch_recommend(["IR99999"])

    assert result.total == 1
    assert result.failed == 1
    assert result.recommendations[0].success is False
    assert "Ticket not found" in result.recommendations[0].error


@pytest.mark.asyncio
async def test_batch_recommend_mixed_results(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
    sample_athena_ticket,
):
    """Should handle a mix of successful and failed recommendations."""
    # First call succeeds, second fails
    mock_athena_client.get_ticket.side_effect = [
        sample_athena_ticket,
        Exception("Not found"),
    ]

    result = await bulk_assignment_service.batch_recommend(["IR1959493", "IR99999"])

    assert result.total == 2
    assert result.failed == 1
    assert result.recommendations[0].success is True
    assert result.recommendations[1].success is False


# ── Bulk Assignment ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assign_tickets_success(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should assign tickets and return success results."""
    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"id": "tq-guid", "name": "EUS"},
        "priority": 3,
    }

    # Lock the ticket first
    bulk_assignment_service.lock_tickets(["IR10001"], "user_a")

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-ir-10001",
            tier_queue_guid="ae9eb3ff-458a-206f-7815-129d50efa285",
            tier_queue_name="EUS",
            priority=3,
        ),
    ]

    result = await bulk_assignment_service.assign_tickets(assignments)

    assert result.total_assigned == 1
    assert result.total_failed == 0
    assert result.results[0].success is True
    assert result.results[0].updated_tier_queue == "EUS"
    assert result.results[0].updated_priority == 3

    # Lock should be removed after successful assignment
    assert "IR10001" not in bulk_assignment_service.get_locks()


@pytest.mark.asyncio
async def test_assign_tickets_calls_athena_correctly(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should call update_ticket with correct parameters."""
    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "EUS"},
        "priority": 3,
    }

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-ir-10001",
            tier_queue_guid="ae9eb3ff-458a-206f-7815-129d50efa285",
            priority=3,
        ),
    ]

    await bulk_assignment_service.assign_tickets(assignments)

    mock_athena_client.update_ticket.assert_called_once_with(
        ticket_id="IR10001",
        entity_id="eid-ir-10001",
        tier_queue_guid="ae9eb3ff-458a-206f-7815-129d50efa285",
        priority=3,
    )


@pytest.mark.asyncio
async def test_assign_tickets_handles_failure(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should handle assignment failures gracefully."""
    mock_athena_client.update_ticket.side_effect = Exception("Athena error")

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-ir-10001",
            tier_queue_guid="some-guid",
        ),
    ]

    result = await bulk_assignment_service.assign_tickets(assignments)

    assert result.total_assigned == 0
    assert result.total_failed == 1
    assert result.results[0].success is False
    assert "Athena error" in result.results[0].error


@pytest.mark.asyncio
async def test_assign_tickets_mixed_results(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should handle a mix of successful and failed assignments."""
    mock_athena_client.update_ticket.side_effect = [
        {"tierQueue": {"name": "EUS"}, "priority": 3},
        Exception("Athena error"),
    ]

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-1",
            tier_queue_guid="guid-1",
        ),
        TicketAssignment(
            ticket_id="IR10002",
            entity_id="eid-2",
            tier_queue_guid="guid-2",
        ),
    ]

    result = await bulk_assignment_service.assign_tickets(assignments)

    assert result.total_assigned == 1
    assert result.total_failed == 1


@pytest.mark.asyncio
async def test_assign_without_priority(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should pass None priority when not specified."""
    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "EUS"},
        "priority": 3,
    }

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-1",
            tier_queue_guid="guid-1",
            priority=None,
        ),
    ]

    await bulk_assignment_service.assign_tickets(assignments)

    mock_athena_client.update_ticket.assert_called_once_with(
        ticket_id="IR10001",
        entity_id="eid-1",
        tier_queue_guid="guid-1",
        priority=None,
    )


# ── Raw Ticket Conversion ────────────────────────────────────────────


def test_raw_to_queue_summary_incident():
    """Should convert a raw IR ticket to QueueTicketSummary."""
    raw = SAMPLE_IR_TICKETS[0]
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")

    assert summary is not None
    assert summary.id == "IR10001"
    assert summary.entity_id == "eid-ir-10001"
    assert summary.ticket_type == "incident"
    assert summary.title == "Printer jam on 2nd floor"
    assert summary.status == "Active"
    assert summary.priority == 3
    assert summary.tier_queue == "Validation"
    assert summary.affected_user == "Alice Smith"


def test_raw_to_queue_summary_service_request():
    """Should convert a raw SR ticket to QueueTicketSummary."""
    raw = SAMPLE_SR_TICKETS[0]
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "servicerequest")

    assert summary is not None
    assert summary.id == "SR20001"
    assert summary.ticket_type == "servicerequest"
    assert summary.priority == "Medium"


def test_raw_to_queue_summary_missing_id():
    """Should return None for tickets without an id."""
    raw = {"entityId": "eid-1", "title": "No ID ticket"}
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")
    assert summary is None


def test_raw_to_queue_summary_missing_entity_id():
    """Should return None for tickets without an entityId."""
    raw = {"id": "IR10001", "title": "No entity ID"}
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")
    assert summary is None


def test_raw_to_queue_summary_preserves_full_description():
    """Should preserve the full description without truncation."""
    raw = {
        "id": "IR10001",
        "entityId": "eid-1",
        "description": "A" * 300,
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")
    assert summary is not None
    assert len(summary.description) == 300  # Full description preserved
    assert summary.description == "A" * 300


def test_raw_to_queue_summary_string_status():
    """Should handle status as a plain string (non-GUID)."""
    raw = {
        "id": "IR10001",
        "entityId": "eid-1",
        "status": "Active",
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")
    assert summary is not None
    assert summary.status == "Active"


# ── GUID-to-Name Resolution ──────────────────────────────────────────


def test_resolve_status_guid_to_name_ir():
    """Should resolve IR status GUIDs to human-readable names."""
    raw = {
        "id": "IR10001",
        "entityId": "eid-1",
        "status": IR_ACTIVE_GUID,
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")
    assert summary is not None
    assert summary.status == "Active"


def test_resolve_status_guid_to_name_sr():
    """Should resolve SR status GUIDs to human-readable names."""
    raw = {
        "id": "SR20001",
        "entityId": "eid-1",
        "status": SR_SUBMITTED_GUID,
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "servicerequest")
    assert summary is not None
    assert summary.status == "Submitted"


def test_resolve_status_guid_closed():
    """Should resolve closed status GUID to 'Closed'."""
    raw = {
        "id": "IR10001",
        "entityId": "eid-1",
        "status": IR_CLOSED_GUID,
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")
    assert summary is not None
    assert summary.status == "Closed"


def test_resolve_status_dict_preserves_name():
    """Should extract name from dict status (not resolve GUID)."""
    raw = {
        "id": "IR10001",
        "entityId": "eid-1",
        "status": {"name": "Active", "id": IR_ACTIVE_GUID},
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")
    assert summary is not None
    assert summary.status == "Active"


def test_resolve_status_unknown_guid_passes_through():
    """Should pass through unknown GUIDs unchanged."""
    unknown_guid = "00000000-0000-0000-0000-000000000000"
    raw = {
        "id": "IR10001",
        "entityId": "eid-1",
        "status": unknown_guid,
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")
    assert summary is not None
    assert summary.status == unknown_guid


def test_resolve_sr_priority_guid_to_name():
    """Should resolve SR priority GUIDs to human-readable names."""
    raw = {
        "id": "SR20001",
        "entityId": "eid-1",
        "priority": "dd43a3a8-c640-2146-85a4-77978e3bb375",  # Medium
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "servicerequest")
    assert summary is not None
    assert summary.priority == "Medium"


def test_resolve_sr_priority_guid_high():
    """Should resolve SR priority GUID 'High'."""
    raw = {
        "id": "SR20001",
        "entityId": "eid-1",
        "priority": "536beaf3-62a8-5dd0-248a-39c2bf86d3bc",  # High
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "servicerequest")
    assert summary is not None
    assert summary.priority == "High"


def test_resolve_ir_priority_numeric_unchanged():
    """Should pass through IR numeric priorities unchanged."""
    raw = {
        "id": "IR10001",
        "entityId": "eid-1",
        "priority": 3,
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")
    assert summary is not None
    assert summary.priority == 3


def test_resolve_sr_priority_string_non_guid():
    """Should resolve SR string priority 'Medium' (already human-readable)."""
    raw = {
        "id": "SR20001",
        "entityId": "eid-1",
        "priority": "Medium",
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "servicerequest")
    assert summary is not None
    assert summary.priority == "Medium"


def test_resolve_sr_priority_unknown_guid_passes_through():
    """Should pass through unknown SR priority GUIDs unchanged."""
    unknown_guid = "00000000-0000-0000-0000-000000000000"
    raw = {
        "id": "SR20001",
        "entityId": "eid-1",
        "priority": unknown_guid,
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "servicerequest")
    assert summary is not None
    assert summary.priority == unknown_guid


def test_resolve_priority_none():
    """Should handle None priority."""
    raw = {
        "id": "IR10001",
        "entityId": "eid-1",
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")
    assert summary is not None
    assert summary.priority is None


# ── Client-Side Status Filtering ─────────────────────────────────────


def test_is_open_status_with_dict():
    """Should detect open status from a dict with 'id' key."""
    raw = {"status": {"name": "Active", "id": IR_ACTIVE_GUID}}
    assert BulkAssignmentService._is_open_status(raw, BulkAssignmentService.IR_OPEN_STATUS_GUIDS) is True


def test_is_open_status_with_guid_string():
    """Should detect open status from a plain GUID string."""
    raw = {"status": IR_ACTIVE_GUID}
    assert BulkAssignmentService._is_open_status(raw, BulkAssignmentService.IR_OPEN_STATUS_GUIDS) is True


def test_is_open_status_closed_dict():
    """Should reject closed status from a dict."""
    raw = {"status": {"name": "Closed", "id": IR_CLOSED_GUID}}
    assert BulkAssignmentService._is_open_status(raw, BulkAssignmentService.IR_OPEN_STATUS_GUIDS) is False


def test_is_open_status_closed_guid_string():
    """Should reject closed status from a plain GUID string."""
    raw = {"status": IR_CLOSED_GUID}
    assert BulkAssignmentService._is_open_status(raw, BulkAssignmentService.IR_OPEN_STATUS_GUIDS) is False


def test_is_open_status_none():
    """Should reject tickets with no status."""
    raw = {}
    assert BulkAssignmentService._is_open_status(raw, BulkAssignmentService.IR_OPEN_STATUS_GUIDS) is False


def test_is_open_status_sr_submitted():
    """Should detect open SR status (Submitted)."""
    raw = {"status": SR_SUBMITTED_GUID}
    assert BulkAssignmentService._is_open_status(raw, BulkAssignmentService.SR_OPEN_STATUS_GUIDS) is True


def test_is_open_status_sr_cancelled():
    """Should reject cancelled SR status."""
    raw = {"status": SR_CANCELLED_GUID}
    assert BulkAssignmentService._is_open_status(raw, BulkAssignmentService.SR_OPEN_STATUS_GUIDS) is False


@pytest.mark.asyncio
async def test_fetch_queue_filters_closed_tickets(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should filter out Closed/Resolved tickets, keeping only open ones."""
    # Mix of open and closed tickets
    all_ir = SAMPLE_IR_TICKETS + SAMPLE_CLOSED_IR_TICKETS
    all_sr = SAMPLE_SR_TICKETS + SAMPLE_CLOSED_SR_TICKETS

    mock_athena_client.search_incidents.return_value = _paged(all_ir)
    mock_athena_client.search_service_requests.return_value = _paged(all_sr)

    result = await bulk_assignment_service.fetch_queue()

    # Should only include the 3 open tickets, not the 3 closed/resolved/cancelled ones
    assert result.total == 3
    ticket_ids = {t.id for t in result.tickets}
    assert ticket_ids == {"IR10001", "IR10002", "SR20001"}
    # Closed/resolved/cancelled should be excluded
    assert "IR10003" not in ticket_ids
    assert "IR10004" not in ticket_ids
    assert "SR20002" not in ticket_ids


@pytest.mark.asyncio
async def test_fetch_queue_all_closed_returns_empty(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should return empty queue if all tickets are closed."""
    mock_athena_client.search_incidents.return_value = _paged(SAMPLE_CLOSED_IR_TICKETS)
    mock_athena_client.search_service_requests.return_value = _paged(SAMPLE_CLOSED_SR_TICKETS)

    result = await bulk_assignment_service.fetch_queue()

    assert result.total == 0
    assert result.tickets == []


# ── Support Group Lists (Manual Assignment) ───────────────────────────


@pytest.mark.asyncio
async def test_get_support_groups_loads_from_json(
    bulk_assignment_service: BulkAssignmentService,
):
    """Should load support groups from the pre-generated JSON file."""
    # Clear cache to force reload
    BulkAssignmentService._support_group_cache.clear()

    groups = await bulk_assignment_service.get_support_groups("incident")

    assert len(groups) > 0
    # Each group should have name and guid
    assert "name" in groups[0]
    assert "guid" in groups[0]
    # Should be sorted by name
    names = [g["name"] for g in groups]
    assert names == sorted(names)


@pytest.mark.asyncio
async def test_get_support_groups_caches_result(
    bulk_assignment_service: BulkAssignmentService,
):
    """Should cache support groups after first load."""
    BulkAssignmentService._support_group_cache.clear()

    groups1 = await bulk_assignment_service.get_support_groups("incident")
    groups2 = await bulk_assignment_service.get_support_groups("incident")

    # Should be the same object (cached)
    assert groups1 is groups2


@pytest.mark.asyncio
async def test_get_support_groups_ir_vs_sr(
    bulk_assignment_service: BulkAssignmentService,
):
    """Should return different groups for IR vs SR ticket types."""
    BulkAssignmentService._support_group_cache.clear()

    ir_groups = await bulk_assignment_service.get_support_groups("incident")
    sr_groups = await bulk_assignment_service.get_support_groups("servicerequest")

    # Both should have groups
    assert len(ir_groups) > 0
    assert len(sr_groups) > 0

    # GUIDs should differ between IR and SR for the same group name
    ir_guids = {g["guid"] for g in ir_groups}
    sr_guids = {g["guid"] for g in sr_groups}
    # There should be zero overlap (IR and SR use completely different GUIDs)
    assert len(ir_guids & sr_guids) == 0


@pytest.mark.asyncio
async def test_get_support_groups_fallback_to_athena(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should fall back to Athena enum tree if JSON file is unavailable."""
    BulkAssignmentService._support_group_cache.clear()

    # Mock _load_groups_from_json to return empty (simulating missing file)
    original_load = BulkAssignmentService._load_groups_from_json
    BulkAssignmentService._load_groups_from_json = staticmethod(lambda tt: [])

    # Mock Athena enum tree response
    mock_athena_client.get_enum_tree.return_value = [
        {
            "name": "Service Desk",
            "id": "sd-guid-001",
            "disabled": False,
            "children": [
                {
                    "name": "Validation",
                    "id": "val-guid-001",
                    "disabled": True,
                    "children": [],
                },
                {
                    "name": "ATLAS",
                    "id": "atlas-guid-001",
                    "disabled": False,
                    "children": [],
                },
            ],
        },
    ]

    try:
        groups = await bulk_assignment_service.get_support_groups("incident")

        assert len(groups) == 2  # Service Desk + Service Desk\ATLAS (Validation is disabled)
        names = [g["name"] for g in groups]
        assert "Service Desk" in names
        assert "Service Desk\\ATLAS" in names
        # Disabled group should be excluded
        assert "Service Desk\\Validation" not in names
    finally:
        BulkAssignmentService._load_groups_from_json = original_load
        BulkAssignmentService._support_group_cache.clear()


def test_flatten_enum_tree_basic():
    """Should flatten a simple enum tree into name/guid pairs."""
    tree = [
        {
            "name": "Group A",
            "id": "guid-a",
            "disabled": False,
            "children": [
                {"name": "Sub A1", "id": "guid-a1", "disabled": False, "children": []},
                {"name": "Sub A2", "id": "guid-a2", "disabled": True, "children": []},
            ],
        },
    ]
    result = []
    BulkAssignmentService._flatten_enum_tree(tree, result, prefix="")

    names = [g["name"] for g in result]
    assert "Group A" in names
    assert "Group A\\Sub A1" in names
    assert "Group A\\Sub A2" not in names  # disabled


def test_flatten_enum_tree_empty():
    """Should handle empty tree."""
    result = []
    BulkAssignmentService._flatten_enum_tree([], result, prefix="")
    assert result == []


def test_flatten_enum_tree_nested():
    """Should handle deeply nested trees."""
    tree = [
        {
            "name": "Top",
            "id": "guid-top",
            "disabled": False,
            "children": [
                {
                    "name": "Mid",
                    "id": "guid-mid",
                    "disabled": False,
                    "children": [
                        {"name": "Leaf", "id": "guid-leaf", "disabled": False, "children": []},
                    ],
                },
            ],
        },
    ]
    result = []
    BulkAssignmentService._flatten_enum_tree(tree, result, prefix="")

    names = [g["name"] for g in result]
    assert "Top" in names
    assert "Top\\Mid" in names
    assert "Top\\Mid\\Leaf" in names


# ══════════════════════════════════════════════════════════════════════
# Streaming Queue Fetch Tests
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fetch_queue_streaming_calls_on_ticket(
    bulk_assignment_service, mock_athena_client
):
    """fetch_queue_streaming should call on_ticket for each open ticket."""
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS,
        "total": 2,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }
    mock_athena_client.search_service_requests.return_value = {
        "results": SAMPLE_SR_TICKETS,
        "total": 1,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }

    received_tickets = []
    received_counts = []

    async def on_ticket(ticket, count):
        received_tickets.append(ticket)
        received_counts.append(count)

    total = await bulk_assignment_service.fetch_queue_streaming(
        on_ticket=on_ticket,
    )

    assert total == 3
    assert len(received_tickets) == 3
    # Counts should be sequential: 1, 2, 3
    assert received_counts == [1, 2, 3]
    # Verify ticket IDs
    ids = [t.id for t in received_tickets]
    assert "IR10001" in ids
    assert "IR10002" in ids
    assert "SR20001" in ids


@pytest.mark.asyncio
async def test_fetch_queue_streaming_filters_closed(
    bulk_assignment_service, mock_athena_client
):
    """fetch_queue_streaming should filter out closed/resolved tickets."""
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS + SAMPLE_CLOSED_IR_TICKETS,
        "total": 4,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }
    mock_athena_client.search_service_requests.return_value = {
        "results": [],
        "total": 0,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }

    received_tickets = []

    async def on_ticket(ticket, count):
        received_tickets.append(ticket)

    total = await bulk_assignment_service.fetch_queue_streaming(
        on_ticket=on_ticket,
    )

    # Only the 2 open IR tickets should be streamed (closed/resolved filtered out)
    assert total == 2
    assert len(received_tickets) == 2
    ids = [t.id for t in received_tickets]
    assert "IR10003" not in ids
    assert "IR10004" not in ids


@pytest.mark.asyncio
async def test_fetch_queue_streaming_calls_on_phase(
    bulk_assignment_service, mock_athena_client
):
    """fetch_queue_streaming should call on_phase for each processing phase."""
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS,
        "total": 2,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }
    mock_athena_client.search_service_requests.return_value = {
        "results": [],
        "total": 0,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }

    phases = []

    async def on_ticket(ticket, count):
        pass

    async def on_phase(phase_name):
        phases.append(phase_name)

    await bulk_assignment_service.fetch_queue_streaming(
        on_ticket=on_ticket,
        on_phase=on_phase,
    )

    assert phases == ["fetching", "processing_ir", "processing_sr", "complete"]


@pytest.mark.asyncio
async def test_fetch_queue_streaming_handles_athena_error(
    bulk_assignment_service, mock_athena_client
):
    """fetch_queue_streaming should handle Athena errors gracefully."""
    mock_athena_client.search_incidents.side_effect = Exception("Athena IR error")
    mock_athena_client.search_service_requests.return_value = {
        "results": SAMPLE_SR_TICKETS,
        "total": 1,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }

    received_tickets = []

    async def on_ticket(ticket, count):
        received_tickets.append(ticket)

    total = await bulk_assignment_service.fetch_queue_streaming(
        on_ticket=on_ticket,
    )

    # Only SR tickets should be streamed (IR failed)
    assert total == 1
    assert received_tickets[0].id == "SR20001"


@pytest.mark.asyncio
async def test_fetch_queue_streaming_annotates_locks(
    bulk_assignment_service, mock_athena_client
):
    """fetch_queue_streaming should annotate tickets with lock state."""
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS,
        "total": 2,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }
    mock_athena_client.search_service_requests.return_value = {
        "results": [],
        "total": 0,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }

    # Lock one ticket
    bulk_assignment_service.lock_tickets(["IR10001"], "user_a")

    received_tickets = []

    async def on_ticket(ticket, count):
        received_tickets.append(ticket)

    await bulk_assignment_service.fetch_queue_streaming(
        on_ticket=on_ticket,
    )

    locked_ticket = next(t for t in received_tickets if t.id == "IR10001")
    unlocked_ticket = next(t for t in received_tickets if t.id == "IR10002")
    assert locked_ticket.locked_by == "user_a"
    assert unlocked_ticket.locked_by is None


@pytest.mark.asyncio
async def test_fetch_queue_streaming_empty_queue(
    bulk_assignment_service, mock_athena_client
):
    """fetch_queue_streaming should return 0 for empty queue."""
    # Default mock returns empty results

    received_tickets = []

    async def on_ticket(ticket, count):
        received_tickets.append(ticket)

    total = await bulk_assignment_service.fetch_queue_streaming(
        on_ticket=on_ticket,
    )

    assert total == 0
    assert len(received_tickets) == 0


# ══════════════════════════════════════════════════════════════════════
# Incremental Queue Refresh (compute_queue_diff)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_compute_queue_diff_no_changes(
    bulk_assignment_service, mock_athena_client
):
    """compute_queue_diff returns empty added/removed when queue is unchanged."""
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:2]
    }
    mock_athena_client.search_service_requests.return_value = {
        "results": SAMPLE_SR_TICKETS[:1]
    }

    # First call — establishes baseline
    diff1 = await bulk_assignment_service.compute_queue_diff()
    assert diff1["total"] == 3
    assert len(diff1["added"]) == 3  # all new on first call
    assert len(diff1["removed"]) == 0

    # Second call — same data, no changes
    diff2 = await bulk_assignment_service.compute_queue_diff()
    assert diff2["total"] == 3
    assert len(diff2["added"]) == 0
    assert len(diff2["removed"]) == 0


@pytest.mark.asyncio
async def test_compute_queue_diff_tickets_added(
    bulk_assignment_service, mock_athena_client
):
    """compute_queue_diff detects newly added tickets."""
    # Initial: 1 IR ticket
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:1]
    }
    mock_athena_client.search_service_requests.return_value = {"results": []}

    diff1 = await bulk_assignment_service.compute_queue_diff()
    assert diff1["total"] == 1
    assert len(diff1["added"]) == 1

    # Now add a second IR ticket
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:2]
    }

    diff2 = await bulk_assignment_service.compute_queue_diff()
    assert diff2["total"] == 2
    assert len(diff2["added"]) == 1
    assert diff2["added"][0].id == "IR10002"
    assert len(diff2["removed"]) == 0


@pytest.mark.asyncio
async def test_compute_queue_diff_tickets_removed(
    bulk_assignment_service, mock_athena_client
):
    """compute_queue_diff detects removed tickets."""
    # Initial: 2 IR tickets
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:2]
    }
    mock_athena_client.search_service_requests.return_value = {"results": []}

    diff1 = await bulk_assignment_service.compute_queue_diff()
    assert diff1["total"] == 2

    # Now only 1 IR ticket remains (IR10002 removed)
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:1]
    }

    diff2 = await bulk_assignment_service.compute_queue_diff()
    assert diff2["total"] == 1
    assert len(diff2["added"]) == 0
    assert len(diff2["removed"]) == 1
    assert "IR10002" in diff2["removed"]


@pytest.mark.asyncio
async def test_compute_queue_diff_mixed_changes(
    bulk_assignment_service, mock_athena_client
):
    """compute_queue_diff handles simultaneous additions and removals."""
    # Initial: IR10001 + SR20001
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:1]
    }
    mock_athena_client.search_service_requests.return_value = {
        "results": SAMPLE_SR_TICKETS[:1]
    }

    diff1 = await bulk_assignment_service.compute_queue_diff()
    assert diff1["total"] == 2

    # Now: IR10002 added, SR20001 removed
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:2]
    }
    mock_athena_client.search_service_requests.return_value = {"results": []}

    diff2 = await bulk_assignment_service.compute_queue_diff()
    assert diff2["total"] == 2
    assert len(diff2["added"]) == 1
    assert diff2["added"][0].id == "IR10002"
    assert len(diff2["removed"]) == 1
    assert "SR20001" in diff2["removed"]


@pytest.mark.asyncio
async def test_compute_queue_diff_cleans_locks_for_removed(
    bulk_assignment_service, mock_athena_client
):
    """compute_queue_diff cleans up locks for removed tickets."""
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:2]
    }
    mock_athena_client.search_service_requests.return_value = {"results": []}

    # Establish baseline
    await bulk_assignment_service.compute_queue_diff()

    # Lock IR10002
    bulk_assignment_service.lock_tickets(["IR10002"], "user_a")
    assert "IR10002" in bulk_assignment_service.get_locks()

    # Remove IR10002 from queue
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:1]
    }

    diff = await bulk_assignment_service.compute_queue_diff()
    assert "IR10002" in diff["removed"]
    # Lock should be cleaned up
    assert "IR10002" not in bulk_assignment_service.get_locks()


def test_snapshot_ticket_ids(bulk_assignment_service):
    """snapshot_ticket_ids sets the last-known ticket ID set."""
    assert bulk_assignment_service._last_known_ticket_ids == set()

    bulk_assignment_service.snapshot_ticket_ids({"IR10001", "IR10002", "SR30001"})
    assert bulk_assignment_service._last_known_ticket_ids == {"IR10001", "IR10002", "SR30001"}


@pytest.mark.asyncio
async def test_compute_queue_diff_after_snapshot(
    bulk_assignment_service, mock_athena_client
):
    """compute_queue_diff produces correct diff after snapshot_ticket_ids."""
    # Snapshot as if initial load had IR10001 and SR20001
    bulk_assignment_service.snapshot_ticket_ids({"IR10001", "SR20001"})

    # Now queue has IR10001 + IR10002 (SR20001 removed, IR10002 added)
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:2]
    }
    mock_athena_client.search_service_requests.return_value = {"results": []}

    diff = await bulk_assignment_service.compute_queue_diff()
    assert diff["total"] == 2
    assert len(diff["added"]) == 1
    assert diff["added"][0].id == "IR10002"
    assert len(diff["removed"]) == 1
    assert "SR20001" in diff["removed"]


# ── Queue Cache Tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_cache_populates_cached_queue(
    bulk_assignment_service, mock_athena_client
):
    """refresh_cache() should populate _cached_queue and _last_known_ticket_ids."""
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:2]
    }
    mock_athena_client.search_service_requests.return_value = {
        "results": SAMPLE_SR_TICKETS[:1]
    }

    assert bulk_assignment_service.cached_queue is None

    count = await bulk_assignment_service.refresh_cache()

    assert count == 3
    assert bulk_assignment_service.cached_queue is not None
    assert len(bulk_assignment_service.cached_queue) == 3
    assert bulk_assignment_service._last_known_ticket_ids == {"IR10001", "IR10002", "SR20001"}


@pytest.mark.asyncio
async def test_fetch_queue_streaming_serves_from_cache(
    bulk_assignment_service, mock_athena_client
):
    """fetch_queue_streaming() should serve from cache without calling Athena."""
    # First, populate the cache
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:1]
    }
    mock_athena_client.search_service_requests.return_value = {
        "results": SAMPLE_SR_TICKETS[:1]
    }
    await bulk_assignment_service.refresh_cache()

    # Reset mock call counts
    mock_athena_client.search_incidents.reset_mock()
    mock_athena_client.search_service_requests.reset_mock()

    # Now stream from cache
    streamed = []

    async def on_ticket(ticket, count):
        streamed.append(ticket)

    phases = []

    async def on_phase(phase):
        phases.append(phase)

    total = await bulk_assignment_service.fetch_queue_streaming(
        on_ticket=on_ticket, on_phase=on_phase
    )

    assert total == 2
    assert len(streamed) == 2
    # Should NOT have called Athena
    mock_athena_client.search_incidents.assert_not_called()
    mock_athena_client.search_service_requests.assert_not_called()
    # Should have used the cache path
    assert "serving_from_cache" in phases
    assert "complete" in phases


@pytest.mark.asyncio
async def test_fetch_queue_streaming_falls_back_to_athena_without_cache(
    bulk_assignment_service, mock_athena_client
):
    """fetch_queue_streaming() should call Athena when cache is None."""
    assert bulk_assignment_service.cached_queue is None

    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:1]
    }
    mock_athena_client.search_service_requests.return_value = {"results": []}

    streamed = []

    async def on_ticket(ticket, count):
        streamed.append(ticket)

    phases = []

    async def on_phase(phase):
        phases.append(phase)

    total = await bulk_assignment_service.fetch_queue_streaming(
        on_ticket=on_ticket, on_phase=on_phase
    )

    assert total == 1
    # Should have called Athena (slow path)
    mock_athena_client.search_incidents.assert_called_once()
    # Should NOT have used the cache path
    assert "serving_from_cache" not in phases
    assert "fetching" in phases


@pytest.mark.asyncio
async def test_compute_queue_diff_updates_cache(
    bulk_assignment_service, mock_athena_client
):
    """compute_queue_diff() should update _cached_queue as a side effect."""
    # Start with no cache
    assert bulk_assignment_service.cached_queue is None

    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:2]
    }
    mock_athena_client.search_service_requests.return_value = {
        "results": SAMPLE_SR_TICKETS[:1]
    }

    await bulk_assignment_service.compute_queue_diff()

    # Cache should now be populated
    assert bulk_assignment_service.cached_queue is not None
    assert len(bulk_assignment_service.cached_queue) == 3


@pytest.mark.asyncio
async def test_refresh_cache_filters_closed_tickets(
    bulk_assignment_service, mock_athena_client
):
    """refresh_cache() should only cache open tickets (closed filtered out)."""
    # Include a closed ticket in the results
    closed_ticket = {
        "id": "IR99999",
        "entityId": "eid-closed",
        "title": "Closed ticket",
        "status": {"name": "Closed", "id": IR_CLOSED_GUID},
        "priority": 1,
        "tierQueue": {"name": "Validation"},
        "createdDate": "2026-04-14T08:00:00Z",
    }
    mock_athena_client.search_incidents.return_value = {
        "results": [SAMPLE_IR_TICKETS[0], closed_ticket]
    }
    mock_athena_client.search_service_requests.return_value = {"results": []}

    count = await bulk_assignment_service.refresh_cache()

    # Only the open ticket should be cached
    assert count == 1
    assert len(bulk_assignment_service.cached_queue) == 1
    assert bulk_assignment_service.cached_queue[0].id == "IR10001"


@pytest.mark.asyncio
async def test_cache_annotates_lock_state_on_streaming(
    bulk_assignment_service, mock_athena_client
):
    """Streaming from cache should re-annotate current lock state."""
    mock_athena_client.search_incidents.return_value = {
        "results": SAMPLE_IR_TICKETS[:2]
    }
    mock_athena_client.search_service_requests.return_value = {"results": []}
    await bulk_assignment_service.refresh_cache()

    # Lock a ticket after cache was built
    bulk_assignment_service.lock_tickets(["IR10001"], "user_a")

    streamed = []

    async def on_ticket(ticket, count):
        streamed.append(ticket)

    await bulk_assignment_service.fetch_queue_streaming(on_ticket=on_ticket)

    # The locked ticket should have the lock annotation
    ir10001 = next(t for t in streamed if t.id == "IR10001")
    ir10002 = next(t for t in streamed if t.id == "IR10002")
    assert ir10001.locked_by == "user_a"
    assert ir10002.locked_by is None


# ── Triage Toggle Tests ───────────────────────────────────────────────


def test_bulk_recommend_request_model_use_triage_default():
    """BulkRecommendRequest.use_triage should default to True."""
    from feature4.models import BulkRecommendRequest

    req = BulkRecommendRequest(ticket_ids=["IR10001"], user_id="user_a")
    assert req.use_triage is True


def test_bulk_recommend_request_model_use_triage_false():
    """BulkRecommendRequest should accept use_triage=False."""
    from feature4.models import BulkRecommendRequest

    req = BulkRecommendRequest(
        ticket_ids=["IR10001"],
        user_id="user_a",
        use_triage=False,
    )
    assert req.use_triage is False


def test_bulk_recommend_request_model_use_triage_true():
    """BulkRecommendRequest should accept use_triage=True explicitly."""
    from feature4.models import BulkRecommendRequest

    req = BulkRecommendRequest(
        ticket_ids=["IR10001", "IR10002"],
        user_id="user_b",
        use_triage=True,
    )
    assert req.use_triage is True


@pytest.mark.asyncio
async def test_batch_recommend_passes_use_triage_true(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
    sample_athena_ticket,
):
    """batch_recommend with use_triage=True should pass it to recommend_assignment."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket

    result = await bulk_assignment_service.batch_recommend(
        ["IR1959493"], use_triage=True
    )

    assert result.total == 1
    assert result.failed == 0
    assert result.recommendations[0].success is True


@pytest.mark.asyncio
async def test_batch_recommend_passes_use_triage_false(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
    sample_athena_ticket,
):
    """batch_recommend with use_triage=False should skip triage rules and use classifier only."""
    mock_athena_client.get_ticket.return_value = sample_athena_ticket

    result = await bulk_assignment_service.batch_recommend(
        ["IR1959493"], use_triage=False
    )

    # Should still succeed — classifier handles the prediction
    assert result.total == 1
    assert result.failed == 0
    assert result.recommendations[0].success is True
    # Method should be 'classifier' since triage was disabled
    assert result.recommendations[0].recommendation.method == "classifier"


@pytest.mark.asyncio
async def test_batch_recommend_use_triage_false_skips_triage_rule(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """When use_triage=False, a ticket that would match a triage rule should use classifier instead."""
    # Create a ticket that would normally match the password_reset triage rule
    triage_ticket = {
        "id": "IR1234567",
        "entityId": "eid-triage-test",
        "title": "Password Reset Request",
        "description": "User needs password reset for their account.",
        "status": {"name": "Active", "id": "5e2d3932-ca6d-1515-7310-6f58584df73e"},
        "priority": 3,
        "tierQueue": {"name": "Validation", "id": "tq-guid-1"},
        "affectedUser": {"displayName": "Test User", "userName": "testuser"},
        "createdDate": "2026-04-14T10:00:00Z",
        "location": {"name": "HUP"},
    }
    mock_athena_client.get_ticket.return_value = triage_ticket

    # With triage OFF — should use classifier, not triage rule
    result_no_triage = await bulk_assignment_service.batch_recommend(
        ["IR1234567"], use_triage=False
    )
    assert result_no_triage.recommendations[0].success is True
    assert result_no_triage.recommendations[0].recommendation.method == "classifier"

    # With triage ON — should use triage rule (password reset → Service Desk)
    result_with_triage = await bulk_assignment_service.batch_recommend(
        ["IR1234567"], use_triage=True
    )
    assert result_with_triage.recommendations[0].success is True
    assert result_with_triage.recommendations[0].recommendation.method == "triage_rule"
    assert result_with_triage.recommendations[0].recommendation.support_group_name == "Service Desk"


# ── Assignment Comment Tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_assign_tickets_adds_comment(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should add an action log comment after successful assignment."""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"id": "tq-guid", "name": "PennChart\\Ambulatory"},
        "priority": 3,
    }

    # Set up mocks for the internal Athena HTTP call used by _add_assignment_comment
    mock_http_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_http_client.put.return_value = mock_response

    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test-token"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"
    mock_athena_client._settings.athena_servicerequest_url = "https://athena.test/v1/servicerequest/"

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-ir-10001",
            tier_queue_guid="ae9eb3ff-458a-206f-7815-129d50efa285",
            tier_queue_name="PennChart\\Ambulatory",
            priority=3,
        ),
    ]

    result = await bulk_assignment_service.assign_tickets(
        assignments, assigned_by="John Smith"
    )

    assert result.total_assigned == 1
    assert result.results[0].success is True

    # Verify the comment PUT was called
    mock_http_client.put.assert_called_once()
    call_args = mock_http_client.put.call_args

    # Check URL (IR ticket → incident URL)
    assert call_args[0][0] == "https://athena.test/v1/incident/"

    # Check payload contains actionLogs
    payload = call_args[1]["json"]
    assert payload["entityId"] == "eid-ir-10001"
    assert len(payload["actionLogs"]) == 1
    assert payload["actionLogs"][0]["title"] == "Assigned to PennChart\\Ambulatory"
    assert "PennChart\\Ambulatory" in payload["actionLogs"][0]["description"]
    assert "priority 3" in payload["actionLogs"][0]["description"]
    assert "John Smith" in payload["actionLogs"][0]["description"]
    assert "Service Desk Helper" in payload["actionLogs"][0]["description"]


@pytest.mark.asyncio
async def test_assign_tickets_comment_includes_user_name(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Comment description should include the assigned_by user name."""
    from unittest.mock import AsyncMock, MagicMock

    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "EUS"},
        "priority": 3,
    }

    mock_http_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_http_client.put.return_value = mock_response

    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"
    mock_athena_client._settings.athena_servicerequest_url = "https://athena.test/v1/servicerequest/"

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-1",
            tier_queue_guid="guid-1",
            tier_queue_name="EUS",
            priority=3,
        ),
    ]

    await bulk_assignment_service.assign_tickets(assignments, assigned_by="Jane Doe")

    payload = mock_http_client.put.call_args[1]["json"]
    assert "by Jane Doe" in payload["actionLogs"][0]["description"]


@pytest.mark.asyncio
async def test_assign_tickets_comment_without_user(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Comment should still be added when assigned_by is None (no user attribution)."""
    from unittest.mock import AsyncMock, MagicMock

    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "EUS"},
        "priority": 3,
    }

    mock_http_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_http_client.put.return_value = mock_response

    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"
    mock_athena_client._settings.athena_servicerequest_url = "https://athena.test/v1/servicerequest/"

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-1",
            tier_queue_guid="guid-1",
            tier_queue_name="EUS",
        ),
    ]

    await bulk_assignment_service.assign_tickets(assignments, assigned_by=None)

    payload = mock_http_client.put.call_args[1]["json"]
    # Should not contain "by" attribution
    assert " by " not in payload["actionLogs"][0]["description"]
    # Should still have the basic info
    assert "EUS" in payload["actionLogs"][0]["description"]
    assert "Service Desk Helper" in payload["actionLogs"][0]["description"]


@pytest.mark.asyncio
async def test_assign_tickets_comment_sr_uses_sr_url(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """SR tickets should use the service request URL for the comment PUT."""
    from unittest.mock import AsyncMock, MagicMock

    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "User Provisioning"},
        "priority": "Medium",
    }

    mock_http_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_http_client.put.return_value = mock_response

    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"
    mock_athena_client._settings.athena_servicerequest_url = "https://athena.test/v1/servicerequest/"

    assignments = [
        TicketAssignment(
            ticket_id="SR20001",
            entity_id="eid-sr-20001",
            tier_queue_guid="guid-sr",
            tier_queue_name="User Provisioning",
        ),
    ]

    await bulk_assignment_service.assign_tickets(assignments, assigned_by="Bob")

    # Should use SR URL
    call_args = mock_http_client.put.call_args
    assert call_args[0][0] == "https://athena.test/v1/servicerequest/"


@pytest.mark.asyncio
async def test_assign_tickets_comment_failure_is_non_fatal(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Comment failure should not affect the assignment result (non-fatal)."""
    from unittest.mock import AsyncMock, MagicMock

    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "EUS"},
        "priority": 3,
    }

    # Make the comment PUT fail
    mock_http_client = AsyncMock()
    mock_http_client.put.side_effect = Exception("Network error on comment")

    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"
    mock_athena_client._settings.athena_servicerequest_url = "https://athena.test/v1/servicerequest/"

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-1",
            tier_queue_guid="guid-1",
            tier_queue_name="EUS",
        ),
    ]

    result = await bulk_assignment_service.assign_tickets(
        assignments, assigned_by="User"
    )

    # Assignment should still succeed even though comment failed
    assert result.total_assigned == 1
    assert result.total_failed == 0
    assert result.results[0].success is True


@pytest.mark.asyncio
async def test_assign_tickets_no_comment_on_failed_assignment(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Should NOT attempt to add a comment when the assignment itself fails."""
    from unittest.mock import AsyncMock, MagicMock

    # Make the assignment fail
    mock_athena_client.update_ticket.side_effect = Exception("Athena PUT failed")

    mock_http_client = AsyncMock()
    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"
    mock_athena_client._settings.athena_servicerequest_url = "https://athena.test/v1/servicerequest/"

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-1",
            tier_queue_guid="guid-1",
            tier_queue_name="EUS",
        ),
    ]

    result = await bulk_assignment_service.assign_tickets(
        assignments, assigned_by="User"
    )

    # Assignment failed
    assert result.total_assigned == 0
    assert result.total_failed == 1

    # Comment PUT should NOT have been called (assignment failed before comment)
    mock_http_client.put.assert_not_called()


@pytest.mark.asyncio
async def test_assign_tickets_comment_without_priority(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Comment should omit priority text when priority is None."""
    from unittest.mock import AsyncMock, MagicMock

    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "EUS"},
        "priority": None,
    }

    mock_http_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_http_client.put.return_value = mock_response

    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"
    mock_athena_client._settings.athena_servicerequest_url = "https://athena.test/v1/servicerequest/"

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-1",
            tier_queue_guid="guid-1",
            tier_queue_name="EUS",
            priority=None,
        ),
    ]

    await bulk_assignment_service.assign_tickets(assignments, assigned_by="User")

    payload = mock_http_client.put.call_args[1]["json"]
    # Should NOT contain priority text
    assert "priority" not in payload["actionLogs"][0]["description"]
    # Should still have group name and user
    assert "EUS" in payload["actionLogs"][0]["description"]
    assert "by User" in payload["actionLogs"][0]["description"]


# ── Resolve Ticket Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_ticket_success(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Resolve ticket should PUT with status GUID and resolutionDescription."""
    from unittest.mock import AsyncMock, MagicMock

    mock_http_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_http_client.put.return_value = mock_response

    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"
    mock_athena_client._settings.athena_servicerequest_url = "https://athena.test/v1/servicerequest/"

    # Lock the ticket first
    bulk_assignment_service.lock_tickets(["IR10001"], "user1")

    result = await bulk_assignment_service.resolve_ticket(
        ticket_id="IR10001",
        entity_id="eid-1",
        resolution_description="Resolved via maintenance template.",
        resolved_by="user1",
    )

    assert result.success is True
    assert result.ticket_id == "IR10001"
    assert result.error is None

    # Verify the first PUT call (resolution) had correct payload
    first_call = mock_http_client.put.call_args_list[0]
    payload = first_call[1]["json"]
    assert payload["entityId"] == "eid-1"
    assert payload["status"]["id"] == "2b8830b6-59f0-f574-9c2a-f4b4682f1681"
    assert payload["resolutionDescription"] == "Resolved via maintenance template."

    # Verify the PUT was sent to the incident URL
    assert first_call[0][0] == "https://athena.test/v1/incident/"


@pytest.mark.asyncio
async def test_resolve_ticket_releases_lock(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Resolve ticket should release the lock after success."""
    from unittest.mock import AsyncMock, MagicMock

    mock_http_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_http_client.put.return_value = mock_response

    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"
    mock_athena_client._settings.athena_servicerequest_url = "https://athena.test/v1/servicerequest/"

    # Lock the ticket
    bulk_assignment_service.lock_tickets(["IR10001"], "user1")
    assert "IR10001" in bulk_assignment_service.get_locks()

    await bulk_assignment_service.resolve_ticket(
        ticket_id="IR10001",
        entity_id="eid-1",
        resolution_description="Test resolution.",
        resolved_by="user1",
    )

    # Lock should be released
    assert "IR10001" not in bulk_assignment_service.get_locks()


@pytest.mark.asyncio
async def test_resolve_ticket_failure(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Resolve ticket should return error on Athena failure."""
    from unittest.mock import AsyncMock, MagicMock
    import httpx

    mock_http_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "400 Bad Request", request=MagicMock(), response=MagicMock()
    )
    mock_http_client.put.return_value = mock_response

    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"

    result = await bulk_assignment_service.resolve_ticket(
        ticket_id="IR10001",
        entity_id="eid-1",
        resolution_description="Test resolution.",
        resolved_by="user1",
    )

    assert result.success is False
    assert result.ticket_id == "IR10001"
    assert result.error is not None


@pytest.mark.asyncio
async def test_resolve_ticket_sr_uses_sr_url(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Resolve ticket for SR should use the service request URL."""
    from unittest.mock import AsyncMock, MagicMock

    mock_http_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_http_client.put.return_value = mock_response

    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"
    mock_athena_client._settings.athena_servicerequest_url = "https://athena.test/v1/servicerequest/"

    result = await bulk_assignment_service.resolve_ticket(
        ticket_id="SR50001",
        entity_id="eid-sr-1",
        resolution_description="SR resolved.",
        resolved_by="user1",
    )

    assert result.success is True
    # First PUT call should use SR URL
    first_call = mock_http_client.put.call_args_list[0]
    assert first_call[0][0] == "https://athena.test/v1/servicerequest/"


@pytest.mark.asyncio
async def test_resolve_ticket_adds_comment(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """Resolve ticket should add an action log comment after resolution."""
    from unittest.mock import AsyncMock, MagicMock

    mock_http_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_http_client.put.return_value = mock_response

    mock_athena_client._get_http_client.return_value = mock_http_client
    mock_athena_client._auth_headers.return_value = {"Authorization": "Bearer test"}
    mock_athena_client._settings = MagicMock()
    mock_athena_client._settings.athena_incident_url = "https://athena.test/v1/incident/"
    mock_athena_client._settings.athena_servicerequest_url = "https://athena.test/v1/servicerequest/"

    await bulk_assignment_service.resolve_ticket(
        ticket_id="IR10001",
        entity_id="eid-1",
        resolution_description="Maintenance resolution.",
        resolved_by="TestUser",
    )

    # Should have 2 PUT calls: resolution + comment
    assert mock_http_client.put.call_count == 2

    # Second call is the comment
    comment_call = mock_http_client.put.call_args_list[1]
    comment_payload = comment_call[1]["json"]
    assert "actionLogs" in comment_payload
    assert comment_payload["actionLogs"][0]["title"] == "Ticket Resolved"
    assert "by TestUser" in comment_payload["actionLogs"][0]["description"]


def test_resolve_ticket_request_model_validation():
    """ResolveTicketRequest should reject empty resolution_description."""
    from pydantic import ValidationError
    from feature4.models import ResolveTicketRequest

    with pytest.raises(ValidationError):
        ResolveTicketRequest(
            ticket_id="IR10001",
            entity_id="eid-1",
            resolution_description="",
            user_id="user1",
        )


def test_resolve_ticket_request_model_valid():
    """ResolveTicketRequest should accept valid data."""
    from feature4.models import ResolveTicketRequest

    req = ResolveTicketRequest(
        ticket_id="IR10001",
        entity_id="eid-1",
        resolution_description="Resolved via maintenance.",
        user_id="user1",
    )
    assert req.ticket_id == "IR10001"
    assert req.resolution_description == "Resolved via maintenance."


# ── SR Priority Conversion Tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_assign_sr_ticket_converts_numeric_priority_to_string(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """SR tickets with numeric priority should be converted to string name."""
    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "PennChart"},
        "priority": {"id": "536beaf3-62a8-5dd0-248a-39c2bf86d3bc", "name": "High"},
    }

    assignments = [
        TicketAssignment(
            ticket_id="SR20001",
            entity_id="eid-sr-20001",
            tier_queue_guid="some-sr-guid",
            tier_queue_name="PennChart",
            priority=2,  # Numeric from LLM — should become "High"
        ),
    ]

    await bulk_assignment_service.assign_tickets(assignments)

    mock_athena_client.update_ticket.assert_called_once_with(
        ticket_id="SR20001",
        entity_id="eid-sr-20001",
        tier_queue_guid="some-sr-guid",
        priority="High",
    )


@pytest.mark.asyncio
async def test_assign_sr_ticket_converts_string_digit_priority(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """SR tickets with string digit priority (e.g., '3') should be converted."""
    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "EUS"},
        "priority": {"name": "Medium"},
    }

    assignments = [
        TicketAssignment(
            ticket_id="SR20001",
            entity_id="eid-sr-20001",
            tier_queue_guid="some-sr-guid",
            priority="3",  # String digit — should become "Medium"
        ),
    ]

    await bulk_assignment_service.assign_tickets(assignments)

    mock_athena_client.update_ticket.assert_called_once_with(
        ticket_id="SR20001",
        entity_id="eid-sr-20001",
        tier_queue_guid="some-sr-guid",
        priority="Medium",
    )


@pytest.mark.asyncio
async def test_assign_sr_ticket_preserves_string_priority(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """SR tickets with string priority (e.g., 'High') should pass through unchanged."""
    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "EUS"},
        "priority": {"name": "High"},
    }

    assignments = [
        TicketAssignment(
            ticket_id="SR20001",
            entity_id="eid-sr-20001",
            tier_queue_guid="some-sr-guid",
            priority="High",  # Already a string name — pass through
        ),
    ]

    await bulk_assignment_service.assign_tickets(assignments)

    mock_athena_client.update_ticket.assert_called_once_with(
        ticket_id="SR20001",
        entity_id="eid-sr-20001",
        tier_queue_guid="some-sr-guid",
        priority="High",
    )


@pytest.mark.asyncio
async def test_assign_ir_ticket_keeps_numeric_priority(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """IR tickets with numeric priority should NOT be converted."""
    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "EUS"},
        "priority": 2,
    }

    assignments = [
        TicketAssignment(
            ticket_id="IR10001",
            entity_id="eid-ir-10001",
            tier_queue_guid="some-ir-guid",
            priority=2,  # Numeric for IR — should stay as 2
        ),
    ]

    await bulk_assignment_service.assign_tickets(assignments)

    mock_athena_client.update_ticket.assert_called_once_with(
        ticket_id="IR10001",
        entity_id="eid-ir-10001",
        tier_queue_guid="some-ir-guid",
        priority=2,
    )


@pytest.mark.asyncio
async def test_assign_sr_ticket_priority_mapping_all_values(
    bulk_assignment_service: BulkAssignmentService,
    mock_athena_client,
):
    """All numeric priority values should map correctly for SR tickets."""
    mock_athena_client.update_ticket.return_value = {
        "tierQueue": {"name": "Service Desk"},
        "priority": {"name": "Medium"},
    }

    expected_map = {1: "Immediate", 2: "High", 3: "Medium", 4: "Low"}

    for numeric, expected_str in expected_map.items():
        mock_athena_client.update_ticket.reset_mock()

        assignments = [
            TicketAssignment(
                ticket_id="SR20001",
                entity_id="eid-sr-20001",
                tier_queue_guid="some-guid",
                priority=numeric,
            ),
        ]

        await bulk_assignment_service.assign_tickets(assignments)

        call_kwargs = mock_athena_client.update_ticket.call_args[1]
        assert call_kwargs["priority"] == expected_str, (
            f"Priority {numeric} should map to '{expected_str}', "
            f"got '{call_kwargs['priority']}'"
        )
