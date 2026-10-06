"""``ocr_media`` as a router: which engine reads which document, and at what cost.

Every route is driven through the tool as a client calls it, against the fake
gramps-webapi and fakes of the outside services, answered through respx:

- Transkribus' processing API and its READ-COOP login, from the shapes in
  ``tests/fixtures/transkribus``. Those are recorded from the published
  Metagrapho v1 OpenAPI document (``transkribus.eu/processing/v1/openapi.json``,
  1.13.1, read on 2026-10-06): its examples for the job created, the job
  finished with regions and lines, and the 429 error; the token is the shape
  READ-COOP's Keycloak realm documents; the PAGE XML follows the 2013-07-15
  schema the API names. The text in them is invented. No Transkribus account
  was used: this path has not been exercised against the live service.
- The Library of Congress's page JSON and full-text service, from a page
  fetched on 2026-10-06 and cut down, with invented text
  (``tests/fixtures/archives``).
- The Internet Archive's metadata, page-number, hOCR page-index and search
  text files, built here in the shapes read from a live item on 2026-10-06.

Images and PDFs are drawn with Pillow; no record from a real tree is used.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from PIL import Image, ImageDraw

from gramps_evidence_mcp import client as client_module
from gramps_evidence_mcp import ocr

FIXTURES = Path(__file__).parent / "fixtures"
TK = FIXTURES / "transkribus"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """Polls happen at once; a job still running is handed back after one look."""

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(ocr, "_sleep", no_sleep)
    monkeypatch.setattr(client_module, "TASK_POLL_SECONDS", 0)


def _page(size=(1200, 1600), lines=("Know all men by these presents",), color="RGB") -> Image:
    img = Image.new(color, size, "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((60, 80 + 40 * i), line, fill="black")
    return img


def _jpeg(img: Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _scan_pdf(*pages: Image) -> bytes:
    """A scanner's PDF: one JPEG per page, no text layer."""
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=list(pages[1:]))
    return buf.getvalue()


def _text_pdf(text: str) -> bytes:
    """A born-digital PDF: a text layer and no image."""
    stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    trailer = b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
    out.write(trailer % (len(objects) + 1, xref))
    return out.getvalue()


async def _media(tools, tmp_path, name: str, content: bytes, description="A document") -> str:
    path = tmp_path / name
    path.write_bytes(content)
    made = await tools("add_media", file_path=str(path), description=description)
    assert made.get("created"), made
    return made["gramps_id"]


def _handle(tools, gid: str) -> str:
    return next(h for h, m in tools.fake.store["media"].items() if m["gramps_id"] == gid)


async def _call_raw(tools, **arguments):
    """The tool's content blocks, as a client receives them."""
    from gramps_evidence_mcp import server

    result = await server.mcp.call_tool("ocr_media", arguments)
    return result.content


def _image_block(blocks) -> Image:
    images = [b for b in blocks if getattr(b, "type", None) == "image"]
    assert len(images) == 1, blocks
    assert images[0].mime_type == "image/jpeg"
    return Image.open(io.BytesIO(base64.b64decode(images[0].data)))


class FakeTranskribus:
    """Transkribus' processing API and READ-COOP's token endpoint, answered in place."""

    def __init__(self, router, statuses=("RUNNING", "FINISHED")):
        self.token_forms: list[dict] = []
        self.submitted: list[dict] = []
        self.polls: list[str] = []
        self.page_requests = 0
        self.statuses = list(statuses)
        self.submit_error: tuple[int, dict] | None = None
        self.next_id = 47725
        router.route(host="account.readcoop.eu").mock(side_effect=self._token)
        router.route(host="transkribus.eu").mock(side_effect=self._api)

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_forms.append(dict(parse_qsl(request.content.decode())))
        return httpx.Response(200, json=json.loads((TK / "token.json").read_text(encoding="utf-8")))

    def _api(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert path.startswith("/processing/v1/processes"), path
        assert request.headers["authorization"] == "Bearer eyJ-invented-access-token"
        if request.method == "POST":
            if self.submit_error:
                status, body = self.submit_error
                return httpx.Response(status, json=body)
            self.submitted.append(json.loads(request.content))
            created = json.loads((TK / "process_created.json").read_text(encoding="utf-8"))
            created["processId"] = self.next_id
            self.next_id += 1
            return httpx.Response(200, json=created)
        process_id = path.rsplit("/processes/", 1)[1]
        if process_id.endswith("/page"):
            self.page_requests += 1
            return httpx.Response(
                200,
                text=(TK / "page.xml").read_text(encoding="utf-8"),
                headers={"content-type": "application/xml"},
            )
        self.polls.append(process_id)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        name = "process_finished.json" if status == "FINISHED" else "process_running.json"
        body = json.loads((TK / name).read_text(encoding="utf-8"))
        body["processId"] = int(process_id)
        body["status"] = status
        return httpx.Response(200, json=body)


def _configure_transkribus(tools, budget=0):
    cfg = tools.service.config
    cfg.transkribus_username = "reader@example.org"
    cfg.transkribus_password = "pw"
    cfg.transkribus_page_budget = budget


# --------------------------------------------------------------------------- #
# Print
# --------------------------------------------------------------------------- #
async def test_print_is_read_by_tesseract_through_gramps_web(tools, tmp_path):
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid)
    assert out["route"] == "print"
    assert out["engine"] == "tesseract"
    assert out["text"] == "OCRED TEXT"
    assert out["provenance"]["engine"] == "tesseract"
    assert out["provenance"]["lang"] == "eng"
    assert out["provenance"]["date"]
    assert "never the evidence" in out["caveat"]
    assert tools.fake.ocr_requests == [{"lang": "eng", "format": "string"}]


async def test_a_queued_ocr_task_is_waited_for(tools, tmp_path):
    """Seen on a live tree: a server with a task queue answers 202 and a task.

    The tool used to hand back ``{"task": {...}}`` as the text, there
    (gramps-webapi 3.21.1 with Celery), so it never returned a word.
    """
    tools.fake.ocr_queued = True
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid)
    assert out["text"] == "OCRED TEXT"


async def test_text_that_looks_like_a_number_stays_text(tools, tmp_path):
    """The server serves Tesseract's string as text/html; parsing it as JSON
    turned a page reading "1850" into the integer 1850."""
    tools.fake.ocr_text = "1850"
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    assert (await tools("ocr_media", media=gid))["text"] == "1850"


async def test_two_letter_codes_reach_tesseract_as_its_own(tools, tmp_path):
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    await tools("ocr_media", media=gid, lang="en+de")
    assert tools.fake.ocr_requests[-1]["lang"] == "eng+deu"


async def test_without_tesseract_the_tool_says_so(tools, tmp_path):
    tools.fake.metadata["server"]["ocr"] = False
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid)
    assert out["error"] == "tesseract_unavailable"
    assert "engine='vision'" in out["message"]
    assert tools.fake.ocr_requests == []


async def test_a_501_from_the_ocr_endpoint_is_tesseract_unavailable(tools, tmp_path):
    del tools.fake.metadata["server"]  # an older server that does not say
    tools.fake.ocr_unavailable = "Tesseract is not installed"
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid)
    assert out["error"] == "tesseract_unavailable"
    assert "Tesseract is not installed" in out["message"]


async def test_a_language_tesseract_lacks_is_named_with_those_it_has(tools, tmp_path):
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, lang="pol")
    assert out["error"] == "language_not_installed"
    assert "pol" in out["message"] and "eng" in out["message"]


async def test_a_transcript_note_the_media_carries_is_used_first(tools, tmp_path):
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    note = await tools(
        "add_note",
        target_type="media",
        target=gid,
        text="Know all men by these presents",
        note_type="Transcript",
    )
    out = await tools("ocr_media", media=gid)
    assert out["engine"] == "existing"
    assert out["text"] == "Know all men by these presents"
    assert out["source_note"] == note["gramps_id"]
    assert out["existing_transcripts"][0]["note"] == note["gramps_id"]
    assert tools.fake.ocr_requests == []


LETTER = (
    "Dear brother, we arrived at Cedar Flat on the fourth and found the farm in good order. " * 4
)


async def test_a_pdf_text_layer_is_existing_text(tools, tmp_path):
    gid = await _media(tools, tmp_path, "letter.pdf", _text_pdf(LETTER))
    out = await tools("ocr_media", media=gid)
    assert out["engine"] == "existing"
    assert out["provenance"]["engine"] == "pdf_text_layer"
    assert "Cedar Flat" in out["text"]
    assert (out["page"], out["pages"]) == (1, 1)


async def test_a_caption_in_a_pdf_text_layer_is_not_taken_for_the_page(tools, tmp_path):
    """Seen on a live tree: a newspaper site's PDF whose text layer was only its
    URL and the paper's title, over a scan of the page."""
    gid = await _media(tools, tmp_path, "clipping.pdf", _text_pdf("Courier, 1 May 1908, p. 3"))
    out = await tools("ocr_media", media=gid)
    assert out["error"] == "tesseract_cannot_read"
    assert "only 25 characters" in out["warnings"][0]


async def test_a_scanned_pdf_is_not_sent_to_tesseract(tools, tmp_path):
    """Gramps Web's OCR reads image files only; a PDF comes back as ``{}``."""
    gid = await _media(tools, tmp_path, "scan.pdf", _scan_pdf(_page()))
    out = await tools("ocr_media", media=gid)
    assert out["error"] == "tesseract_cannot_read"
    assert "engine='vision'" in out["message"]
    assert tools.fake.ocr_requests == []


async def _source_naming(tools, gid: str, url: str) -> None:
    source = await tools("add_source", title="The Cedar Flat weekly courier")
    added = await tools(
        "add_attribute",
        object_type="source",
        target=source["gramps_id"],
        name="URL",
        value=url,
        allow_new_type=True,
    )
    assert "error" not in added, added
    attached = await tools(
        "attach_media", target=source["gramps_id"], target_type="source", media_ref=gid
    )
    assert "error" not in attached, attached


async def test_library_of_congress_ocr_for_a_page_its_source_names(tools, tmp_path):
    gid = await _media(tools, tmp_path, "courier.jpg", _jpeg(_page()))
    await _source_naming(
        tools, gid, "https://www.loc.gov/resource/sn99999999/1908-05-01/ed-1/?sp=1&st=text"
    )
    asked = []

    def loc(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        name = "loc_fulltext.json"
        if request.url.host == "www.loc.gov":
            assert request.url.path == "/resource/sn99999999/1908-05-01/ed-1/"
            assert dict(request.url.params) == {"sp": "1", "fo": "json"}
            name = "loc_resource.json"
        return httpx.Response(
            200, json=json.loads((FIXTURES / "archives" / name).read_text(encoding="utf-8"))
        )

    tools.fake.router.route(host__regex=r"(www|tile)\.loc\.gov").mock(side_effect=loc)
    out = await tools("ocr_media", media=gid)
    assert out["engine"] == "existing", out
    assert out["provenance"]["engine"] == "loc"
    assert "Mercy Ashbee of Cedar Flat" in out["text"]
    assert tools.fake.ocr_requests == []
    assert asked[1].startswith("https://tile.loc.gov/text-services/")


def _ia_files(identifier: str, leaves: list[str]) -> dict:
    """An item's OCR files, as archive.org serves them: leaf 0 is a cover never shown."""
    text, index = "", []
    for leaf_text in ["(cover)", *leaves]:
        start = len(text)
        text += leaf_text + "\n"
        index.append([start, len(text), 0, 0])
    numbers = {
        "pages": [
            {"leafNum": n, "pageNumber": str(40 + n), "confidence": 90}
            for n in range(1, len(leaves) + 1)
        ]
    }
    return {
        f"/metadata/{identifier}": httpx.Response(
            200,
            json={
                "files": [
                    {"name": f"{identifier}_hocr_pageindex.json.gz", "format": "OCR Page Index"},
                    {"name": f"{identifier}_hocr_searchtext.txt.gz", "format": "OCR Search Text"},
                    {"name": f"{identifier}_page_numbers.json", "format": "Page Numbers JSON"},
                ]
            },
        ),
        f"/download/{identifier}/{identifier}_hocr_pageindex.json.gz": httpx.Response(
            200, content=gzip.compress(json.dumps(index).encode())
        ),
        f"/download/{identifier}/{identifier}_hocr_searchtext.txt.gz": httpx.Response(
            200, content=gzip.compress(text.encode())
        ),
        f"/download/{identifier}/{identifier}_page_numbers.json": httpx.Response(200, json=numbers),
    }


async def test_internet_archive_ocr_for_the_leaf_a_url_names(tools, tmp_path):
    """``/page/n1`` is the second leaf shown, which is scanned leaf 2: the cover
    the viewer hides is leaf 0."""
    gid = await _media(tools, tmp_path, "history.jpg", _jpeg(_page()))
    await _source_naming(tools, gid, "https://archive.org/details/cedarflat00inve/page/n1/mode/1up")
    files = _ia_files("cedarflat00inve", ["page forty-one", "page forty-two", "page forty-three"])
    tools.fake.router.route(host="archive.org").mock(
        side_effect=lambda request: files[request.url.path]
    )
    out = await tools("ocr_media", media=gid)
    assert out["provenance"]["engine"] == "internet_archive", out
    assert out["text"] == "page forty-two"
    assert out["provenance"]["printed_page"] == "42"


async def test_an_archive_url_naming_no_page_is_passed_over(tools, tmp_path):
    gid = await _media(tools, tmp_path, "history.jpg", _jpeg(_page()))
    await _source_naming(tools, gid, "https://archive.org/details/cedarflat00inve")
    files = _ia_files("cedarflat00inve", ["one", "two"])
    tools.fake.router.route(host="archive.org").mock(
        side_effect=lambda request: files[request.url.path]
    )
    out = await tools("ocr_media", media=gid)
    assert out["engine"] == "tesseract"
    assert "not a page" in out["looked_at"][0]["reason"]


async def test_existing_only_says_where_it_looked(tools, tmp_path):
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, engine="existing")
    assert out["engine"] is None and out["text"] is None
    assert "No Transcript note is attached" in out["message"]
    assert "PDF" not in out["message"]
    assert tools.fake.ocr_requests == []


# --------------------------------------------------------------------------- #
# English handwriting: the image, for the caller to read
# --------------------------------------------------------------------------- #
async def test_english_handwriting_returns_the_image_and_a_diplomatic_instruction(tools, tmp_path):
    gid = await _media(tools, tmp_path, "will.jpg", _jpeg(_page()))
    blocks = await _call_raw(tools, media=gid, doc_type="hand")
    out = json.loads(blocks[0].text)
    assert out["route"] == "hand_english" and out["engine"] == "vision"
    assert out["text"] is None
    for rule in ("[?]", "line breaks", "never normalise", "abbreviations"):
        assert rule in out["instruction"]
    assert "add_note" in out["after_reading"]
    assert out["image"]["media"] == gid
    image = _image_block(blocks)
    assert image.size == (1200, 1600) == tuple(out["image"]["sent"])
    assert tools.fake.ocr_requests == []


async def test_a_large_scan_is_scaled_to_what_a_model_reads_whole(tools, tmp_path):
    gid = await _media(tools, tmp_path, "deed.jpg", _jpeg(_page(size=(4000, 5200))))
    blocks = await _call_raw(tools, media=gid, doc_type="hand")
    out = json.loads(blocks[0].text)
    image = _image_block(blocks)
    assert max(image.size) <= ocr.VISION_MAX_EDGE
    assert image.width * image.height <= ocr.VISION_MAX_PIXELS
    assert out["image"]["original"] == [4000, 5200]
    assert any("region=" in w for w in out["warnings"])


async def test_a_region_returns_that_part_at_full_detail(tools, tmp_path):
    gid = await _media(tools, tmp_path, "deed.jpg", _jpeg(_page(size=(4000, 5200))))
    blocks = await _call_raw(tools, media=gid, doc_type="hand", region=[0, 0, 50, 25])
    image = _image_block(blocks)
    assert image.size == (2000, 1300)


async def test_a_region_that_is_no_rectangle_is_refused_before_anything_is_fetched(tools, tmp_path):
    gid = await _media(tools, tmp_path, "deed.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, doc_type="hand", region=[50, 0, 40, 100])
    assert out["error"] == "bad_region"


async def test_a_pdf_page_is_the_scan_embedded_in_it(tools, tmp_path):
    pdf = _scan_pdf(_page(size=(1000, 1400)), _page(size=(900, 1300)))
    gid = await _media(tools, tmp_path, "probate.pdf", pdf)
    blocks = await _call_raw(tools, media=gid, doc_type="hand", page=2)
    out = json.loads(blocks[0].text)
    assert (out["page"], out["pages"]) == (2, 2)
    assert _image_block(blocks).size == (900, 1300)
    assert "page 2 of the PDF" in out["image"]["from"]


async def test_a_page_the_pdf_does_not_have_is_named(tools, tmp_path):
    gid = await _media(tools, tmp_path, "probate.pdf", _scan_pdf(_page()))
    out = await tools("ocr_media", media=gid, doc_type="hand", page=3)
    assert out["error"] == "unreadable_media"
    assert "1 page" in out["message"]


async def test_a_pdf_page_with_no_scan_is_rendered_by_gramps_web(tools, tmp_path):
    gid = await _media(tools, tmp_path, "typed.pdf", _text_pdf(LETTER))
    blocks = await _call_raw(tools, media=gid, engine="vision")
    out = json.loads(blocks[0].text)
    assert "rendered by Gramps Web" in out["image"]["from"]
    assert tools.fake.thumbnails == [(_handle(tools, gid), 3000)]
    _image_block(blocks)


async def test_second_witness_runs_transkribus_and_says_to_compare(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router)
    gid = await _media(tools, tmp_path, "will.jpg", _jpeg(_page()))
    blocks = await _call_raw(
        tools, media=gid, doc_type="hand", second_witness=True, spend_credits=True
    )
    out = json.loads(blocks[0].text)
    _image_block(blocks)
    assert out["engine"] == "vision"
    witness = out["witnesses"][0]
    assert witness["engine"] == "transkribus"
    assert witness["provenance"]["model_id"] == ocr.TEXT_TITAN_II.id
    assert "Johann Georg Wendler Bauer" in witness["text"]
    assert "list every name, date and number where they differ" in out["instruction"]
    assert out["credits"]["pages_sent"] == 1
    assert tk.submitted[0]["config"] == {"textRecognition": {"htrId": ocr.TEXT_TITAN_II.id}}


async def test_second_witness_without_transkribus_still_returns_the_image(tools, tmp_path):
    gid = await _media(tools, tmp_path, "will.jpg", _jpeg(_page()))
    blocks = await _call_raw(tools, media=gid, doc_type="hand", second_witness=True)
    out = json.loads(blocks[0].text)
    _image_block(blocks)
    assert "witnesses" not in out
    assert out["transkribus"]["reason_code"] == "not_configured"


# --------------------------------------------------------------------------- #
# German: Transkribus only
# --------------------------------------------------------------------------- #
async def test_german_handwriting_without_transkribus_is_refused_not_guessed(tools, tmp_path):
    gid = await _media(tools, tmp_path, "kirchenbuch.jpg", _jpeg(_page()))
    blocks = await _call_raw(tools, media=gid, doc_type="hand", lang="deu")
    out = json.loads(blocks[0].text)
    assert len(blocks) == 1, "no image: a vision read is never the fallback"
    assert out["error"] == "transkribus_required"
    assert ocr.CER_VISION_GERMAN in out["message"]
    assert "GRAMPS_MCP_TRANSKRIBUS_USERNAME" in out["message"]
    assert tools.fake.ocr_requests == []


async def test_german_handwriting_is_never_given_to_vision_even_when_asked(tools, tmp_path):
    gid = await _media(tools, tmp_path, "kirchenbuch.jpg", _jpeg(_page()))
    blocks = await _call_raw(tools, media=gid, doc_type="hand", lang="de", engine="vision")
    out = json.loads(blocks[0].text)
    assert len(blocks) == 1
    assert out["error"] == "vision_refused"


async def test_transkribus_is_not_paid_for_without_consent_or_budget(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router)
    gid = await _media(tools, tmp_path, "kirchenbuch.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, doc_type="hand", lang="deu")
    assert out["error"] == "spend_not_approved"
    assert "spend_credits=true" in out["message"]
    assert tk.submitted == [] and tk.token_forms == []


async def test_german_handwriting_is_read_by_the_kurrent_model(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router)
    original = _jpeg(_page())
    gid = await _media(tools, tmp_path, "kirchenbuch.jpg", original)
    blocks = await _call_raw(tools, media=gid, doc_type="hand", lang="deu", spend_credits=True)
    out = json.loads(blocks[0].text)
    assert len(blocks) == 1, "no image beside a German reading"
    assert out["engine"] == "transkribus"
    assert out["text"] == (
        "Johann Georg Wendler Bauer\nzu Oberhausen geb. d. 3t. Mertz 1791\n\n"
        "Pathe: Anna Maria Kölbl"
    )
    assert out["provenance"] == {
        "engine": "transkribus",
        "model": "Transkribus German Kurrent",
        "model_id": 36508,
        "date": out["provenance"]["date"],
        "process_id": 47725,
    }
    # The file itself, at full resolution, and no language model to normalise it.
    sent = tk.submitted[0]
    assert sent["config"] == {"textRecognition": {"htrId": 36508}}
    assert base64.b64decode(sent["image"]["base64"]) == original
    assert tk.token_forms[0] == {
        "client_id": "processing-api-client",
        "grant_type": "password",
        "username": "reader@example.org",
        "password": "pw",
    }
    assert out["credits"]["pages_sent"] == 1
    assert out["credits"]["estimated_credits"] == ocr.CREDITS_PER_PAGE


async def test_a_monthly_budget_spends_without_asking_until_it_is_used(tools, tmp_path):
    _configure_transkribus(tools, budget=1)
    tk = FakeTranskribus(tools.fake.router, statuses=("FINISHED",))
    first = await _media(tools, tmp_path, "a.jpg", _jpeg(_page()))
    second = await _media(tools, tmp_path, "b.jpg", _jpeg(_page(lines=("Other",))))
    ran = await tools("ocr_media", media=first, doc_type="hand", lang="deu")
    assert ran["engine"] == "transkribus"
    assert ran["credits"]["pages_this_month"] == 1
    refused = await tools("ocr_media", media=second, doc_type="hand", lang="deu")
    assert refused["error"] == "spend_not_approved"
    assert "budget of 1 pages is used up" in refused["message"]
    assert len(tk.submitted) == 1
    ledger = json.loads(tools.service.config.transkribus_ledger.read_text(encoding="utf-8"))
    assert list(ledger["months"].values()) == [{"pages": 1}]


async def test_the_same_page_is_fetched_again_not_paid_for_again(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router, statuses=("FINISHED",))
    gid = await _media(tools, tmp_path, "a.jpg", _jpeg(_page()))
    await tools("ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True)
    again = await tools("ocr_media", media=gid, doc_type="hand", lang="deu")
    assert again["engine"] == "transkribus"
    assert again["credits"]["pages_sent"] == 0
    assert again["credits"]["reused_job"] == 47725
    assert len(tk.submitted) == 1


async def test_a_job_still_running_is_handed_back_to_call_again(tools, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "TRANSKRIBUS_WAIT_SECONDS", 0)
    _configure_transkribus(tools)
    FakeTranskribus(tools.fake.router, statuses=("RUNNING",))
    gid = await _media(tools, tmp_path, "a.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True)
    assert "error" not in out, out
    assert out["text"] is None
    assert out["transkribus"]["status"] == "RUNNING"
    assert "not paid for twice" in out["transkribus"]["message"]


async def test_credits_used_up_is_said_plainly(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router)
    tk.submit_error = (429, json.loads((TK / "error_429.json").read_text(encoding="utf-8")))
    gid = await _media(tools, tmp_path, "a.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True)
    assert out["error"] == "transkribus"
    assert "processing volume is depleted" in out["message"]
    assert "credits are used up" in out["message"]


async def test_a_private_media_object_is_never_sent_to_transkribus(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router)
    gid = await _media(tools, tmp_path, "a.jpg", _jpeg(_page()))
    tools.fake.store["media"][_handle(tools, gid)]["private"] = True
    out = await tools("ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True)
    assert out["error"] == "private_media"
    assert tk.token_forms == [] and tk.submitted == []


async def test_a_refused_token_is_renewed_once(tools, tmp_path):
    """A 401 on a call renews the token (by refresh) and repeats the call once."""
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router, statuses=("FINISHED",))
    seen = {"refused": False}
    real = tk._api

    def refuse_once(request):
        if request.method == "GET" and not seen["refused"]:
            seen["refused"] = True
            return httpx.Response(401, json={"statusCode": 401, "message": "expired"})
        return real(request)

    tools.fake.router.routes[-1].side_effect = refuse_once
    gid = await _media(tools, tmp_path, "a.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True)
    assert out["engine"] == "transkribus", out
    assert [f["grant_type"] for f in tk.token_forms] == ["password", "refresh_token"]


# --------------------------------------------------------------------------- #
# Norwegian and the rest
# --------------------------------------------------------------------------- #
async def test_norwegian_gets_norhand_and_the_image_to_check_it(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router, statuses=("FINISHED",))
    gid = await _media(tools, tmp_path, "ministerialbok.jpg", _jpeg(_page()))
    blocks = await _call_raw(tools, media=gid, doc_type="hand", lang="nor", spend_credits=True)
    out = json.loads(blocks[0].text)
    _image_block(blocks)
    assert out["engine"] == "transkribus"
    assert out["provenance"]["model_id"] == 55080
    assert "Check the Transkribus reading against the image" in out["instruction"]
    assert tk.submitted[0]["config"]["textRecognition"]["htrId"] == 55080


async def test_norwegian_without_transkribus_is_the_image_and_its_error_rate(tools, tmp_path):
    gid = await _media(tools, tmp_path, "ministerialbok.jpg", _jpeg(_page()))
    blocks = await _call_raw(tools, media=gid, doc_type="hand", lang="nb")
    out = json.loads(blocks[0].text)
    _image_block(blocks)
    assert out["route"] == "hand_norwegian" and out["engine"] == "vision"
    assert any(ocr.CER_VISION_NORWEGIAN in w for w in out["warnings"])
    assert any("not configured" in w for w in out["warnings"])


async def test_another_language_goes_to_text_titan(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router, statuses=("FINISHED",))
    gid = await _media(tools, tmp_path, "husförhör.jpg", _jpeg(_page()))
    await tools("ocr_media", media=gid, doc_type="hand", lang="swe", spend_credits=True)
    assert tk.submitted[0]["config"]["textRecognition"]["htrId"] == ocr.TEXT_TITAN_II.id


# --------------------------------------------------------------------------- #
# Tables and volumes
# --------------------------------------------------------------------------- #
async def test_a_census_page_is_sent_to_familysearchs_index(tools, tmp_path):
    gid = await _media(tools, tmp_path, "census.jpg", _jpeg(_page()))
    blocks = await _call_raw(tools, media=gid, doc_type="table")
    out = json.loads(blocks[0].text)
    assert len(blocks) == 1
    assert out["text"] is None
    assert "get_records_on_image" in out["guidance"]
    assert "wrong line" in out["guidance"]
    assert tools.fake.ocr_requests == []


async def test_a_volume_points_to_full_text_search_first(tools, tmp_path):
    gid = await _media(tools, tmp_path, "deedbook.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, doc_type="volume")
    assert "fulltext_search" in out["guidance"]
    assert "engine='transkribus'" in out["guidance"]
    assert "not configured" in out["guidance"]


# --------------------------------------------------------------------------- #
# Storing a reading
# --------------------------------------------------------------------------- #
async def test_store_keeps_the_reading_as_a_transcript_note_with_its_provenance(tools, tmp_path):
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, store=True)
    assert out["stored"]["stored"] is True and out["stored"]["verified"] is True
    media = tools.fake.store["media"][_handle(tools, gid)]
    assert len(media["note_list"]) == 1
    note = tools.fake.store["note"][media["note_list"][0]]
    assert note["type"] == "Transcript"
    first, body = note["text"]["string"].split("\n\n", 1)
    assert first.startswith(ocr.HEADER_PREFIX + "Tesseract (Gramps Web), language eng")
    assert "A finding aid, not evidence" in first
    assert body == "OCRED TEXT"
    assert tools.fake.partial_writes == []
    # The next call reads the note back, and stores nothing twice.
    again = await tools("ocr_media", media=gid, store=True)
    assert again["engine"] == "existing"
    assert again["provenance"]["engine"] == "tesseract"
    assert again["stored"]["stored"] is False
    assert len(tools.fake.store["media"][_handle(tools, gid)]["note_list"]) == 1


async def test_store_keeps_transkribus_page_xml_beside_the_text(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router, statuses=("FINISHED",))
    gid = await _media(tools, tmp_path, "kirchenbuch.jpg", _jpeg(_page()))
    out = await tools(
        "ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True, store=True
    )
    assert out["stored"]["stored"] is True, out
    assert tk.page_requests == 1
    media = tools.fake.store["media"][_handle(tools, gid)]
    text, xml = (tools.fake.store["note"][h] for h in media["note_list"])
    assert "Transkribus, model 36508 (Transkribus German Kurrent)" in text["text"]["string"]
    assert xml["format"] == 1
    lines = xml["text"]["string"].splitlines()
    assert lines[0].startswith("<?xml")
    assert lines[1].startswith("<!-- PAGE XML from Transkribus, model 36508")
    assert "<PcGts" in xml["text"]["string"]
    assert "_page_xml" not in out


async def test_a_private_media_objects_transcript_is_private(tools, tmp_path):
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    tools.fake.store["media"][_handle(tools, gid)]["private"] = True
    await tools("ocr_media", media=gid, store=True)
    media = tools.fake.store["media"][_handle(tools, gid)]
    assert tools.fake.store["note"][media["note_list"][0]]["private"] is True


async def test_a_vision_read_is_the_callers_to_store(tools, tmp_path):
    gid = await _media(tools, tmp_path, "will.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, doc_type="hand", store=True)
    assert out["stored"]["stored"] is False
    assert "add_note" in out["stored"]["message"]
    assert not tools.fake.store["media"][_handle(tools, gid)].get("note_list")


# --------------------------------------------------------------------------- #
# The tool surface
# --------------------------------------------------------------------------- #
async def test_ocr_media_is_annotated_as_reaching_outside_and_writing():
    """It may spend money at Transkribus and add a note, so a client should ask."""
    from gramps_evidence_mcp.server import mcp

    tool = next(t for t in await mcp.list_tools() if t.name == "ocr_media")
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.open_world_hint is True


async def test_todays_callers_still_work(tools, tmp_path):
    """``ocr_media(media, lang)`` answers as it did: handle, media, lang, text, caveat."""
    gid = await _media(tools, tmp_path, "page.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, lang="eng")
    assert {"handle", "media", "lang", "text", "caveat"} <= set(out)
    assert out["media"] == gid


# --------------------------------------------------------------------------- #
# The pieces
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("lang", "base", "tesseract"),
    [
        ("eng", "eng", "eng"),
        ("en", "eng", "eng"),
        ("ger", "deu", "deu"),
        ("frk", "deu", "frk"),
        ("deu_latf+eng", "deu", "deu_latf+eng"),
        ("nb", "nor", "nor"),
        ("eng+deu", "eng", "eng+deu"),
        ("pol", "pol", "pol"),
    ],
)
def test_language_codes(lang, base, tesseract):
    assert ocr.base_language(lang) == base
    assert ocr.tesseract_language(lang) == tesseract


@pytest.mark.parametrize(
    ("url", "api"),
    [
        (
            "https://chroniclingamerica.loc.gov/lccn/sn99999999/1908-05-01/ed-1/seq-3/",
            "https://www.loc.gov/resource/sn99999999/1908-05-01/ed-1/?sp=3&fo=json",
        ),
        (
            "https://www.loc.gov/resource/sn99999999/1908-05-01/ed-1/?sp=2&st=text",
            "https://www.loc.gov/resource/sn99999999/1908-05-01/ed-1/?sp=2&fo=json",
        ),
        ("https://www.loc.gov/item/sn99999999/", None),
    ],
)
def test_library_of_congress_page_urls(url, api):
    assert ocr.loc_json_url(url) == api


def test_urls_are_found_in_free_text_without_trailing_punctuation():
    found = ocr.find_urls(
        ["Read at https://archive.org/details/x/page/n5. Also (https://www.loc.gov/item/y/)."]
    )
    assert found == ["https://archive.org/details/x/page/n5", "https://www.loc.gov/item/y/"]
    assert ocr.archive_kind("http://web.archive.org/web/2020/https://example.org/") is None


def test_a_stored_header_reads_back_as_provenance():
    provenance = {
        "engine": "transkribus",
        "model": "NorHand 1820-1940",
        "model_id": 55080,
        "date": "2026-10-06",
        "page_of": (2, 7),
    }
    line = ocr.header(provenance)
    assert ocr.parse_header(line + "\n\ntext") == {
        "engine": "transkribus",
        "model": "NorHand 1820-1940",
        "model_id": 55080,
        "date": "2026-10-06",
        "page": 2,
    }
    assert ocr.body_of(line + "\n\nthe text") == "the text"


def test_the_ledger_survives_a_restart(tmp_path):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 6, tzinfo=UTC)
    ledger = ocr.Ledger.load(tmp_path / "usage.json")
    ledger.record("O1:abc:1:36508", 47725, 36508, now)
    again = ocr.Ledger.load(tmp_path / "usage.json")
    assert again.pages_in(now) == 1
    assert again.job("O1:abc:1:36508", now)["process_id"] == 47725
    later = datetime(2026, 10, 8, tzinfo=UTC)
    assert again.job("O1:abc:1:36508", later) is None
    assert again.pages_in(datetime(2026, 11, 1, tzinfo=UTC)) == 0


# --------------------------------------------------------------------------- #
# Found in review
# --------------------------------------------------------------------------- #
async def test_a_reading_of_a_pdf_page_is_stored_and_found_again_for_that_page(tools, tmp_path):
    _configure_transkribus(tools)
    FakeTranskribus(tools.fake.router, statuses=("FINISHED",))
    gid = await _media(tools, tmp_path, "kirchenbuch.pdf", _scan_pdf(_page(), _page()))
    out = await tools(
        "ocr_media",
        media=gid,
        doc_type="hand",
        lang="deu",
        page=2,
        spend_credits=True,
        store=True,
    )
    assert out["stored"]["stored"] is True, out
    media = tools.fake.store["media"][_handle(tools, gid)]
    header = tools.fake.store["note"][media["note_list"][0]]["text"]["string"].split("\n")[0]
    assert ", page 2 of 2," in header
    again = await tools("ocr_media", media=gid, engine="existing", page=2)
    assert again["engine"] == "existing" and "Wendler" in again["text"]
    other = await tools("ocr_media", media=gid, engine="existing", page=1)
    assert other["text"] is None


async def test_an_archive_url_is_not_taken_for_one_page_of_a_longer_pdf(tools, tmp_path):
    gid = await _media(tools, tmp_path, "courier.pdf", _scan_pdf(_page(), _page()))
    await _source_naming(
        tools, gid, "https://www.loc.gov/resource/sn99999999/1908-05-01/ed-1/?sp=1"
    )
    out = await tools("ocr_media", media=gid, engine="existing", page=2)
    assert out["text"] is None and "looked_at" not in out


async def test_transkribus_out_of_reach_still_returns_the_image(tools, tmp_path):
    _configure_transkribus(tools)
    tools.fake.router.route(host="account.readcoop.eu").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    gid = await _media(tools, tmp_path, "ministerialbok.jpg", _jpeg(_page()))
    blocks = await _call_raw(tools, media=gid, doc_type="hand", lang="nor", spend_credits=True)
    out = json.loads(blocks[0].text)
    _image_block(blocks)
    assert out["transkribus"]["reason_code"] == "network"
    german = await tools(
        "ocr_media",
        media=await _media(tools, tmp_path, "k.jpg", _jpeg(_page(lines=("x",)))),
        doc_type="hand",
        lang="deu",
        spend_credits=True,
    )
    assert german["error"] == "transkribus" and "ConnectError" in german["message"]


async def test_a_failed_job_is_not_kept_in_place_of_a_fresh_one(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router, statuses=("FAILED",))
    gid = await _media(tools, tmp_path, "a.jpg", _jpeg(_page()))
    first = await tools("ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True)
    assert first["transkribus"]["status"] == "FAILED"
    tk.statuses = ["FINISHED"]
    second = await tools("ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True)
    assert second["engine"] == "transkribus"
    assert len(tk.submitted) == 2


async def test_a_job_transkribus_no_longer_has_is_read_afresh_with_consent(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router, statuses=("FINISHED",))
    gid = await _media(tools, tmp_path, "a.jpg", _jpeg(_page()))
    await tools("ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True)
    real = tk._api

    def expired(request):
        if request.method == "GET" and request.url.path.endswith("/47725"):
            return httpx.Response(404, json={"statusCode": 404, "message": "no such job"})
        return real(request)

    tools.fake.router.routes[-1].side_effect = expired
    refused = await tools("ocr_media", media=gid, doc_type="hand", lang="deu")
    assert refused["error"] == "spend_not_approved"
    out = await tools("ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True)
    assert out["engine"] == "transkribus", out
    assert len(tk.submitted) == 2


async def test_a_refused_job_gives_its_page_back(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router)
    tk.submit_error = (429, json.loads((TK / "error_429.json").read_text(encoding="utf-8")))
    gid = await _media(tools, tmp_path, "a.jpg", _jpeg(_page()))
    await tools("ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True)
    ledger = json.loads(tools.service.config.transkribus_ledger.read_text(encoding="utf-8"))
    assert list(ledger["months"].values()) == [{"pages": 0}]
    assert ledger["jobs"] == {}


async def test_no_page_is_sent_when_it_cannot_be_counted(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router)
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    tools.service.config.cache_dir = blocker / "cache"
    gid = await _media(tools, tmp_path, "a.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, doc_type="hand", lang="deu", spend_credits=True)
    assert out["error"] == "transkribus"
    assert "could not be written" in out["message"]
    assert tk.submitted == []


@pytest.mark.parametrize("lang", ["de-DE", "de_DE", "German", "Deutsch"])
async def test_german_written_any_way_is_never_given_to_vision(tools, tmp_path, lang):
    gid = await _media(tools, tmp_path, "k.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, doc_type="hand", lang=lang)
    assert out["error"] == "transkribus_required"
    for doc_type in ("hand", "table", "volume"):
        refused = await tools("ocr_media", media=gid, doc_type=doc_type, lang=lang, engine="vision")
        assert refused["error"] == "vision_refused", doc_type


async def test_a_german_table_is_not_pointed_at_a_vision_read(tools, tmp_path):
    gid = await _media(tools, tmp_path, "k.jpg", _jpeg(_page()))
    out = await tools("ocr_media", media=gid, doc_type="table", lang="deu")
    assert "engine='vision'" not in out["guidance"]


async def test_a_language_no_model_covers_is_not_sent_to_transkribus(tools, tmp_path):
    _configure_transkribus(tools)
    tk = FakeTranskribus(tools.fake.router)
    gid = await _media(tools, tmp_path, "metryka.jpg", _jpeg(_page()))
    blocks = await _call_raw(tools, media=gid, doc_type="hand", lang="pol", spend_credits=True)
    out = json.loads(blocks[0].text)
    _image_block(blocks)
    assert out["transkribus"]["reason_code"] == "no_model"
    assert tk.token_forms == [] and tk.submitted == []


def test_scaling_never_rounds_past_the_area_limit():
    img = Image.new("L", (5990, 3713), 255)
    _jpeg_bytes, (w, h) = ocr.vision_jpeg(img)
    assert w * h <= ocr.VISION_MAX_PIXELS
    assert max(w, h) <= ocr.VISION_MAX_EDGE


async def test_a_library_of_congress_item_url_is_followed_to_its_resource(tools, tmp_path):
    """Outside the newspapers an item's id is not its resource's."""
    gid = await _media(tools, tmp_path, "history.jpg", _jpeg(_page()))
    await _source_naming(tools, gid, "https://www.loc.gov/item/2020999999/?sp=12")

    def loc(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/item/2020999999/":
            return httpx.Response(
                200,
                json={"resources": [{"url": "https://www.loc.gov/resource/gdc.invented01/"}]},
            )
        if request.url.path == "/resource/gdc.invented01/":
            assert dict(request.url.params) == {"sp": "12", "fo": "json"}
            return httpx.Response(
                200,
                json=json.loads(
                    (FIXTURES / "archives/loc_resource.json").read_text(encoding="utf-8")
                ),
            )
        return httpx.Response(
            200,
            json=json.loads((FIXTURES / "archives/loc_fulltext.json").read_text(encoding="utf-8")),
        )

    tools.fake.router.route(host__regex=r"(www|tile)\.loc\.gov").mock(side_effect=loc)
    out = await tools("ocr_media", media=gid)
    assert out["provenance"]["engine"] == "loc", out
