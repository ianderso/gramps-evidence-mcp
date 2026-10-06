"""GrampsQL over list fields, and errors that say what went wrong.

``~`` on a list compares the list itself: ``urls ~ "http"`` asks whether the
string is one of the URL objects, so it returned zero rows with HTTP 200 on a
tree where 298 people had one (TOOL-REQUESTS #26). The items are reached with
``.any.``, and a handle is followed with ``get_<type>``. And an OR of two such
conditions over 6,092 citations took longer than the 30 s request timeout;
httpx's timeout carries no message, so the caller got ``{"error":
"unexpected", "message": ""}`` and nothing to correct (TOOL-REQUESTS #27).
"""

from __future__ import annotations

import httpx
import pytest

from gramps_evidence_mcp.client import GQL_TIMEOUT, _detail
from gramps_evidence_mcp.server import _error


async def _source(tools, title: str, **fields) -> dict:
    """A source with whatever lists a test needs, written straight to the fake."""
    out = await tools("add_source", title=title)
    stored = next(s for s in tools.fake.store["source"].values() if s["handle"] == out["handle"])
    stored.update(fields)
    return out


async def _note(tools, text: str) -> str:
    return (await tools("add_note", text=text))["handle"]


# --------------------------------------------------------------------------- #
# #26: a list is searched through its items
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("gql", "suggestion"),
    [
        ('urls ~ "http"', 'urls.any.path ~ "http"'),
        ('attribute_list ~ "blm.gov"', 'attribute_list.any.value ~ "blm.gov"'),
        ("attribute_list ~ 'blm.gov'", 'attribute_list.any.value ~ "blm.gov"'),
        ('primary_name.surname_list ~ "Ray"', 'primary_name.surname_list.any.surname ~ "Ray"'),
        ('urls.any ~ "http"', 'urls.any.path ~ "http"'),
        ('note_list ~ "blm.gov"', 'note_list.any.get_note.text.string ~ "blm.gov"'),
        ('citation_list = "p. 4"', 'citation_list.any.get_citation.page ~ "p. 4"'),
    ],
)
async def test_tilde_on_a_whole_list_is_refused_with_the_query_that_works(tools, gql, suggestion):
    out = await tools("query_objects", object_type="person", gql=gql)
    assert out["error"] == "gql_list_field"
    assert suggestion in out["message"]
    assert ".length > 0" in out["message"]


async def test_a_negated_comparison_on_a_whole_list_is_refused_too(tools):
    """``media_list !~ "x"`` matched every one of 1,538 sources on a real tree."""
    out = await tools("query_objects", object_type="source", gql='media_list !~ "x"')
    assert out["error"] == "gql_list_field"
    assert "every record" in out["message"]


async def test_the_reported_or_query_is_refused_and_its_rewrite_answers(tools):
    """The call from TOOL-REQUESTS #27, and what it should have been."""
    fake = tools.fake
    note = await _note(tools, "Patent at https://glorecords.blm.gov/details/x")
    by_note = await tools("add_citation", citation={"source_title": "Patent", "page": "acc. 1"})
    by_attr = await tools("add_citation", citation={"source_title": "Tract", "page": "p. 2"})
    await tools("add_citation", citation={"source_title": "Census", "page": "p. 3"})
    fake.store["citation"][by_note["handle"]]["note_list"] = [note]
    fake.store["citation"][by_attr["handle"]]["attribute_list"] = [
        {"private": False, "type": "URL", "value": "https://glorecords.blm.gov/"}
    ]

    refused = await tools(
        "query_objects",
        object_type="citation",
        gql='attribute_list ~ "blm.gov" OR note_list ~ "blm.gov"',
    )
    assert refused["error"] == "gql_list_field"
    assert refused["message"], "a refusal says what to do"

    found = await tools(
        "query_objects",
        object_type="citation",
        gql='attribute_list.any.value ~ "blm.gov" OR '
        'note_list.any.get_note.text.string ~ "blm.gov"',
        keys="gramps_id",
    )
    assert "error" not in found, found
    assert sorted(r["gramps_id"] for r in found["results"]) == sorted(
        [by_note["gramps_id"], by_attr["gramps_id"]]
    )


async def test_any_reaches_url_paths(tools):
    await _source(
        tools, "With a link", urls=[{"desc": "", "path": "https://blm.gov/x", "type": "Web Home"}]
    )
    await _source(tools, "Without")
    out = await tools(
        "query_objects", object_type="source", gql='urls.any.path ~ "BLM.GOV"', keys="title"
    )
    assert [r["title"] for r in out["results"]] == ["With a link"], "matched ignoring case"


async def test_a_handle_in_a_handle_list_is_still_a_membership_test(tools):
    """``tag_list ~ "<handle>"`` is meant, and works."""
    tagged = await tools("add_source", title="Tagged")
    await tools("add_source", title="Untagged")
    await tools("tag_object", object_type="source", target=tagged["gramps_id"], tag="Land")
    [tag] = list(tools.fake.store["tag"])
    out = await tools(
        "query_objects", object_type="source", gql=f'tag_list ~ "{tag}"', keys="title"
    )
    assert [r["title"] for r in out["results"]] == ["Tagged"]
    assert "warning" not in out


async def test_a_handle_test_that_finds_nothing_says_what_it_asked(tools):
    """``note_list ~ census`` is a handle test; zero rows must not read as "no notes say census"."""
    await _source(tools, "Census", note_list=[await _note(tools, "1880 census entry")])
    out = await tools("query_objects", object_type="source", gql="note_list ~ census")
    assert out["total_matched"] == 0
    assert 'note_list.any.get_note.text.string ~ "census"' in out["warning"]


async def test_text_inside_a_value_is_not_read_as_a_field(tools):
    await _source(tools, "Odd")
    out = await tools("query_objects", object_type="source", gql='title ~ "urls ~ x"')
    assert "error" not in out, out


@pytest.mark.parametrize(
    "gql",
    [
        "media_list.length = 0",
        'media_list.any.citation_list ~ "h000001"',
        "urls",
        'child_ref_list[0].ref = "h1"',
        'confidence >= 3 AND page = ""',
    ],
)
async def test_list_forms_that_work_are_let_through(tools, gql):
    out = await tools("query_objects", object_type="source", gql=gql)
    assert "error" not in out, out


# --------------------------------------------------------------------------- #
# #27: an error always says what happened
# --------------------------------------------------------------------------- #
async def test_a_timeout_is_reported_as_one_with_a_message(tools, monkeypatch):
    async def slow(*args, **kwargs):
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(tools.service.client._http, "request", slow)
    out = await tools("query_objects", object_type="citation", gql='page ~ "x"')
    assert out["error"] == "timeout"
    assert "ReadTimeout" in out["message"]
    assert "query_records" in out["message"]


async def test_a_grampsql_list_is_allowed_more_time_than_other_requests(tools, monkeypatch):
    seen = []
    real = tools.service.client._http.request

    async def recording(method, url, **kwargs):
        if url == "/api/citations/":
            seen.append(("gql" in (kwargs.get("params") or {}), kwargs.get("timeout")))
        return await real(method, url, **kwargs)

    monkeypatch.setattr(tools.service.client._http, "request", recording)
    await tools("query_objects", object_type="citation", gql='page ~ "x"')
    await tools("query_objects", object_type="citation", keys="gramps_id")
    assert seen == [(True, GQL_TIMEOUT), (False, None)], "others keep the configured timeout"


def test_no_error_envelope_has_an_empty_message():
    for exc in (
        RuntimeError(),
        httpx.ReadTimeout(""),
        httpx.ConnectError(""),
        httpx.RemoteProtocolError(""),
    ):
        out = _error(exc)
        assert out["message"].strip(), (type(exc).__name__, out)
    assert _error(httpx.ConnectError(""))["error"] == "connection"
    assert _error(RuntimeError())["message"] == "RuntimeError, with no message."


def test_the_servers_own_message_is_what_an_api_error_says():
    """gramps-webapi nests it: {"error": {"code": 422, "message": "..."}}."""
    request = httpx.Request("GET", "http://testserver/api/people/")
    parse = httpx.Response(
        422,
        json={"error": {"code": 422, "message": "Expected end of text, found '['"}},
        request=request,
    )
    assert _detail(parse) == "Expected end of text, found '['"
    assert _detail(httpx.Response(500, content=b"", request=request)) == (
        "HTTP 500 with no explanation in the body."
    )
    assert _detail(httpx.Response(404, json={"message": "no such task"}, request=request)) == (
        "no such task"
    )
