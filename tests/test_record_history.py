"""get_record_history: one record's changes, from the undo log.

gramps-webapi 3.22 serves one record's changes; on 3.21 the tool reads the
whole transaction log instead (TOOL-REQUESTS #29), and the two must give the
same answer. The fake records a change for every object a write touched, as
the server's undo log does; ``tests/live/test_contract_live.py`` holds that to
a server of each version.
"""

from __future__ import annotations

import pytest

from gramps_evidence_mcp import service as service_module

CITED = {"source_title": "Register", "page": "p. 1"}


@pytest.fixture(params=["3.21.1", "3.22.3"])
def version(request, fake):
    fake.metadata["gramps_webapi"]["version"] = request.param
    return request.param


@pytest.fixture
def on_3_22(fake):
    fake.metadata["gramps_webapi"]["version"] = "3.22.3"
    return fake


def _read_from(version: str) -> str:
    return "transaction log" if version.startswith("3.21") else "record history"


async def test_changes_come_newest_first_with_their_transactions(tools, version):
    person = await tools("add_person", given="Elias", surname="Wren")
    await tools("update_person", person=person["gramps_id"], gender="male")
    out = await tools("get_record_history", object_type="person", ref=person["gramps_id"])
    assert [c["change"] for c in out["changes"]] == ["edited", "added"]
    assert out["total_changes"] == 2 and out["deleted"] is False
    assert all(c["user"] == "mcp" and c["timestamp"] for c in out["changes"])
    first, second = out["changes"][1]["transaction_id"], out["changes"][0]["transaction_id"]
    assert second > first
    assert out["read_from"] == _read_from(version)


async def test_a_write_to_a_family_is_in_its_members_history(tools, version):
    father = await tools("add_person", given="Elias", surname="Wren")
    await tools("add_family", father=father["gramps_id"])
    out = await tools("get_record_history", object_type="person", ref=father["gramps_id"])
    assert [c["change"] for c in out["changes"]] == ["edited", "added"]


async def test_a_deleted_record_is_found_by_its_handle(tools, version):
    person = await tools("add_person", given="Mercy", surname="Wren")
    await tools("delete_object", object_type="person", target=person["gramps_id"])
    out = await tools("get_record_history", object_type="person", ref=person["handle"])
    assert out["deleted"] is True and out["gramps_id"] is None
    assert [c["change"] for c in out["changes"]] == ["deleted", "added"]
    assert "since been deleted" in out["message"]


async def test_limit_takes_the_latest(tools, version):
    person = await tools("add_person", given="Hope", surname="Wren")
    for gender in ("male", "female", "unknown"):
        await tools("update_person", person=person["gramps_id"], gender=gender)
    out = await tools("get_record_history", object_type="person", ref=person["gramps_id"], limit=2)
    assert out["total_changes"] == 4 and out["returned"] == 2
    assert [c["change"] for c in out["changes"]] == ["edited", "edited"]


async def test_a_record_never_seen_is_not_found(tools, version):
    out = await tools("get_record_history", object_type="person", ref="I9999")
    assert out["error"] == "not_found"
    assert "found by its handle" in out["message"]


async def test_on_3_21_the_log_is_read_and_the_result_says_so(tools, fake):
    person = await tools("add_person", given="Elias", surname="Wren")
    out = await tools("get_record_history", object_type="person", ref=person["gramps_id"])
    assert out["read_from"] == "transaction log" and out["log_complete"] is True
    assert "3.21 has no per-record history" in out["message"]
    assert "back to the record's creation" in out["message"]
    fake.metadata["gramps_webapi"]["version"] = "3.22.3"
    out = await tools("get_record_history", object_type="person", ref=person["gramps_id"])
    assert out["read_from"] == "record history" and "log_complete" not in out


async def test_the_log_is_read_by_cursor_and_only_back_to_the_creation(tools, monkeypatch):
    monkeypatch.setattr(service_module, "_HISTORY_PAGE", 2)
    early = await tools("add_person", given="Early", surname="Wren")
    person = await tools("add_person", given="Elias", surname="Wren")
    for gender in ("male", "female", "unknown", "male"):
        await tools("update_person", person=person["gramps_id"], gender=gender)
    asked = []
    read = tools.service.client.transactions

    async def counting(**kwargs):
        asked.append(kwargs)
        return await read(**kwargs)

    monkeypatch.setattr(tools.service.client, "transactions", counting)
    out = await tools("get_record_history", object_type="person", ref=person["gramps_id"])
    assert out["total_changes"] == 5
    # Five transactions back to the creation, two a page: three pages, by cursor.
    assert [a.get("before_id") for a in asked] == [None, 5, 3]
    assert all(a["page"] == 1 and a["sort"] == "-id" for a in asked)
    assert early["handle"] not in str(out)


async def test_a_log_too_long_to_read_says_where_it_stopped(tools, monkeypatch):
    monkeypatch.setattr(service_module, "_HISTORY_PAGE", 2)
    monkeypatch.setattr(service_module, "_HISTORY_SCAN_LIMIT", 4)
    person = await tools("add_person", given="Elias", surname="Wren")
    for gender in ("male", "female", "unknown", "male", "female"):
        await tools("update_person", person=person["gramps_id"], gender=gender)
    out = await tools("get_record_history", object_type="person", ref=person["gramps_id"])
    assert out["log_complete"] is False and out["total_changes"] == 4
    assert "stopping after 4 transactions" in out["message"]
    assert "older changes were not read" in out["message"]


async def test_a_restore_by_undo_does_not_end_the_search(tools, fake):
    person = await tools("add_person", given="Elias", surname="Wren")
    await tools("update_person", person=person["gramps_id"], gender="male")
    # An undone delete re-adds the record, in a transaction the API calls "Undo".
    fake.transactions.append(
        {
            **fake.transactions[-1],
            "id": fake.transactions[-1]["id"] + 1,
            "description": "Undo",
            "changes": [{**fake.transactions[0]["changes"][0], "id": 1}],
        }
    )
    out = await tools("get_record_history", object_type="person", ref=person["gramps_id"])
    assert [c["change"] for c in out["changes"]] == ["added", "edited", "added"]


# --------------------------------------------------------------------------- #
# field: the writes that changed one field, with its value before and after
# --------------------------------------------------------------------------- #
async def _doubled_link(tools) -> tuple[dict, dict]:
    """A child whose parent_family_list a write doubled, as TOOL-REQUESTS #2 found."""
    child = await tools("add_person", given="Mercy", surname="Wren")
    family = await tools("add_family", children=[child["gramps_id"]])
    client = tools.service.client
    raw = await client.get_object("person", child["handle"])
    raw["parent_family_list"] = raw["parent_family_list"] * 2
    await client.update_object("person", child["handle"], raw)
    await tools("update_person", person=child["gramps_id"], gender="female")
    return child, family


async def test_a_field_reports_the_writes_that_changed_it(tools, version):
    child, family = await _doubled_link(tools)
    out = await tools(
        "get_record_history",
        object_type="person",
        ref=child["gramps_id"],
        field="parent_family_list",
    )
    assert out["total_changes"] == 4 and out["field_changes"] == 3, out
    (repaired, doubled, linked) = out["changes"]
    fam = family["handle"]
    assert (linked["before"], linked["after"]) == ([], [fam])
    assert (doubled["before"], doubled["after"]) == ([fam], [fam, fam])
    assert (repaired["before"], repaired["after"]) == ([fam, fam], [fam])
    assert doubled["transaction_id"] > linked["transaction_id"]
    assert out["read_from"] == _read_from(version)


async def test_a_nested_field_and_a_creation_that_set_it(tools, version):
    person = await tools("add_person", given="Elias", surname="Wren")
    await tools(
        "update_person",
        person=person["gramps_id"],
        name={"given": "Elijah", "surname": "Wren"},
        keep_old_as_alternate=False,
        reason="Misread.",
    )
    out = await tools(
        "get_record_history",
        object_type="person",
        ref=person["gramps_id"],
        field="primary_name.first_name",
    )
    assert [(c["change"], c["before"], c["after"]) for c in out["changes"]] == [
        ("edited", "Elias", "Elijah"),
        ("added", None, "Elias"),
    ]


async def test_a_field_the_record_never_had_says_how_to_check(tools, version):
    person = await tools("add_person", given="Elias", surname="Wren")
    out = await tools(
        "get_record_history", object_type="person", ref=person["gramps_id"], field="parents"
    )
    assert out["field_changes"] == 0 and out["changes"] == []
    assert "get_object shows the record's fields" in out["message"]


async def test_a_malformed_field_is_refused(tools, on_3_22):
    out = await tools("get_record_history", object_type="person", ref="I0001", field="family list")
    assert out["error"] == "unsupported_field"


async def test_a_change_no_transaction_covers_is_said_to_have_changed_nothing(tools, on_3_22):
    """3.22 serves a change rolled back at its commit, with no transaction (PITFALLS 30)."""
    person = await tools("add_person", given="Elias", surname="Wren")
    await tools("update_person", person=person["gramps_id"], gender="male")
    rolled_back = {**on_3_22.history[-1], "id": len(on_3_22.history) + 1, "transaction_id": None}
    on_3_22.history.append(rolled_back)
    out = await tools("get_record_history", object_type="person", ref=person["gramps_id"])
    assert out["changes"][0]["transaction_id"] is None
    assert "1 shown with no transaction was a write rolled back" in out["message"]
    fields = await tools(
        "get_record_history", object_type="person", ref=person["gramps_id"], field="gender"
    )
    assert (fields["total_changes"], fields["field_changes"]) == (2, 2), fields
    assert all(c["transaction_id"] is not None for c in fields["changes"])
