"""Recording a DNA match as evidence.

Gramps stores a match as an association from the tested person to the match
with relationship "DNA" (gramps_webapi/api/resources/dna.py, and what the
Gramps Web interface writes). The segment data sits in a note on the
association and the citation on it names the test.
"""

from __future__ import annotations

SEGMENTS = "chromosome,start,stop,cM,SNPs\n1,1000000,5000000,7.5,1200\n7,2000000,9000000,21.25,3100"
TEST = {
    "source_title": "DNA test of Josiah Pembrook, kit A1",
    "page": "match list, viewed 2026-09-01",
    "confidence": "high",
}


async def _pair(tools) -> tuple[dict, dict]:
    tested = await tools("add_person", given="Josiah", surname="Pembrook")
    match = await tools("add_person", given="Mercy", surname="Ashbee")
    return tested, match


async def test_a_match_is_stored_as_gramps_web_stores_one(tools):
    tested, match = await _pair(tools)
    out = await tools(
        "add_dna_match",
        person=tested["gramps_id"],
        match=match["gramps_id"],
        segments=SEGMENTS,
        citation=TEST,
    )
    assert out["changed"] is True
    ref = tools.fake.store["person"][tested["handle"]]["person_ref_list"][0]
    assert ref["rel"] == "DNA"
    assert ref["ref"] == match["handle"]
    assert ref["citation_list"] == [out["citation_handle"]]
    note = tools.fake.store["note"][ref["note_list"][0]]
    assert note["text"]["string"] == SEGMENTS


async def test_the_citation_names_the_test_and_grades_the_match(tools):
    tested, match = await _pair(tools)
    out = await tools(
        "add_dna_match",
        person=tested["gramps_id"],
        match=match["gramps_id"],
        segments=SEGMENTS,
        citation=TEST,
    )
    citation = tools.fake.store["citation"][out["citation_handle"]]
    assert citation["page"] == TEST["page"]
    assert citation["confidence"] == 3
    source = tools.fake.store["source"][citation["source_handle"]]
    assert source["title"] == TEST["source_title"]


async def test_a_recorded_match_reads_back_with_its_totals(tools):
    """Written and read in the shape the server uses, the round trip closes."""
    tested, match = await _pair(tools)
    out = await tools(
        "add_dna_match",
        person=tested["gramps_id"],
        match=match["gramps_id"],
        segments=SEGMENTS,
        citation=TEST,
    )
    assert (out["segment_count"], out["total_cM"], out["largest_segment_cM"]) == (2, 28.75, 21.25)
    read = await tools("get_dna_matches", person=tested["gramps_id"])
    assert read["match_count"] == 1
    assert read["matches"][0]["handle"] == match["handle"]
    assert read["matches"][0]["total_cM"] == 28.75


async def test_unreadable_segments_write_nothing(tools):
    """The server parses garbage to no segments; that must not become a match."""
    tested, match = await _pair(tools)
    before = {t: len(v) for t, v in tools.fake.store.items()}
    out = await tools(
        "add_dna_match",
        person=tested["gramps_id"],
        match=match["gramps_id"],
        segments="shared 28 cM across 2 segments",
        citation=TEST,
    )
    assert out["error"] == "unparsed_segments"
    assert {t: len(v) for t, v in tools.fake.store.items()} == before


async def test_a_second_record_of_the_same_match_is_refused(tools):
    """Two companies' reports of one match are the same DNA; adding both
    would double the shared centiMorgans."""
    tested, match = await _pair(tools)
    args = dict(person=tested["gramps_id"], match=match["gramps_id"], segments=SEGMENTS)
    await tools("add_dna_match", citation=TEST, **args)
    before = len(tools.fake.store["citation"])
    out = await tools("add_dna_match", citation=TEST, **args)
    assert out["error"] == "match_exists"
    assert len(tools.fake.store["citation"]) == before


async def test_a_person_is_not_their_own_match(tools):
    tested, _ = await _pair(tools)
    out = await tools(
        "add_dna_match",
        person=tested["gramps_id"],
        match=tested["gramps_id"],
        segments=SEGMENTS,
        citation=TEST,
    )
    assert out["error"] == "same_person"


async def test_the_match_mints_its_own_citation(tools):
    """A citation carries one confidence; reusing one re-grades another claim."""
    tested, match = await _pair(tools)
    existing = await tools("add_citation", citation={"source_title": "Census", "page": "p. 1"})
    out = await tools(
        "add_dna_match",
        person=tested["gramps_id"],
        match=match["gramps_id"],
        segments=SEGMENTS,
        citation={"citation": existing["gramps_id"]},
    )
    assert out["error"] == "citation_reuse_refused"
    assert (
        "person_ref_list" not in tools.fake.store["person"][tested["handle"]]
        or not (tools.fake.store["person"][tested["handle"]]["person_ref_list"])
    )


async def test_a_failed_write_leaves_no_debris(tools, monkeypatch):
    """The note and citation are minted first; if the match itself does not
    land, they would be orphans that look like evidence of something."""
    tested, match = await _pair(tools)

    async def refuse(*args, **kwargs):
        raise RuntimeError("write refused")

    monkeypatch.setattr(tools.service, "_mutate", refuse)
    before = (len(tools.fake.store["note"]), len(tools.fake.store["citation"]))
    out = await tools(
        "add_dna_match",
        person=tested["gramps_id"],
        match=match["gramps_id"],
        segments=SEGMENTS,
        citation=TEST,
    )
    assert out["error"] == "unexpected"
    assert (len(tools.fake.store["note"]), len(tools.fake.store["citation"])) == before
