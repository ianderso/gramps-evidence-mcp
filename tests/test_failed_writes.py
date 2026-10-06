"""A write answered with a 5xx says what it wrote (TOOL-REQUESTS #28).

A research session's add_note got HTTP 500 and nothing was written; the tool
said only "HTTP 500". The transaction log showed why the length was not the
cause: the same session created notes of 5,994 and 4,463 characters in one
call minutes later, and set the 8,054-character text by an edit. What failed
was the commit -- three requests reached the database and none was recorded
-- which on a SQLite tree is the database staying locked past its five-second
wait (docs/PITFALLS.md section 6). So a 5xx is checked against the tree: a
create by the handle it was given, an edit by reading the record again.
"""

from __future__ import annotations


async def _person(tools):
    return await tools("add_person", given="Hester", surname="Crane")


async def test_a_long_note_is_written_in_one_call(tools):
    person = await _person(tools)
    text = "Estate file, item 14: " + "the appraisers' inventory, line by line. " * 2500
    out = await tools("add_note", text=text, target=person["gramps_id"], target_type="person")
    assert out["verified"] is True
    assert tools.fake.store["note"][out["handle"]]["text"]["string"] == text
    assert len(text) > 100_000


async def test_a_create_that_wrote_nothing_says_so(tools):
    tools.fake.post_error = 500
    out = await tools("add_note", text="Transcription.")
    assert out["error"] == "api" and out["status"] == 500
    assert out["written"] is False
    assert out["message"].startswith("Nothing was written")
    assert "Retrying the same call is safe" in out["message"]
    assert "not the size of what was sent" in out["message"]
    assert tools.fake.store["note"] == {}


async def test_a_create_that_landed_before_the_error_is_taken_as_done(tools):
    person = await _person(tools)
    tools.fake.post_error_after_commit = 500
    out = await tools(
        "add_note", text="Transcription.", target=person["gramps_id"], target_type="person"
    )
    assert out["verified"] is True, out
    assert "answered HTTP 500 after creating it" in out["message"]
    (note,) = tools.fake.store["note"].values()
    assert tools.fake.store["person"][person["handle"]]["note_list"] == [note["handle"]]


async def test_an_attach_that_wrote_nothing_takes_the_note_back(tools):
    person = await _person(tools)
    tools.fake.put_error = 500
    out = await tools(
        "add_note", text="Transcription.", target=person["gramps_id"], target_type="person"
    )
    assert out["written"] is False, out
    assert "the new note was removed again" in out["message"]
    assert tools.fake.store["note"] == {}
    assert tools.fake.store["person"][person["handle"]]["note_list"] == []


async def test_an_edit_that_wrote_nothing_says_so(tools):
    person = await _person(tools)
    tools.fake.put_error = 503
    out = await tools("update_person", person=person["gramps_id"], gender="female")
    assert (out["error"], out["status"], out["written"]) == ("api", 503, False)
    assert f"person {person['gramps_id']} reads exactly as it did before" in out["message"]


async def test_an_edit_that_may_have_landed_says_to_re_read(tools):
    person = await _person(tools)
    tools.fake.put_error_after_commit = 500
    out = await tools("update_person", person=person["gramps_id"], gender="female")
    assert out["written"] is None
    assert out["message"].startswith("The write may have landed")
    assert "Re-read it before retrying" in out["message"]


async def test_a_client_error_is_reported_as_before(tools):
    """Only a 5xx is in doubt: a 4xx is the server refusing, and wrote nothing."""
    person = await _person(tools)
    tools.fake.write_forbidden = True
    out = await tools("update_person", person=person["gramps_id"], gender="female")
    assert out["status"] == 403 and "written" not in out
