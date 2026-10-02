"""get_record_history: one record's changes, from gramps-webapi 3.22's undo log.

The fake records a change for every object a write touched, as the server's
undo log does; ``tests/live/test_contract_live.py`` holds that to a server.
"""

from __future__ import annotations

import pytest

CITED = {"source_title": "Register", "page": "p. 1"}


@pytest.fixture
def on_3_22(fake):
    fake.metadata["gramps_webapi"]["version"] = "3.22.3"
    return fake


async def test_changes_come_newest_first_with_their_transactions(tools, on_3_22):
    person = await tools("add_person", given="Elias", surname="Wren")
    await tools("update_person", person=person["gramps_id"], gender="male")
    out = await tools("get_record_history", object_type="person", ref=person["gramps_id"])
    assert [c["change"] for c in out["changes"]] == ["edited", "added"]
    assert out["total_changes"] == 2 and out["deleted"] is False
    assert all(c["user"] == "mcp" and c["timestamp"] for c in out["changes"])
    first, second = out["changes"][1]["transaction_id"], out["changes"][0]["transaction_id"]
    assert second > first


async def test_a_write_to_a_family_is_in_its_members_history(tools, on_3_22):
    father = await tools("add_person", given="Elias", surname="Wren")
    await tools("add_family", father=father["gramps_id"])
    out = await tools("get_record_history", object_type="person", ref=father["gramps_id"])
    assert [c["change"] for c in out["changes"]] == ["edited", "added"]


async def test_a_deleted_record_is_found_by_its_handle(tools, on_3_22):
    person = await tools("add_person", given="Mercy", surname="Wren")
    await tools("delete_object", object_type="person", target=person["gramps_id"])
    out = await tools("get_record_history", object_type="person", ref=person["handle"])
    assert out["deleted"] is True and out["gramps_id"] is None
    assert [c["change"] for c in out["changes"]] == ["deleted", "added"]
    assert "since been deleted" in out["message"]


async def test_limit_takes_the_latest(tools, on_3_22):
    person = await tools("add_person", given="Hope", surname="Wren")
    for gender in ("male", "female", "unknown"):
        await tools("update_person", person=person["gramps_id"], gender=gender)
    out = await tools("get_record_history", object_type="person", ref=person["gramps_id"], limit=2)
    assert out["total_changes"] == 4 and out["returned"] == 2
    assert [c["change"] for c in out["changes"]] == ["edited", "edited"]


async def test_a_record_never_seen_is_not_found(tools, on_3_22):
    out = await tools("get_record_history", object_type="person", ref="I9999")
    assert out["error"] == "not_found"
    assert "found by its handle" in out["message"]


async def test_before_3_22_the_tool_says_which_version_it_needs(tools):
    person = await tools("add_person", given="Elias", surname="Wren")
    out = await tools("get_record_history", object_type="person", ref=person["gramps_id"])
    assert out["error"] == "unsupported_server"
    assert "3.22 or later" in out["message"] and "3.21.1" in out["message"]
