"""Reports, custom filters, consolidated timelines, task list, transactions.

Gramps ships a report engine and a filter-rule vocabulary that no query
language here reaches. These tests pin the two things that make them safe to
drive from a tool: a report takes a whole option dict rather than a partial
one, and a filter delete touches the definition and nothing in the tree.
"""

from __future__ import annotations

import json

import pytest

from .conftest import timeline_row


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
async def test_reports_are_listed_with_their_option_keys(tools):
    """A caller cannot set an option it does not know the name of."""
    out = await tools("list_reports")
    assert out["report_count"] == 1
    entry = out["reports"][0]
    assert entry["id"] == "ancestor_report"
    assert "maxgen" in entry["options"]


async def test_report_options_come_back_with_their_defaults(tools):
    """Defaults are what an override is merged over."""
    out = await tools("get_report_options", report_id="ancestor_report")
    assert out["default_options"]["maxgen"] == 10
    assert out["default_options"]["living_people"] == 99


async def test_unknown_report_is_an_error_envelope(tools):
    """A typo in a report id must not look like an empty report."""
    out = await tools("get_report_options", report_id="nope_report")
    assert out["error"] == "api"
    assert out["status"] == 404


async def test_overrides_are_merged_over_the_defaults_not_substituted(tools):
    """Reports take a whole option dict; a partial one makes most of them fail.

    This is the property worth a test: setting maxgen must not drop pid,
    living_people and off along with it.
    """
    await tools("run_report", report_id="ancestor_report", options={"maxgen": 4})
    _, params = tools.fake.report_runs[-1]
    sent = json.loads(params["options"])
    assert sent["maxgen"] == 4
    assert sent["pid"] == ""
    assert sent["off"] == "print"


async def test_reports_leave_out_living_people_by_default(tools):
    """Gramps' default is 99: living people included with all their data.

    A report is a file made to be shared, which is exactly the leak the
    privacy default exists to prevent.
    """
    out = await tools("run_report", report_id="ancestor_report")
    _, params = tools.fake.report_runs[-1]
    sent = json.loads(params["options"])
    assert sent["living_people"] == 0
    assert sent["maxgen"] == 10  # still the whole default dict
    assert out["privacy_options"] == {"living_people": 0}


async def test_a_caller_can_still_ask_for_living_people_in_a_report(tools):
    await tools("run_report", report_id="ancestor_report", options={"living_people": 2})
    _, params = tools.fake.report_runs[-1]
    assert json.loads(params["options"])["living_people"] == 2


async def test_running_without_overrides_sends_no_option_string(tools):
    """With privacy off, sending the defaults back is noise, and risks stale values."""
    tools.service.config.expose_private = True
    await tools("run_report", report_id="ancestor_report")
    _, params = tools.fake.report_runs[-1]
    assert "options" not in params


async def test_report_returns_a_task_and_a_filename(tools):
    """Reports run in the background; the caller needs both handles."""
    out = await tools("run_report", report_id="ancestor_report")
    assert out["task_id"] == "report1"
    assert out["file_name"] == "ancestor_report.pdf"
    assert "get_job" in out["message"]


async def test_synchronous_report_reports_no_task(tools):
    """A small report may finish inline; do not invent a task to poll."""
    tools.fake.report_task_id = None
    out = await tools("run_report", report_id="ancestor_report")
    assert out["task_id"] is None
    assert out["file_name"] == "ancestor_report.pdf"


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
async def test_filter_rules_expose_their_arguments(tools):
    """A rule is unusable without knowing what values it expects."""
    out = await tools("list_filter_rules", namespace="people")
    rule = out["rules"][0]
    assert rule["rule"] == "IsDescendantOf"
    assert rule["arguments"] == ["ID:", "Inclusive:"]


async def test_unknown_namespace_returns_an_empty_vocabulary(tools):
    """An unknown namespace is empty, not an exception."""
    out = await tools("list_filter_rules", namespace="unicorns")
    assert out["rule_count"] == 0


async def test_creating_a_filter_sends_the_rules_and_combiner(tools):
    """The filter is only as good as the body that reaches the server."""
    await tools(
        "create_filter",
        namespace="Person",
        name="Pembrook descendants",
        rules=[{"name": "IsDescendantOf", "values": ["I0001", "1"]}],
        function="or",
        comment="Audit scope",
    )
    namespace, body = tools.fake.created_filters[-1]
    assert namespace == "persons"
    assert body["function"] == "or"
    assert body["rules"][0]["name"] == "IsDescendantOf"
    assert body["comment"] == "Audit scope"


async def test_created_filter_then_appears_in_the_list(tools):
    """Round trip: what was saved is what comes back."""
    await tools(
        "create_filter",
        namespace="Person",
        name="Scope",
        rules=[{"name": "IsDescendantOf", "values": ["I0001"]}],
    )
    out = await tools("list_custom_filters", namespace="persons")
    assert any(f["name"] == "Scope" for f in out["persons"])


async def test_deleting_a_filter_touches_only_the_definition(tools):
    """A filter is a saved selection; deleting one must not delete records."""
    person = await tools("add_person", given="Josiah", surname="Pembrook")
    await tools("delete_filter", namespace="people", name="Scope")
    assert tools.fake.deleted_filters[-1] == ("people", "Scope")
    still_there = await tools("get_person", person=person["gramps_id"])
    assert still_there["name"] == "Josiah Pembrook"
    assert tools.fake.partial_writes == []


# --------------------------------------------------------------------------- #
# Consolidated timeline
# --------------------------------------------------------------------------- #
async def _dead(tools, given: str, surname: str) -> dict:
    """Someone born long ago, so privacy does not withhold their events."""
    return await tools(
        "add_person",
        given=given,
        surname=surname,
        birth={"type": "Birth", "date": "1770", "citation": {"source_title": "S", "page": "p"}},
    )


async def test_consolidated_timeline_merges_several_people(tools):
    """Seeing a household move through censuses together is the point."""
    a = await _dead(tools, "Josiah", "Pembrook")
    b = await _dead(tools, "Mercy", "Ashbee")
    people = tools.fake.store["person"]
    tools.fake.consolidated = [
        timeline_row("E0001", "Census", "1800", people[a["handle"]], citations=1),
        timeline_row("E0002", "Census", "1810", people[b["handle"]], relationship="wife"),
    ]
    out = await tools(
        "consolidated_timeline",
        targets=[a["gramps_id"], b["gramps_id"]],
        anchor=a["gramps_id"],
    )
    assert out["included"] == 2
    assert out["event_count"] == 2
    assert out["uncited_count"] == 1
    assert out["events"][1]["person"]["gramps_id"] == b["gramps_id"]


async def test_consolidated_timeline_keeps_to_the_people_named(tools):
    """An anchor brings its relatives with it; only the people named stay.

    The same unasked generation each way as get_timeline (TOOL-REQUESTS #30).
    """
    a = await _dead(tools, "Josiah", "Pembrook")
    b = await _dead(tools, "Mercy", "Ashbee")
    sister = await _dead(tools, "Ruth", "Pembrook")
    people = tools.fake.store["person"]
    tools.fake.consolidated = [
        timeline_row("E0001", "Birth", "1770", people[a["handle"]]),
        timeline_row("E0002", "Birth", "1772", people[sister["handle"]], relationship="sister"),
        timeline_row("E0003", "Birth", "1774", people[b["handle"]], relationship="wife"),
    ]
    out = await tools(
        "consolidated_timeline", targets=[b["gramps_id"]], anchor=a["gramps_id"], limit=5
    )
    assert [e["gramps_id"] for e in out["events"]] == ["E0001", "E0003"]
    assert tools.fake.consolidated_params["omit_anchor"] == "0"
    assert "page" not in tools.fake.consolidated_params

    out = await tools("consolidated_timeline", targets=[a["gramps_id"]], limit=5)
    assert tools.fake.consolidated_params["page"] == "1"


async def test_consolidated_family_timeline_sends_only_what_it_takes(tools):
    """The families endpoint takes no omit_anchor; sent, the server answers 422."""
    a = await tools("add_person", given="Josiah", surname="Pembrook")
    fam = await tools("add_family", father=a["gramps_id"])
    out = await tools("consolidated_timeline", targets=[fam["gramps_id"]], object_type="family")
    assert "error" not in out
    assert "omit_anchor" not in tools.fake.consolidated_params


async def test_consolidated_timeline_asks_for_evidence_ratings(tools):
    """Citation count and confidence are optional on this endpoint; ask."""
    a = await tools("add_person", given="Solo", surname="Person")
    await tools("consolidated_timeline", targets=[a["gramps_id"]])
    assert tools.fake.consolidated_params["ratings"] == "1"


async def test_consolidated_timeline_resolves_gramps_ids_to_handles(tools):
    """The endpoint takes handles; callers work in gramps_ids."""
    a = await tools("add_person", given="Solo", surname="Person")
    await tools("consolidated_timeline", targets=[a["gramps_id"]])
    assert tools.fake.consolidated_params["handles"] == a["handle"]


async def test_consolidated_timeline_rejects_an_unsupported_type(tools):
    """Only people and families have timelines."""
    out = await tools("consolidated_timeline", targets=["S0001"], object_type="source")
    assert out["error"] == "unsupported_type"


# --------------------------------------------------------------------------- #
# Tasks and transactions
# --------------------------------------------------------------------------- #
async def test_task_list_reports_recent_jobs(tools):
    """For when a task_id was lost, or to check nothing is still running."""
    tools.fake.task_list = [
        {"task_id": "t1", "name": "verify", "state": "SUCCESS"},
        {"task_id": "t2", "name": "undo", "state": "PENDING"},
    ]
    out = await tools("list_jobs")
    assert out["job_count"] == 2
    assert out["jobs"][1]["state"] == "PENDING"


async def test_empty_task_list_is_not_an_error(tools):
    """A tree where nothing has run is the normal state."""
    out = await tools("list_jobs")
    assert out["job_count"] == 0


async def test_single_transaction_is_readable_before_undoing_it(tools):
    """list_transactions summarises; this is what actually moved."""
    tools.fake.transactions = [{"id": 42, "changes": [{"_class": "Person", "handle": "h1"}]}]
    out = await tools("get_transaction", transaction_id=42)
    assert out["id"] == 42
    assert out["changes"][0]["_class"] == "Person"


async def test_unknown_transaction_is_an_error_envelope(tools):
    """A bad id must not read as an empty transaction."""
    out = await tools("get_transaction", transaction_id=999)
    assert out["error"] == "api"
    assert out["status"] == 404


@pytest.mark.parametrize("tool_name", ["list_reports", "list_jobs", "list_custom_filters"])
async def test_listing_tools_survive_an_empty_instance(tools, tool_name):
    """Nothing configured yet is a normal state, not a failure."""
    tools.fake.reports = []
    tools.fake.filters = {}
    out = await tools(tool_name)
    assert "error" not in out
