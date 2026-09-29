"""Background tasks and genealogical verification.

Undo, import, reindex and verification are dispatched to a worker, so the
call that starts one returns before the work is done. Without a way to poll,
an undo is submitted and its outcome simply assumed.
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# get_task
# --------------------------------------------------------------------------- #
async def test_pending_task_is_reported_as_unfinished(tools):
    """A caller must be able to tell 'not yet' from 'worked'."""
    tools.fake.tasks["t1"] = {"task_id": "t1", "state": "PENDING", "name": "undo", "info": "queued"}
    out = await tools("get_task", task_id="t1")
    assert out["finished"] is False
    assert out["succeeded"] is None


async def test_successful_task_reports_its_result(tools):
    """SUCCESS is terminal and carries whatever the worker produced."""
    tools.fake.tasks["t2"] = {
        "task_id": "t2",
        "state": "SUCCESS",
        "name": "verify",
        "result_object": {"findings": []},
    }
    out = await tools("get_task", task_id="t2")
    assert out["finished"] is True
    assert out["succeeded"] is True
    assert out["result"] == {"findings": []}


async def test_failed_task_is_finished_but_not_successful(tools):
    """A failure that reads as 'done' would be worse than no tool at all."""
    tools.fake.tasks["t3"] = {"task_id": "t3", "state": "FAILURE", "info": "worker died"}
    out = await tools("get_task", task_id="t3")
    assert out["finished"] is True
    assert out["succeeded"] is False
    assert out["info"] == "worker died"


async def test_revoked_task_is_terminal(tools):
    """A cancelled task will never change again; do not let a caller wait."""
    tools.fake.tasks["t4"] = {"task_id": "t4", "state": "REVOKED"}
    out = await tools("get_task", task_id="t4")
    assert out["finished"] is True
    assert out["succeeded"] is False


async def test_unknown_task_is_an_error_envelope(tools):
    """A bad id must not look like a pending task."""
    out = await tools("get_task", task_id="nope")
    assert out["error"] == "api"
    assert out["status"] == 404


async def test_undo_hands_back_the_task_id_to_poll(tools):
    """The gap this closes: undo used to report 'submitted' and stop there."""
    tools.fake.transactions = [{"id": 7, "changes": [], "_conflicts": []}]
    out = await tools("undo_transaction", transaction_id=7, dry_run=False)
    assert out["task_id"] == "t1"
    assert "get_task" in out["message"]


# --------------------------------------------------------------------------- #
# verify_tree
# --------------------------------------------------------------------------- #
async def test_verification_reports_findings_with_a_count(tools):
    """The count is what makes a run comparable to the last one."""
    tools.fake.verify_findings = [
        {"handle": "h1", "message": "Mother was too young"},
        {"handle": "h2", "message": "Invalid date"},
    ]
    out = await tools("verify_tree")
    assert out["finding_count"] == 2
    assert out["tree_id"] == "tree1"


async def test_clean_tree_reports_zero_not_an_error(tools):
    """No findings is the good outcome and must read like one."""
    out = await tools("verify_tree")
    assert out["finding_count"] == 0
    assert out["findings"] == []


async def test_tree_id_is_resolved_when_there_is_only_one(tools):
    """The tree is bound to the account, so the caller should not need it."""
    out = await tools("verify_tree")
    assert out["tree_id"] == "tree1"


async def test_ambiguous_tree_is_refused_rather_than_guessed(tools):
    """Verifying the wrong tree silently would be worse than failing."""
    tools.fake.trees = [
        {"id": "tree1", "name": "Mine"},
        {"id": "tree2", "name": "Test copy"},
    ]
    out = await tools("verify_tree")
    assert out["error"] == "not_found"
    assert "tree1" in out["message"] and "tree2" in out["message"]


async def test_explicit_tree_id_is_honoured(tools):
    """Naming a tree resolves the ambiguity above."""
    tools.fake.trees = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
    out = await tools("verify_tree", tree_id="b")
    assert out["tree_id"] == "b"


async def test_no_reachable_tree_is_an_error(tools):
    """Credentials that reach nothing must say so."""
    tools.fake.trees = []
    out = await tools("verify_tree")
    assert out["error"] == "not_found"


async def test_omitted_thresholds_are_not_sent(tools):
    """Sending nulls would override the server's own defaults with nothing."""
    await tools("verify_tree")
    assert "oldage" not in tools.fake.verify_params
    assert "yngmom" not in tools.fake.verify_params


@pytest.mark.parametrize(
    ("argument", "wire_name", "value"),
    [
        ("max_age_at_death", "oldage", 110),
        ("min_mother_age", "yngmom", 12),
        ("max_children_father", "mxchilddad", 20),
        ("max_widowhood_years", "lngwdw", 40),
    ],
)
async def test_thresholds_map_to_their_wire_names(tools, argument, wire_name, value):
    """The tool's readable names must reach the API's cryptic ones."""
    await tools("verify_tree", **{argument: value})
    assert tools.fake.verify_params[wire_name] == str(value)


async def test_flag_invalid_dates_is_only_sent_when_disabled(tools):
    """The server already defaults it on; sending it again is noise."""
    await tools("verify_tree", flag_invalid_dates=True)
    assert "invdate" not in tools.fake.verify_params
    await tools("verify_tree", flag_invalid_dates=False)
    assert tools.fake.verify_params["invdate"] == "false"


async def test_background_verification_returns_a_task_to_poll(tools):
    """A large tree runs this in the background; the caller needs the handle."""
    tools.fake.verify_task_id = "v99"
    out = await tools("verify_tree")
    assert out["task_id"] == "v99"
    assert "get_task" in out["message"]
