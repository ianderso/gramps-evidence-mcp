"""Record what gramps-webapi fills in when it stores an object.

The server completes every object it is sent: each field the object's class
has and the request left out is stored with its default, nested objects
included. The fake in ``tests/conftest.py`` completes what it is sent from the
file this writes, so the unit tests see the shapes a real server returns.
``test_contract_live.py`` checks the file against the server, so it cannot
drift unnoticed. To regenerate it, against an empty throwaway tree::

    export $(tests/live/start_server.sh 3.21.1)
    uv run python -m tests.live.capture_defaults

It also records Gramps' standard type names (``GET /api/types/``, under
``default``), which the tools match a caller's type names against
(docs/PITFALLS.md section 26), so the fake refuses and accepts the same names.

Each request below sends, somewhere, one instance of every class with nothing
but its ``_class`` (and the ``ref`` a reference needs): what comes back for it
is that class's default. Every nested object sent with a ``_class`` also
records which class the field holds, which is how the fake knows to complete
the objects inside a list or under a key. Last, each top-level field is sent
as null, to record whether the server stores something else or refuses it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from gramps_evidence_mcp.client import GrampsApiError, GrampsWebClient

from .harness import LIVE_PASSWORD, LIVE_URL, LIVE_USERNAME, live_version, session_tokens, wipe

OUT = Path(__file__).parent.parent / "fixtures" / "server_defaults.json"

TOP_LEVEL = (
    ("person", "Person"),
    ("family", "Family"),
    ("event", "Event"),
    ("place", "Place"),
    ("source", "Source"),
    ("citation", "Citation"),
    ("repository", "Repository"),
    ("note", "Note"),
    ("tag", "Tag"),
)

#: Assigned by the server for each object, never a default.
ASSIGNED = {"handle", "gramps_id", "change"}


def _stored(value: Any) -> Any:
    """Drop the ``year`` the server adds to every date it serves.

    It is stored only when a client writes it back (docs/PITFALLS.md section
    24), so it is no default; the fake adds it on read, as the server does.
    """
    if isinstance(value, dict):
        return {k: _stored(v) for k, v in value.items() if not (k == "year" and "dateval" in value)}
    if isinstance(value, list):
        return [_stored(v) for v in value]
    return value


def _c(cls: str, **fields: Any) -> dict:
    return {"_class": cls, **fields}


def rich(refs: dict[str, str]) -> dict[str, dict]:
    """One object per type, holding a bare instance of every nested class."""
    date = _c("Date")
    return {
        "person": _c(
            "Person",
            primary_name=_c("Name", surname_list=[_c("Surname")], date=date),
            alternate_names=[_c("Name")],
            event_ref_list=[_c("EventRef", ref=refs["event"])],
            person_ref_list=[_c("PersonRef", ref=refs["person"])],
            attribute_list=[_c("Attribute")],
            address_list=[_c("Address"), _c("Address", date=date)],
            urls=[_c("Url")],
            lds_ord_list=[_c("LdsOrd"), _c("LdsOrd", date=date)],
        ),
        "family": _c(
            "Family",
            child_ref_list=[_c("ChildRef", ref=refs["person"])],
            event_ref_list=[_c("EventRef", ref=refs["event"])],
            attribute_list=[_c("Attribute")],
        ),
        "event": _c("Event", date=date, attribute_list=[_c("Attribute")]),
        "place": _c(
            "Place",
            name=_c("PlaceName"),
            alt_names=[_c("PlaceName"), _c("PlaceName", date=date)],
            placeref_list=[
                _c("PlaceRef", ref=refs["place"]),
                _c("PlaceRef", ref=refs["place"], date=date),
            ],
            urls=[_c("Url")],
        ),
        "source": _c(
            "Source",
            reporef_list=[_c("RepoRef", ref=refs["repository"])],
            attribute_list=[_c("SrcAttribute")],
        ),
        "citation": _c("Citation", date=date, attribute_list=[_c("SrcAttribute")]),
        "repository": _c("Repository", address_list=[_c("Address")], urls=[_c("Url")]),
        "note": _c("Note", text=_c("StyledText")),
    }


def _learn(sent: Any, got: Any, defaults: dict, nested: dict) -> None:
    """Walk a request and its stored result together, recording both maps."""
    if isinstance(sent, dict) and isinstance(got, dict):
        cls = sent.get("_class")
        if cls and set(sent) <= {"_class", "ref"} and cls not in defaults:
            drop = ASSIGNED | set(sent)
            defaults[cls] = _stored({k: v for k, v in got.items() if k not in drop})
        for key, value in sent.items():
            child = value
            if isinstance(value, list) and value and isinstance(value[0], dict):
                child = value[0]
            if cls and isinstance(child, dict) and "_class" in child:
                kind = [child["_class"]] if isinstance(value, list) else child["_class"]
                nested.setdefault(cls, {})[key] = kind
            _learn(value, got.get(key), defaults, nested)
    elif isinstance(sent, list) and isinstance(got, list):
        for s, g in zip(sent, got, strict=False):
            _learn(s, g, defaults, nested)


async def capture() -> dict:
    client = GrampsWebClient(LIVE_URL, LIVE_USERNAME, LIVE_PASSWORD)
    client._access, client._refresh = session_tokens()
    defaults: dict[str, dict] = {}
    nested: dict[str, dict] = {}
    try:
        types = (await client.types())["default"]
        refs = {}
        for object_type, cls in TOP_LEVEL:
            sent = _c(cls)
            made = await client.create_object(object_type, sent)
            refs[object_type] = made["handle"]
            _learn(sent, await client.get_object(object_type, made["handle"]), defaults, nested)
        for object_type, sent in rich(refs).items():
            made = await client.create_object(object_type, sent)
            _learn(sent, await client.get_object(object_type, made["handle"]), defaults, nested)
        nulls = await _nulls(client, defaults)
    finally:
        await wipe(client)
        await client.aclose()
    version = ".".join(map(str, live_version()))
    return {
        "_comment": (
            f"What gramps-webapi {version} stores for a field a request leaves out, by "
            "class, which class each nested field holds, and its standard type names. "
            "Written by tests/live/capture_defaults.py; checked by "
            "tests/live/test_contract_live.py."
        ),
        "defaults": dict(sorted(defaults.items())),
        "nested": {k: dict(sorted(v.items())) for k, v in sorted(nested.items())},
        "nulls": nulls,
        "types": dict(sorted(types.items())),
    }


async def _nulls(client: GrampsWebClient, defaults: dict) -> dict:
    """What the server does with each top-level field sent as null.

    It differs field by field: a family's parent handles are stored as "", an
    event's place stays null, and a string field -- or a citation's source --
    is refused with 400. Recorded as the stored value, or "refused".
    """
    out: dict[str, dict] = {}
    for object_type, cls in TOP_LEVEL:
        for key in sorted(defaults[cls]):
            try:
                made = await client.create_object(object_type, {"_class": cls, key: None})
            except GrampsApiError as exc:
                assert exc.status == 400, exc
                out.setdefault(cls, {})[key] = "refused"
                continue
            stored = _stored((await client.get_object(object_type, made["handle"])).get(key))
            if stored is not None:
                out.setdefault(cls, {})[key] = stored
            await client.delete_object(object_type, made["handle"])
    return out


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    OUT.write_text(json.dumps(asyncio.run(capture()), indent=2, sort_keys=False) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
