"""Tests for the pure wire-format mapping layer (esp. date parsing)."""

from __future__ import annotations

import pytest

from gramps_evidence_mcp import mapping
from gramps_evidence_mcp.mapping import (
    MOD_ABOUT,
    MOD_AFTER,
    MOD_BEFORE,
    MOD_FROM,
    MOD_NONE,
    MOD_RANGE,
    MOD_SPAN,
    MOD_TEXTONLY,
    MOD_TO,
    QUAL_CALCULATED,
    QUAL_ESTIMATED,
    confidence_to_int,
    date_display,
    gender_to_int,
    is_open_span,
    parse_date,
    year_from_date_dict,
)
from gramps_evidence_mcp.models import Confidence, Gender


def test_gender_and_confidence_ints():
    assert gender_to_int(Gender.female) == 0
    assert gender_to_int(Gender.male) == 1
    assert gender_to_int(Gender.unknown) == 2
    assert confidence_to_int(Confidence.very_low) == 0
    assert confidence_to_int(Confidence.very_high) == 4


def test_year_only():
    d = parse_date("1899")
    assert d["dateval"] == [0, 0, 1899, False]
    assert d["modifier"] == MOD_NONE


def test_full_date_day_month_year():
    d = parse_date("12 Jan 1899")
    assert d["dateval"] == [12, 1, 1899, False]


def test_month_year():
    d = parse_date("Jan 1899")
    assert d["dateval"] == [0, 1, 1899, False]


def test_about():
    d = parse_date("ABT 1900")
    assert d["modifier"] == MOD_ABOUT
    assert d["dateval"][2] == 1900


def test_before_after():
    assert parse_date("BEF 1950")["modifier"] == MOD_BEFORE
    assert parse_date("AFT 1850")["modifier"] == MOD_AFTER


def test_range():
    d = parse_date("BET 1898 AND 1901")
    assert d["modifier"] == MOD_RANGE
    assert d["dateval"] == [0, 0, 1898, False, 0, 0, 1901, False]


def test_estimated_quality():
    d = parse_date("EST 1900")
    assert d["quality"] == QUAL_ESTIMATED
    assert d["dateval"][2] == 1900


def test_unparseable_becomes_text_only():
    d = parse_date("sometime during the war")
    assert d["modifier"] == MOD_TEXTONLY
    assert d["text"] == "sometime during the war"


def test_empty_date():
    d = parse_date(None)
    assert d["dateval"] == [0, 0, 0, False]
    assert d["modifier"] == MOD_NONE


def test_iso_style_date():
    d = parse_date("1899-01-12")
    assert d["dateval"][2] == 1899 and d["dateval"][1] == 1 and d["dateval"][0] == 12


def test_year_from_date_dict():
    assert year_from_date_dict(parse_date("12 Jan 1899")) == 1899
    assert year_from_date_dict(parse_date("sometime in 1920")) == 1920
    assert year_from_date_dict(parse_date(None)) is None


# --------------------------------------------------------------------------- #
# Spans: "from ... to ..." lasted the whole interval; "between" happened once
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "from 4 May 1864 to 16 Sep 1864",
        "FROM 4 MAY 1864 TO 16 SEP 1864",
        "from 1864-05-04 to 1864-09-16",
    ],
)
def test_from_to_is_a_span_not_a_range(text):
    """Every spelling a session tried was stored as a range ("between")."""
    d = parse_date(text)
    assert d["modifier"] == MOD_SPAN
    assert d["dateval"] == [4, 5, 1864, False, 16, 9, 1864, False]


def test_between_is_still_a_range():
    assert parse_date("between 1882 and 1883")["modifier"] == MOD_RANGE
    assert parse_date("bet. 1882 and 1883")["modifier"] == MOD_RANGE


def test_a_lone_from_is_open_ended_not_dropped():
    """ "from 1880" used to be stored as plain 1880, the word lost."""
    d = parse_date("from 1880")
    assert d["modifier"] == MOD_FROM
    assert d["dateval"] == [0, 0, 1880, False]
    assert is_open_span("from 1880")


def test_a_lone_to_is_open_ended():
    d = parse_date("TO 12 Mar 1890")
    assert d["modifier"] == MOD_TO
    assert d["dateval"] == [12, 3, 1890, False]


def test_an_open_span_falls_back_to_text_where_unsupported():
    """Before Gramps 5.2 there is no "from X" date: keep the words, as text."""
    d = parse_date("from 1880", open_spans=False)
    assert d["modifier"] == MOD_TEXTONLY
    assert d["text"] == "from 1880"
    # A closed span needs no new modifier and is unaffected.
    assert parse_date("from 1880 to 1890", open_spans=False)["modifier"] == MOD_SPAN


def test_quality_combines_with_a_span():
    d = parse_date("est from 1880 to 1890")
    assert d["quality"] == QUAL_ESTIMATED
    assert d["modifier"] == MOD_SPAN


def test_an_unreadable_span_end_keeps_the_whole_text():
    d = parse_date("from the war to 1870")
    assert d["modifier"] == MOD_TEXTONLY
    assert d["text"] == "from the war to 1870"


# --------------------------------------------------------------------------- #
# Display: never more precise than what is stored
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("between 1882 and 1883", "between 1882 and 1883"),
        ("from 4 May 1864 to 16 Sep 1864", "from 1864-05-04 to 1864-09-16"),
        ("from 1880", "from 1880"),
        ("to 1890", "to 1890"),
        ("ABT 1900", "about 1900"),
        ("BEF 1950", "before 1950"),
        ("AFT 1850", "after 1850"),
        ("EST 1900", "estimated 1900"),
        ("12 Jan 1899", "1899-01-12"),
        ("Jan 1899", "1899-01"),
        ("1899", "1899"),
        ("sometime during the war", "sometime during the war"),
    ],
)
def test_date_display(text, shown):
    """A range shown as its first year states a precision the tree lacks."""
    assert date_display(parse_date(text)) == shown


def test_date_display_shows_calculated_quality_and_calendar():
    d = parse_date("cal abt 1700")
    d["calendar"] = 1
    assert d["quality"] == QUAL_CALCULATED
    assert date_display(d) == "calculated about 1700 (Julian)"


def test_date_display_of_an_empty_date_is_none():
    assert date_display(parse_date(None)) is None
    assert date_display(None) is None


def test_age_between_counts_whole_years_and_says_when_rough():
    """The anchor's age on consolidated_timeline (TOOL-REQUESTS #34)."""
    born = mapping.parse_date("10 Mar 1850")
    assert mapping.age_between(born, mapping.parse_date("9 Mar 1880")) == "29 years"
    assert mapping.age_between(born, mapping.parse_date("10 Mar 1880")) == "30 years"
    assert mapping.age_between(born, mapping.parse_date("10 Mar 1851")) == "1 year"
    assert mapping.age_between(born, mapping.parse_date("1880")) == "about 30 years"
    assert mapping.age_between(born, mapping.parse_date("about 1 Jun 1880")) == "about 30 years"
    assert mapping.age_between(mapping.parse_date("1850"), born) == "about 0 years"
    assert mapping.age_between(born, mapping.parse_date("1849")) is None
    assert mapping.age_between(born, mapping.parse_date("in the spring")) is None
    assert mapping.age_between(None, born) is None
