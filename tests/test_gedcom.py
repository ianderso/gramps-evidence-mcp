"""Tests for the read-only GEDCOM reference layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from gramps_evidence_mcp.gedcom_ref import ReferenceFile, apid_to_ancestry_url

FIXTURE = Path(__file__).parent / "fixtures" / "sample_ancestry.ged"


@pytest.fixture
def ref(tmp_path) -> ReferenceFile:
    return ReferenceFile(
        path=FIXTURE, label="Ancestry Sample", trust="untrusted hints", cache_dir=tmp_path
    )


def test_parses_all_individuals(ref):
    people = ref._load()
    assert {p.name for p in people} == {"John Smith", "Mary Jones", "Baby Smith"}


def test_name_and_year_search(ref):
    hits = list(ref.search("Smith", approx_birth_year=1900))
    # John (1899) matches within tolerance; Baby (1925) does not.
    assert [p.name for p in hits] == ["John Smith"]


def test_search_substring_only(ref):
    hits = list(ref.search("mary", approx_birth_year=None))
    assert len(hits) == 1 and hits[0].surname == "Jones"


def test_birth_year_extraction_handles_abt(ref):
    mary = next(ref.search("Mary", None))
    assert mary.birth_year == 1902


def test_sourced_vs_unsourced_facts(ref):
    john = next(ref.search("John Smith", None))
    birth = next(f for f in john.facts if f.kind == "Birth")
    death = next(f for f in john.facts if f.kind == "Death")
    assert birth.has_source is True
    assert any("Marriage Index" in s for s in birth.source_text)
    assert any("Page: Vol 3" in s for s in birth.source_text)
    assert "1,61903::1234567" in birth.apid
    # Death has no SOUR in the fixture.
    assert death.has_source is False
    assert death.source_text == []


def test_inline_source_text(ref):
    baby = next(ref.search("Baby", None))
    birth = next(f for f in baby.facts if f.kind == "Birth")
    assert birth.has_source is True
    assert birth.source_text == ["Uncited family bible transcription"]


def test_family_marriage_event_attached_to_spouses(ref):
    john = next(ref.search("John Smith", None))
    marr = next(f for f in john.facts if f.kind == "Marriage")
    assert marr.date == "5 JUN 1921"
    assert marr.has_source is True


def test_apid_to_ancestry_url():
    assert apid_to_ancestry_url("1,61903::1234567") == (
        "https://www.ancestry.com/discoveryui-content/view/1234567:61903"
    )
    assert apid_to_ancestry_url("garbage") is None
    assert apid_to_ancestry_url("") is None


def test_person_level_apid_media_and_urls(ref):
    john = next(ref.search("John Smith", None))
    assert john.apid == ["1,7602::100"]
    assert john.media == ["john_smith_portrait.jpg"]
    assert john.ancestry_urls == ["https://www.ancestry.com/discoveryui-content/view/100:7602"]


def test_fact_apid_produces_ancestry_url(ref):
    john = next(ref.search("John Smith", None))
    birth = next(f for f in john.facts if f.kind == "Birth")
    assert "1,61903::1234567" in birth.apid
    assert "https://www.ancestry.com/discoveryui-content/view/1234567:61903" in birth.ancestry_urls


def test_fact_record_url_and_transcription(ref):
    john = next(ref.search("John Smith", None))
    birth = next(f for f in john.facts if f.kind == "Birth")
    assert "https://www.ancestry.com/discoveryui-content/view/1234567:61903" in birth.record_urls
    assert any("John Smith" in t for t in birth.transcription)
    # all_urls prefers the direct WWW link and dedups against the derived one.
    assert birth.all_urls[0] == birth.record_urls[0]


def test_person_notes_captured(ref):
    john = next(ref.search("John Smith", None))
    assert any("carpenter" in n for n in john.notes)


def test_custom_even_type_becomes_kind(ref):
    john = next(ref.search("John Smith", None))
    kinds = [f.kind for f in john.facts]
    assert "Military Service" in kinds


def test_disk_cache_roundtrip(tmp_path):
    ref1 = ReferenceFile(FIXTURE, "L", "t", tmp_path)
    ref1._load()
    caches = list(tmp_path.glob("*.json"))
    assert len(caches) == 1
    # Fresh instance should read from cache and produce identical results.
    ref2 = ReferenceFile(FIXTURE, "L", "t", tmp_path)
    assert {p.name for p in ref2._load()} == {"John Smith", "Mary Jones", "Baby Smith"}
