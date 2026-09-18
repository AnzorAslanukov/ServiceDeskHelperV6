"""
Unit tests for deploy.py's ticket-embedding scope prompt parsing.

Scope: the pure, I/O-free parser ``parse_limit_choice`` that backs the Step 4
"how many NEW tickets to embed this run?" prompt. The interactive prompt and
the SSH/SCP deploy flow are I/O-bound and left to manual/integration testing,
consistent with the repo's style.
"""

import pytest

import deploy


@pytest.mark.parametrize("raw", ["", "  ", "a", "A", "all", "ALL", "  all  "])
def test_all_variants_map_to_all(raw):
    # Blank / whitespace-only == pressing Enter == the "All" default.
    assert deploy.parse_limit_choice(raw) == ("all", None)


@pytest.mark.parametrize("raw", ["c", "C", "cancel", "CANCEL", "  cancel "])
def test_cancel_variants_map_to_cancel(raw):
    assert deploy.parse_limit_choice(raw) == ("cancel", None)


@pytest.mark.parametrize(
    "raw,expected",
    [("1", 1), ("42", 42), ("5000", 5000), ("  100 ", 100), ("000123", 123)],
)
def test_positive_integers_map_to_limit(raw, expected):
    assert deploy.parse_limit_choice(raw) == ("limit", expected)


@pytest.mark.parametrize("raw", ["0", "-5", "-1", "abc", "5k", "1.5", "12x"])
def test_zero_negative_and_nonnumbers_are_invalid(raw):
    # 0 and negatives are NOT treated as "all"; non-numbers are rejected.
    assert deploy.parse_limit_choice(raw) == ("invalid", None)


def test_none_input_is_treated_as_all():
    # Defensive: a None (e.g. from an empty read) behaves like blank -> all.
    assert deploy.parse_limit_choice(None) == ("all", None)
