"""A media object holds the file sent, or does not exist (TOOL-REQUESTS #31).

``POST /api/media/`` takes the file itself as its body. A Media object sent
there as JSON is stored as a ``.json`` file, under a handle the server makes
(docs/PITFALLS.md section 32). The tools did exactly that, and sent the image
afterwards with ``PUT .../file``; a connection lost between the two left a
media object holding the request body -- path ``<md5>.json``, mime
``application/json``, no description and no reference. Now the file goes in
the one request that creates the object, a failed request is looked up by the
file's checksum, and an object that does not hold the file is removed again.
"""

from __future__ import annotations

import hashlib

PAGE = b"\xff\xd8\xff\xe0 a yearbook page, 1931"
MD5 = hashlib.md5(PAGE).hexdigest()  # noqa: S324 - the server's own checksum


async def _attach(tools, tmp_path, **extra):
    path = tmp_path / "page.jpg"
    path.write_bytes(PAGE)
    source = await tools("add_source", title="High School Yearbook, 1931")
    out = await tools(
        "attach_media",
        target=source["gramps_id"],
        target_type="source",
        file_path=str(path),
        description="Yearbook page",
        **extra,
    )
    return source, out


def _media(tools) -> list[dict]:
    return list(tools.fake.store["media"].values())


def _source_media(tools, source) -> list[str]:
    stored = tools.fake.store["source"][source["handle"]]
    return [r["ref"] for r in stored.get("media_list") or []]


async def test_the_file_goes_in_the_request_that_creates_the_object(tools, tmp_path):
    source, out = await _attach(tools, tmp_path)
    assert out["verified"] is True, out
    assert tools.fake.media_posts == ["image/jpeg"], "never a JSON body to /api/media/"
    [media] = _media(tools)
    assert (media["checksum"], media["mime"]) == (MD5, "image/jpeg")
    assert media["desc"] == "Yearbook page"
    assert _source_media(tools, source) == [media["handle"]]


async def test_a_connection_lost_after_the_object_landed_finds_it_and_goes_on(tools, tmp_path):
    tools.fake.upload_drop = "after"
    source, out = await _attach(tools, tmp_path)
    assert out["verified"] is True, out
    assert "ReadError" in out["message"]
    [media] = _media(tools)
    assert (media["checksum"], media["mime"]) == (MD5, "image/jpeg")
    assert _source_media(tools, source) == [media["handle"]]


async def test_a_connection_lost_before_anything_landed_says_nothing_was_written(tools, tmp_path):
    tools.fake.upload_drop = "before"
    source, out = await _attach(tools, tmp_path)
    assert out["error"] == "connection", out
    assert out["written"] is False
    assert _media(tools) == []
    assert _source_media(tools, source) == []

    tools.fake.upload_drop = None
    path = tmp_path / "page.jpg"
    retry = await tools(
        "attach_media", target=source["gramps_id"], target_type="source", file_path=str(path)
    )
    assert retry["verified"] is True, retry


async def test_an_object_that_does_not_hold_the_file_is_removed(tools, tmp_path):
    tools.fake.media_mime_stored = "application/json"
    source, out = await _attach(tools, tmp_path)
    assert out["error"] == "upload_mismatch", out
    assert _media(tools) == []
    assert _source_media(tools, source) == []


async def test_a_failed_attach_names_the_media_object_it_left(tools, tmp_path):
    """The upload is kept: the file is on the server whatever happens, and a
    retry with the same file finds it by checksum, description and all."""
    tools.fake.put_error = 500
    source, out = await _attach(tools, tmp_path)
    assert out["error"] == "not_attached", out
    assert out["written"] is False
    [media] = _media(tools)
    assert out["gramps_id"] == media["gramps_id"]
    assert media["gramps_id"] in out["message"]

    tools.fake.put_error = None
    path = tmp_path / "page.jpg"
    retry = await tools(
        "attach_media",
        target=source["gramps_id"],
        target_type="source",
        file_path=str(path),
        description="Yearbook page",
    )
    assert retry["verified"] is True and retry["media_created"] is False, retry
    assert _media(tools)[0]["desc"] == "Yearbook page"
    assert _source_media(tools, source) == [media["handle"]]
