"""Editing an event in place, and sharing one between people.

From a research session: a wrong event type could only be fixed by creating a
replacement event and deleting the original, which changes its gramps_id,
loses whatever was not copied by hand, and for an event shared by two people
cannot be done at all, since nothing added an existing event to a person. A
place no source states could not be removed. A "from ... to" date was stored
as a range, and a range was read back as its first year.
"""

from __future__ import annotations

from gramps_evidence_mcp.mapping import MOD_FROM, MOD_SPAN, MOD_TEXTONLY


async def _person_with_event(tools, event_type="Residence", date="1898", place=None):
    person = await tools("add_person", given="Wilbur", surname="Asquith")
    event = {
        "type": event_type,
        "date": date,
        "citation": {"source_title": "County history, 1898", "page": "p. 212"},
    }
    if place:
        event["place"] = place
    added = await tools("add_event_to_person", person=person["gramps_id"], event=event)
    stored = tools.fake.store["event"][added["event_handle"]]
    return person, stored


def _puts(tools) -> int:
    return len([r for r in tools.fake.requests if r[0] == "PUT"])


# --------------------------------------------------------------------------- #
# type
# --------------------------------------------------------------------------- #
async def test_the_type_changes_in_place_and_nothing_else_moves(tools):
    person, event = await _person_with_event(tools, event_type="Elected")
    await tools("tag_object", object_type="event", target=event["gramps_id"], tag="Conflict")
    before = dict(tools.fake.store["event"][event["handle"]])

    out = await tools("update_event", event=event["gramps_id"], event_type="Property")

    assert out["changed"] is True
    assert "type Elected -> Property" in out["message"]
    after = tools.fake.store["event"][event["handle"]]
    assert after["type"] == "Property"
    assert after["gramps_id"] == before["gramps_id"]
    assert after["citation_list"] == before["citation_list"]
    assert after["tag_list"] == before["tag_list"]
    assert tools.fake.partial_writes == []


async def test_an_unknown_type_is_refused_before_writing(tools):
    """Gramps would store the typo as a new custom type, for good."""
    _, event = await _person_with_event(tools)
    puts = _puts(tools)
    out = await tools("update_event", event=event["gramps_id"], event_type="Censsus")
    assert out["error"] == "unknown_type"
    assert "Census" in out["message"]
    assert _puts(tools) == puts


async def test_a_custom_type_the_tree_has_is_accepted_and_spelled_its_way(tools):
    _, event = await _person_with_event(tools)
    await tools("update_event", event=event["gramps_id"], event_type="widowhood")
    assert tools.fake.store["event"][event["handle"]]["type"] == "Widowhood"


async def test_a_deliberate_new_type_needs_allow_new_type(tools):
    _, event = await _person_with_event(tools)
    out = await tools(
        "update_event", event=event["gramps_id"], event_type="Visit", allow_new_type=True
    )
    assert out["changed"] is True
    assert tools.fake.store["event"][event["handle"]]["type"] == "Visit"


# --------------------------------------------------------------------------- #
# clearing
# --------------------------------------------------------------------------- #
async def test_clear_place_removes_a_place_no_source_states(tools):
    _, event = await _person_with_event(tools, place="Cedar Flat, Brannock, Ohio, USA")
    assert tools.fake.store["event"][event["handle"]]["place"]
    places_before = len(tools.fake.store["place"])

    out = await tools("update_event", event=event["gramps_id"], clear_place=True)

    assert "place cleared" in out["message"]
    assert tools.fake.store["event"][event["handle"]]["place"] == ""
    assert len(tools.fake.store["place"]) == places_before


async def test_an_empty_place_or_date_is_refused_not_guessed_at(tools):
    _, event = await _person_with_event(tools, place="Cedar Flat")
    puts = _puts(tools)
    for args in ({"place": ""}, {"place": "  "}, {"date": ""}):
        out = await tools("update_event", event=event["gramps_id"], **args)
        assert out["error"] == "empty_value", args
    assert _puts(tools) == puts


async def test_clear_date(tools):
    _, event = await _person_with_event(tools, date="ABT 1900")
    await tools("update_event", event=event["gramps_id"], clear_date=True)
    shown = await tools("get_event", event=event["gramps_id"])
    assert shown["date"] is None


async def test_a_value_and_its_clearing_flag_together_are_refused(tools):
    _, event = await _person_with_event(tools)
    out = await tools("update_event", event=event["gramps_id"], date="1900", clear_date=True)
    assert out["error"] == "conflicting_arguments"


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #
async def test_a_service_period_is_stored_as_a_span_and_read_back_as_one(tools):
    _, event = await _person_with_event(tools, event_type="Military Service")
    await tools("update_event", event=event["gramps_id"], date="from 4 May 1864 to 16 Sep 1864")
    assert tools.fake.store["event"][event["handle"]]["date"]["modifier"] == MOD_SPAN
    shown = await tools("get_event", event=event["gramps_id"])
    assert shown["date"] == "from 1864-05-04 to 1864-09-16"


async def test_a_range_is_read_back_whole_not_as_its_first_year(tools):
    _, event = await _person_with_event(tools)
    await tools("update_event", event=event["gramps_id"], date="between 1882 and 1883")
    shown = await tools("get_event", event=event["gramps_id"])
    assert shown["date"] == "between 1882 and 1883"


async def test_an_open_span_is_stored_on_a_current_server(tools):
    _, event = await _person_with_event(tools)
    await tools("update_event", event=event["gramps_id"], date="from 1880")
    assert tools.fake.store["event"][event["handle"]]["date"]["modifier"] == MOD_FROM


async def test_an_open_span_stays_text_on_a_server_older_than_gramps_5_2(tools):
    """No such modifier there: keep the words rather than drop "from"."""
    tools.fake.metadata = {"gramps": {"version": "5.1.6"}}
    _, event = await _person_with_event(tools)
    await tools("update_event", event=event["gramps_id"], date="from 1880")
    date = tools.fake.store["event"][event["handle"]]["date"]
    assert date["modifier"] == MOD_TEXTONLY
    assert date["text"] == "from 1880"


async def test_an_unchanged_date_is_not_rewritten(tools):
    _, event = await _person_with_event(tools, date="12 Jan 1899")
    out = await tools("update_event", event=event["gramps_id"], date="12 JAN 1899")
    assert out["changed"] is False


# --------------------------------------------------------------------------- #
# sharing an existing event
# --------------------------------------------------------------------------- #
async def test_an_event_is_shared_not_copied(tools):
    host, event = await _person_with_event(tools, event_type="Census", date="1880")
    guest = await tools("add_person", given="Ada", surname="Asquith")
    events_before = len(tools.fake.store["event"])

    out = await tools("add_event_ref", person=guest["gramps_id"], event=event["gramps_id"])

    assert out["changed"] is True
    assert out["role"] == "Primary"
    assert len(tools.fake.store["event"]) == events_before
    refs = tools.fake.store["person"][guest["handle"]]["event_ref_list"]
    assert [r["ref"] for r in refs] == [event["handle"]]
    links = await tools("get_backlinks", object_type="event", ref=event["gramps_id"])
    assert links["referenced_by"]["person"]["count"] == 2
    assert tools.fake.partial_writes == []


async def test_a_role_is_matched_against_the_tree_and_refused_when_unknown(tools):
    _, event = await _person_with_event(tools, event_type="Burial")
    witness = await tools("add_person", given="Ezra", surname="Pell")
    ok = await tools(
        "add_event_ref", person=witness["gramps_id"], event=event["gramps_id"], role="witness"
    )
    assert ok["role"] == "Witness"
    other = await tools("add_person", given="Ruth", surname="Pell")
    bad = await tools(
        "add_event_ref", person=other["gramps_id"], event=event["gramps_id"], role="Witnes"
    )
    assert bad["error"] == "unknown_type"


async def test_a_person_who_already_has_the_event_is_refused(tools):
    host, event = await _person_with_event(tools)
    out = await tools("add_event_ref", person=host["gramps_id"], event=event["gramps_id"])
    assert out["error"] == "already_referenced"
    assert len(tools.fake.store["person"][host["handle"]]["event_ref_list"]) == 1


async def test_a_shared_birth_becomes_the_primary_birth_only_in_the_primary_role(tools):
    _, birth = await _person_with_event(tools, event_type="Birth", date="1850")
    twin = await tools("add_person", given="Abel", surname="Asquith")
    await tools("add_event_ref", person=twin["gramps_id"], event=birth["gramps_id"])
    assert tools.fake.store["person"][twin["handle"]]["birth_ref_index"] == 0

    midwife = await tools("add_person", given="Hester", surname="Crane")
    await tools("add_event_ref", person=midwife["gramps_id"], event=birth["gramps_id"], role="Aide")
    assert tools.fake.store["person"][midwife["handle"]].get("birth_ref_index", -1) == -1
