"""
Unit tests for src/services/site_routing.py — UPHS vs LGH detection helpers.
"""

import pytest

from src.services.site_routing import (
    NOTEBOOK_LGH,
    NOTEBOOK_UPHS,
    SITE_LGH,
    SITE_UPHS,
    detect_site,
    group_site,
    is_holding_queue,
    notebook_for_site,
    site_for_location_path,
    sites_conflict,
)


# ── is_holding_queue ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "group, expected",
    [
        ("Validation", True),
        ("validation", True),
        ("  VALIDATION  ", True),
        ("Service Desk\\Validation", True),
        ("service desk\\validation", True),
        ("Service Desk Validation", True),
        ("Some Parent\\Validation", True),
        ("Professional Billing (Resolute PB)", False),
        ("Service Desk", False),
        ("EUS\\HUP", False),
        ("", False),
        (None, False),
    ],
)
def test_is_holding_queue(group, expected):
    assert is_holding_queue(group) is expected


# ── detect_site ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fields, expected",
    [
        (("HUP",), SITE_UPHS),
        (("HUP\\Ravdin",), SITE_UPHS),
        (("Lancaster General Hospital",), SITE_LGH),
        (("LGHP",), SITE_LGH),
        (("User cannot log into MyLGHealth",), SITE_LGH),
        (("PennChart access issue at PCAM",), SITE_UPHS),
        (("eStar portal error", "lha.org LDAP"), SITE_LGH),
        (("Remote User",), None),
        (("",), None),
        ((None,), None),
    ],
)
def test_detect_site(fields, expected):
    assert detect_site(*fields) == expected


def test_detect_site_combines_multiple_fields():
    # Location gives no signal but description does.
    assert detect_site("Remote User", "issue with MyLGHealth portal") == SITE_LGH


def test_detect_site_tie_is_ambiguous():
    # One strong signal on each side → ambiguous → None.
    assert detect_site("HUP and Lancaster shared incident") is None


# ── notebook_for_site ─────────────────────────────────────────────────


def test_notebook_for_site():
    assert notebook_for_site(SITE_UPHS) == NOTEBOOK_UPHS
    assert notebook_for_site(SITE_LGH) == NOTEBOOK_LGH
    assert notebook_for_site(None) is None


# ── group_site ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "group, expected",
    [
        ("LGH", SITE_LGH),
        ("LGH\\Epic", SITE_LGH),
        ("LGH\\Technical Services", SITE_LGH),
        ("EUS\\HUP", SITE_UPHS),
        ("EUS\\CCH\\CCH Telecom", SITE_UPHS),
        ("Service Desk", None),
        ("Applications", None),
        # Short group aliases used by KB/KG text (no path or campus marker).
        # 'PC Techs' is the LGH-only PC Technicians structure.
        ("PC Techs", SITE_LGH),
        ("PC Techs per location", SITE_LGH),
        ("PC Technicians (LGH)", SITE_LGH),
        ("LGH\\Shared Services (LGH)\\PC Technicians (LGH)\\Hospital (LGH)", SITE_LGH),
        ("Field Services", SITE_UPHS),
    ],
)
def test_group_site(group, expected):
    assert group_site(group) == expected


def test_group_site_none():
    assert group_site(None) is None
    assert group_site("") is None


# ── sites_conflict ────────────────────────────────────────────────────


def test_sites_conflict():
    assert sites_conflict(SITE_UPHS, SITE_LGH) is True
    assert sites_conflict(SITE_LGH, SITE_UPHS) is True
    assert sites_conflict(SITE_UPHS, SITE_UPHS) is False
    assert sites_conflict(SITE_LGH, SITE_LGH) is False
    # Unknown never conflicts.
    assert sites_conflict(None, SITE_UPHS) is False
    assert sites_conflict(SITE_LGH, None) is False
    assert sites_conflict(None, None) is False


# ── site_for_location_path ────────────────────────────────────────────


@pytest.mark.parametrize(
    "path, expected",
    [
        # Top-level campus segment drives the decision (leaf need not be a keyword).
        ("PPMC\\MUTCH", SITE_UPHS),
        ("HUP\\RAVDIN", SITE_UPHS),
        ("PAH", SITE_UPHS),
        ("Doylestown (PMDH)\\Some Wing", SITE_UPHS),
        ("LGH\\Epic", SITE_LGH),
        ("LGHP", SITE_LGH),
        # Ambiguous/site-neutral top segments → None (fall back to keyword detect).
        ("CAMPUS\\1500 MARKET ST", None),
        ("Remote User", None),
        # Case-insensitive top segment.
        ("ppmc\\mutch", SITE_UPHS),
        # Empty / missing.
        ("", None),
        (None, None),
    ],
)
def test_site_for_location_path(path, expected):
    assert site_for_location_path(path) == expected


def test_site_for_location_path_falls_back_to_keywords():
    # Unknown top segment but a UPHS keyword deeper in the path.
    assert site_for_location_path("CAMPUS\\PennChart Support") == SITE_UPHS
