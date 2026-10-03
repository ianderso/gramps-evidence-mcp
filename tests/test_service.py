"""End-to-end service orchestration tests against the fake gramps-webapi."""

from __future__ import annotations

import pytest

from gramps_evidence_mcp.client import GrampsApiError
from gramps_evidence_mcp.models import CitationInput, Confidence, EventInput, Gender, NameParts
from gramps_evidence_mcp.service import CitationRequiredError, NotFoundError


def _payloads(fake, typ):
    return [p for (m, t, p) in fake.requests if t == typ and m == "POST"]


async def test_add_person_with_cited_birth_roundtrip(service, fake):
    """The worked example: add a person with a birth cited to a certificate."""
    result = await service.add_person(
        NameParts(given="Martha", surname="Ellery"),
        Gender.female,
        birth=EventInput(
            type="Birth",
            date="12 Jan 1899",
            place="Columbus, Ohio, USA",
            citation=CitationInput(
                source_title="Ohio Birth Certificate #12345",
                page="cert. 12345",
                confidence=Confidence.very_high,
            ),
        ),
    )
    assert result["gramps_id"] == "I0001"
    assert result["unsourced_facts"] == []

    # A source, a citation, an event, and the person were created.
    assert len(_payloads(fake, "source")) == 1
    assert len(_payloads(fake, "citation")) == 1
    event_payloads = _payloads(fake, "event")
    assert len(event_payloads) == 1
    # The event carries the citation handle.
    assert len(event_payloads[0]["citation_list"]) == 1
    # The person references the birth event and records its index.
    person = _payloads(fake, "person")[0]
    assert person["birth_ref_index"] == 0
    assert len(person["event_ref_list"]) == 1

    # And we can read it back.
    detail = await service.get_person(result["handle"])
    assert detail["name"] == "Martha Ellery"
    assert detail["gender"] == "female"
    assert detail["events"][0]["type"] == "Birth"
    assert detail["events"][0]["citation_count"] == 1


async def test_citation_required_by_default(service):
    """A fact without a citation is refused when require_citation is True."""
    with pytest.raises(CitationRequiredError):
        await service.add_person(
            NameParts(given="Jane", surname="Doe"),
            Gender.female,
            birth=EventInput(type="Birth", date="1900"),  # no citation
            require_citation=True,
        )


async def test_unsourced_escape_hatch_tags_attribute(service, fake):
    """require_citation=False records the fact but stamps UNSOURCED."""
    result = await service.add_person(
        NameParts(given="Jane", surname="Doe"),
        Gender.female,
        birth=EventInput(type="Birth", date="1900"),
        require_citation=False,
    )
    assert result["unsourced_facts"] == ["birth"]
    event = _payloads(fake, "event")[0]
    attrs = event.get("attribute_list", [])
    assert any(a["type"] == "UNSOURCED" and a["value"] == "true" for a in attrs)
    assert event["citation_list"] == []


async def test_existing_citation_reused_not_recreated(service, fake):
    """Naming a citation attaches it rather than minting a second one."""
    seed = await service.add_citation(CitationInput(source_title="Pension W.12345", page="img 3"))
    before_citations = len(_payloads(fake, "citation"))
    before_sources = len(_payloads(fake, "source"))

    await service.add_person(
        NameParts(given="A", surname="B"),
        Gender.unknown,
        birth=EventInput(
            type="Birth",
            date="1900",
            citation=CitationInput(citation=seed["gramps_id"]),
        ),
    )

    assert len(_payloads(fake, "citation")) == before_citations
    assert len(_payloads(fake, "source")) == before_sources
    assert _payloads(fake, "event")[0]["citation_list"] == [seed["handle"]]


async def test_an_existing_citation_is_accepted_by_handle_or_gramps_id(service, fake):
    """One parameter takes either form, so a caller need not know which it holds."""
    seed = await service.add_citation(CitationInput(source_title="Register", page="img 7"))
    for reference in (seed["handle"], seed["gramps_id"]):
        assert await service.resolve_citation(CitationInput(citation=reference)) == seed["handle"]


async def test_an_unknown_citation_reference_is_refused(service):
    """A handle that names nothing must not be written into a citation_list.

    The collapsed ``citation`` field resolves what it is given, where the old
    ``citation_handle`` was trusted verbatim -- so a typo used to land a
    dangling reference pointing at no object at all.
    """
    with pytest.raises(NotFoundError):
        await service.resolve_citation(CitationInput(citation="h-nonexistent"))


async def test_an_unknown_source_reference_is_refused(service):
    """Same guarantee for the source side."""
    with pytest.raises(NotFoundError):
        await service.resolve_citation(CitationInput(source="S9999"))


async def test_write_refused_when_db_readonly(service, fake):
    """The concurrency analog: a locked/read-only DB -> 403 -> actionable error."""
    fake.write_forbidden = True
    with pytest.raises(GrampsApiError) as exc:
        await service.add_source("A book", None, None, None, None, None)
    assert exc.value.status == 403
    assert "read-only" in exc.value.detail


async def test_place_is_found_or_created_once(service, fake):
    cit = CitationInput(source_title="Src", page="1", confidence=Confidence.normal)
    await service.add_person(
        NameParts(given="P1"),
        Gender.unknown,
        birth=EventInput(type="Birth", place="Springfield", citation=cit),
    )
    await service.add_person(
        NameParts(given="P2"),
        Gender.unknown,
        birth=EventInput(type="Birth", place="Springfield", citation=cit),
    )
    # "Springfield" created exactly once, reused the second time.
    assert len(_payloads(fake, "place")) == 1


async def test_add_family_wires_members(service, fake):
    dad = await service.add_person(NameParts(given="Dad"), Gender.male)
    mom = await service.add_person(NameParts(given="Mom"), Gender.female)
    kid = await service.add_person(NameParts(given="Kid"), Gender.unknown)
    fam = await service.add_family(
        father_ref=dad["handle"],
        mother_ref=mom["handle"],
        child_refs=[kid["handle"]],
        marriage=EventInput(
            type="Marriage",
            date="1920",
            citation=CitationInput(
                source_title="Marriage record", page="p1", confidence=Confidence.high
            ),
        ),
    )
    family = _payloads(fake, "family")[0]
    assert family["father_handle"] == dad["handle"]
    assert family["child_ref_list"][0]["ref"] == kid["handle"]
    assert fam["unsourced_marriage"] is False


async def test_resolve_by_gramps_id(service):
    created = await service.add_person(NameParts(given="Zed"), Gender.unknown)
    # Fetch by gramps_id rather than handle.
    detail = await service.get_person(created["gramps_id"])
    assert detail["handle"] == created["handle"]


async def test_not_found_raises(service):
    with pytest.raises(NotFoundError):
        await service.get_person("does-not-exist")


async def test_list_unsourced_facts(service, fake):
    await service.add_person(
        NameParts(given="Un", surname="Sourced"),
        Gender.unknown,
        birth=EventInput(type="Birth", date="1900"),
        require_citation=False,
    )
    audit = await service.list_unsourced_facts()
    assert audit["unsourced_count"] == 1
    assert audit["facts"][0]["reason"] == "tagged-unsourced"


async def test_cite_event_sources_existing_event(service, fake):
    """An unsourced event can be cited after the fact; the audit then clears it."""
    await service.add_person(
        NameParts(given="Un", surname="Cited"),
        Gender.unknown,
        birth=EventInput(type="Birth", date="1900"),
        require_citation=False,
    )
    # One event exists; it's unsourced to start with.
    event_handle = next(iter(fake.store["event"]))
    assert fake.store["event"][event_handle]["citation_list"] == []
    audit_before = await service.list_unsourced_facts()
    assert audit_before["unsourced_count"] == 1

    result = await service.cite_event(
        event_handle,
        CitationInput(
            source_title="Ohio Birth Certificate #999", page="p1", confidence=Confidence.high
        ),
    )
    assert result["object_type"] == "event"
    # The event now carries a citation handle...
    assert fake.store["event"][event_handle]["citation_list"]
    # ...and no longer shows up as unsourced.
    audit_after = await service.list_unsourced_facts()
    assert audit_after["unsourced_count"] == 0


async def test_update_event_changes_place(service, fake):
    """Updating an event's place finds-or-creates a place and sets the handle."""
    cit = CitationInput(source_title="Src", page="1", confidence=Confidence.normal)
    await service.add_person(
        NameParts(given="Mover"),
        Gender.unknown,
        birth=EventInput(type="Birth", date="1900", citation=cit),
    )
    event_handle = next(iter(fake.store["event"]))
    assert not fake.store["event"][event_handle].get("place")

    result = await service.update_event(event_handle, place="Springfield")
    assert result["object_type"] == "event"
    # A place was created and the event now points at its handle.
    place_handle = fake.store["event"][event_handle]["place"]
    assert place_handle
    assert place_handle in fake.store["place"]
    assert len(_payloads(fake, "place")) == 1


async def test_delete_object_removes_it(service, fake):
    source = await service.add_source("A book", None, None, None, None, None)
    stats_before = await service.db_stats()
    assert stats_before["counts"]["source"] == 1

    result = await service.delete_object("source", source["handle"])
    assert result["object_type"] == "source"
    stats_after = await service.db_stats()
    assert stats_after["counts"]["source"] == 0


async def test_db_stats_counts(service):
    await service.add_person(NameParts(given="One"), Gender.unknown)
    await service.add_person(NameParts(given="Two"), Gender.unknown)
    stats = await service.db_stats()
    assert stats["counts"]["person"] == 2


async def test_tag_object_finds_or_creates_and_lists(service, fake):
    p = await service.add_person(NameParts(given="Tagged"), Gender.unknown)
    r1 = await service.tag_object("person", p["handle"], "Verified", color="#FF8800")
    assert r1["tag"] == "Verified"
    # The person now references the created tag handle.
    tag_handle = next(iter(fake.store["tag"]))
    assert tag_handle in fake.store["person"][p["handle"]]["tag_list"]
    assert fake.store["tag"][tag_handle]["color"] == "#FF8800"

    # Tagging a second object reuses the same tag (no duplicate created).
    q = await service.add_person(NameParts(given="Other"), Gender.unknown)
    await service.tag_object("person", q["handle"], "Verified")
    assert len(fake.store["tag"]) == 1

    tags = await service.list_tags()
    assert tags == [{"handle": tag_handle, "name": "Verified", "color": "#FF8800"}]


async def test_tag_object_no_duplicate_handle_on_reapply(service, fake):
    p = await service.add_person(NameParts(given="Retag"), Gender.unknown)
    await service.tag_object("person", p["handle"], "Star")
    await service.tag_object("person", p["handle"], "Star")
    assert len(fake.store["person"][p["handle"]]["tag_list"]) == 1


async def test_add_attribute_picks_attribute_vs_srcattribute(service, fake):
    """What is sent decides the class; the server keeps no ``_class`` to read back."""
    p = await service.add_person(NameParts(given="Attr"), Gender.unknown)
    await service.add_attribute("person", p["handle"], "Occupation", "Blacksmith")
    method, typ, sent = fake.requests[-1]
    assert (method, typ) == ("PUT", "person")
    assert sent["attribute_list"][0]["_class"] == "Attribute"
    assert fake.store["person"][p["handle"]]["attribute_list"][0]["type"] == "Occupation"

    src = await service.add_source("A book", None, None, None, None, None)
    await service.add_attribute(
        "source", src["handle"], "URL", "https://example.com", allow_new_type=True
    )
    method, typ, sent = fake.requests[-1]
    assert (method, typ) == ("PUT", "source")
    assert sent["attribute_list"][0]["_class"] == "SrcAttribute"


async def test_add_url_supported_and_unsupported(service, fake):
    p = await service.add_person(NameParts(given="Linked"), Gender.unknown)
    ok = await service.add_url("person", p["handle"], "https://findagrave.com/1")
    assert "error" not in ok
    url = fake.store["person"][p["handle"]]["urls"][0]
    assert url["path"] == "https://findagrave.com/1"
    assert url["type"] == "Web Home"

    src = await service.add_source("Book", None, None, None, None, None)
    bad = await service.add_url("source", src["handle"], "https://x")
    assert bad["error"] == "unsupported"
    # Nothing written to the source.
    assert "urls" not in fake.store["source"][src["handle"]]


async def test_set_private_toggles_flag(service, fake):
    p = await service.add_person(NameParts(given="Secret"), Gender.unknown)
    await service.set_private("person", p["handle"], True)
    assert fake.store["person"][p["handle"]]["private"] is True
    await service.set_private("person", p["handle"], False)
    assert fake.store["person"][p["handle"]]["private"] is False


async def test_update_source_sets_only_provided(service, fake):
    src = await service.add_source("Old title", "Old author", None, None, None, None)
    await service.update_source(src["handle"], title="New title", abbrev="NT")
    stored = fake.store["source"][src["handle"]]
    assert stored["title"] == "New title"
    assert stored["abbrev"] == "NT"
    # Author left unchanged.
    assert stored["author"] == "Old author"


async def test_link_repository_dedups(service, fake):
    repo = await service.add_repository("NARA", "Archive", None)
    src = await service.add_source("Census", None, None, None, None, None)
    await service.link_repository(src["handle"], repo["handle"], call_number="T9-123")
    reporefs = fake.store["source"][src["handle"]]["reporef_list"]
    assert len(reporefs) == 1
    assert reporefs[0]["ref"] == repo["handle"]
    assert reporefs[0]["call_number"] == "T9-123"
    # Linking again does not duplicate.
    await service.link_repository(src["handle"], repo["handle"])
    assert len(fake.store["source"][src["handle"]]["reporef_list"]) == 1


async def test_add_event_to_family(service, fake):
    fam = await service.add_family(None, None, None)
    cit = CitationInput(source_title="Divorce record", page="p1", confidence=Confidence.high)
    result = await service.add_event_to_family(
        fam["handle"],
        EventInput(type="Divorce", date="1930", citation=cit),
    )
    assert result["object_type"] == "event"
    assert result["unsourced"] is False
    ev_refs = fake.store["family"][fam["handle"]]["event_ref_list"]
    assert ev_refs[-1]["role"] == "Family"
    assert ev_refs[-1]["ref"] == result["event_handle"]


async def test_add_child_to_family_links_both_ways(service, fake):
    fam = await service.add_family(None, None, None)
    kid = await service.add_person(NameParts(given="Kid"), Gender.unknown)
    await service.add_child_to_family(fam["handle"], kid["handle"], frel="Adopted")
    child_refs = fake.store["family"][fam["handle"]]["child_ref_list"]
    assert child_refs[0]["ref"] == kid["handle"]
    assert child_refs[0]["frel"] == "Adopted"
    # The child's parent_family_list points back at the family.
    assert fam["handle"] in fake.store["person"][kid["handle"]]["parent_family_list"]
    # Re-adding does not duplicate.
    await service.add_child_to_family(fam["handle"], kid["handle"])
    assert len(fake.store["family"][fam["handle"]]["child_ref_list"]) == 1


async def test_add_alternate_name(service, fake):
    p = await service.add_person(NameParts(given="Mary", surname="Smith"), Gender.female)
    await service.add_alternate_name(
        p["handle"], NameParts(given="Mary", surname="Jones"), name_type="Married Name"
    )
    alts = fake.store["person"][p["handle"]]["alternate_names"]
    assert alts[0]["type"] == "Married Name"
    assert alts[0]["surname_list"][0]["surname"] == "Jones"


async def test_get_source_shape(service, fake):
    repo = await service.add_repository("NARA", "Archive", None)
    src = await service.add_source("Census", "Gov", "1900", None, None, None)
    await service.link_repository(src["handle"], repo["handle"], call_number="T9")
    await service.add_attribute("source", src["handle"], "URL", "https://x", allow_new_type=True)
    detail = await service.get_source(src["handle"])
    assert detail["title"] == "Census"
    assert detail["author"] == "Gov"
    assert detail["repositories"][0]["ref"] == repo["handle"]
    assert detail["repositories"][0]["call_number"] == "T9"
    assert detail["attributes"][0]["type"] == "URL"
    assert detail["citation_count"] == 0


async def test_get_repository_shape(service, fake):
    repo = await service.add_repository("Local Library", "Library", "https://lib.example")
    detail = await service.get_repository(repo["handle"])
    assert detail["name"] == "Local Library"
    assert detail["type"] == "Library"
    assert detail["urls"][0]["path"] == "https://lib.example"
    assert detail["address_count"] == 0


async def test_get_event_shape(service, fake):
    cit = CitationInput(source_title="Cert", page="p1", confidence=Confidence.high)
    await service.add_person(
        NameParts(given="Ev"),
        Gender.unknown,
        birth=EventInput(
            type="Birth", date="1900", place="Springfield", description="at home", citation=cit
        ),
    )
    event_handle = next(iter(fake.store["event"]))
    detail = await service.get_event(event_handle)
    assert detail["type"] == "Birth"
    assert detail["date"] == "1900"
    assert detail["description"] == "at home"
    assert detail["place_handle"]
    assert detail["citation_count"] == 1
