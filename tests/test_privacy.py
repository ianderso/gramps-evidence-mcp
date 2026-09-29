"""Tests for privacy assessment logic."""

from __future__ import annotations

from gramps_evidence_mcp.privacy import LIVING_THRESHOLD_YEARS, assess

NOW = 2026


def test_deceased_is_not_restricted():
    a = assess(private_flag=False, birth_year=1900, death_year=1970, current_year=NOW)
    assert not a.is_probably_living and not a.restricted


def test_recent_birth_no_death_is_living():
    a = assess(private_flag=False, birth_year=2000, death_year=None, current_year=NOW)
    assert a.is_probably_living and a.restricted


def test_old_birth_no_death_presumed_dead():
    a = assess(
        private_flag=False,
        birth_year=NOW - LIVING_THRESHOLD_YEARS - 1,
        death_year=None,
        current_year=NOW,
    )
    assert not a.is_probably_living


def test_unknown_birth_no_death_is_conservatively_living():
    a = assess(private_flag=False, birth_year=None, death_year=None, current_year=NOW)
    assert a.is_probably_living


def test_private_flag_restricts_even_if_deceased():
    a = assess(private_flag=True, birth_year=1800, death_year=1850, current_year=NOW)
    assert a.is_private and a.restricted and not a.is_probably_living


def test_an_undated_death_is_still_a_recorded_death():
    """A Death event with no date on it: the person died, whenever it was."""
    v = assess(private_flag=False, birth_year=1950, death_year=None, current_year=2026, died=True)
    assert v.is_probably_living is False
    assert v.restricted is False
