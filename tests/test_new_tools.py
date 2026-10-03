"""Tests for the write-safety, citation, place and audit operations.

The emphasis is on failure modes seen against a real tree -- partial PUTs
dropping fields, merges that didn't re-point references, citations detached into
orphans, writes reporting success they can't demonstrate -- rather than on happy
paths alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gramps_evidence_mcp.models import CitationInput, Confidence, EventInput, Gender, NameParts


async def _person(service, given="Ann", surname="Pembrook"):
    return await service.add_person(NameParts(given=given, surname=surname), Gender.female)


async def _source(service, title="1900 census, Cedar Flat, OH"):
    return await service.add_source(
        title=title,
        author=None,
        pubinfo=None,
        abbrev=None,
        repository_ref=None,
        call_number=None,
    )


async def _event(service, person_gid, ev, require_citation=True):
    """add_event_to_person reports the PERSON's handle; the event is event_handle."""
    result = await service.add_event_to_person(person_gid, ev, require_citation)
    return result["event_handle"]


# --------------------------------------------------------------------------- #
# safety spine
# --------------------------------------------------------------------------- #
async def test_mutate_writes_the_whole_object_not_just_changed_fields(service, fake):
    """A partial PUT is the data-loss defect in docs/PITFALLS.md section 1: the
    API replaces the record with exactly what it is sent, so every unsent
    field is dropped."""
    src = await _source(service)
    await service.link_repository(
        src["gramps_id"],
        (await service.add_repository("Family papers", "Collection", None))["gramps_id"],
        call_number="box 4",
    )
    await service.update_object_fields("source", src["gramps_id"], {"author": "Anon"})

    stored = fake.store["source"][src["handle"]]
    assert stored["author"] == "Anon"
    # The fields the edit never mentioned survived it.
    assert stored["title"] == "1900 census, Cedar Flat, OH"
    assert len(stored["reporef_list"]) == 1


async def test_mutate_skips_the_write_when_nothing_changes(service, fake):
    src = await _source(service)
    before = len([r for r in fake.requests if r[0] == "PUT"])
    result = await service.update_object_fields(
        "source", src["gramps_id"], {"title": "1900 census, Cedar Flat, OH"}
    )
    assert result["changed"] is False
    # No no-op PUT, so the transaction log stays honest about what happened.
    assert len([r for r in fake.requests if r[0] == "PUT"]) == before


async def test_update_object_fields_refuses_structural_lists(service):
    src = await _source(service)
    result = await service.update_object_fields("source", src["gramps_id"], {"media_list": []})
    assert result["error"] == "unsupported_fields"
    assert "attach_media" in result["message"]


# --------------------------------------------------------------------------- #
# query + backlinks
# --------------------------------------------------------------------------- #
async def test_query_objects_filters_server_side(service, fake):
    await _source(service, "A")
    await _source(service, "B")
    result = await service.query_objects("source", gql='title = "A"', keys="gramps_id,title")
    assert result["returned"] == 1
    assert result["results"][0]["title"] == "A"


async def test_query_objects_redacts_private_people(service):
    person = await _person(service)
    await service.set_private("person", person["gramps_id"], True)
    result = await service.query_objects("person", keys="gramps_id,private")
    assert result["redacted_count"] == 1
    assert result["results"][0]["redacted"] is True
    assert "name" not in result["results"][0]


async def test_query_objects_private_flag_holds_whatever_keys_are_asked_for(service):
    """Leaving `private` out of keys used to leave it out of the judgement too."""
    person = await _person(service)
    await service.set_private("person", person["gramps_id"], True)
    result = await service.query_objects("person", keys="gramps_id,primary_name")
    assert result["results"][0]["redacted"] is True
    assert "primary_name" not in result["results"][0]


async def test_query_objects_withholds_probably_living_people(service):
    """The same 110-year rule as every other bulk read, not the flag alone."""
    living = await _person(service, "Jane", "Pembrook")
    await _event(
        service, living["gramps_id"], EventInput(type="Birth", date="1990"), require_citation=False
    )
    historical = await _person(service, "Silas", "Pembrook")
    await _event(
        service,
        historical["gramps_id"],
        EventInput(type="Birth", date="1840"),
        require_citation=False,
    )

    result = await service.query_objects("person", keys="gramps_id,primary_name")
    by_id = {r["gramps_id"]: r for r in result["results"]}
    assert by_id[living["gramps_id"]]["redacted"] is True
    assert by_id[historical["gramps_id"]]["primary_name"]["first_name"] == "Silas"
    # The fields fetched only to judge are not handed back.
    assert set(by_id[historical["gramps_id"]]) == {"gramps_id", "primary_name"}
    assert result["redacted_count"] == 1


async def test_query_objects_withholds_other_private_records(service):
    """A private note is bulk output too."""
    person = await _person(service)
    note = await service.add_note(person["gramps_id"], "person", "Open question.", "Research")
    await service.set_private("note", note["gramps_id"], True)
    result = await service.query_objects("note", keys="gramps_id")
    assert result["results"][0]["redacted"] is True


async def test_get_backlinks_finds_what_cites_a_source(service):
    """A source has no citation_list -- citations point AT it. Reading the wrong
    field makes every source report zero citations."""
    person = await _person(service)
    src = await _source(service)
    await _event(
        service,
        person["gramps_id"],
        EventInput(
            type="Birth", date="1880", citation=CitationInput(source=src["gramps_id"], page="p. 1")
        ),
    )
    links = await service.get_backlinks("source", src["gramps_id"])
    assert links["total_references"] == 1
    # Grouped by singular type name, as the server returns it.
    assert "citation" in links["referenced_by"]


async def test_get_backlinks_reports_zero_for_an_orphan(service):
    src = await _source(service)
    links = await service.get_backlinks("source", src["gramps_id"])
    assert links["total_references"] == 0
    assert "Nothing references" in links["message"]


# --------------------------------------------------------------------------- #
# duplicates
# --------------------------------------------------------------------------- #
async def test_find_duplicates_flags_divergent_confidence_on_one_page(service):
    """The same page graded 1 here and 3 there is the interesting case: the grade
    is supposed to be a judgement about the evidence, not about the mood."""
    src = await _source(service)
    for confidence in (Confidence.low, Confidence.high):
        await service.add_citation(
            CitationInput(source=src["gramps_id"], page="p. 45", confidence=confidence)
        )
    result = await service.find_duplicates("citation_page")
    assert result["group_count"] == 1
    assert result["groups"][0]["divergent_confidence"] is True


async def test_find_duplicates_catches_two_births_on_one_person(service):
    person = await _person(service)
    src = await _source(service)
    for date in ("1880", "ABT 1881"):
        await _event(
            service,
            person["gramps_id"],
            EventInput(
                type="Birth",
                date=date,
                citation=CitationInput(source=src["gramps_id"], page="p. 1"),
            ),
        )
    result = await service.find_duplicates("vital_events")
    assert result["group_count"] == 1
    assert result["surplus_objects"] == 1


async def test_find_duplicates_rejects_an_unknown_kind(service):
    result = await service.find_duplicates("nonsense")
    assert result["error"] == "unknown_kind"


# --------------------------------------------------------------------------- #
# citation plumbing
# --------------------------------------------------------------------------- #
async def test_cite_object_attaches_to_a_family(service, fake):
    """A family citation supports a claim no event makes: that these two people
    were a couple."""
    father = await _person(service, "John", "Pembrook")
    mother = await _person(service, "Lydia", "Ashbee")
    family = await service.add_family(
        father["gramps_id"], mother["gramps_id"], None, require_citation=False
    )
    src = await _source(service)
    result = await service.cite_object(
        "family",
        family["gramps_id"],
        CitationInput(source=src["gramps_id"], page="p. 2"),
    )
    assert result["verified"] is True
    assert len(fake.store["family"][family["handle"]]["citation_list"]) == 1


async def test_cite_object_refuses_a_type_with_no_citation_list(service):
    result = await service.cite_object("note", "N0001", CitationInput(source_title="x"))
    assert result["error"] == "unsupported_type"


async def test_cite_child_link_makes_its_own_citation_for_the_parentage_claim(service, fake):
    """The link is a different claim from 'the child appears in this record', so
    it gets its own citation object -- reusing the child's handle makes the
    link inherit a confidence assigned to something else."""
    father = await _person(service, "John", "Pembrook")
    child = await _person(service, "Hattie", "Pembrook")
    family = await service.add_family(
        father["gramps_id"], None, [child["gramps_id"]], require_citation=False
    )
    src = await _source(service)
    result = await service.cite_child_link(
        family["gramps_id"],
        child["gramps_id"],
        CitationInput(source=src["gramps_id"], page="1855 census", confidence=Confidence.low),
    )
    assert result["changed"] is True
    child_ref = fake.store["family"][family["handle"]]["child_ref_list"][0]
    assert child_ref["citation_list"] == [result["citation_handle"]]
    # The citation carries the grade given HERE, not one borrowed from elsewhere.
    assert fake.store["citation"][result["citation_handle"]]["confidence"] == 1


async def test_cite_child_link_rejects_a_non_child(service):
    father = await _person(service, "John", "Pembrook")
    stranger = await _person(service, "Someone", "Else")
    family = await service.add_family(father["gramps_id"], None, None, require_citation=False)
    result = await service.cite_child_link(
        family["gramps_id"], stranger["gramps_id"], CitationInput(source_title="x")
    )
    assert result["error"] == "not_a_child"


async def test_uncite_deletes_the_citation_it_orphans(service, fake):
    """Detaching without deleting is how orphan citations accumulate: the fact is
    gone but the citation stays, still looking like evidence of something."""
    person = await _person(service)
    src = await _source(service)
    event_handle = await _event(
        service,
        person["gramps_id"],
        EventInput(
            type="Birth", date="1880", citation=CitationInput(source=src["gramps_id"], page="p. 1")
        ),
    )
    citation_handle = fake.store["event"][event_handle]["citation_list"][0]

    result = await service.uncite("event", event_handle, citation_handle)
    assert result["citation_deleted"] is True
    assert citation_handle not in fake.store["citation"]


async def test_uncite_keeps_a_citation_another_fact_still_uses(service, fake):
    person = await _person(service)
    src = await _source(service)
    first = await _event(
        service,
        person["gramps_id"],
        EventInput(
            type="Birth", date="1880", citation=CitationInput(source=src["gramps_id"], page="p. 1")
        ),
    )
    citation_handle = fake.store["event"][first]["citation_list"][0]
    second = await _event(
        service,
        person["gramps_id"],
        EventInput(type="Residence", date="1900"),
        require_citation=False,
    )
    await service.cite_event(second, CitationInput(citation=citation_handle))

    result = await service.uncite("event", first, citation_handle)
    assert result["remaining_references"] == 1
    assert result.get("citation_deleted") is not True
    assert citation_handle in fake.store["citation"]


async def test_uncite_is_a_no_op_when_the_citation_was_not_attached(service):
    person = await _person(service)
    src = await _source(service)
    citation = await service.add_citation(CitationInput(source=src["gramps_id"], page="p. 1"))
    result = await service.uncite("person", person["gramps_id"], citation["handle"])
    assert result["changed"] is False
    assert "was not attached" in result["message"]


# --------------------------------------------------------------------------- #
# update_citation
# --------------------------------------------------------------------------- #
async def test_update_citation_repoints_at_another_source(service, fake):
    """The fix when a fact was cited to a compiled bucket while the real record
    is already in the tree."""
    bucket = await _source(service, "compiled records")
    record = await _source(service, "1900 census page")
    citation = await service.add_citation(CitationInput(source=bucket["gramps_id"], page=""))
    result = await service.update_citation(
        citation["handle"],
        page="ED 12, sheet 4A",
        confidence=Confidence.high,
        source_ref=record["gramps_id"],
    )
    assert result["changed"] is True
    stored = fake.store["citation"][citation["handle"]]
    assert stored["source_handle"] == record["handle"]
    assert stored["page"] == "ED 12, sheet 4A"
    assert stored["confidence"] == 3


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
async def test_merge_dry_run_changes_nothing_and_reports_what_would_move(service, fake):
    keep = await _source(service, "Find a Grave memorial 123")
    drop = await _source(service, "Find a Grave memorial 123 (dup)")
    await service.add_citation(CitationInput(source=drop["gramps_id"], page="memorial 123"))
    result = await service.merge_objects("source", keep["gramps_id"], drop["gramps_id"])
    assert result["dry_run"] is True
    assert result["references_moving_to_keep"] == 1
    assert fake.merges == []
    assert drop["handle"] in fake.store["source"]


async def test_merge_repoints_references_to_the_survivor(service, fake):
    """A duplicate source makes a single-sourced fact look corroborated. The
    merge must move the citation, not just delete the loser."""
    keep = await _source(service, "Find a Grave memorial 123")
    drop = await _source(service, "Find a Grave memorial 123 (dup)")
    citation = await service.add_citation(
        CitationInput(source=drop["gramps_id"], page="memorial 123")
    )
    result = await service.merge_objects(
        "source", keep["gramps_id"], drop["gramps_id"], dry_run=False
    )
    assert result["dry_run"] is False
    assert fake.merges == [("source", keep["handle"], drop["handle"])]
    assert drop["handle"] not in fake.store["source"]
    assert fake.store["citation"][citation["handle"]]["source_handle"] == keep["handle"]


async def test_merge_refuses_an_object_with_itself(service):
    src = await _source(service)
    result = await service.merge_objects(
        "source", src["gramps_id"], src["gramps_id"], dry_run=False
    )
    assert result["error"] == "same_object"


async def test_merge_refuses_an_unmergeable_type(service):
    result = await service.merge_objects("tag", "T0001", "T0002", dry_run=False)
    assert result["error"] == "unsupported_type"


# --------------------------------------------------------------------------- #
# detach
# --------------------------------------------------------------------------- #
async def test_detach_event_from_person_keeps_the_event_by_default(service, fake):
    person = await _person(service)
    event_handle = await _event(
        service,
        person["gramps_id"],
        EventInput(type="Residence", date="1900"),
        require_citation=False,
    )
    result = await service.detach_object("person", person["gramps_id"], "event", event_handle)
    assert result["changed"] is True
    assert fake.store["person"][person["handle"]]["event_ref_list"] == []
    assert event_handle in fake.store["event"]


async def test_detach_will_not_delete_something_still_referenced(service, fake):
    """Deleting an object other facts still point at leaves dangling handles."""
    first = await _person(service, "Ann")
    second = await _person(service, "Mary")
    event_handle = await _event(
        service,
        first["gramps_id"],
        EventInput(type="Marriage", date="1900"),
        require_citation=False,
    )
    # The same event also sits on the second person (a shared event).
    obj = await service._resolve("person", second["handle"])
    obj.setdefault("event_ref_list", []).append(
        {"_class": "EventRef", "ref": event_handle, "role": "Primary"}
    )
    await service.client.update_object("person", second["handle"], obj)

    result = await service.detach_object(
        "person",
        first["gramps_id"],
        "event",
        event_handle,
        delete_if_orphan=True,
    )
    assert result["deleted"] is False
    assert result["remaining_references"] == 1
    assert event_handle in fake.store["event"]


async def test_detach_rejects_an_unknown_kind(service):
    person = await _person(service)
    result = await service.detach_object("person", person["gramps_id"], "sibling", "I0002")
    assert result["error"] == "unknown_kind"


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #
async def test_add_media_reuses_an_identical_file(service, fake, tmp_path):
    """One image, one Media object. The same photo uploaded once per person it
    depicts is the duplicate pattern that has to be unpicked by hand later."""
    path = tmp_path / "headstone.jpg"
    path.write_bytes(b"same bytes")

    first = await service.add_media(str(path), "Headstone of Ann Pembrook")
    assert first["created"] is True
    # The fake echoes back what was stored; give it the checksum the real server
    # would have computed on upload.
    fake.store["media"][first["handle"]]["checksum"] = first["checksum"]

    second = await service.add_media(str(path), "Headstone (again)")
    assert second["created"] is False
    assert second["handle"] == first["handle"]
    assert "already in the tree" in second["message"]


async def test_attach_media_links_an_existing_media_object(service, fake, tmp_path):
    path = tmp_path / "census.jpg"
    path.write_bytes(b"page image")
    media = await service.add_media(str(path), "1900 census page")
    person = await _person(service)

    result = await service.attach_media(person["gramps_id"], "person", media_ref=media["handle"])
    assert result["verified"] is True
    assert result["media_created"] is False
    refs = fake.store["person"][person["handle"]]["media_list"]
    assert [r["ref"] for r in refs] == [media["handle"]]


async def test_attach_media_needs_a_file_or_a_ref(service):
    person = await _person(service)
    result = await service.attach_media(person["gramps_id"], "person")
    assert result["error"] == "no_media"


# --------------------------------------------------------------------------- #
# ops
# --------------------------------------------------------------------------- #
async def test_export_backup_writes_a_file(service, tmp_path):
    target = tmp_path / "dump.gramps"
    result = await service.export_backup(str(target))
    assert target.exists()
    assert result["bytes"] == target.stat().st_size


async def test_export_backup_never_replaces_an_existing_file(service, tmp_path):
    """The path comes from the model; an earlier backup there must survive."""
    target = tmp_path / "tree.gramps"
    target.write_bytes(b"the only copy")
    result = await service.export_backup(str(target))
    assert result["error"] == "file_exists"
    assert target.read_bytes() == b"the only copy"


async def test_export_backup_does_not_create_directories_it_was_given(service, tmp_path):
    """A mistyped directory is refused, not conjured into existence."""
    result = await service.export_backup(str(tmp_path / "no" / "such" / "tree.gramps"))
    assert result["error"] == "no_such_directory"
    assert not (tmp_path / "no").exists()


async def test_export_backup_defaults_to_a_fresh_name_in_the_cache(service, tmp_path):
    service.config.cache_dir = tmp_path
    result = await service.export_backup()
    assert Path(result["path"]).parent == tmp_path / "backups"


async def test_undo_dry_run_reports_conflicts_without_undoing(service, fake):
    fake.transactions = [
        {
            "id": 42,
            "description": "Edit Citation",
            "timestamp": 1787021872.0,
            "changes": [{"id": 1}],
            "_conflicts": [{"obj_handle": "h1"}],
            "connection": {"user": {"name": "mcp"}},
        }
    ]
    result = await service.undo_transaction(42)
    assert result["dry_run"] is True
    assert result["can_undo_cleanly"] is False
    assert fake.undos == []


async def test_undo_refuses_a_conflicting_transaction_without_force(service, fake):
    fake.transactions = [
        {
            "id": 42,
            "description": "Edit Citation",
            "timestamp": 1787021872.0,
            "changes": [{"id": 1}],
            "_conflicts": [{"obj_handle": "h1"}],
            "connection": {"user": {"name": "mcp"}},
        }
    ]
    result = await service.undo_transaction(42, dry_run=False)
    assert fake.undos == []
    assert "Refused" in result["message"]

    forced = await service.undo_transaction(42, dry_run=False, force=True)
    assert fake.undos == [42]
    assert forced["dry_run"] is False


async def test_list_transactions_reports_who_changed_what(service, fake):
    fake.transactions = [
        {
            "id": 7,
            "description": "Edit Media",
            "timestamp": 1787021872.0,
            "changes": [{"id": 1}],
            "undo": False,
            "connection": {"user": {"name": "mcp", "full_name": "Claude"}},
        }
    ]
    result = await service.list_transactions(10)
    row = result["transactions"][0]
    assert row["transaction_id"] == 7
    assert row["user"] == "mcp"
    assert row["timestamp"].startswith("2026-")


# --------------------------------------------------------------------------- #
# regressions in the tools that were already there
# --------------------------------------------------------------------------- #
async def test_add_note_reports_whether_the_attach_actually_landed(service):
    """add_note has been observed reporting success while the note never
    attached. Success it cannot demonstrate is worse than a failure."""
    person = await _person(service)
    result = await service.add_note(person["gramps_id"], "person", "Research note", "Research")
    assert result["verified"] is True
    assert result["attached_to"] == person["gramps_id"]


async def test_list_unsourced_facts_does_not_fetch_each_event_separately(service, fake):
    """The original issued one GET per event -- over a thousand round-trips on
    a tree of a few hundred people, which timed out."""
    person = await _person(service)
    await _event(
        service,
        person["gramps_id"],
        EventInput(type="Death", date="1931", citation=CitationInput(source_title="Register")),
    )
    for i in range(5):
        await _event(
            service,
            person["gramps_id"],
            EventInput(type="Residence", date=f"190{i}"),
            require_citation=False,
        )
    before = len(fake.requests)
    result = await service.list_unsourced_facts()
    assert result["unsourced_count"] == 5
    # Two collection reads, not one request per event.
    assert len(fake.requests) - before <= 3


async def test_list_unsourced_facts_withholds_a_living_person_across_the_tree(service, fake):
    """Bulk output: a name and a 1990s date would otherwise go out together."""
    living = await _person(service, "Jane", "Pembrook")
    await _event(
        service, living["gramps_id"], EventInput(type="Birth", date="1992"), require_citation=False
    )
    historical = await _person(service, "Silas", "Pembrook")
    await _event(
        service,
        historical["gramps_id"],
        EventInput(type="Birth", date="1840"),
        require_citation=False,
    )

    result = await service.list_unsourced_facts()
    assert [f["person_gramps_id"] for f in result["facts"]] == [historical["gramps_id"]]
    assert result["withheld_fact_count"] == 1
    assert result["withheld"][0]["gramps_id"] == living["gramps_id"]
    assert "person" not in result["withheld"][0]

    # Asked about by name, the person is a deliberate lookup and shown in full.
    one = await service.list_unsourced_facts(living["gramps_id"])
    assert one["unsourced_count"] == 1
    assert one["facts"][0]["person"] == "Jane Pembrook"


async def test_person_name_duplicates_drop_a_living_namesake(service, fake):
    """The group key is the name itself, so a stub would still identify them."""
    for year in ("1801", "1803", "1995"):
        person = await _person(service, "John", "Pembrook")
        await _event(
            service,
            person["gramps_id"],
            EventInput(type="Birth", date=year),
            require_citation=False,
        )
    result = await service.find_duplicates("person_name")
    assert result["group_count"] == 1
    assert len(result["groups"][0]["members"]) == 2

    # With only one historical John left, there is no group to report.
    for person in [p for p in fake.store["person"].values()][:1]:
        person["private"] = True
    result = await service.find_duplicates("person_name")
    assert result["group_count"] == 0


async def test_vital_event_duplicates_skip_a_living_person(service):
    person = await _person(service, "Jane", "Pembrook")
    for year in ("1992", "1993"):
        await _event(
            service,
            person["gramps_id"],
            EventInput(type="Birth", date=year),
            require_citation=False,
        )
    assert (await service.find_duplicates("vital_events"))["group_count"] == 0


@pytest.mark.parametrize("kind", ["media_checksum", "source_title", "person_name"])
async def test_find_duplicates_runs_clean_on_an_empty_tree(service, kind):
    result = await service.find_duplicates(kind)
    assert result["group_count"] == 0


# --------------------------------------------------------------------------- #
# place resolution (PITFALLS #10) and update_place / update_url
# --------------------------------------------------------------------------- #
async def _place(service, name, title=None, place_type=None, parent=None):
    """Create a place directly through the client, shaped like the live API."""
    payload = {
        "_class": "Place",
        "name": {"_class": "PlaceName", "value": name},
        "title": title or name,
    }
    if place_type:
        payload["place_type"] = place_type
    if parent:
        payload["placeref_list"] = [{"_class": "PlaceRef", "ref": parent}]
    return await service.client.create_object("place", payload)


async def test_place_resolution_by_gramps_id_does_not_mint_a_place_named_P0000(service, fake):
    """Passing an id as the place once minted a place literally titled
    'P0000'."""
    p = await _place(service, "Columbus", "Columbus, Ohio, USA")
    resolved = await service.find_or_create_place(p["gramps_id"])
    assert resolved == p["handle"]
    assert len(fake.store["place"]) == 1  # nothing new created


async def test_place_resolution_falls_back_from_title_to_unique_name(service, fake):
    p = await _place(service, "Columbus", "Columbus, Ohio, USA")
    assert await service.find_or_create_place("Columbus, Ohio, USA") == p["handle"]
    assert await service.find_or_create_place("Columbus") == p["handle"]
    assert len(fake.store["place"]) == 1


async def test_place_resolution_refuses_to_guess_between_namesakes(service, fake):
    """Two places sharing a name is the state a silent guess (or a third
    duplicate) would make worse."""
    from gramps_evidence_mcp.service import AmbiguousPlaceError

    await _place(service, "Cedar Flat", "Cedar Flat, Ohio, USA")
    await _place(service, "Cedar Flat", "Cedar Flat, Oregon, USA")
    with pytest.raises(AmbiguousPlaceError):
        await service.find_or_create_place("Cedar Flat")
    assert len(fake.store["place"]) == 2  # and did not mint a third


async def test_place_resolution_still_creates_when_nothing_matches(service, fake):
    handle = await service.find_or_create_place("Cedar Flat, Brannock, Ohio, USA")
    assert handle in fake.store["place"]


async def test_update_place_sets_type_and_parent(service, fake):
    state = await _place(service, "Ohio", place_type="State")
    county = await _place(
        service, "Brannock", "Brannock, Ohio, USA", place_type="Country", parent=state["handle"]
    )  # the typo class
    result = await service.update_place(county["gramps_id"], place_type="County")
    assert result["changed"] is True
    stored = fake.store["place"][county["handle"]]
    assert stored["place_type"] == "County"
    # the enclosure it already had survived the write untouched
    assert [r["ref"] for r in stored["placeref_list"]] == [state["handle"]]

    city = await _place(service, "Cedar Flat")
    result = await service.update_place(
        city["gramps_id"], place_type="City", parent=county["gramps_id"]
    )
    assert result["changed"] is True
    stored = fake.store["place"][city["handle"]]
    assert stored["place_type"] == "City"
    assert [r["ref"] for r in stored["placeref_list"]] == [county["handle"]]


async def test_update_place_requires_an_existing_parent(service, fake):
    """A typo'd parent must fail loudly, never be minted from the name."""
    from gramps_evidence_mcp.service import NotFoundError

    city = await _place(service, "Cedar Flat")
    with pytest.raises(NotFoundError):
        await service.update_place(city["gramps_id"], parent="Brannock Cnty")  # typo
    assert len(fake.store["place"]) == 1


async def test_update_place_refuses_an_enclosure_cycle(service, fake):
    state = await _place(service, "Ohio")
    county = await _place(service, "Brannock", parent=state["handle"])
    result = await service.update_place(state["gramps_id"], parent=county["gramps_id"])
    assert result.get("error") == "enclosure_cycle"
    assert not fake.store["place"][state["handle"]].get("placeref_list")


async def test_update_place_refuses_to_flatten_dated_enclosures(service, fake):
    """A territory that became a state is two DATED placerefs; replacing
    them wholesale would erase that history."""
    from gramps_evidence_mcp.service import MultipleEnclosuresError

    territory = await _place(service, "Utah Territory")
    state = await _place(service, "Utah")
    town = await _place(service, "Cedar Flat")
    stored = fake.store["place"][town["handle"]]
    stored["placeref_list"] = [
        {"_class": "PlaceRef", "ref": territory["handle"], "date": {"year": 1870}},
        {"_class": "PlaceRef", "ref": state["handle"], "date": {"year": 1896}},
    ]
    with pytest.raises(MultipleEnclosuresError):
        await service.update_place(town["gramps_id"], parent=state["gramps_id"])
    assert len(fake.store["place"][town["handle"]]["placeref_list"]) == 2


async def test_update_url_retypes_one_entry_and_leaves_the_rest(service, fake):
    """A Find a Grave link filed under the wrong type, 'Web Home'."""
    person = await _person(service)
    await service.add_url(
        "person", person["gramps_id"], "https://example.com/home", "homepage", "Web Home"
    )
    await service.add_url(
        "person",
        person["gramps_id"],
        "https://www.findagrave.com/memorial/123",
        "memorial",
        "Web Home",
    )
    result = await service.update_url(
        "person",
        person["gramps_id"],
        "findagrave.com/memorial/123",
        url_type="Find A Grave",
        allow_new_type=True,
    )
    assert result["changed"] is True
    urls = fake.store["person"][person["handle"]]["urls"]
    assert [u["type"] for u in urls] == ["Web Home", "Find A Grave"]
    assert urls[1]["path"] == "https://www.findagrave.com/memorial/123"  # untouched


async def test_update_url_refuses_zero_or_multiple_matches(service, fake):
    from gramps_evidence_mcp.service import NotFoundError

    person = await _person(service)
    await service.add_url(
        "person",
        person["gramps_id"],
        "https://www.findagrave.com/memorial/123",
        "",
        "Web Home",
    )
    await service.add_url(
        "person",
        person["gramps_id"],
        "https://www.findagrave.com/memorial/456",
        "",
        "Web Home",
    )
    with pytest.raises(NotFoundError):
        await service.update_url(
            "person", person["gramps_id"], "ancestry.com", url_type="Web Search"
        )
    with pytest.raises(NotFoundError):
        await service.update_url("person", person["gramps_id"], "findagrave", url_type="Web Search")
    urls = fake.store["person"][person["handle"]]["urls"]
    assert all(u["type"] == "Web Home" for u in urls)  # nothing changed


async def test_update_url_can_remove_exactly_one_entry(service, fake):
    person = await _person(service)
    await service.add_url("person", person["gramps_id"], "https://example.com/a", "", "Web Home")
    await service.add_url("person", person["gramps_id"], "https://example.com/b", "", "Web Home")
    result = await service.update_url("person", person["gramps_id"], "example.com/a", remove=True)
    assert result["changed"] is True
    urls = fake.store["person"][person["handle"]]["urls"]
    assert [u["path"] for u in urls] == ["https://example.com/b"]


async def test_source_title_duplicates_leave_out_a_private_source(service):
    """A title can name a person; a private source is kept out of bulk output."""
    await _source(service, "Pembrook family Bible")
    second = await _source(service, "Pembrook family Bible")
    assert (await service.find_duplicates("source_title"))["group_count"] == 1
    await service.set_private("source", second["gramps_id"], True)
    assert (await service.find_duplicates("source_title"))["group_count"] == 0


@pytest.mark.parametrize("name", ["id_ed25519", ".env", "notes.txt", "tree.ged"])
async def test_only_media_files_are_uploaded(service, fake, tmp_path, name):
    """The path comes from the model. A key or a .env must stay on disk."""
    secret = tmp_path / name
    secret.write_text("SECRET=1")
    person = await _person(service)
    for result in (
        await service.add_media(str(secret), "anything"),
        await service.attach_media(person["gramps_id"], "person", file_path=str(secret)),
    ):
        assert result["error"] == "unsupported_media_type"
    assert fake.store["media"] == {}


@pytest.mark.parametrize("name", ["page.jpg", "scan.TIF", "stone.heic", "certificate.pdf"])
async def test_images_and_documents_are_uploaded(service, fake, tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x00image-ish")
    result = await service.add_media(str(path), "A document")
    assert result["created"] is True


async def test_list_unsourced_facts_leaves_out_a_private_event(service):
    """Private is a flag on any record, events included."""
    person = await _person(service)
    await _event(
        service,
        person["gramps_id"],
        EventInput(type="Death", date="1931", citation=CitationInput(source_title="Register")),
    )
    await _event(
        service,
        person["gramps_id"],
        EventInput(type="Residence", date="1900"),
        require_citation=False,
    )
    hidden = await _event(
        service,
        person["gramps_id"],
        EventInput(type="Residence", date="1910"),
        require_citation=False,
    )
    await service.set_private("event", hidden, True)
    result = await service.list_unsourced_facts()
    assert [f["date"] for f in result["facts"]] == ["1900"]


async def test_export_backup_refuses_an_unknown_format(service, tmp_path):
    """The name reaches a URL and a file name, so it is checked first."""
    result = await service.export_backup(str(tmp_path / "x"), extension="../gramps")
    assert result["error"] == "unsupported_format"


async def test_two_default_backups_in_one_second_do_not_collide(service, tmp_path):
    service.config.cache_dir = tmp_path
    first = await service.export_backup()
    second = await service.export_backup()
    assert first["path"] != second["path"]
    assert "error" not in second


async def test_cite_child_link_refuses_to_reuse_an_existing_citation(service, fake):
    """Reusing one would give the link a confidence graded for another claim."""
    father = await _person(service, "John", "Pembrook")
    child = await _person(service, "Hattie", "Pembrook")
    family = await service.add_family(
        father["gramps_id"], None, [child["gramps_id"]], require_citation=False
    )
    existing = await service.add_citation(CitationInput(source_title="Census", page="p. 1"))
    result = await service.cite_child_link(
        family["gramps_id"], child["gramps_id"], CitationInput(citation=existing["gramps_id"])
    )
    assert result["error"] == "citation_reuse_refused"
    assert fake.store["family"][family["handle"]]["child_ref_list"][0].get("citation_list") in (
        None,
        [],
    )
