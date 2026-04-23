"""
Unit tests for TicketSearchService — Feature #1: Enhanced Ticket Search.

Tests all four search modes with mocked clients:
1. Field match
2. Description match
3. Semantic search
4. Ticket similarity
"""

import pytest

from src.clients.athena_client import AthenaClient
from src.services.ticket_search import TicketSearchService


# ── Mode 1: Field Match ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_by_field_returns_mapped_tickets(
    search_service: TicketSearchService,
    mock_athena_client,
    sample_athena_tickets,
):
    """Field search should call Athena and return mapped TicketSummary objects."""
    mock_athena_client.search_tickets.return_value = {
        "results": sample_athena_tickets,
        "total": 2,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }

    result = await search_service.search_by_field(
        field="contactMethod",
        value="215-555-1234",
        ticket_type="incident",
    )

    assert result.total == 2
    assert result.page == 1
    assert result.page_size == 50
    assert result.has_more is False
    assert result.tickets[0].id == "IR1959493"
    assert result.tickets[0].title == "Printer not working on 3rd floor"
    assert result.tickets[0].status == "Active"
    assert result.tickets[0].affected_user == "John Smith"
    assert result.tickets[0].support_group == "EUS\\HUP"
    mock_athena_client.search_tickets.assert_called_once()


@pytest.mark.asyncio
async def test_search_by_field_empty_results(
    search_service: TicketSearchService,
    mock_athena_client,
):
    """Field search with no matches should return empty list."""
    mock_athena_client.search_tickets.return_value = {
        "results": [], "total": 0, "page": 1, "page_size": 50, "has_more": False,
    }

    result = await search_service.search_by_field(
        field="contactMethod",
        value="000-000-0000",
    )

    assert result.total == 0
    assert result.tickets == []
    assert result.has_more is False


@pytest.mark.asyncio
async def test_search_by_field_uses_correct_operator(
    search_service: TicketSearchService,
    mock_athena_client,
):
    """Field search should pass the operator to the filter builder."""
    mock_athena_client.search_tickets.return_value = {
        "results": [], "total": 0, "page": 1, "page_size": 50, "has_more": False,
    }

    await search_service.search_by_field(
        field="title",
        value="printer",
        operator="contains",
        ticket_type="servicerequest",
    )

    call_args = mock_athena_client.search_tickets.call_args
    filters = call_args[0][0]
    assert filters[0]["filters"][0]["operator"] == "contains"
    assert call_args[0][1] == "servicerequest"


# ── Mode 2: Description Match ────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_by_description_returns_results(
    search_service: TicketSearchService,
    mock_athena_client,
    sample_athena_tickets,
):
    """Description search should use 'contains' operator on description field."""
    mock_athena_client.search_tickets.return_value = {
        "results": sample_athena_tickets,
        "total": 2,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }

    result = await search_service.search_by_description(
        text="printer not printing",
        ticket_type="incident",
    )

    assert result.total == 2
    call_args = mock_athena_client.search_tickets.call_args
    filters = call_args[0][0]
    word_filters = filters[0]["filters"]
    assert len(word_filters) == 3
    assert all(f["property"] == "description" for f in word_filters)
    assert all(f["operator"] == "contains" for f in word_filters)
    assert word_filters[0]["value"] == "printer"
    assert word_filters[1]["value"] == "not"
    assert word_filters[2]["value"] == "printing"


@pytest.mark.asyncio
async def test_search_by_description_truncates_long_descriptions(
    search_service: TicketSearchService,
    mock_athena_client,
):
    """Descriptions longer than 500 chars should be truncated."""
    long_desc = "A" * 600
    mock_athena_client.search_tickets.return_value = {
        "results": [{"id": "IR1000001", "title": "Test", "description": long_desc}],
        "total": 1,
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }

    result = await search_service.search_by_description(text="test")

    assert result.tickets[0].description is not None
    assert len(result.tickets[0].description) == 503  # 500 + "..."
    assert result.tickets[0].description.endswith("...")


# ── Mode 3: Semantic Search ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_search_generates_embedding(
    search_service: TicketSearchService,
    mock_databricks_client,
):
    """Semantic search should generate an embedding for the query text."""
    await search_service.semantic_search(query="printer not working")

    mock_databricks_client.generate_embedding.assert_called_once_with("printer not working")


@pytest.mark.asyncio
async def test_semantic_search_returns_tickets_and_docs(
    search_service: TicketSearchService,
    mock_databricks_client,
    sample_similar_results,
    sample_documentation_results,
):
    """Semantic search should return both similar tickets and documentation."""
    mock_databricks_client.find_similar_by_embedding.return_value = sample_similar_results
    mock_databricks_client.find_similar_documentation.return_value = sample_documentation_results

    result = await search_service.semantic_search(query="printer issue", top_k=5)

    assert len(result.similar_tickets) == 5
    assert result.similar_tickets[0].id == "IR1959100"
    assert result.similar_tickets[0].similarity == 0.95

    assert len(result.documentation) == 2
    assert result.documentation[0].title == "HP LaserJet Troubleshooting"
    assert result.documentation[0].notebook == "uphs_notebook"
    assert result.documentation[0].similarity == 0.92


@pytest.mark.asyncio
async def test_semantic_search_empty_results(
    search_service: TicketSearchService,
    mock_databricks_client,
):
    """Semantic search with no matches should return empty lists."""
    mock_databricks_client.find_similar_by_embedding.return_value = []
    mock_databricks_client.find_similar_documentation.return_value = []

    result = await search_service.semantic_search(query="something very unusual")

    assert result.similar_tickets == []
    assert result.documentation == []


# ── Mode 4: Ticket Similarity ────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_similar_tickets_returns_results(
    search_service: TicketSearchService,
    mock_databricks_client,
    sample_similar_results,
):
    """Ticket similarity should look up embedding and find similar tickets."""
    mock_databricks_client.get_ticket_embedding.return_value = [0.5] * 1024
    mock_databricks_client.find_similar_by_embedding.return_value = sample_similar_results

    result = await search_service.find_similar_tickets(ticket_id="IR1959493", top_k=5)

    assert result.source_ticket_id == "IR1959493"
    assert len(result.similar_tickets) == 5
    mock_databricks_client.get_ticket_embedding.assert_called_once_with("IR1959493")


@pytest.mark.asyncio
async def test_find_similar_tickets_excludes_self(
    search_service: TicketSearchService,
    mock_databricks_client,
):
    """Ticket similarity should exclude the source ticket from results."""
    mock_databricks_client.get_ticket_embedding.return_value = [0.5] * 1024
    mock_databricks_client.find_similar_by_embedding.return_value = [
        {"id": "IR1959493", "similarity": 1.0},  # self — should be excluded
        {"id": "IR1959100", "similarity": 0.95},
        {"id": "IR1959101", "similarity": 0.91},
    ]

    result = await search_service.find_similar_tickets(ticket_id="IR1959493", top_k=2)

    assert result.source_ticket_id == "IR1959493"
    assert len(result.similar_tickets) == 2
    assert all(t.id != "IR1959493" for t in result.similar_tickets)


@pytest.mark.asyncio
async def test_find_similar_tickets_no_embedding_raises(
    search_service: TicketSearchService,
    mock_databricks_client,
):
    """Ticket similarity should raise ValueError if no embedding exists."""
    mock_databricks_client.get_ticket_embedding.return_value = None

    with pytest.raises(ValueError, match="No embedding found"):
        await search_service.find_similar_tickets(ticket_id="IR9999999")


# ── Helper: Ticket Mapping ───────────────────────────────────────────


def test_map_ticket_handles_nested_objects(sample_athena_ticket):
    """_map_ticket should extract display names from nested Athena objects."""
    result = TicketSearchService._map_ticket(sample_athena_ticket)

    assert result.id == "IR1959493"
    assert result.status == "Active"
    assert result.affected_user == "John Smith"
    assert result.support_group == "EUS\\HUP"
    assert result.priority == 3
    assert result.created_date == "10:30 01/15/2024"
    assert result.location == "HUP"


def test_map_ticket_handles_string_fields():
    """_map_ticket should handle fields that are plain strings (not nested)."""
    raw = {
        "id": "IR1000001",
        "title": "Test ticket",
        "status": "Active",
        "supportGroup": "Service Desk",
        "affectedUser": "smithjoh",
    }
    result = TicketSearchService._map_ticket(raw)

    assert result.status == "Active"
    assert result.support_group == "Service Desk"
    assert result.affected_user == "smithjoh"


def test_map_ticket_handles_missing_fields():
    """_map_ticket should handle tickets with minimal fields."""
    raw = {"id": "IR1000002"}
    result = TicketSearchService._map_ticket(raw)

    assert result.id == "IR1000002"
    assert result.title is None
    assert result.status is None
    assert result.description is None


def test_map_ticket_view_endpoint_format():
    """_map_ticket should use *Value companion fields from the view endpoint."""
    raw = {
        "id": "IR10374608",
        "name": "IR10374608",
        "title": "Printer not working",
        "status": "9accddda-fbf5-10d4-b402-69bdd276a69b",
        "statusValue": "Work in Progress",
        "supportGroup": "5321a3e7-a2ce-5566-b306-8e1aeee6a02a",
        "supportGroupValue": "Ambulatory Clinical (LGH)",
        "tierQueue": "5321a3e7-a2ce-5566-b306-8e1aeee6a02a",
        "affectedUser_DisplayName": "Massey, Chuck",
        "priority": 2,
        "createdDate": "2026-04-13T19:57:18.303",
        "location": "570b9d6a-8680-0e6c-216f-0da4bb04d89b",
        "locationValue": "Downtown Outpatient Pavilion (DOP)",
    }
    result = TicketSearchService._map_ticket(raw)

    assert result.id == "IR10374608"
    assert result.status == "Work in Progress"
    assert result.support_group == "Ambulatory Clinical (LGH)"
    assert result.affected_user == "Massey, Chuck"
    assert result.priority == 2
    assert result.created_date == "19:57 04/13/2026"
    assert result.location == "Downtown Outpatient Pavilion (DOP)"


def test_map_ticket_guid_status_without_value_field():
    """_map_ticket should return None for bare GUID status with no companion field."""
    raw = {
        "id": "IR1000003",
        "status": "9accddda-fbf5-10d4-b402-69bdd276a69b",
    }
    result = TicketSearchService._map_ticket(raw)

    assert result.status is None  # GUID should not be displayed


def test_map_ticket_guid_support_group_falls_back_to_tier_queue():
    """_map_ticket should fall back to tierQueue when supportGroup is a GUID."""
    raw = {
        "id": "IR1000004",
        "supportGroup": "5321a3e7-a2ce-5566-b306-8e1aeee6a02a",
        "tierQueue": {"name": "Validation", "id": "1a59b3b9-84a3-13ce-f50c-79b8a99f5531"},
    }
    result = TicketSearchService._map_ticket(raw)

    assert result.support_group == "Validation"


def test_map_ticket_affected_user_flat_format():
    """_map_ticket should use affectedUser_DisplayName from view endpoint."""
    raw = {
        "id": "IR1000005",
        "affectedUser_DisplayName": "Smith, John",
        "affectedUser_EntityId": "abc-123",
    }
    result = TicketSearchService._map_ticket(raw)

    assert result.affected_user == "Smith, John"


def test_map_ticket_priority_dict():
    """_map_ticket should extract name from priority dict (SR object endpoint format)."""
    raw = {
        "id": "SR1000001",
        "priority": {"name": "Medium", "id": "dd43a3a8-c640-2146-85a4-77978e3bb375"},
    }
    result = TicketSearchService._map_ticket(raw)

    assert result.priority == "Medium"


def test_map_ticket_priority_sr_view_endpoint_format():
    """_map_ticket should use priorityValue for SR tickets from the view endpoint."""
    raw = {
        "id": "SR1000002",
        "priority": "dd43a3a8-c640-2146-85a4-77978e3bb375",
        "priorityValue": "Medium",
    }
    result = TicketSearchService._map_ticket(raw)

    assert result.priority == "Medium"


def test_map_ticket_priority_guid_rejected():
    """_map_ticket should return None for bare GUID priority without companion field."""
    raw = {
        "id": "SR1000003",
        "priority": "dd43a3a8-c640-2146-85a4-77978e3bb375",
    }
    result = TicketSearchService._map_ticket(raw)

    assert result.priority is None


# ── GUID Detection Tests ─────────────────────────────────────────────


def test_is_guid_valid():
    """_is_guid should return True for valid GUID strings."""
    assert TicketSearchService._is_guid("9accddda-fbf5-10d4-b402-69bdd276a69b") is True
    assert TicketSearchService._is_guid("5e2d3932-ca6d-1515-7310-6f58584df73e") is True


def test_is_guid_invalid():
    """_is_guid should return False for non-GUID strings."""
    assert TicketSearchService._is_guid("Active") is False
    assert TicketSearchService._is_guid("Work in Progress") is False
    assert TicketSearchService._is_guid("Service Desk") is False
    assert TicketSearchService._is_guid("IR1959493") is False
    assert TicketSearchService._is_guid("") is False


# ── _extract_field Tests ─────────────────────────────────────────────


def test_extract_field_prefers_value_companion():
    """_extract_field should prefer the *Value companion field."""
    raw = {
        "status": "9accddda-fbf5-10d4-b402-69bdd276a69b",
        "statusValue": "Work in Progress",
    }
    assert TicketSearchService._extract_field(raw, "status") == "Work in Progress"


def test_extract_field_dict_format():
    """_extract_field should extract name from dict format."""
    raw = {"status": {"name": "Active", "id": "5e2d3932-ca6d-1515-7310-6f58584df73e"}}
    assert TicketSearchService._extract_field(raw, "status") == "Active"


def test_extract_field_plain_string():
    """_extract_field should return plain non-GUID strings."""
    raw = {"status": "Active"}
    assert TicketSearchService._extract_field(raw, "status") == "Active"


def test_extract_field_rejects_guid_string():
    """_extract_field should return None for bare GUID strings."""
    raw = {"status": "9accddda-fbf5-10d4-b402-69bdd276a69b"}
    assert TicketSearchService._extract_field(raw, "status") is None


def test_extract_field_missing():
    """_extract_field should return None for missing fields."""
    raw = {}
    assert TicketSearchService._extract_field(raw, "status") is None


# ── Filter Builder Tests ─────────────────────────────────────────────


def test_build_field_filter():
    """build_field_filter should create correct JSON filter structure."""
    filters = AthenaClient.build_field_filter("contactMethod", "215-555-1234")

    assert len(filters) == 1
    assert filters[0]["condition"] == "and"
    inner = filters[0]["filters"][0]
    assert inner["property"] == "contactMethod"
    assert inner["operator"] == "eq"
    assert inner["value"] == "215-555-1234"


def test_build_field_filter_custom_operator():
    """build_field_filter should support custom operators."""
    filters = AthenaClient.build_field_filter("title", "printer", operator="contains")

    inner = filters[0]["filters"][0]
    assert inner["operator"] == "contains"


def test_build_description_filter_single_word():
    """build_description_filter with a single word should create one contains filter."""
    filters = AthenaClient.build_description_filter("printer")

    assert len(filters) == 1
    assert len(filters[0]["filters"]) == 1
    inner = filters[0]["filters"][0]
    assert inner["property"] == "description"
    assert inner["operator"] == "contains"
    assert inner["value"] == "printer"


def test_build_description_filter_multi_word():
    """build_description_filter with multiple words should create one contains filter per word."""
    filters = AthenaClient.build_description_filter("cerner printer")

    assert len(filters) == 1
    word_filters = filters[0]["filters"]
    assert len(word_filters) == 2
    assert word_filters[0]["property"] == "description"
    assert word_filters[0]["operator"] == "contains"
    assert word_filters[0]["value"] == "cerner"
    assert word_filters[1]["property"] == "description"
    assert word_filters[1]["operator"] == "contains"
    assert word_filters[1]["value"] == "printer"


def test_build_description_filter_trims_whitespace():
    """build_description_filter should handle extra whitespace gracefully."""
    filters = AthenaClient.build_description_filter("  cerner   printer  ")

    word_filters = filters[0]["filters"]
    assert len(word_filters) == 2
    assert word_filters[0]["value"] == "cerner"
    assert word_filters[1]["value"] == "printer"


# ── _extract_name Tests ──────────────────────────────────────────────


def test_extract_name_from_dict():
    """_extract_name should extract name from a dict."""
    assert TicketSearchService._extract_name({"name": "HUP", "id": "abc"}) == "HUP"


def test_extract_name_from_dict_display_name():
    """_extract_name should fall back to displayName."""
    assert TicketSearchService._extract_name({"displayName": "John Smith"}) == "John Smith"


def test_extract_name_from_string():
    """_extract_name should return plain non-GUID strings."""
    assert TicketSearchService._extract_name("HUP") == "HUP"


def test_extract_name_rejects_guid():
    """_extract_name should return None for bare GUID strings."""
    assert TicketSearchService._extract_name("d5469f7c-d8b1-ff41-255a-9956ea42d843") is None


def test_extract_name_none():
    """_extract_name should return None for None input."""
    assert TicketSearchService._extract_name(None) is None


# ── _format_date Tests ───────────────────────────────────────────────


def test_format_date_iso_with_z():
    """_format_date should format ISO dates with Z suffix."""
    assert TicketSearchService._format_date("2024-01-15T10:30:00Z") == "10:30 01/15/2024"


def test_format_date_iso_with_millis_and_z():
    """_format_date should format ISO dates with milliseconds and Z suffix."""
    assert TicketSearchService._format_date("2026-04-13T19:57:18.303Z") == "19:57 04/13/2026"


def test_format_date_iso_with_millis_no_z():
    """_format_date should format ISO dates with milliseconds but no Z suffix."""
    assert TicketSearchService._format_date("2026-04-13T19:57:18.303") == "19:57 04/13/2026"


def test_format_date_iso_no_z():
    """_format_date should format ISO dates without Z suffix."""
    assert TicketSearchService._format_date("2024-06-01T08:00:00") == "08:00 06/01/2024"


def test_format_date_unknown_format_fallback():
    """_format_date should return raw string for unrecognized formats."""
    assert TicketSearchService._format_date("Jan 15, 2024") == "Jan 15, 2024"


def test_format_date_none():
    """_format_date should return None for None input."""
    assert TicketSearchService._format_date(None) is None


def test_format_date_empty_string():
    """_format_date should return None for empty string."""
    assert TicketSearchService._format_date("") is None


# ── Location Extraction Tests ────────────────────────────────────────


def test_map_ticket_location_dict():
    """_map_ticket should extract location name from dict."""
    raw = {"id": "IR1000010", "location": {"name": "PPMC", "id": "some-guid"}}
    result = TicketSearchService._map_ticket(raw)
    assert result.location == "PPMC"


def test_map_ticket_location_string():
    """_map_ticket should use plain string location."""
    raw = {"id": "IR1000011", "location": "HUP Cedar"}
    result = TicketSearchService._map_ticket(raw)
    assert result.location == "HUP Cedar"


def test_map_ticket_location_guid_rejected():
    """_map_ticket should return None for GUID location."""
    raw = {"id": "IR1000012", "location": "d5469f7c-d8b1-ff41-255a-9956ea42d843"}
    result = TicketSearchService._map_ticket(raw)
    assert result.location is None


def test_map_ticket_location_missing():
    """_map_ticket should return None when location is absent."""
    raw = {"id": "IR1000013"}
    result = TicketSearchService._map_ticket(raw)
    assert result.location is None


def test_map_ticket_location_view_endpoint_format():
    """_map_ticket should use locationValue from the view endpoint flat format."""
    raw = {
        "id": "IR1000014",
        "location": "570b9d6a-8680-0e6c-216f-0da4bb04d89b",
        "locationValue": "Downtown Outpatient Pavilion (DOP)",
    }
    result = TicketSearchService._map_ticket(raw)
    assert result.location == "Downtown Outpatient Pavilion (DOP)"
