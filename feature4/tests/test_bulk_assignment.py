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


def test_raw_to_queue_summary_truncates_description():
    """Should truncate long descriptions to 200 chars."""
    raw = {
        "id": "IR10001",
        "entityId": "eid-1",
        "description": "A" * 300,
    }
    summary = BulkAssignmentService._raw_to_queue_summary(raw, "incident")
    assert summary is not None
    assert len(summary.description) == 203  # 200 + "..."
    assert summary.description.endswith("...")


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
