"""The fake behaves as the server does where the tools can tell the difference.

Each of these is a behaviour of gramps-webapi the live suite established
(``tests/live``), pinned here so the fake keeps it between live runs. The
contract tests in ``tests/live/test_contract_live.py`` compare the fake with a
real server wholesale; these name the specific behaviours, so a regression in
the fake says what broke.
"""

from __future__ import annotations

import pytest

from gramps_evidence_mcp.client import GrampsApiError


async def test_a_write_is_stored_complete_and_served_without_class(service):
    made = await service.client.create_object("place", {"_class": "Place", "name": {"value": "X"}})
    served = await service.client.get_object("place", made["handle"])
    assert served["place_type"] == "Unknown"
    assert served["name"] == {"value": "X", "lang": "", "date": served["name"]["date"]}
    assert "_class" not in served and "_class" not in served["name"]


async def test_a_put_from_a_partial_read_leaves_defaults_not_holes(service, fake):
    """PITFALLS 1: what the PUT leaves out is stored empty, not kept."""
    made = await service.client.create_object(
        "source", {"_class": "Source", "title": "Register", "author": "Clerk"}
    )
    partial = await service.client.get_object("source", made["handle"], keys="handle,title")
    await service.client.update_object("source", made["handle"], partial)
    assert fake.store["source"][made["handle"]]["author"] == ""
    assert fake.partial_writes


async def test_a_date_is_served_with_its_year_and_a_written_back_year_goes_stale(service):
    """PITFALLS 24."""
    made = await service.client.create_object(
        "event", {"_class": "Event", "type": "Birth", "date": {"dateval": [0, 0, 1850, False]}}
    )
    served = await service.client.get_object("event", made["handle"])
    assert (served["date"]["year"], served["date"]["sortval"]) == (1850, 2396759)
    served["date"]["dateval"][2] = 1860
    await service.client.update_object("event", made["handle"], served)
    stale = (await service.client.get_object("event", made["handle"]))["date"]
    assert (stale["dateval"][2], stale["year"], stale["sortval"]) == (1860, 1850, 2400411)


async def test_nulls_are_refused_or_converted_field_by_field(service, fake):
    family = await service.client.create_object(
        "family", {"_class": "Family", "mother_handle": None}
    )
    assert fake.store["family"][family["handle"]]["mother_handle"] == ""
    event = await service.client.create_object("event", {"_class": "Event", "place": None})
    assert fake.store["event"][event["handle"]]["place"] is None
    with pytest.raises(GrampsApiError) as exc:
        await service.client.create_object("event", {"_class": "Event", "description": None})
    assert exc.value.status == 400


async def test_profile_and_extended_come_only_when_asked_for(service):
    made = await service.client.create_object(
        "person", {"_class": "Person", "primary_name": {"first_name": "Ada"}}
    )
    plain = await service.client.get_object("person", made["handle"])
    assert "profile" not in plain and "extended" not in plain
    asked = await service.client.get_object("person", made["handle"], profile="self", extend="all")
    assert "profile" in asked and "extended" in asked


async def test_a_tag_has_no_gramps_id(service):
    made = await service.client.create_object("tag", {"_class": "Tag", "name": "To verify"})
    assert "gramps_id" not in await service.client.get_object("tag", made["handle"])


async def test_the_query_engine_reads_an_event_type_as_its_number(service):
    """PITFALLS 12: served as "Birth", stored as {"string": "", "value": 12}."""
    await service.client.create_object("event", {"_class": "Event", "type": "Birth"})
    await service.client.create_object("event", {"_class": "Event", "type": "Death"})

    async def where(path, value):
        rows, total, _ = await service.client.structured_query(
            "event",
            {
                "select": ["handle"],
                "where": [{"column": {"json_path": path}, "op": "eq", "value": value}],
            },
        )
        return total

    assert await where(["type", "value"], 12) == 1
    assert await where(["type", "string"], "Birth") == 0


async def test_a_name_the_server_does_not_know_exactly_becomes_a_lasting_custom_type(service):
    """PITFALLS 26: matched case and all; listed from then on, even once unused."""
    census = await service.client.create_object("event", {"_class": "Event", "type": "census"})
    await service.client.create_object("event", {"_class": "Event", "type": "Census"})
    types = await service.client.types()
    assert "census" in types["custom"]["event_types"]
    assert "Census" not in types["custom"]["event_types"]
    assert "Census" in types["default"]["event_types"]
    await service.client.delete_object("event", census["handle"])
    assert "census" in (await service.client.types())["custom"]["event_types"]
