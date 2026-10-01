"""Defects a write repairs on the way through ``_mutate``.

Two were found in a research session against a live tree:

- people listing the same family twice in ``family_list`` or
  ``parent_family_list`` -- gramps-webapi appends a family to a new parent
  without checking, and a later detach then left a one-sided link;
- places created by ``add_place`` with their type under a stray ``type`` key,
  so the place stayed Unknown while ``get_place`` reported the type it was
  asked for.

Each is repaired by the next write of the object, and no write re-creates it.
"""

from __future__ import annotations

from gramps_evidence_mcp.models import EventInput, Gender, NameParts


async def _person(service, given="Orrin", surname="Tallow"):
    return await service.add_person(NameParts(given=given, surname=surname), Gender.male)


def _puts(fake, typ: str) -> int:
    return len([r for r in fake.requests if r[0] == "PUT" and r[1] == typ])


# --------------------------------------------------------------------------- #
# duplicated person -> family links
# --------------------------------------------------------------------------- #
async def test_any_write_to_a_person_removes_a_duplicated_parent_family(service, fake):
    child = await _person(service)
    family = await service.add_family(None, None, [child["gramps_id"]])
    stored = fake.store["person"][child["handle"]]
    other = "h-other-family"
    stored["parent_family_list"] = [family["handle"], other, family["handle"]]

    result = await service.update_person(child["gramps_id"])

    assert result["changed"] is True
    assert result["repaired"] == ["removed 1 duplicate parent_family_list entry"]
    assert "Repaired person" in result["message"]
    # Order kept: the first occurrence stays where it was.
    assert fake.store["person"][child["handle"]]["parent_family_list"] == [
        family["handle"],
        other,
    ]


async def test_a_duplicated_family_list_entry_is_repaired_too(service, fake):
    spouse = await _person(service)
    family = await service.add_family(spouse["gramps_id"], None, [])
    fake.store["person"][spouse["handle"]]["family_list"] = [family["handle"]] * 3

    result = await service.set_private("person", spouse["gramps_id"], False)

    assert fake.store["person"][spouse["handle"]]["family_list"] == [family["handle"]]
    assert "removed 2 duplicate family_list entries" in result["message"]


async def test_an_ordinary_edit_repairs_and_says_so(service, fake):
    """The repair rides along with whatever the caller came to do."""
    child = await _person(service)
    family = await service.add_family(None, None, [child["gramps_id"]])
    fake.store["person"][child["handle"]]["parent_family_list"] = [family["handle"]] * 2

    result = await service.add_event_to_person(
        child["gramps_id"], EventInput(type="Residence", date="1910"), require_citation=False
    )

    assert "also repaired" in result["message"]
    assert result["repaired"]
    assert fake.store["person"][child["handle"]]["parent_family_list"] == [family["handle"]]


async def test_a_clean_person_is_not_rewritten(service, fake):
    """No duplicate, no change: no PUT, so the transaction log stays honest."""
    person = await _person(service)
    before = _puts(fake, "person")
    result = await service.update_person(person["gramps_id"])
    assert result["changed"] is False
    assert _puts(fake, "person") == before


async def test_the_fake_reproduces_how_the_duplicate_arises(service, fake):
    """gramps-webapi 3.21.1 appends a family to a new father without checking.

    Proves the fake models the server's defect, so the repair tests above
    exercise the real shape of the problem rather than an invented one.
    """
    father = await _person(service)
    family = await service.add_family(None, None, [])
    fake.store["person"][father["handle"]]["family_list"] = [family["handle"]]
    await service.client.update_object(
        "family",
        family["handle"],
        dict(fake.store["family"][family["handle"]], father_handle=father["handle"]),
    )
    assert fake.store["person"][father["handle"]]["family_list"] == [family["handle"]] * 2


# --------------------------------------------------------------------------- #
# write ordering: a bad reference fails before anything is created
# --------------------------------------------------------------------------- #
async def test_a_bad_person_creates_no_orphan_event(service, fake):
    out = None
    try:
        out = await service.add_event_to_person(
            "I9999", EventInput(type="Residence", date="1910"), require_citation=False
        )
    except Exception as exc:  # noqa: BLE001 - the not-found is what is expected
        out = exc
    assert "I9999" in str(out)
    assert fake.store["event"] == {}


async def test_a_bad_note_target_creates_no_orphan_note(service, fake):
    try:
        await service.add_note("I9999", "person", "A finding.", "Research")
    except Exception:  # noqa: BLE001
        pass
    assert fake.store["note"] == {}


async def test_a_bad_family_creates_no_orphan_event(service, fake):
    try:
        await service.add_event_to_family(
            "F9999", EventInput(type="Marriage", date="1910"), require_citation=False
        )
    except Exception:  # noqa: BLE001
        pass
    assert fake.store["event"] == {}


# --------------------------------------------------------------------------- #
# place type
# --------------------------------------------------------------------------- #
async def test_add_place_writes_the_place_type_field(service, fake):
    """1.0.x wrote a stray 'type' key; Gramps reads place_type."""
    out = await service.add_place("Brannock", place_type="County")
    raw = fake.store["place"][out["handle"]]
    assert raw["place_type"] == "County"
    assert "type" not in raw
    assert (await service.get_place(out["gramps_id"]))["type"] == "County"


async def test_get_place_reports_the_real_type_and_flags_a_stray_key(service, fake):
    out = await service.add_place("Cedar Flat")
    raw = fake.store["place"][out["handle"]]
    raw["type"] = "Town"
    raw["place_type"] = "Unknown"

    shown = await service.get_place(out["gramps_id"])
    assert shown["type"] == "Unknown"
    assert shown["stray_type_key"] == "Town"


async def test_a_place_write_moves_a_stray_type_into_an_unknown_place_type(service, fake):
    out = await service.add_place("Cedar Flat")
    raw = fake.store["place"][out["handle"]]
    raw["type"] = "Town"

    result = await service.update_place(out["gramps_id"])

    assert result["changed"] is True
    stored = fake.store["place"][out["handle"]]
    assert stored["place_type"] == "Town"
    assert "type" not in stored
    assert "stray_type_key" not in await service.get_place(out["gramps_id"])


async def test_a_stray_type_never_overrides_a_real_one(service, fake):
    out = await service.add_place("Brannock", place_type="County")
    fake.store["place"][out["handle"]]["type"] = "Town"

    result = await service.update_place(out["gramps_id"], code="39001")

    stored = fake.store["place"][out["handle"]]
    assert stored["place_type"] == "County"
    assert "type" not in stored
    assert "dropped a stray 'type' key" in result["message"]


# --------------------------------------------------------------------------- #
# found in review
# --------------------------------------------------------------------------- #
async def test_a_refused_event_edit_creates_no_place(tools):
    """Place resolution can create a place, so it runs after every check."""
    person = await tools("add_person", given="Ada", surname="Wren")
    added = await tools(
        "add_event_to_person",
        person=person["gramps_id"],
        event={"type": "Residence", "date": "1900"},
        require_citation=False,
    )
    event = tools.fake.store["event"][added["event_handle"]]["gramps_id"]
    for args in (
        {"event": "E9999", "place": "Springfeild"},
        {"event": event, "place": "New Town", "event_type": "Censsus"},
    ):
        out = await tools("update_event", **args)
        assert "error" in out, args
    assert tools.fake.store["place"] == {}


async def test_a_failed_name_write_leaves_no_minted_citation(tools):
    person = await tools("add_person", given="Ada", surname="Wren")
    tools.fake.put_error = 500
    out = await tools(
        "add_alternate_name",
        person=person["gramps_id"],
        surname="Renn",
        citation={"source_title": "Census 1880", "page": "p. 3", "note": "Spelled Renn."},
    )
    assert out["error"] == "api"
    assert tools.fake.store["citation"] == {}
    assert tools.fake.store["note"] == {}
    assert tools.fake.store["source"] == {}


async def test_a_passing_metadata_failure_does_not_decide_the_session(tools):
    from gramps_evidence_mcp.mapping import MOD_FROM, MOD_TEXTONLY

    person = await tools("add_person", given="Ada", surname="Wren")
    tools.fake.metadata_error = 502
    first = await tools(
        "add_event_to_person",
        person=person["gramps_id"],
        event={"type": "Residence", "date": "from 1880"},
        require_citation=False,
    )
    second = await tools(
        "add_event_to_person",
        person=person["gramps_id"],
        event={"type": "Residence", "date": "from 1890"},
        require_citation=False,
    )
    store = tools.fake.store["event"]
    assert store[first["event_handle"]]["date"]["modifier"] == MOD_TEXTONLY
    assert store[second["event_handle"]]["date"]["modifier"] == MOD_FROM


async def test_a_bad_row_does_not_end_a_sweep(tools):
    good = await tools("add_citation", citation={"source_title": "Cemetery", "page": "p. 1"})
    out = await tools(
        "update_citations",
        items=[{"citation": ".", "page": "x"}, {"citation": good["gramps_id"], "page": "p. 2"}],
    )
    assert [r["status"] for r in out["rows"]] == ["error", "applied"]
