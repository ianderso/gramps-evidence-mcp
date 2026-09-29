"""Tests for the pure wire-format mapping layer (esp. date parsing)."""

from __future__ import annotations

from gramps_evidence_mcp.mapping import (
    MOD_ABOUT,
    MOD_AFTER,
    MOD_BEFORE,
    MOD_NONE,
    MOD_RANGE,
    MOD_TEXTONLY,
    QUAL_ESTIMATED,
    confidence_to_int,
    gender_to_int,
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
