"""DNA matches, Y-DNA and segment parsing.

DNA is a different evidence model from the rest of this server. A match
proves a biological relationship exists without saying which one, so the
tools report the figures a relationship estimate rests on -- total shared
centiMorgans and the largest segment -- rather than implying a conclusion.
"""

from __future__ import annotations

import pytest

SEGMENTS = [
    {
        "chromosome": "1",
        "start": 1000000,
        "stop": 5000000,
        "cM": 7.5,
        "SNPs": 1200,
        "side": "M",
        "comment": "",
    },
    {
        "chromosome": "7",
        "start": 2000000,
        "stop": 9000000,
        "cM": 21.25,
        "SNPs": 3100,
        "side": "U",
        "comment": "reviewed",
    },
]


async def _person(tools, given="Josiah", surname="Pembrook") -> dict:
    return await tools("add_person", given=given, surname=surname)


# --------------------------------------------------------------------------- #
# get_dna_matches
# --------------------------------------------------------------------------- #
async def test_match_totals_are_computed_from_the_segments(tools):
    """Total and largest cM are what a relationship estimate rests on.

    The server returns segments but not the sums, so an assistant would
    otherwise have to add them up itself -- exactly the arithmetic that goes
    wrong quietly.
    """
    p = await _person(tools)
    tools.fake.dna_matches[p["handle"]] = [
        {
            "handle": "h_other",
            "relation": "second cousin",
            "segments": SEGMENTS,
            "ancestor_handles": [],
        }
    ]
    out = await tools("get_dna_matches", person=p["gramps_id"])
    match = out["matches"][0]
    assert match["total_cM"] == 28.75
    assert match["largest_segment_cM"] == 21.25
    assert match["segment_count"] == 2


async def test_matches_without_a_common_ancestor_are_counted(tools):
    """That count is the open research: a match nobody has placed yet."""
    p = await _person(tools)
    tools.fake.dna_matches[p["handle"]] = [
        {"handle": "a", "segments": SEGMENTS, "ancestor_handles": []},
        {
            "handle": "b",
            "segments": SEGMENTS,
            "ancestor_handles": ["anc1"],
            "ancestor_profiles": [{"name": "Josiah Pembrook"}],
        },
    ]
    out = await tools("get_dna_matches", person=p["gramps_id"])
    assert out["match_count"] == 2
    assert out["unattributed_count"] == 1


async def test_common_ancestors_pair_handles_with_names(tools):
    """A bare handle is not something a reader can act on."""
    p = await _person(tools)
    tools.fake.dna_matches[p["handle"]] = [
        {
            "handle": "a",
            "segments": SEGMENTS,
            "ancestor_handles": ["anc1"],
            "ancestor_profiles": [{"name": "Mercy Ashbee"}],
        }
    ]
    out = await tools("get_dna_matches", person=p["gramps_id"])
    assert out["matches"][0]["common_ancestors"] == [{"handle": "anc1", "name": "Mercy Ashbee"}]


async def test_ancestor_handles_without_profiles_still_appear(tools):
    """Never drop an ancestor for want of a profile to name it."""
    p = await _person(tools)
    tools.fake.dna_matches[p["handle"]] = [
        {"handle": "a", "segments": [], "ancestor_handles": ["anc1", "anc2"]}
    ]
    out = await tools("get_dna_matches", person=p["gramps_id"])
    names = out["matches"][0]["common_ancestors"]
    assert [a["handle"] for a in names] == ["anc1", "anc2"]


async def test_segment_sides_are_spelled_out(tools):
    """'M' means maternal. A reader should not have to know the code."""
    p = await _person(tools)
    tools.fake.dna_matches[p["handle"]] = [
        {"handle": "a", "segments": SEGMENTS, "ancestor_handles": []}
    ]
    out = await tools("get_dna_matches", person=p["gramps_id"])
    sides = [s["side"] for s in out["matches"][0]["segments"]]
    assert sides == ["maternal", "unknown"]


async def test_a_person_with_no_matches_is_not_an_error(tools):
    """Almost nobody in a tree has DNA recorded; that is normal."""
    p = await _person(tools)
    out = await tools("get_dna_matches", person=p["gramps_id"])
    assert out["match_count"] == 0
    assert out["matches"] == []


async def test_raw_note_text_is_withheld_unless_asked_for(tools):
    """The parsed segments are the useful form; the notes are bulk."""
    p = await _person(tools)
    tools.fake.dna_matches[p["handle"]] = [
        {
            "handle": "a",
            "segments": SEGMENTS,
            "ancestor_handles": [],
            "raw_data": ["1,1000000,5000000,7.5,1200"],
        }
    ]
    plain = await tools("get_dna_matches", person=p["gramps_id"])
    assert "raw_data" not in plain["matches"][0]
    verbose = await tools("get_dna_matches", person=p["gramps_id"], include_raw=True)
    assert verbose["matches"][0]["raw_data"]


# --------------------------------------------------------------------------- #
# get_ydna
# --------------------------------------------------------------------------- #
async def test_ydna_reports_the_terminal_clade(tools):
    """The terminal clade is the answer; the lineage is the working."""
    p = await _person(tools)
    tools.fake.ydna[p["handle"]] = {
        "clade_lineage": [
            {"name": "R", "snps": ["M207"]},
            {"name": "R-M269", "snps": ["M269"]},
            {"name": "R-L21", "snps": ["L21"]},
        ],
        "tree_version": "11.03.00",
    }
    out = await tools("get_ydna", person=p["gramps_id"])
    assert out["has_data"] is True
    assert out["terminal_clade"] == "R-L21"
    assert [c["name"] for c in out["clade_lineage"]] == ["R", "R-M269", "R-L21"]
    assert out["tree_version"] == "11.03.00"


async def test_no_ydna_reports_has_data_false(tools):
    """An empty document must not read as an unknown haplogroup."""
    p = await _person(tools)
    out = await tools("get_ydna", person=p["gramps_id"])
    assert out["has_data"] is False
    assert out["terminal_clade"] is None


# --------------------------------------------------------------------------- #
# parse_dna_segments
# --------------------------------------------------------------------------- #
async def test_parsing_reports_totals_and_chromosomes(tools):
    """What a caller wants before recording a match is the shape of it."""
    tools.fake.parsed_segments = SEGMENTS
    out = await tools("parse_dna_segments", data="1,1000000,5000000,7.5,1200")
    assert out["parsed"] is True
    assert out["total_cM"] == 28.75
    assert out["largest_segment_cM"] == 21.25
    assert out["chromosomes"] == ["1", "7"]


async def test_unparseable_input_is_reported_as_a_parse_failure(tools):
    """The server answers garbage with zero segments and HTTP 200.

    Verified live 2026-09-23. Passing that through unmarked would read as
    "this person shares no DNA" rather than "I could not read that", which
    is the difference between a negative finding and a mistake.
    """
    tools.fake.parsed_segments = []
    out = await tools("parse_dna_segments", data="not dna data at all")
    assert out["parsed"] is False
    assert out["segment_count"] == 0
    assert "parse failure" in out["message"] or "not an absence" in out["message"]


async def test_the_input_reaches_the_parser_unmodified(tools):
    """Tab-separated and multi-line input must not be mangled on the way."""
    tools.fake.parsed_segments = SEGMENTS
    payload = "Chromosome\tStart\tEnd\tcM\tSNPs\n1\t1000000\t5000000\t7.5\t1200"
    await tools("parse_dna_segments", data=payload)
    assert tools.fake.parser_inputs[-1] == payload


@pytest.mark.parametrize("tool_name", ["get_dna_matches", "get_ydna"])
async def test_dna_tools_report_an_unknown_person_cleanly(tools, tool_name):
    """A bad id is not_found, never an exception."""
    out = await tools(tool_name, person="I9999")
    assert out["error"] == "not_found"
