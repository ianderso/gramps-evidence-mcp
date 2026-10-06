"""Research tasks: Gramps Web's own task list, written as Gramps Web writes it.

A task in Gramps Web's Tasks view is a Source tagged ``ToDo``, with a
``Priority`` ("1", "5" or "9") and a ``Status`` source attribute, and its
description in a first note of type To Do (``GrampsjsViewNewTask.js``,
``GrampsjsViewTasks.js`` and ``GrampsjsTask.js`` in the Gramps Web frontend).
A task the tools make must be the same thing as one made by hand, and an
edit must leave one Status, because Gramps Web reads the first.
"""

from __future__ import annotations

import pytest


def _by_id(fake, typ: str, gramps_id: str) -> dict:
    return next(o for o in fake.store[typ].values() if o.get("gramps_id") == gramps_id)


def _tag_named(fake, name: str) -> list[str]:
    return [h for h, t in fake.store["tag"].items() if t.get("name") == name]


async def _ui_task(tools, title: str, *, status: str = "Open", priority: str = "5") -> str:
    """Make a task as Gramps Web's New Task form posts it, to POST /api/objects/.

    The form's ``_processedData()`` with a description: the source and its
    note in one request, linked by handles the form made, both carrying the
    ToDo tag. Returns the source's gramps_id.
    """
    todo = _tag_named(tools.fake, "ToDo")
    if not todo:
        await tools.service.client.create_object("tag", {"name": "ToDo"})
        todo = _tag_named(tools.fake, "ToDo")
    source_handle, note_handle = f"ui-src-{title}", f"ui-note-{title}"
    await tools.service.client.create_objects(
        [
            {
                "_class": "Source",
                "title": title,
                "attribute_list": [
                    {"_class": "SrcAttribute", "type": "Priority", "value": priority},
                    {"_class": "SrcAttribute", "type": "Status", "value": status},
                ],
                "handle": source_handle,
                "note_list": [note_handle],
                "tag_list": todo,
            },
            {
                "_class": "Note",
                "text": {"_class": "StyledText", "string": f"About {title}.", "tags": []},
                "handle": note_handle,
                "tag_list": todo,
                "type": "To Do",
            },
        ]
    )
    return tools.fake.store["source"][source_handle]["gramps_id"]


# --------------------------------------------------------------------------- #
# add_research_task
# --------------------------------------------------------------------------- #
async def test_a_new_task_is_stored_as_gramps_webs_form_stores_one(tools):
    out = await tools(
        "add_research_task",
        title="Order the Jamison probate file",
        description="Furnas Co. court, 1938.",
    )
    assert "error" not in out, out
    fake = tools.fake
    source = _by_id(fake, "source", out["gramps_id"])
    [todo] = _tag_named(fake, "ToDo")
    assert source["title"] == "Order the Jamison probate file"
    assert [(a["type"], a["value"]) for a in source["attribute_list"]] == [
        ("Priority", "5"),
        ("Status", "Open"),
    ]
    assert source["tag_list"] == [todo]
    [note_handle] = source["note_list"]
    note = fake.store["note"][note_handle]
    assert note["type"] == "To Do"
    assert note["text"]["string"] == "Furnas Co. court, 1938."
    assert note["tag_list"] == [todo]
    assert out["status"] == "Open" and out["priority"] == "medium"
    assert out["description_note"] == note["gramps_id"]
    # The ToDo tag is created as the form creates it: a name, nothing else.
    assert fake.store["tag"][todo]["color"] == "#000000000000"
    assert out["created_tags"] == ["ToDo"]


async def test_an_agent_task_and_a_hand_made_one_are_the_same_shape(tools):
    """The point of writing what the UI writes: one kind of task, not two."""
    ui = await _ui_task(tools, "By hand")
    agent = await tools("add_research_task", title="By agent", description="About By agent.")
    fake = tools.fake

    def shape(gramps_id: str) -> dict:
        source = dict(_by_id(fake, "source", gramps_id))
        note = dict(fake.store["note"][source["note_list"][0]])
        for obj in (source, note):
            for volatile in ("handle", "gramps_id", "change", "title", "note_list"):
                obj.pop(volatile, None)
        note["text"] = {**note["text"], "string": None}
        return {"source": source, "note": note}

    assert shape(agent["gramps_id"]) == shape(ui)
    assert len(_tag_named(fake, "ToDo")) == 1, "the existing ToDo tag is reused"


@pytest.mark.parametrize(("priority", "stored"), [("high", "1"), ("medium", "5"), ("low", "9")])
async def test_priority_is_stored_as_gramps_web_stores_it(tools, priority, stored):
    out = await tools("add_research_task", title="T", priority=priority)
    source = _by_id(tools.fake, "source", out["gramps_id"])
    assert source["attribute_list"][0] == {"private": False, "type": "Priority", "value": stored}


async def test_a_task_without_a_description_has_no_note(tools):
    out = await tools("add_research_task", title="Look for the 1880 census entry")
    source = _by_id(tools.fake, "source", out["gramps_id"])
    assert source["note_list"] == []
    assert tools.fake.store["note"] == {}
    assert out["description_note"] is None


async def test_more_tags_and_attributes_follow_todo_and_status(tools):
    tools.fake.custom_types["source_attribute_types"].add("Request-Custodian")
    out = await tools(
        "add_research_task",
        title="RR-0001 Order: probate file",
        tags=["Probate", "ToDo"],
        attributes={"request-custodian": "ne-furnas-county-court"},
    )
    fake = tools.fake
    source = _by_id(fake, "source", out["gramps_id"])
    [todo], [probate] = _tag_named(fake, "ToDo"), _tag_named(fake, "Probate")
    assert source["tag_list"] == [todo, probate]
    assert [(a["type"], a["value"]) for a in source["attribute_list"]][2:] == [
        ("Request-Custodian", "ne-furnas-county-court")
    ], "the name is spelt as the tree already spells it"
    assert out["tags"] == ["ToDo", "Probate"]


async def test_an_attribute_name_the_tree_lacks_is_refused_before_anything_is_written(tools):
    out = await tools("add_research_task", title="T", attributes={"Bears-On": "I0035"})
    assert out["error"] == "unknown_type"
    assert not any(tools.fake.store.values()), "nothing may be written, not even the tag"

    made = await tools(
        "add_research_task", title="T", attributes={"Bears-On": "I0035"}, allow_new_type=True
    )
    source = _by_id(tools.fake, "source", made["gramps_id"])
    assert ("Bears-On", "I0035") in [(a["type"], a["value"]) for a in source["attribute_list"]]


@pytest.mark.parametrize("name", ["Status", "priority"])
async def test_status_and_priority_cannot_be_given_as_attributes(tools, name):
    out = await tools("add_research_task", title="T", attributes={name: "Done"})
    assert out["error"] == "conflicting_arguments"
    assert not any(tools.fake.store.values())


async def test_a_private_task_keeps_its_description_private_too(tools):
    out = await tools("add_research_task", title="T", description="living cousin", private=True)
    source = _by_id(tools.fake, "source", out["gramps_id"])
    assert source["private"] is True
    assert tools.fake.store["note"][source["note_list"][0]]["private"] is True


async def test_a_task_needs_a_title(tools):
    out = await tools("add_research_task", title="   ")
    assert out["error"] == "title_required"
    assert not any(tools.fake.store.values())


async def test_a_failed_source_write_takes_its_new_note_with_it(tools, monkeypatch):
    """The note is made first, to be linked; it must not outlive a failed task."""
    client = tools.service.client
    create = client.create_object

    async def failing(object_type, payload, *args, **kwargs):
        if object_type == "source":
            raise RuntimeError("source write failed")
        return await create(object_type, payload, *args, **kwargs)

    monkeypatch.setattr(client, "create_object", failing)
    out = await tools("add_research_task", title="T", description="details")
    assert out["error"] == "unexpected"
    assert tools.fake.store["note"] == {}


# --------------------------------------------------------------------------- #
# list_research_tasks
# --------------------------------------------------------------------------- #
async def test_listing_reads_hand_made_and_agent_made_tasks_alike(tools):
    await _ui_task(tools, "By hand", status="In Progress", priority="1")
    await tools("add_research_task", title="By agent", description="details", priority="low")
    await tools("add_source", title="1880 census, Furnas Co.")  # evidence, not a task

    out = await tools("list_research_tasks")
    assert out["task_count"] == 2
    by_title = {t["title"]: t for t in out["tasks"]}
    assert by_title["By hand"]["status"] == "In Progress"
    assert by_title["By hand"]["priority"] == "high"
    assert by_title["By hand"]["description"] == "About By hand."
    assert by_title["By agent"]["status"] == "Open"
    assert by_title["By agent"]["priority"] == "low"
    assert by_title["By agent"]["description"] == "details"
    assert by_title["By agent"]["tags"] == []
    assert out["by_status"] == {"Open": 1, "In Progress": 1}


async def test_tasks_come_in_the_order_the_tasks_view_shows_them(tools):
    """Open, In Progress, Blocked, any other status, Done; newest id first within."""
    for title, status in [
        ("done", "Done"),
        ("open 1", "Open"),
        ("odd", "Waiting"),
        ("blocked", "Blocked"),
        ("open 2", "Open"),
        ("doing", "In Progress"),
    ]:
        await _ui_task(tools, title, status=status)
    out = await tools("list_research_tasks")
    assert [t["title"] for t in out["tasks"]] == [
        "open 2",
        "open 1",
        "doing",
        "blocked",
        "odd",
        "done",
    ]


async def test_listing_filters_by_status_tag_and_attribute(tools):
    tools.fake.custom_types["source_attribute_types"].add("Request-Custodian")
    first = await tools(
        "add_research_task",
        title="RR-0001",
        tags=["Probate"],
        attributes={"Request-Custodian": "ne-furnas"},
    )
    await tools("add_research_task", title="RR-0002", attributes={"Request-Custodian": "wi-dhs"})
    await tools("update_research_task", task=first["gramps_id"], status="In Progress")

    def titles(out: dict) -> list[str]:
        return sorted(t["title"] for t in out["tasks"])

    assert titles(await tools("list_research_tasks", status=["Open"])) == ["RR-0002"]
    assert titles(await tools("list_research_tasks", status=["Open", "In Progress"])) == [
        "RR-0001",
        "RR-0002",
    ]
    assert titles(await tools("list_research_tasks", tag="Probate")) == ["RR-0001"]
    assert titles(
        await tools("list_research_tasks", attributes={"request-custodian": "WI-DHS"})
    ) == ["RR-0002"]
    assert titles(await tools("list_research_tasks", attributes={"Request-Custodian": ""})) == [
        "RR-0001",
        "RR-0002",
    ]
    attrs = (await tools("list_research_tasks", tag="Probate"))["tasks"][0]["attributes"]
    assert attrs == {"Request-Custodian": "ne-furnas"}


async def test_a_tree_without_the_todo_tag_has_no_tasks(tools):
    await tools("add_source", title="A source")
    out = await tools("list_research_tasks")
    assert out["task_count"] == 0
    assert "add_research_task" in out["message"]


async def test_an_unknown_tag_filter_is_reported_not_answered_with_nothing(tools):
    await tools("add_research_task", title="T")
    out = await tools("list_research_tasks", tag="Probat")
    assert out["error"] == "not_found"


async def test_a_private_task_is_a_stub_unless_asked_for(tools):
    await tools("add_research_task", title="Order my cousin's birth record", private=True)
    await tools("add_research_task", title="Public task")
    hidden = await tools("list_research_tasks")
    assert hidden["redacted_count"] == 1
    assert "Order my cousin's birth record" not in str(hidden)

    shown = await tools("list_research_tasks", include_private=True)
    assert shown["redacted_count"] == 0
    assert {t["title"] for t in shown["tasks"]} == {
        "Order my cousin's birth record",
        "Public task",
    }


async def test_a_long_description_is_cut_and_names_its_note(tools):
    await tools("add_research_task", title="T", description="x" * 1000)
    [task] = (await tools("list_research_tasks"))["tasks"]
    assert task["description"].endswith(" ...")
    assert len(task["description"]) < 500
    assert task["description_note"].startswith("N")


# --------------------------------------------------------------------------- #
# update_research_task
# --------------------------------------------------------------------------- #
async def test_a_status_change_replaces_the_status_rather_than_adding_one(tools):
    out = await tools("add_research_task", title="T")
    done = await tools("update_research_task", task=out["gramps_id"], status="Done")
    assert done["status"] == "Done"
    source = _by_id(tools.fake, "source", out["gramps_id"])
    assert [(a["type"], a["value"]) for a in source["attribute_list"]] == [
        ("Priority", "5"),
        ("Status", "Done"),
    ], "one Status, in its place"
    listed = await tools("list_research_tasks")
    assert listed["tasks"][0]["status"] == "Done"


async def test_add_attribute_twice_is_repaired_by_the_next_update(tools):
    """The workaround before these tools: add_attribute appends, and Gramps Web
    reads the first Status, so the second never showed."""
    out = await tools("add_research_task", title="T")
    tools.fake.custom_types["source_attribute_types"].update({"Status", "Priority"})
    await tools(
        "add_attribute", object_type="source", target=out["gramps_id"], name="Status", value="Done"
    )
    listed = await tools("list_research_tasks")
    assert listed["tasks"][0]["status"] == "Open", "the appended Status is invisible"

    fixed = await tools("update_research_task", task=out["gramps_id"], status="Blocked")
    source = _by_id(tools.fake, "source", out["gramps_id"])
    statuses = [a["value"] for a in source["attribute_list"] if a["type"] == "Status"]
    assert statuses == ["Blocked"]
    assert any("1 more Status" in c for c in fixed["changes"])


async def test_priority_and_attributes_are_set_in_place_and_removed_with_null(tools):
    tools.fake.custom_types["source_attribute_types"].update({"Request-Sent", "Request-Ref"})
    out = await tools("add_research_task", title="T", attributes={"Request-Ref": "check 101"})
    first = await tools(
        "update_research_task",
        task=out["gramps_id"],
        priority="high",
        attributes={"Request-Sent": "2026-10-05", "Request-Ref": "check 102"},
    )
    assert first["priority"] == "high"
    source = _by_id(tools.fake, "source", out["gramps_id"])
    assert [(a["type"], a["value"]) for a in source["attribute_list"]] == [
        ("Priority", "1"),
        ("Status", "Open"),
        ("Request-Ref", "check 102"),
        ("Request-Sent", "2026-10-05"),
    ]
    await tools("update_research_task", task=out["gramps_id"], attributes={"Request-Ref": None})
    source = _by_id(tools.fake, "source", out["gramps_id"])
    assert "Request-Ref" not in [a["type"] for a in source["attribute_list"]]


async def test_replacing_keeps_what_the_attribute_carries(tools):
    out = await tools("add_research_task", title="T")
    source = _by_id(tools.fake, "source", out["gramps_id"])
    source["attribute_list"][1]["private"] = True
    await tools("update_research_task", task=out["gramps_id"], status="Done")
    source = _by_id(tools.fake, "source", out["gramps_id"])
    assert source["attribute_list"][1] == {"private": True, "type": "Status", "value": "Done"}


async def test_an_update_that_changes_nothing_writes_nothing(tools):
    out = await tools("add_research_task", title="T")
    puts = len([r for r in tools.fake.requests if r[0] == "PUT"])
    same = await tools("update_research_task", task=out["gramps_id"], status="Open")
    assert same["changed"] is False
    assert len([r for r in tools.fake.requests if r[0] == "PUT"]) == puts


async def test_note_append_adds_a_paragraph_to_the_description(tools):
    out = await tools("add_research_task", title="T", description="Write to the court.")
    done = await tools(
        "update_research_task",
        task=out["gramps_id"],
        status="In Progress",
        note_append="2026-10-05: letter sent.",
    )
    note = _by_id(tools.fake, "note", done["description_note"])
    assert note["text"]["string"] == "Write to the court.\n\n2026-10-05: letter sent."
    assert note["type"] == "To Do"
    assert len(tools.fake.store["note"]) == 1


async def test_note_append_on_a_task_with_no_note_creates_the_description(tools):
    out = await tools("add_research_task", title="T", tags=["Probate"])
    done = await tools("update_research_task", task=out["gramps_id"], note_append="Asked.")
    source = _by_id(tools.fake, "source", out["gramps_id"])
    [note_handle] = source["note_list"]
    note = tools.fake.store["note"][note_handle]
    assert note["text"]["string"] == "Asked."
    assert note["type"] == "To Do"
    assert note["tag_list"] == source["tag_list"]
    assert done["description_note"] == note["gramps_id"]


async def test_a_source_that_is_not_a_task_is_refused(tools):
    """A Status attribute on an evidence source would mean nothing anywhere."""
    await tools("add_research_task", title="T")
    evidence = await tools("add_source", title="1880 census")
    out = await tools("update_research_task", task=evidence["gramps_id"], status="Done")
    assert out["error"] == "not_a_task"
    assert _by_id(tools.fake, "source", evidence["gramps_id"])["attribute_list"] == []


async def test_an_update_must_say_what_to_change(tools):
    out = await tools("add_research_task", title="T")
    assert (await tools("update_research_task", task=out["gramps_id"]))["error"] == "nothing_to_do"


async def test_status_cannot_be_set_through_attributes(tools):
    out = await tools("add_research_task", title="T")
    refused = await tools(
        "update_research_task", task=out["gramps_id"], attributes={"status": "Done"}
    )
    assert refused["error"] == "conflicting_arguments"


async def test_updates_write_whole_objects(tools):
    out = await tools("add_research_task", title="T", description="d")
    await tools(
        "update_research_task",
        task=out["gramps_id"],
        status="Blocked",
        priority="low",
        note_append="waiting on the fee",
    )
    assert tools.fake.partial_writes == []


# --------------------------------------------------------------------------- #
# Background jobs: renamed so they are not mistaken for research tasks
# --------------------------------------------------------------------------- #
async def test_list_jobs_reports_background_jobs(tools):
    tools.fake.task_list = [{"task_id": "t1", "name": "verify", "state": "SUCCESS"}]
    out = await tools("list_jobs")
    assert out == {"job_count": 1, "jobs": tools.fake.task_list}


async def test_the_old_job_names_are_gone():
    """Removed in 2.0: a tool named for tasks that reports jobs misleads."""
    from gramps_evidence_mcp.server import mcp

    names = {t.name for t in await mcp.list_tools()}
    assert {"list_jobs", "get_job"} <= names
    assert not {"list_tasks", "get_task"} & names


# --------------------------------------------------------------------------- #
# Privacy: a private task's description note is private too
# --------------------------------------------------------------------------- #
def _task_and_note(fake, gramps_id: str) -> tuple[dict, dict]:
    source = _by_id(fake, "source", gramps_id)
    return source, fake.store["note"][source["note_list"][0]]


async def test_making_a_task_private_makes_its_description_private(tools):
    out = await tools("add_research_task", title="Order my cousin's record", description="d")
    done = await tools("update_research_task", task=out["gramps_id"], private=True)
    source, note = _task_and_note(tools.fake, out["gramps_id"])
    assert source["private"] is True
    assert note["private"] is True
    assert done["private"] is True and done["description_note_private"] is True


async def test_making_a_task_public_leaves_its_note_private_and_says_so(tools):
    out = await tools("add_research_task", title="T", description="d", private=True)
    done = await tools("update_research_task", task=out["gramps_id"], private=False)
    source, note = _task_and_note(tools.fake, out["gramps_id"])
    assert source["private"] is False
    assert note["private"] is True, "a note is never made public by default"
    assert "stays private" in done["message"]
    assert "private_note=false" in done["notices"][0]

    shown = await tools(
        "update_research_task", task=out["gramps_id"], private=False, private_note=False
    )
    assert _task_and_note(tools.fake, out["gramps_id"])[1]["private"] is False
    assert "notices" not in shown


async def test_a_note_added_to_a_private_task_is_private(tools):
    out = await tools("add_research_task", title="T", private=True)
    done = await tools("update_research_task", task=out["gramps_id"], note_append="Asked.")
    _, note = _task_and_note(tools.fake, out["gramps_id"])
    assert note["private"] is True
    assert done["description_note_private"] is True


async def test_a_note_added_while_the_task_is_made_private_is_private(tools):
    out = await tools("add_research_task", title="T")
    await tools("update_research_task", task=out["gramps_id"], private=True, note_append="x")
    source, note = _task_and_note(tools.fake, out["gramps_id"])
    assert (source["private"], note["private"]) == (True, True)


async def test_editing_a_private_tasks_public_note_makes_it_private(tools):
    """A task made private in Gramps Web leaves its note public: the form marks the source only."""
    out = await tools("add_research_task", title="T", description="d")
    source, note = _task_and_note(tools.fake, out["gramps_id"])
    source["private"] = True
    assert note["private"] is False
    await tools("update_research_task", task=out["gramps_id"], status="Done")
    assert _task_and_note(tools.fake, out["gramps_id"])[1]["private"] is True


async def test_private_note_sets_the_note_alone(tools):
    out = await tools("add_research_task", title="T", description="d")
    done = await tools("update_research_task", task=out["gramps_id"], private_note=True)
    source, note = _task_and_note(tools.fake, out["gramps_id"])
    assert (source["private"], note["private"]) == (False, True)
    assert done["changed"] is True


async def test_a_public_tasks_note_is_left_alone_when_nothing_asks(tools):
    out = await tools("add_research_task", title="T", description="d")
    puts = len([r for r in tools.fake.requests if r[0] == "PUT"])
    await tools("update_research_task", task=out["gramps_id"], private=False)
    assert len([r for r in tools.fake.requests if r[0] == "PUT"]) == puts
    assert _task_and_note(tools.fake, out["gramps_id"])[1]["private"] is False


async def test_private_note_on_a_task_without_a_note_says_there_is_none(tools):
    out = await tools("add_research_task", title="T")
    done = await tools("update_research_task", task=out["gramps_id"], private_note=True)
    assert done["changed"] is False
    assert "no description note" in done["message"]
