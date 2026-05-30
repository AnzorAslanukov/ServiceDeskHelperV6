"""
Tests for Feature #3: Ticket Assignment Recommendation (TF-IDF Classifier).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.assignment import (
    AssignmentRecommendation,
    AssignmentRequest,
    AssignmentResponse,
    ClassifierPrediction,
    TicketInfo,
)
from src.services.assignment import (
    AssignmentService,
    check_service_desk_triage,
    check_specific_triage,
    resolve_group_guid,
    IR_SUPPORT_GROUPS,
    SR_SUPPORT_GROUPS,
)


# ═══════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_athena_client():
    """Create a mock AthenaClient."""
    client = AsyncMock()
    return client


@pytest.fixture
def mock_classifier():
    """Create a mock TicketClassifier."""
    classifier = MagicMock()
    classifier.predict.return_value = [
        {"support_group": "HUP", "confidence": 0.75},
        {"support_group": "User Provisioning", "confidence": 0.15},
        {"support_group": "Account Provisioning", "confidence": 0.05},
    ]
    return classifier


@pytest.fixture
def assignment_service(mock_athena_client, mock_classifier):
    """Create an AssignmentService with mocked dependencies."""
    return AssignmentService(
        athena_client=mock_athena_client,
        classifier=mock_classifier,
    )


@pytest.fixture
def sample_ticket():
    """A sample raw ticket response from Athena."""
    return {
        "shortDescription": "Computer not turning on",
        "description": "User reports their desktop PC at HUP won't power on after the weekend.",
        "status": "Open",
        "priority": "3",
        "tierQueue": "Service Desk",
        "affectedUser": "John Smith",
        "location": "HUP",
        "classificationPath": "Hardware",
        "source": "Phone",
        "createdDate": "2024-01-15T10:30:00Z",
    }


@pytest.fixture
def password_reset_ticket():
    """A ticket that should match Service Desk triage."""
    return {
        "shortDescription": "SD Password Reset Request",
        "description": "PennID verified, username verified, DOB verified. User needs password reset.",
        "status": "Open",
        "priority": "3",
        "tierQueue": "Service Desk",
        "affectedUser": "Jane Doe",
        "location": "Campus",
        "source": "Phone",
        "createdDate": "2024-01-15T11:00:00Z",
    }


@pytest.fixture
def estar_ticket():
    """A ticket that should match specific triage (Kronos)."""
    return {
        "shortDescription": "eStar login issue",
        "description": "User unable to log in to eStar timekeeping system.",
        "status": "Open",
        "priority": "3",
        "tierQueue": "Service Desk",
        "affectedUser": "Bob Wilson",
        "location": "HUP",
        "source": "Phone",
        "createdDate": "2024-01-15T12:00:00Z",
    }


# ═══════════════════════════════════════════════════════════════════════
# TRIAGE RULE TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestServiceDeskTriage:
    """Tests for Service Desk triage rules."""

    def test_password_reset_matches(self):
        assert check_service_desk_triage("Password Reset", "User needs password reset") is True

    def test_account_lockout_matches(self):
        assert check_service_desk_triage("Account Locked", "User account locked out") is True

    def test_mychart_matches(self):
        assert check_service_desk_triage("MyChart issue", "Patient can't access MyPennMedicine") is True

    def test_general_inquiry_matches(self):
        assert check_service_desk_triage("Caller decided to end", "No issue reported") is True

    def test_pennid_matches(self):
        assert check_service_desk_triage("SD Password Reset", "PennID verified, DOB verified") is True

    def test_normal_ticket_does_not_match(self):
        assert check_service_desk_triage("Printer not working", "Printer on 3rd floor is jammed") is False

    def test_negative_keyword_prevents_match(self):
        """Password reset with 'provisioning' should NOT match Service Desk."""
        assert check_service_desk_triage(
            "Password Reset", "Need provisioning for PennChart access"
        ) is False

    def test_epic_access_prevents_match(self):
        """Password reset with 'epic access' should NOT match Service Desk."""
        assert check_service_desk_triage(
            "Password Reset", "Need epic access provisioning"
        ) is False


class TestSpecificTriage:
    """Tests for specific triage rules."""

    def test_estar_login_matches_kronos(self):
        result = check_specific_triage(
            "eStar issue", "User unable to log in to eStar",
            "HUP", IR_SUPPORT_GROUPS,
        )
        assert result is not None
        group_name, _ = result
        assert "kronos" in group_name.lower()

    def test_hris_form_matches_kronos(self):
        result = check_specific_triage(
            "HRIS Support Form", "HRIS support form - eStar access needed",
            "HUP", IR_SUPPORT_GROUPS,
        )
        assert result is not None
        group_name, _ = result
        assert "kronos" in group_name.lower()

    def test_windows_defender_matches_cyber(self):
        result = check_specific_triage(
            "Windows Defender", "Windows Defender has locked the user out",
            "HUP", IR_SUPPORT_GROUPS,
        )
        assert result is not None
        group_name, _ = result
        assert "cyber" in group_name.lower()

    def test_ravdin_location_matches_hup_west(self):
        result = check_specific_triage(
            "Computer issue", "PC not working",
            "RAVDIN 3rd Floor", IR_SUPPORT_GROUPS,
        )
        assert result is not None
        group_name, _ = result
        assert "hup west" in group_name.lower()

    def test_normal_ticket_no_match(self):
        result = check_specific_triage(
            "Printer jam", "Printer on 5th floor is jammed",
            "HUP", IR_SUPPORT_GROUPS,
        )
        assert result is None

    def test_negative_keyword_prevents_match(self):
        """HRIS form with 'transfer' should NOT match Kronos."""
        result = check_specific_triage(
            "HRIS Support Form", "HRIS support form - employee transfer to new department",
            "HUP", IR_SUPPORT_GROUPS,
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# GUID RESOLUTION TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestResolveGroupGuid:
    """Tests for GUID resolution from classifier predictions."""

    def test_direct_match(self):
        groups = {"Service Desk": "guid-sd", "EUS\\HUP": "guid-hup"}
        assert resolve_group_guid("Service Desk", groups) == "guid-sd"

    def test_leaf_match(self):
        groups = {"EUS\\HUP": "guid-hup", "EUS\\Campus": "guid-campus"}
        assert resolve_group_guid("HUP", groups) == "guid-hup"

    def test_partial_match(self):
        groups = {"Applications\\Corporate Applications\\Kronos": "guid-kronos"}
        assert resolve_group_guid("Kronos", groups) == "guid-kronos"

    def test_no_match_returns_empty(self):
        groups = {"Service Desk": "guid-sd"}
        assert resolve_group_guid("NonExistentGroup", groups) == ""

    def test_empty_input(self):
        groups = {"Service Desk": "guid-sd"}
        assert resolve_group_guid("", groups) == ""


# ═══════════════════════════════════════════════════════════════════════
# ASSIGNMENT SERVICE TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestAssignmentService:
    """Tests for the AssignmentService pipeline."""

    @pytest.mark.asyncio
    async def test_classifier_prediction(self, assignment_service, mock_athena_client, sample_ticket):
        """Test that the classifier is called for normal tickets."""
        mock_athena_client.get_ticket.return_value = sample_ticket

        result = await assignment_service.recommend_assignment("IR1234567")

        assert isinstance(result, AssignmentResponse)
        assert result.ticket.id == "IR1234567"
        assert result.recommendation.support_group_name == "HUP"
        assert result.recommendation.confidence == 0.75
        assert result.recommendation.method == "classifier"
        assert len(result.recommendation.alternatives) == 2

    @pytest.mark.asyncio
    async def test_service_desk_triage_short_circuits(
        self, assignment_service, mock_athena_client, mock_classifier, password_reset_ticket
    ):
        """Test that Service Desk triage rules bypass the classifier."""
        mock_athena_client.get_ticket.return_value = password_reset_ticket

        result = await assignment_service.recommend_assignment("IR1234567")

        assert result.recommendation.support_group_name == "Service Desk"
        assert result.recommendation.method == "triage_rule"
        assert result.recommendation.confidence == 1.0
        # Classifier should NOT have been called
        mock_classifier.predict.assert_not_called()

    @pytest.mark.asyncio
    async def test_specific_triage_short_circuits(
        self, assignment_service, mock_athena_client, mock_classifier, estar_ticket
    ):
        """Test that specific triage rules bypass the classifier."""
        mock_athena_client.get_ticket.return_value = estar_ticket

        result = await assignment_service.recommend_assignment("IR1234567")

        assert result.recommendation.method == "triage_rule"
        assert result.recommendation.confidence == 1.0
        assert "kronos" in result.recommendation.support_group_name.lower()
        # Classifier should NOT have been called
        mock_classifier.predict.assert_not_called()

    @pytest.mark.asyncio
    async def test_ticket_id_validation_empty(self, assignment_service):
        """Test that empty ticket IDs raise ValueError."""
        with pytest.raises(ValueError, match="Ticket ID is required"):
            await assignment_service.recommend_assignment("")

    @pytest.mark.asyncio
    async def test_ticket_id_validation_bad_prefix(self, assignment_service):
        """Test that invalid prefixes raise ValueError."""
        with pytest.raises(ValueError, match="must start with 'IR'"):
            await assignment_service.recommend_assignment("XX1234567")

    @pytest.mark.asyncio
    async def test_ticket_id_validation_non_numeric(self, assignment_service):
        """Test that non-numeric suffixes raise ValueError."""
        with pytest.raises(ValueError, match="invalid format"):
            await assignment_service.recommend_assignment("IRABCDEF")

    @pytest.mark.asyncio
    async def test_ticket_id_normalization(self, assignment_service, mock_athena_client, sample_ticket):
        """Test that ticket IDs are normalized (uppercased, stripped)."""
        mock_athena_client.get_ticket.return_value = sample_ticket

        result = await assignment_service.recommend_assignment("  ir1234567  ")

        assert result.ticket.id == "IR1234567"
        mock_athena_client.get_ticket.assert_called_once_with("IR1234567")

    @pytest.mark.asyncio
    async def test_sr_ticket_type(self, assignment_service, mock_athena_client, sample_ticket):
        """Test that SR tickets are handled correctly."""
        mock_athena_client.get_ticket.return_value = sample_ticket

        result = await assignment_service.recommend_assignment("SR1234567")

        assert result.ticket.id == "SR1234567"
        assert result.ticket.ticket_type == "servicerequest"

    @pytest.mark.asyncio
    async def test_alternatives_filter_low_confidence(
        self, assignment_service, mock_athena_client, mock_classifier, sample_ticket
    ):
        """Test that alternatives with very low confidence are filtered out."""
        mock_athena_client.get_ticket.return_value = sample_ticket
        mock_classifier.predict.return_value = [
            {"support_group": "HUP", "confidence": 0.9},
            {"support_group": "Campus", "confidence": 0.05},
            {"support_group": "Wintel", "confidence": 0.0001},  # Should be filtered
        ]

        result = await assignment_service.recommend_assignment("IR1234567")

        assert result.recommendation.support_group_name == "HUP"
        assert len(result.recommendation.alternatives) == 1  # Only Campus, not Wintel

    @pytest.mark.asyncio
    async def test_empty_predictions_fallback(
        self, assignment_service, mock_athena_client, mock_classifier, sample_ticket
    ):
        """Test fallback to Service Desk when classifier returns no predictions."""
        mock_athena_client.get_ticket.return_value = sample_ticket
        mock_classifier.predict.return_value = []

        result = await assignment_service.recommend_assignment("IR1234567")

        assert result.recommendation.support_group_name == "Service Desk"
        assert result.recommendation.confidence == 0.0
        assert result.recommendation.method == "classifier"


# ═══════════════════════════════════════════════════════════════════════
# MODEL TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestModels:
    """Tests for Pydantic models."""

    def test_assignment_request_defaults(self):
        req = AssignmentRequest()
        assert req.top_k == 5

    def test_assignment_request_custom(self):
        req = AssignmentRequest(top_k=10)
        assert req.top_k == 10

    def test_classifier_prediction_model(self):
        pred = ClassifierPrediction(support_group="HUP", confidence=0.85)
        assert pred.support_group == "HUP"
        assert pred.confidence == 0.85

    def test_assignment_recommendation_model(self):
        rec = AssignmentRecommendation(
            support_group_name="HUP",
            support_group_guid="test-guid",
            confidence=0.85,
            method="classifier",
            rationale="Test rationale",
            alternatives=[
                ClassifierPrediction(support_group="Campus", confidence=0.1),
            ],
        )
        assert rec.support_group_name == "HUP"
        assert rec.method == "classifier"
        assert len(rec.alternatives) == 1

    def test_ticket_info_model(self):
        info = TicketInfo(
            id="IR1234567",
            ticket_type="incident",
            title="Test ticket",
        )
        assert info.id == "IR1234567"
        assert info.description is None

    def test_assignment_response_model(self):
        resp = AssignmentResponse(
            ticket=TicketInfo(id="IR1234567", ticket_type="incident"),
            recommendation=AssignmentRecommendation(
                support_group_name="HUP",
                support_group_guid="guid",
                confidence=0.9,
                method="classifier",
                rationale="Test",
            ),
        )
        assert resp.ticket.id == "IR1234567"
        assert resp.recommendation.support_group_name == "HUP"