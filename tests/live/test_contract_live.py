"""The fake against the real server: the same calls must get the same answers.

Every unit test runs against the fake in ``tests/conftest.py``, so a test is
only as good as the fake's likeness to gramps-webapi. Here one scenario of
tool calls runs twice -- against the fake, then against the throwaway server --
and every answer, and every object the scenario leaves stored, must match
once handles and ids are given names that mean the same in both runs.

A difference means the fake is wrong (fix the fake, then whichever unit tests
leaned on its mistake) or the server changed (fix the tools, then the fake).
It is never fixed by widening ``VOLATILE``, which holds only what differs
between any two runs against the same server.
"""

from __future__ import annotations

import json
from typing import Any

from .capture_defaults import OUT as DEFAULTS_FILE
from .capture_defaults import capture
from .harness import WIPE_ORDER, Normaliser, fake_tools, live_tools

#: Keys whose values differ between two runs against the same server.
VOLATILE = {"change", "timestamp", "path", "transaction_id"}


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


async def test_the_recorded_defaults_are_the_servers(live_server):
    """The fake completes objects from this file; it must say what the server does."""
    recorded = json.loads(DEFAULTS_FILE.read_text())
    current = await capture()
    for key in ("defaults", "nested", "nulls", "types"):
        assert current[key] == recorded[key], (
            f"gramps-webapi now stores different {key}. Regenerate the file with "
            "`uv run python -m tests.live.capture_defaults`, then run the unit tests."
        )


class Recorder:
    """Runs a scenario's tool calls and keeps every answer, named for comparison."""

    def __init__(self, call) -> None:
        self.call = call
        self.client = call.client
        self.norm = Normaliser()
        self.answers: list[tuple[str, Any]] = []

    async def __call__(self, tool: str, **args: Any) -> Any:
        out = await self.call(tool, **args)
        await self.norm.learn(self.client)
        self.answers.append((f"{len(self.answers):02d} {tool}", out))
        return out

    async def finish(self) -> list[tuple[str, Any]]:
        """Add everything the scenario left stored, as get_object serves it."""
        for object_type in WIPE_ORDER:
            rows = await self.client.list_objects(object_type, keys="handle,gramps_id")
            # A tag has no gramps_id, and the two list tags in different
            # orders: its name in this comparison orders it the same in both.
            rows.sort(key=lambda r: (r.get("gramps_id") or "", self.norm(r["handle"])))
            for row in rows:
                stored = await self.call("get_object", object_type=object_type, ref=row["handle"])
                self.answers.append((f"stored {self.norm(row['handle'])}", stored))
        return [(label, self.norm(_stable(out))) for label, out in self.answers]


async def _building_and_reading(step: Recorder) -> None:
    """Tool calls creating every kind of object, then the reads a session makes."""
    ohio = await step("add_place", name="Ohio", place_type="State")
    county = await step(
        "add_place", name="Brannock County", place_type="County", parent=ohio["gramps_id"]
    )
    town = await step("add_place", name="Cedar Flat", place_type="City", parent=county["gramps_id"])
    father = await step(
        "add_person",
        given="Elias",
        surname="Wren",
        gender="male",
        birth={
            "type": "Birth",
            "date": "about 1801",
            "place": town["gramps_id"],
            "citation": {"source_title": "Register of Births", "page": "p. 12"},
        },
        death={
            "type": "Death",
            "date": "12 MAR 1866",
            "citation": {"source_title": "Register of Deaths", "page": "p. 3"},
        },
    )
    mother = await step("add_person", given="Hannah", surname="Ashbee", gender="female")
    child = await step(
        "add_person",
        given="Mercy",
        surname="Wren",
        gender="female",
        birth={
            "type": "Birth",
            "date": "4 May 1830",
            "citation": {"source_title": "Register of Births", "page": "p. 40"},
        },
    )
    family = await step(
        "add_family",
        father=father["gramps_id"],
        mother=mother["gramps_id"],
        children=[child["gramps_id"]],
        marriage={
            "type": "Marriage",
            "date": "1828",
            "citation": {"source_title": "Register of Marriages", "page": "p. 7"},
        },
    )
    residence = await step(
        "add_event_to_person",
        person=child["gramps_id"],
        event={
            "type": "Residence",
            "date": "from 1850 to 1860",
            "place": town["gramps_id"],
            "citation": {
                "source_title": "Census 1850",
                "page": "sheet 4",
                "note": "listed with her parents",
            },
        },
    )
    await step(
        "add_event_ref", person=mother["gramps_id"], event=residence["event_handle"], role="witness"
    )
    await step("update_event", event=residence["event_handle"], description="with her parents")
    await step(
        "add_alternate_name",
        person=child["gramps_id"],
        given="Mercy",
        surname="Holt",
        name_type="Married Name",
    )
    await step(
        "update_child_ref", family=family["gramps_id"], child=child["gramps_id"], frel="adopted"
    )
    repo = await step("add_repository", name="County Library", repository_type="Library")
    deed = await step(
        "add_source",
        title="Deed Book 3",
        author="County Recorder",
        repository=repo["handle"],
        call_number="DB-3",
        media_type="Book",
    )
    citation = await step(
        "add_citation", citation={"source": deed["handle"], "page": "p. 210", "confidence": "high"}
    )
    await step(
        "cite_object",
        object_type="person",
        ref=father["gramps_id"],
        citation={"citation": citation["handle"]},
    )
    await step("tag_object", object_type="person", target=father["gramps_id"], tag="To verify")
    await step("set_private", object_type="person", target=mother["gramps_id"], private=True)
    # Unrelated in the tree: the relationship and common ancestors a match
    # shows are Gramps' relationship calculator, which the fake does not model
    # (the unit tests hand it the server's answers instead).
    stranger = await step("add_person", given="Josiah", surname="Pembrook", gender="male")
    await step(
        "add_dna_match",
        person=child["gramps_id"],
        match=stranger["gramps_id"],
        segments="1,1000000,5000000,7.5,1200",
        citation={"source_title": "DNA test, kit A1", "page": "match list"},
    )

    # Reads: what a session sees.
    for gid in (father["gramps_id"], mother["gramps_id"], child["gramps_id"]):
        await step("get_person", person=gid)
    await step("get_family", family=family["gramps_id"])
    await step("get_event", event=residence["event_handle"])
    await step("get_place", place=town["gramps_id"])
    await step("get_backlinks", object_type="source", ref=deed["handle"])
    await step("search_people", name="Wren", include_private=True)
    await step("check_family_links", include_private=True)
    await step("get_dna_matches", person=child["gramps_id"])
    await step("list_tags")
    await step("get_record_history", object_type="person", ref=father["gramps_id"])
    await step("get_record_history", object_type="family", ref=family["gramps_id"])
    task = await step(
        "add_research_task",
        title="Order the deed",
        description="Deed Book 3, p. 210",
        tags=["Land"],
        priority="high",
    )
    await step(
        "update_research_task",
        task=task["gramps_id"],
        status="In Progress",
        note_append="Letter sent.",
    )
    await step("list_research_tasks")
    await step(
        "query_records",
        object_type="event",
        event_type="Birth",
        select=["gramps_id", {"json_path": ["date", "dateval", 2], "as": "year"}],
        order_by=[{"column": "gramps_id", "direction": "asc"}],
    )
    await step(
        "query_objects",
        object_type="person",
        gql='alternate_names.any.surname_list.any.surname ~ "holt"',
        keys="gramps_id",
    )
    await step(
        "query_objects",
        object_type="person",
        gql='event_ref_list.any.ref.get_event.description ~ "parents" OR urls.length > 0',
        keys="gramps_id",
    )


async def _editing_and_deleting(step: Recorder) -> None:
    """The edit, merge and delete paths, where the fake models the server's cascades."""
    state = await step("add_place", name="Ohio", place_type="State")
    county = await step(
        "add_place", name="Brannock County", place_type="County", parent=state["gramps_id"]
    )
    town_a = await step(
        "add_place", name="Cedar Flat", place_type="City", parent=county["gramps_id"]
    )
    town_b = await step(
        "add_place", name="Cedar Flat", place_type="City", parent=state["gramps_id"]
    )
    for dry_run in (True, False):
        await step(
            "merge_objects",
            object_type="place",
            keep=town_a["gramps_id"],
            drop=town_b["gramps_id"],
            dry_run=dry_run,
        )

    father = await step("add_person", given="Elias", surname="Wren", gender="male")
    one = await step("add_person", given="Mercy", surname="Wren", gender="female")
    two = await step("add_person", given="Hope", surname="Wren", gender="female")
    family = await step(
        "add_family", father=father["gramps_id"], children=[one["gramps_id"], two["gramps_id"]]
    )
    # The links the server duplicates (PITFALLS 15), made the way it makes them.
    client = step.client
    second = await step("add_family")
    person = await client.get_object("person", father["handle"])
    person["family_list"].append(second["handle"])
    await client.update_object("person", father["handle"], person)
    fam = await client.get_object("family", second["handle"])
    fam["father_handle"] = father["handle"]
    await client.update_object("family", second["handle"], fam)
    await step("check_family_links", include_private=True)
    await step("update_person", person=father["gramps_id"])
    await step(
        "detach_object",
        parent_type="family",
        parent=family["gramps_id"],
        child_kind="child",
        child=two["gramps_id"],
    )
    await step(
        "update_child_ref", family=family["gramps_id"], child=one["gramps_id"], frel="stepchild"
    )
    # Parents and children changed in place (TOOL-REQUESTS #21), and what the
    # log says about it (#29): 3.21 reads the whole log, 3.22 the record's.
    wife = await step("add_person", given="Ruth", surname="Ashbee", gender="female")
    await step(
        "set_family_parent", family=family["gramps_id"], role="mother", person=wife["gramps_id"]
    )
    await step(
        "move_child",
        child=one["gramps_id"],
        from_family=family["gramps_id"],
        to_family=second["gramps_id"],
        mrel="stepchild",
    )
    await step(
        "get_record_history",
        object_type="person",
        ref=one["gramps_id"],
        field="parent_family_list",
    )

    added = await step(
        "add_event_to_person",
        person=one["gramps_id"],
        event={
            "type": "Residence",
            "date": "1900",
            "place": town_a["gramps_id"],
            "citation": {"source_title": "Census 1900", "page": "sheet 4", "note": "finding"},
        },
    )
    event = added["event_handle"]
    await step("update_event", event=event, event_type="census")
    await step("update_event", event=event, date="from 4 May 1864 to 16 Sep 1864")
    await step("update_event", event=event, clear_place=True)
    await step("add_event_ref", person=father["gramps_id"], event=event, role="witness")
    await step(
        "update_event_ref",
        person=father["gramps_id"],
        event=event,
        attributes={"As enumerated": "Wren, Elias, 49, head", "age": "49"},
    )
    citation = (await client.get_object("event", event))["citation_list"][0]
    source = (await client.get_object("citation", citation))["source_handle"]
    await step("uncite", object_type="event", ref=event, citation=citation)
    other = await step("add_citation", citation={"source": source, "page": "sheet 5"})
    await step(
        "uncite", object_type="event", ref=event, citation=citation, carry_to=other["handle"]
    )
    await step(
        "update_citations",
        items=[
            {"citation": other["handle"], "page": "sheet 5b", "expect_page_prefix": "sheet 5"},
            {"citation": other["handle"], "page": "x", "expect_page_prefix": "not the page"},
        ],
    )

    alt = await step(
        "add_alternate_name",
        person=two["gramps_id"],
        given="Hope",
        surname="Holt",
        citation={"source": source, "page": "p. 2"},
    )
    await step(
        "update_alternate_name", person=two["gramps_id"], match={"surname": "Holt"}, remove=True
    )
    await step(
        "uncite",
        object_type="name",
        ref=two["gramps_id"],
        name={"surname": "Holt"},
        citation=alt["citation_handle"],
    )
    await step(
        "update_alternate_name", person=two["gramps_id"], match={"surname": "Holt"}, remove=True
    )

    repo = await step("add_repository", name="County Library", repository_type="Library")
    await step(
        "link_repositories",
        items=[{"source": source, "repository": repo["handle"], "media_type": "Book"}],
    )
    await step(
        "detach_object",
        parent_type="source",
        parent=source,
        child_kind="repository",
        child=repo["handle"],
    )
    await step("tag_object", object_type="person", target=one["gramps_id"], tag="To verify")
    await step(
        "detach_object",
        parent_type="person",
        parent=one["gramps_id"],
        child_kind="tag",
        child="To verify",
    )

    await step("delete_object", object_type="source", target=source)  # refused: cited
    await step("delete_object", object_type="event", target=event)
    await step("delete_object", object_type="family", target=second["gramps_id"])
    await step("get_person", person=father["gramps_id"])
    await step("check_family_links", include_private=True)


def _differences(fake: Any, live: Any, path: str = "") -> list[str]:
    if isinstance(fake, dict) and isinstance(live, dict):
        out = []
        for key in dict.fromkeys([*fake, *live]):
            if key not in live:
                out.append(f"{path}.{key}: only the fake has it ({json.dumps(fake[key])[:120]})")
            elif key not in fake:
                out.append(f"{path}.{key}: only the server has it ({json.dumps(live[key])[:120]})")
            else:
                out += _differences(fake[key], live[key], f"{path}.{key}")
        return out
    if isinstance(fake, list) and isinstance(live, list) and len(fake) == len(live):
        return [
            d
            for i, (f, s) in enumerate(zip(fake, live, strict=True))
            for d in _differences(f, s, f"{path}[{i}]")
        ]
    if fake != live:
        return [f"{path}: fake {json.dumps(fake)[:120]} / server {json.dumps(live)[:120]}"]
    return []


async def _merging(step: Recorder) -> None:
    """Each kind of object merged through merge_objects, as Gramps merges it."""

    def cite(title: str, page: str) -> dict:
        return {"source_title": title, "page": page}

    # Two records of one man, each with what the other lacks.
    elias = await step(
        "add_person",
        given="Elias",
        surname="Wren",
        gender="male",
        birth={"date": "1801", "citation": cite("Register of Births", "p. 12")},
    )
    other = await step(
        "add_person",
        given="Elias",
        surname="Wrenn",
        gender="male",
        birth={"date": "about 1801", "citation": cite("Census 1850", "sheet 4")},
        death={"date": "1866", "citation": cite("Register of Deaths", "p. 3")},
    )
    await step(
        "add_alternate_name",
        person=other["gramps_id"],
        given="Elias",
        surname="Renn",
        citation=cite("Census 1860", "sheet 9"),
    )
    await step(
        "add_note", text="seen in two censuses", target=other["gramps_id"], target_type="person"
    )
    await step(
        "add_attribute",
        object_type="person",
        target=other["gramps_id"],
        name="Occupation",
        value="Smith",
    )
    await step(
        "add_url", object_type="person", target=other["gramps_id"], url="https://example.org/elias"
    )
    await step("tag_object", object_type="person", target=other["gramps_id"], tag="Duplicate")
    hannah = await step("add_person", given="Hannah", surname="Ashbee", gender="female")
    mercy = await step("add_person", given="Mercy", surname="Wren", gender="female")
    hope = await step("add_person", given="Hope", surname="Wren", gender="female")
    first = await step(
        "add_family",
        father=elias["gramps_id"],
        mother=hannah["gramps_id"],
        children=[mercy["gramps_id"]],
        marriage={"date": "1828", "citation": cite("Register of Marriages", "p. 7")},
    )
    second = await step(
        "add_family",
        father=other["gramps_id"],
        mother=hannah["gramps_id"],
        children=[hope["gramps_id"]],
        marriage={"date": "1828", "citation": cite("Banns", "p. 2")},
    )
    for dry_run in (True, False):
        await step(
            "merge_objects",
            object_type="person",
            keep=elias["gramps_id"],
            drop=other["gramps_id"],
            dry_run=dry_run,
        )
    await step("get_person", person=elias["gramps_id"])
    await step("check_family_links", include_private=True)

    # The two families now share both parents: merge them.
    for dry_run in (True, False):
        await step(
            "merge_objects",
            object_type="family",
            keep=first["gramps_id"],
            drop=second["gramps_id"],
            dry_run=dry_run,
        )
    await step("get_family", family=first["gramps_id"])
    await step("check_family_links", include_private=True)

    # Two events, two sources, two citations, two notes, two repositories.
    residences = [
        await step(
            "add_event_to_person",
            person=mercy["gramps_id"],
            event={
                "type": "Residence",
                "date": "1850",
                "citation": cite(f"Census 1850 copy {n}", f"sheet {n}"),
            },
        )
        for n in (1, 2)
    ]
    await step(
        "merge_objects",
        object_type="event",
        keep=residences[0]["event_handle"],
        drop=residences[1]["event_handle"],
        dry_run=False,
    )
    sources = [await step("add_source", title=f"Deed Book {n}", author="Recorder") for n in (1, 2)]
    repos = [
        await step("add_repository", name=f"Library {n}", repository_type="Library") for n in (1, 2)
    ]
    for source, repo in zip(sources, repos, strict=True):
        await step(
            "link_repositories",
            items=[
                {"source": source["handle"], "repository": repo["handle"], "media_type": "Book"}
            ],
        )
    citations = [
        await step("add_citation", citation={"source": sources[n]["handle"], "page": f"p. {n}"})
        for n in (0, 1)
    ]
    await step(
        "merge_objects",
        object_type="source",
        keep=sources[0]["handle"],
        drop=sources[1]["handle"],
        dry_run=False,
    )
    await step(
        "merge_objects",
        object_type="citation",
        keep=citations[0]["handle"],
        drop=citations[1]["handle"],
        dry_run=False,
    )
    await step(
        "merge_objects",
        object_type="repository",
        keep=repos[0]["handle"],
        drop=repos[1]["handle"],
        dry_run=False,
    )
    notes = [await step("add_note", text=f"finding {n}") for n in (1, 2)]
    await step(
        "merge_objects",
        object_type="note",
        keep=notes[0]["handle"],
        drop=notes[1]["handle"],
        dry_run=False,
    )
    await step("get_backlinks", object_type="source", ref=sources[0]["handle"])


async def _compare(tmp_path, scenario) -> None:
    async with fake_tools(tmp_path / "fake") as call:
        step = Recorder(call)
        await scenario(step)
        faked = await step.finish()
    async with live_tools(tmp_path / "live") as call:
        step = Recorder(call)
        await scenario(step)
        real = await step.finish()

    assert [label for label, _ in faked] == [label for label, _ in real]
    report = []
    for (label, fake), (_, live) in zip(faked, real, strict=True):
        report += [f"{label}{d}" for d in _differences(fake, live)]
    assert not report, "The fake and the server disagree:\n" + "\n".join(report)


async def test_building_and_reading_answer_as_the_server_does(live_server, tmp_path):
    await _compare(tmp_path, _building_and_reading)


async def test_editing_and_deleting_answer_as_the_server_does(live_server, tmp_path):
    await _compare(tmp_path, _editing_and_deleting)


async def test_merging_answers_as_the_server_does(live_server, tmp_path):
    await _compare(tmp_path, _merging)
