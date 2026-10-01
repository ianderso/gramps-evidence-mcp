"""The live harness itself: it reaches the server, and leaves the tree empty."""

from __future__ import annotations

from .harness import LIVE_URL, count_objects


async def test_a_write_round_trips_through_the_real_server(live):
    person = await live("add_person", given="Ada", surname="Wren")
    shown = await live("get_person", person=person["gramps_id"])
    assert shown["handle"] == person["handle"]


def test_the_tree_is_empty_after_each_test(live_server):
    """Runs after the test above: its wipe must have left nothing behind."""
    assert not any(count_objects(LIVE_URL).values())
