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
    notebook_for_site,
    sites_conflict,
)


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
