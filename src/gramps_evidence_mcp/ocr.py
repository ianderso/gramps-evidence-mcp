"""The ``ocr_media`` router: which engine reads a document, and how to reach it.

Gramps Web's own OCR is Tesseract, which reads print and nothing else. The
documents a family history turns on are handwritten -- wills, deeds, letters,
German church books in Kurrent, Norwegian parish registers -- and the right
reader depends on the hand and the language, as published benchmarks measure
it:

- Vision models read English hands of the 18th and 19th centuries at 5.7-7 %
  character error, better than dedicated engines (Humphries et al., 2025),
  and modern English at 4.4 % (METATR, May 2026).
- The same models read Norwegian (the NorHand set) at about 10 %, and
  historical German (READ-2016) at **48.8 %**: half the characters wrong.
- Vision models also "normalise" unusual spellings ("Do VLMs Read or
  Rewrite?", 2026), which is exactly the failure that matters for surnames.

So this module routes: print to existing OCR text or Tesseract, an English
hand to the calling model's own eyes with a diplomatic-transcription
instruction, German handwriting to Transkribus only, Norwegian and the rest to
Transkribus with the image for a cross-check, census tables to FamilySearch's
index, and whole volumes to full-text search.

What it reaches, besides Gramps Web: the Library of Congress and the Internet
Archive, keyless and read-only, for OCR text they already hold; and
Transkribus, only when configured, and only within a page budget or with the
caller's per-call consent, because every page costs credits.

The Transkribus client is built to the published Metagrapho v1 API
(``https://transkribus.eu/processing/v1/openapi.json``, version 1.13.1, read
on 2026-10-06) and its READ-COOP OpenID Connect login. It has not been run
against the live service: no account was used. The v2 "developer platform"
documents the same request and response bodies under ``/v2/processes``.

Nothing here logs record contents or recognised text: only ids, routes and
HTTP status.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import io
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from . import __version__
from .config import TRANSKRIBUS_API_URL

logger = logging.getLogger("gramps_evidence_mcp.ocr")
# pypdf reports every repairable defect in a scanner's PDF at WARNING.
logging.getLogger("pypdf").setLevel(logging.ERROR)

#: Sent to the Library of Congress and the Internet Archive, which ask
#: automated clients to say who they are.
USER_AGENT = f"gramps-evidence-mcp/{__version__} (+https://github.com/ianderso/gramps-evidence-mcp)"


# --------------------------------------------------------------------------- #
# Languages and routes
# --------------------------------------------------------------------------- #
#: Two-letter and bibliographic codes, and Tesseract's Fraktur models, to the
#: three-letter code a route is chosen by. Tesseract takes the same codes.
_LANG_ALIASES = {
    "en": "eng",
    "de": "deu",
    "ger": "deu",
    "frk": "deu",
    "deu_frak": "deu",
    "deu_latf": "deu",
    "no": "nor",
    "nb": "nor",
    "nn": "nor",
    "nob": "nor",
    "nno": "nor",
    "sv": "swe",
    "da": "dan",
    "fr": "fra",
    "fre": "fra",
    "nl": "nld",
    "dut": "nld",
    "la": "lat",
    "it": "ita",
    "es": "spa",
    "pt": "por",
    "fi": "fin",
}

#: Languages given by name, in English or their own.
_LANG_NAMES = {
    "english": "eng",
    "german": "deu",
    "deutsch": "deu",
    "kurrent": "deu",
    "sütterlin": "deu",
    "suetterlin": "deu",
    "fraktur": "deu",
    "norwegian": "nor",
    "norsk": "nor",
    "bokmål": "nor",
    "nynorsk": "nor",
    "swedish": "swe",
    "svenska": "swe",
    "danish": "dan",
    "dansk": "dan",
    "french": "fra",
    "dutch": "nld",
    "latin": "lat",
    "italian": "ita",
    "spanish": "spa",
    "portuguese": "por",
    "finnish": "fin",
}

#: Tesseract model names that are not a language's plain code.
_TESSERACT_KEEP = {"frk", "deu_frak", "deu_latf", "osd"}


def _one_language(code: str) -> str:
    """One language code as a three-letter code: ``de-DE``, ``de_DE``, ``German`` -> ``deu``.

    A Tesseract model name (``deu_latf``, ``chi_sim``) keeps what it names.
    """
    code = code.strip().lower()
    if code in _TESSERACT_KEEP or code in _LANG_ALIASES:
        return _LANG_ALIASES.get(code, code)
    if code in _LANG_NAMES:
        return _LANG_NAMES[code]
    head = re.split(r"[-_]", code)[0]
    if head != code and (head in _LANG_ALIASES or len(head) in (2, 3)):
        return _LANG_ALIASES.get(head, head)
    return code


LANGUAGE_NAMES = {
    "eng": "English",
    "deu": "German",
    "nor": "Norwegian",
    "swe": "Swedish",
    "dan": "Danish",
    "fra": "French",
    "nld": "Dutch",
    "lat": "Latin",
    "ita": "Italian",
    "spa": "Spanish",
    "por": "Portuguese",
    "fin": "Finnish",
}


def base_language(lang: str) -> str:
    """The language a route is chosen by: the first of ``eng+deu``, as ``eng``.

    Parameters
    ----------
    lang : str
        A Tesseract-style code, a two-letter code, or several joined by ``+``.

    Returns
    -------
    str
        A three-letter code where one is known, else the input lower-cased.
    """
    first = re.split(r"[+,\s]+", (lang or "").strip().lower())[0]
    return _one_language(first)


def _tesseract_model(code: str) -> str:
    """A Tesseract model name kept as it is (``deu_latf``, ``chi_sim``); a
    language otherwise, as its three-letter code."""
    head, _, tail = code.partition("_")
    if code in _TESSERACT_KEEP or (tail and len(head) == 3 and head.isalpha()):
        return code
    return _one_language(code)


def tesseract_language(lang: str) -> str:
    """The code to send Tesseract: names and two-letter codes widened, ``+`` kept."""
    parts = [p for p in re.split(r"[+,\s]+", (lang or "").strip().lower()) if p]
    return "+".join(dict.fromkeys(_tesseract_model(p) for p in parts)) or "eng"


@dataclass(frozen=True)
class HtrModel:
    """A Transkribus text recognition model, addressed by its ``htrId``."""

    id: int
    name: str
    covers: str


#: The models a route sends to Transkribus. Each id and description is from
#: the model's page on transkribus.org, read on 2026-10-06.
TRANSKRIBUS_MODELS = {
    "deu": HtrModel(
        36508,
        "Transkribus German Kurrent",
        "German Kurrent, Sütterlin and Fraktur, 17th-20th century; 5.4 % character "
        "error on its validation set",
    ),
    "nor": HtrModel(
        55080,
        "NorHand 1820-1940",
        "Norwegian handwriting about 1820-1940, from the National Library of Norway's "
        "letters and manuscripts; 4 % character error on its validation set",
    ),
}
#: Transkribus' general model of June 2026: print and handwriting in the
#: major Latin-script languages.
TEXT_TITAN_II = HtrModel(
    579509,
    "Text Titan II",
    "print and handwriting in Danish, German, English, Finnish, French, Italian, Latin, "
    "Dutch, Norwegian, Portuguese, Spanish and Swedish",
)
TEXT_TITAN_LANGUAGES = {
    "dan",
    "deu",
    "eng",
    "fin",
    "fra",
    "ita",
    "lat",
    "nld",
    "nor",
    "por",
    "spa",
    "swe",
}


def transkribus_model(lang: str) -> HtrModel | None:
    """The model a route sends a page in this language to; None when none covers it."""
    base = base_language(lang)
    if base in TRANSKRIBUS_MODELS:
        return TRANSKRIBUS_MODELS[base]
    return TEXT_TITAN_II if base in TEXT_TITAN_LANGUAGES else None


#: Credits a page costs through the API: text recognition is 1 credit a page
#: in the Transkribus app, and API jobs are charged half the app's rate
#: (transkribus.org/pricing and docs.transkribus.org, read on 2026-10-06).
CREDITS_PER_PAGE = 0.5
#: Euro a page, at the on-demand price of 250 credits for EUR 59.50.
EUR_PER_PAGE = 0.12

#: Benchmarks quoted in the routing messages; ``docs`` and the README cite them.
CER_VISION_GERMAN = "48.8 %"
CER_VISION_NORWEGIAN = "10.2 %"

DIPLOMATIC_INSTRUCTION = (
    "Transcribe the image diplomatically, exactly as written. Keep every spelling, "
    "capital and punctuation mark as it stands: never normalise or modernise a name, "
    "place or word. Keep the line breaks, one line of output for each written line. "
    "Keep abbreviations, contractions and superscripts as written (Jno, decd, inst), "
    "unexpanded. Write [?] after any word or figure you are unsure of, and [illegible] "
    "where nothing can be read: never guess a name or a number silently. Mark struck-out "
    "words [struck: ...] and insertions [inserted: ...]. Add nothing the page does not say."
)

KEEPING_A_READING = (
    "To keep your reading, add it to this media object with add_note(target_type='media', "
    "note_type='Transcript'), its first line saying who read it and how: 'Diplomatic "
    "transcription from the image by <model>, <date>.' The transcript is never the "
    "evidence: cite the image."
)

COMPARE_WITNESSES = (
    "Transcribe the image yourself first, without reading the Transkribus text. Then "
    "compare the two readings and list every name, date and number where they differ, "
    "and settle each from the image or mark it [?]. Two machine readings agreeing is "
    "not proof: the image is the evidence."
)

TABLE_GUIDANCE = (
    "A census page or other table is not read here. OCR and handwriting engines read "
    "across a row and lose which cell belongs to which column and which person, so an "
    "age or a birthplace lands on the wrong line, silently. FamilySearch has indexed "
    "these pages field by field: find the image there and call get_records_on_image "
    "(familysearch-mcp) for the people on it, then check each row you use against the "
    "image."
)
TABLE_LOOK = (
    "To look at the page, call again with engine='vision' and a region to zoom on the "
    "rows you need."
)

VOLUME_GUIDANCE = (
    "A whole volume is a search problem before it is a reading one. First search "
    "FamilySearch Full-Text Search (familysearch-mcp's fulltext_search, where it is "
    "installed): FamilySearch has run handwriting recognition over its deed, will and "
    "probate images, and finds names no index holds. If FamilySearch does not have the "
    "volume, Transkribus reads it a page at a time, at about {credits} credits "
    "(EUR {eur}) a page: call this tool for each page with engine='transkribus'."
)


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
#: The largest image current Claude models read without scaling it down:
#: 2576 pixels on the long edge and 3.75 megapixels (Anthropic's vision
#: documentation, models from Opus 4.7 on). Older models scale it to 1568.
VISION_MAX_EDGE = 2576
VISION_MAX_PIXELS = 3_750_000
#: A JPEG this size encodes to 5 MB of base64, the API's limit for one image.
VISION_MAX_BYTES = 3_750_000
#: Transkribus takes JPEG, PNG or TIFF up to 20 MB.
TRANSKRIBUS_MIMES = {"image/jpeg", "image/png", "image/tiff"}
TRANSKRIBUS_MAX_BYTES = 20 * 1024 * 1024

#: The image formats a media file is opened as. Naming them keeps Pillow from
#: trying every decoder it has on whatever bytes a file holds.
_IMAGE_FORMATS = ["JPEG", "PNG", "TIFF", "WEBP", "GIF", "BMP", "AVIF", "JPEG2000"]


class ImageError(ValueError):
    """A media file that cannot be read as an image."""


def open_image(data: bytes) -> Any:
    """Open image bytes as a Pillow image, upright.

    Raises
    ------
    ImageError
        When the bytes are not an image of a known format.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data), formats=_IMAGE_FORMATS)
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageError(f"not a readable image ({type(exc).__name__})") from exc
    with contextlib.suppress(Exception):
        img = ImageOps.exif_transpose(img)
    return img


def _plain(img: Any) -> Any:
    """Grey or RGB: what a JPEG can hold. A grey scan stays grey, and smaller."""
    if img.mode in ("L", "RGB"):
        return img
    if img.mode in ("1", "I", "I;16", "I;16B", "I;16L", "F", "LA"):
        return img.convert("L")
    return img.convert("RGB")


def crop_region(img: Any, region: list[float] | None) -> Any:
    """Crop to ``[x1, y1, x2, y2]`` in percent of width and height, as Gramps' own
    media references do. ``None`` keeps the whole image."""
    if not region:
        return img
    x1, y1, x2, y2 = region
    w, h = img.size
    box = (
        math.floor(x1 * w / 100),
        math.floor(y1 * h / 100),
        math.ceil(x2 * w / 100),
        math.ceil(y2 * h / 100),
    )
    return img.crop(box)


def region_refusal(region: list[float] | None) -> str | None:
    """Why a region is unusable, or None when it is fine (or absent)."""
    if region is None:
        return None
    if len(region) != 4:
        return "region takes four numbers: [x1, y1, x2, y2], in percent."
    x1, y1, x2, y2 = region
    if not (0 <= x1 < x2 <= 100 and 0 <= y1 < y2 <= 100):
        return (
            "region is [x1, y1, x2, y2] in percent of the width and height, each "
            "0-100, with x1 < x2 and y1 < y2: [0, 0, 100, 50] is the top half."
        )
    return None


def vision_jpeg(img: Any) -> tuple[bytes, tuple[int, int]]:
    """Scale an image to what a vision model reads in full, and encode it.

    The long edge is held to :data:`VISION_MAX_EDGE`, the area to
    :data:`VISION_MAX_PIXELS`, and the file to :data:`VISION_MAX_BYTES`, so a
    client does not scale it down again, coarsely, on the way in. An image
    already smaller is never enlarged.

    Returns
    -------
    tuple
        The JPEG bytes and the (width, height) sent.
    """
    from PIL import Image

    img = _plain(img)
    w, h = img.size
    scale = min(1.0, VISION_MAX_EDGE / max(w, h), math.sqrt(VISION_MAX_PIXELS / (w * h)))
    while True:
        # Floored, so rounding never carries the area past the limit.
        size = (max(1, math.floor(w * scale)), max(1, math.floor(h * scale)))
        scaled = img if size == img.size else img.resize(size, Image.Resampling.LANCZOS)
        for quality in (85, 75, 65):
            buf = io.BytesIO()
            scaled.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= VISION_MAX_BYTES:
                return buf.getvalue(), size
        scale *= 0.8


def transkribus_image(data: bytes, mime: str) -> bytes:
    """The bytes to send Transkribus: the file itself when it can take it.

    A JPEG, PNG or TIFF up to 20 MB goes as it is, at full resolution, which
    is what handwriting recognition wants. Anything else is re-encoded as a
    JPEG, scaled down only as far as the size limit needs.
    """
    img = open_image(data)  # also proves the bytes are an image
    if mime in TRANSKRIBUS_MIMES and len(data) <= TRANSKRIBUS_MAX_BYTES:
        return data
    from PIL import Image

    img = _plain(img)
    scale = 1.0
    while True:
        size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        scaled = img if size == img.size else img.resize(size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        scaled.save(buf, format="JPEG", quality=92)
        if buf.tell() <= TRANSKRIBUS_MAX_BYTES:
            return buf.getvalue()
        scale *= 0.8


@dataclass
class PdfPage:
    """One page of a PDF: its text layer and its scanned image, if any."""

    pages: int
    text: str
    image: bytes | None = None
    image_mime: str | None = None


_PDF_IMAGE_MIMES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def pdf_page(data: bytes, page: int) -> PdfPage:
    """Read one page of a PDF: the text layer a scanner left, and the page image.

    A scanned PDF holds one image per page, usually a JPEG, often with an OCR
    text layer over it. The largest image on the page is taken as the scan.

    Raises
    ------
    ImageError
        When the file is not a readable PDF, or has no such page.
    """
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        count = len(reader.pages)
    except (PyPdfError, ValueError, OSError) as exc:
        raise ImageError(f"not a readable PDF ({type(exc).__name__})") from exc
    if not 1 <= page <= count:
        raise ImageError(f"the PDF has {count} page{'s' * (count != 1)}; page {page} is not one")
    pdf = reader.pages[page - 1]
    try:
        text = pdf.extract_text() or ""
    except Exception:  # noqa: BLE001 - a broken text layer is no text layer
        text = ""
    best: tuple[int, bytes, str] | None = None
    try:
        for image in pdf.images:
            pil = image.image
            if pil is None:
                continue
            area = pil.width * pil.height
            if best is None or area > best[0]:
                suffix = Path(image.name or "").suffix.lower()
                mime = _PDF_IMAGE_MIMES.get(suffix)
                if mime:
                    raw = image.data
                else:
                    buf = io.BytesIO()
                    _plain(pil).save(buf, format="PNG")
                    raw, mime = buf.getvalue(), "image/png"
                best = (area, raw, mime)
    except Exception:  # noqa: BLE001 - an image pypdf cannot decode is no image
        logger.info("pypdf could not extract an image from a PDF page")
    return PdfPage(
        pages=count,
        text=text.strip(),
        image=best[1] if best else None,
        image_mime=best[2] if best else None,
    )


# --------------------------------------------------------------------------- #
# Existing OCR text: the Library of Congress and the Internet Archive
# --------------------------------------------------------------------------- #
_URL = re.compile(r"https?://[^\s\"'<>()\[\]{}]+", re.I)


def find_urls(texts: Iterable[str]) -> list[str]:
    """Every distinct http(s) URL in some free text, trailing punctuation removed."""
    found: list[str] = []
    for text in texts:
        for match in _URL.findall(text or ""):
            url = match.rstrip(".,;:!?")
            if url not in found:
                found.append(url)
    return found


def _host_is(host: str, *domains: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def archive_kind(url: str) -> str | None:
    """``loc`` or ``internet_archive`` for a URL this module can take text from."""
    host = urlparse(url).hostname or ""
    if _host_is(host, "loc.gov"):
        return "loc"
    if _host_is(host, "archive.org") and not host.startswith("web."):
        return "internet_archive"
    return None


@dataclass
class FoundText:
    """OCR text an archive already holds for this page."""

    text: str
    provenance: dict
    note: str = ""


@dataclass
class Lookup:
    """What one archive URL gave: text, or why not."""

    url: str
    found: FoundText | None = None
    reason: str = ""


def _redirect_guard(*domains: str):
    """A response hook refusing a redirect that leaves the given domains."""

    async def check(response: httpx.Response) -> None:
        # A hook sees a redirect before httpx builds the next request, so the
        # target is read from Location (archive.org's downloads move to a
        # data node, ia800704.us.archive.org and the like).
        if response.is_redirect:
            target = response.url.join(response.headers.get("location", "")).host
            if not _host_is(target, *domains):
                raise httpx.HTTPError(f"refused a redirect to {target!r}")

    return check


def archive_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """An HTTP client for the Library of Congress and the Internet Archive only."""
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"response": [_redirect_guard("loc.gov", "archive.org")]},
    )


_CHRONAM = re.compile(r"/lccn/([^/]+)/(\d{4}-\d{2}-\d{2})/ed-(\d+)/seq-(\d+)")


def loc_json_url(url: str) -> str | None:
    """The loc.gov JSON for a page URL; None when the URL names no page.

    Takes ``www.loc.gov/resource/<id>/...?sp=<n>`` and the old Chronicling
    America form ``chroniclingamerica.loc.gov/lccn/<lccn>/<date>/ed-<e>/seq-<s>/``,
    which loc.gov now serves as ``resource/<lccn>/<date>/ed-<e>/?sp=<s>``. An
    ``/item/`` URL is :func:`loc_item_url`'s.
    """
    parsed = urlparse(url)
    old = _CHRONAM.search(parsed.path)
    if old:
        lccn, date, ed, seq = old.groups()
        return f"https://www.loc.gov/resource/{lccn}/{date}/ed-{ed}/?sp={seq}&fo=json"
    page = (parse_qs(parsed.query).get("sp") or [""])[0]
    match = re.match(r"^/resource/(.+?)/?$", parsed.path)
    if not match or not page.isdigit():
        return None
    return f"https://www.loc.gov/resource/{match.group(1)}/?sp={page}&fo=json"


def loc_item_url(url: str) -> tuple[str, str] | None:
    """``(item JSON URL, page)`` for ``www.loc.gov/item/<id>/?sp=<n>``, else None.

    An item's id is not its resource's outside the newspapers, so the item is
    read first: its JSON names the resource whose page is wanted (checked on a
    live item on 2026-10-06: ``resources[0].url``).
    """
    parsed = urlparse(url)
    page = (parse_qs(parsed.query).get("sp") or [""])[0]
    match = re.match(r"^/item/(.+?)/?$", parsed.path)
    if not match or not page.isdigit():
        return None
    return f"https://www.loc.gov/item/{match.group(1)}/?fo=json", page


async def loc_text(http: httpx.AsyncClient, url: str, today: str) -> Lookup:
    """The Library of Congress's OCR text for the page a URL names."""
    api = loc_json_url(url)
    if api is None and (item := loc_item_url(url)):
        item_api, page = item
        resp = await http.get(item_api)
        if resp.status_code >= 400:
            return Lookup(url, reason=f"loc.gov answered {resp.status_code}")
        resources = resp.json().get("resources") or []
        target = resources[0].get("url") if len(resources) == 1 else None
        if not target or not _host_is(urlparse(target).hostname or "", "loc.gov"):
            return Lookup(
                url,
                reason=f"the item has {len(resources)} resources; a /resource/ URL names the page",
            )
        api = f"{target.split('?')[0]}?sp={page}&fo=json"
    if api is None:
        return Lookup(url, reason="names an item or a title, not a page (no sp=)")
    resp = await http.get(api)
    if resp.status_code == 429:
        return Lookup(url, reason="the Library of Congress asked for slower requests (429)")
    if resp.status_code >= 400:
        return Lookup(url, reason=f"loc.gov answered {resp.status_code}")
    body = resp.json()
    resource = body.get("resource") or {}
    fulltext = resource.get("fulltext_file") or body.get("fulltext_service")
    if not fulltext or not _host_is(urlparse(fulltext).hostname or "", "loc.gov"):
        return Lookup(url, reason="the page has no OCR text at loc.gov")
    if "full_text=" not in fulltext and "word-coordinates-service" in fulltext:
        fulltext += "&full_text=1"
    text_resp = await http.get(fulltext)
    if text_resp.status_code >= 400:
        return Lookup(url, reason=f"loc.gov's text service answered {text_resp.status_code}")
    text = ""
    try:
        data = text_resp.json()
    except ValueError:
        text = text_resp.text
    else:
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, dict) and value.get("full_text"):
                    text = value["full_text"]
                    break
    if not text.strip():
        return Lookup(url, reason="loc.gov holds no text for the page")
    item = body.get("item") or {}
    return Lookup(
        url,
        FoundText(
            text=text.strip(),
            provenance={
                "engine": "loc",
                "model": "Library of Congress OCR",
                "date": today,
                "url": resource.get("url") or url,
                "item": item.get("title"),
            },
        ),
    )


def _ia_identifier_and_page(url: str) -> tuple[str | None, str | None]:
    """``(identifier, page)`` from ``archive.org/details/<id>[/page/<page>]``."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2 or parts[0] not in ("details", "stream"):
        return None, None
    page = None
    if "page" in parts:
        at = parts.index("page")
        if at + 1 < len(parts):
            page = parts[at + 1]
    return parts[1], page


async def internet_archive_text(http: httpx.AsyncClient, url: str, today: str) -> Lookup:
    """The Internet Archive's OCR text for the leaf a URL names.

    ``/page/n251`` counts the leaves BookReader shows, from 0; ``/page/258``
    is a printed page number. The item's page-number file says which scanned
    leaf each is, and its hOCR page index where that leaf's text lies in its
    search text. Checked on 2026-10-06 against an item whose ``n251`` is
    printed page 258 (``portraitbiographwci00acme``).
    """
    identifier, page = _ia_identifier_and_page(url)
    if not identifier:
        return Lookup(url, reason="not an archive.org item URL")
    if page is None:
        # Nothing is fetched for it: a whole book's text is not one page's.
        return Lookup(url, reason="names the whole item, not a page (no /page/n<leaf>)")
    meta = await http.get(f"https://archive.org/metadata/{identifier}")
    if meta.status_code >= 400 or not meta.json():
        return Lookup(url, reason=f"archive.org has no item {identifier!r}")
    files = {f.get("format"): f.get("name") for f in meta.json().get("files") or []}
    index_name, text_name = files.get("OCR Page Index"), files.get("OCR Search Text")
    if not (index_name and text_name):
        return Lookup(url, reason="the item has no OCR text")

    async def download(name: str) -> bytes | None:
        resp = await http.get(f"https://archive.org/download/{identifier}/{name}")
        return resp.content if resp.status_code < 400 else None

    numbers_name = files.get("Page Numbers JSON")
    leaves: list[dict] = []
    if numbers_name and (raw := await download(numbers_name)):
        with contextlib.suppress(ValueError):
            leaves = json.loads(raw).get("pages") or []
    index_raw, text_raw = await download(index_name), await download(text_name)
    if not (index_raw and text_raw):
        return Lookup(url, reason="the item's OCR text could not be downloaded")
    index = json.loads(gzip.decompress(index_raw))
    text = gzip.decompress(text_raw).decode("utf-8", "replace")

    printed = None
    if page.startswith("n") and page[1:].isdigit():
        n = int(page[1:])
        if leaves:
            if n >= len(leaves):
                return Lookup(url, reason=f"the item shows {len(leaves)} leaves; n{n} is not one")
            leaf, printed = int(leaves[n].get("leafNum", n)), leaves[n].get("pageNumber")
        else:
            leaf = n
    else:
        match = next((p for p in leaves if str(p.get("pageNumber")) == page), None)
        if match is None:
            return Lookup(url, reason=f"no leaf is printed page {page!r}")
        leaf, printed = int(match["leafNum"]), page
    if not 0 <= leaf < len(index):
        return Lookup(url, reason=f"leaf {leaf} has no OCR text")
    start, end = index[leaf][0], index[leaf][1]
    page_text = text[start:end].strip()
    if not page_text:
        return Lookup(url, reason=f"leaf {leaf} has no OCR text")
    return Lookup(
        url,
        FoundText(
            text=page_text,
            provenance={
                "engine": "internet_archive",
                "model": "Internet Archive OCR",
                "date": today,
                "url": url,
                "item": identifier,
                "leaf": leaf,
                "printed_page": printed or None,
            },
            note=""
            if leaves
            else "The item has no page-number file, so the leaf was counted from the URL "
            "alone: check the text is this page's.",
        ),
    )


async def archive_text(http: httpx.AsyncClient, url: str, today: str) -> Lookup:
    """OCR text for one Library of Congress or Internet Archive URL."""
    kind = archive_kind(url)
    try:
        if kind == "loc":
            return await loc_text(http, url, today)
        if kind == "internet_archive":
            return await internet_archive_text(http, url, today)
    except Exception as exc:  # noqa: BLE001 - a best-effort lookup; the route goes on
        return Lookup(url, reason=f"could not be read ({type(exc).__name__})")
    return Lookup(url, reason="not a Library of Congress or Internet Archive URL")


# --------------------------------------------------------------------------- #
# Transkribus
# --------------------------------------------------------------------------- #
TRANSKRIBUS_TOKEN_URL = (
    "https://account.readcoop.eu/auth/realms/readcoop/protocol/openid-connect/token"
)
TRANSKRIBUS_CLIENT_ID = "processing-api-client"
#: Results stay at Transkribus 24 hours after a job finishes (v2's
#: documentation; v1's says two days), so a job is reused within a day.
TRANSKRIBUS_RETENTION_SECONDS = 24 * 3600
#: How long one call waits for a job before handing back its id to call
#: again for: an MCP client's patience is finite, Transkribus' queue is not.
TRANSKRIBUS_WAIT_SECONDS = 90.0
_TERMINAL = {"FINISHED", "FAILED", "CANCELLED"}


class TranskribusError(RuntimeError):
    """Transkribus refused a request, or answered one in a way it should not."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _transkribus_detail(resp: httpx.Response) -> str:
    """The message in a Transkribus or READ-COOP error body."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200].strip() or f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        return str(
            body.get("message")
            or body.get("error_description")
            or body.get("reasonPhrase")
            or body.get("error")
            or body
        )
    return str(body)


class TranskribusClient:
    """The Transkribus processing API: submit one image, poll, fetch PAGE XML.

    Logs in to READ-COOP's OpenID Connect realm with the account's user name
    and password (the documented flow: ``grant_type=password``, client
    ``processing-api-client``), renews the token with its refresh token, and
    logs in again when that fails.

    Parameters
    ----------
    username, password : str
        A Transkribus account on the Scholar plan or above.
    api_url : str
        The processing API, by default Metagrapho v1.
    """

    def __init__(
        self,
        username: str,
        password: str,
        api_url: str = TRANSKRIBUS_API_URL,
        *,
        timeout: float = 60.0,
    ):
        self._username = username
        self._password = password
        self._api = api_url.rstrip("/")
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires = 0.0
        self._http = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        """Close the HTTP transport."""
        await self._http.aclose()

    async def _token_request(self, **form: str) -> bool:
        resp = await self._http.post(
            TRANSKRIBUS_TOKEN_URL, data={"client_id": TRANSKRIBUS_CLIENT_ID, **form}
        )
        if resp.status_code != 200:
            if form.get("grant_type") == "password":
                raise TranskribusError(
                    resp.status_code,
                    "READ-COOP refused the Transkribus login: "
                    f"{_transkribus_detail(resp)}. Check GRAMPS_MCP_TRANSKRIBUS_USERNAME "
                    "and GRAMPS_MCP_TRANSKRIBUS_PASSWORD.",
                )
            return False
        data = resp.json()
        self._access = data["access_token"]
        self._refresh = data.get("refresh_token") or self._refresh
        # Renew a little early, so a token does not expire in flight.
        self._expires = time.monotonic() + float(data.get("expires_in") or 300) - 30
        return True

    async def _token(self, *, fresh: bool = False) -> str:
        if self._access and not fresh and time.monotonic() < self._expires:
            return self._access
        renewed = bool(self._refresh) and await self._token_request(
            grant_type="refresh_token", refresh_token=self._refresh or ""
        )
        if not renewed:
            await self._token_request(
                grant_type="password", username=self._username, password=self._password
            )
        assert self._access is not None
        return self._access

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self._token()
        resp = await self._http.request(
            method, self._api + path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if resp.status_code == 401:
            token = await self._token(fresh=True)
            resp = await self._http.request(
                method, self._api + path, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        if resp.status_code >= 400:
            raise TranskribusError(resp.status_code, _transkribus_detail(resp))
        return resp

    async def submit(self, image: bytes, htr_id: int) -> dict:
        """Start recognition of one image with one model.

        No language model is asked for: one steers a reading toward the words
        it knows, which is the normalising a diplomatic reading must avoid.

        Returns
        -------
        dict
            ``processId`` and ``status``.
        """
        body = {
            "config": {"textRecognition": {"htrId": htr_id}},
            "image": {"base64": base64.b64encode(image).decode("ascii")},
        }
        resp = await self._request("POST", "/processes", json=body)
        data = resp.json()
        if "processId" not in data:
            raise TranskribusError(resp.status_code, "Transkribus accepted the job but sent no id")
        return data

    async def status(self, process_id: int | str) -> dict:
        """The job's status, and once FINISHED its text and layout."""
        return (await self._request("GET", f"/processes/{process_id}")).json()

    async def page_xml(self, process_id: int | str) -> str | None:
        """The finished job as PAGE XML (2013-07-15), or None if it is not there."""
        try:
            resp = await self._request("GET", f"/processes/{process_id}/page")
        except TranskribusError as exc:
            if exc.status == 404:
                return None
            raise
        return resp.text

    async def wait(
        self, process_id: int | str, *, timeout: float | None = None, interval: float = 3.0
    ) -> dict:
        """Poll until the job ends or ``timeout`` passes; the last status either way."""
        deadline = time.monotonic() + (TRANSKRIBUS_WAIT_SECONDS if timeout is None else timeout)
        while True:
            state = await self.status(process_id)
            if str(state.get("status", "")).upper() in _TERMINAL:
                return state
            if time.monotonic() + interval > deadline:
                return state
            await _sleep(interval)


async def _sleep(seconds: float) -> None:
    """``asyncio.sleep``, as a name the tests can replace."""
    import asyncio

    await asyncio.sleep(seconds)


def content_text(content: dict | None) -> str:
    """The recognised text, a line of output for each line found on the page.

    Regions are separated by a blank line. Falls back to the job's whole
    ``text`` when it carries no lines.
    """
    content = content or {}
    blocks = []
    for region in content.get("regions") or []:
        lines = [str(line.get("text") or "") for line in region.get("lines") or []]
        if any(lines):
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else str(content.get("text") or "").strip()


@dataclass
class Ledger:
    """The pages sent to Transkribus each month, and the jobs still retrievable.

    Kept as JSON in the cache directory, so a page budget holds across
    sessions and a job is fetched again, not paid for again, within the day
    Transkribus keeps its result.
    """

    path: Path
    data: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Ledger:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("months", {})
        data.setdefault("jobs", {})
        return cls(path, data)

    @staticmethod
    def month(now: datetime) -> str:
        return now.strftime("%Y-%m")

    def pages_in(self, now: datetime) -> int:
        return int((self.data["months"].get(self.month(now)) or {}).get("pages", 0))

    def job(self, key: str, now: datetime) -> dict | None:
        """A job submitted for this key whose result Transkribus still keeps."""
        job = self.data["jobs"].get(key)
        if not job or job.get("process_id") is None:
            return None
        if now.timestamp() - float(job.get("submitted", 0)) > TRANSKRIBUS_RETENTION_SECONDS:
            return None
        return job

    def reserve(self, key: str, model: int, now: datetime) -> None:
        """Count a page before it is sent, so a page the ledger cannot record is
        never sent, and two calls cannot both take the budget's last page."""
        month = self.data["months"].setdefault(self.month(now), {"pages": 0})
        month["pages"] = int(month.get("pages", 0)) + 1
        self.data["jobs"][key] = {"process_id": None, "model": model, "submitted": now.timestamp()}
        cutoff = now.timestamp() - 2 * TRANSKRIBUS_RETENTION_SECONDS
        self.data["jobs"] = {
            k: v for k, v in self.data["jobs"].items() if float(v.get("submitted", 0)) > cutoff
        }
        self.save()

    def assign(self, key: str, process_id: Any) -> None:
        """Remember the job a reserved page became."""
        if key in self.data["jobs"]:
            self.data["jobs"][key]["process_id"] = process_id
            self.save()

    def release(self, key: str, now: datetime) -> None:
        """Give back a reserved page Transkribus refused, and so did not charge."""
        month = self.data["months"].get(self.month(now))
        if month and int(month.get("pages", 0)) > 0:
            month["pages"] = int(month["pages"]) - 1
        self.data["jobs"].pop(key, None)
        self.save()

    def forget(self, key: str) -> None:
        """Drop a job that failed or that Transkribus no longer has; its page stays counted."""
        if self.data["jobs"].pop(key, None) is not None:
            self.save()

    def record(self, key: str, process_id: Any, model: int, now: datetime) -> None:
        """Count a page and remember its job in one step."""
        self.reserve(key, model, now)
        self.assign(key, process_id)

    def save(self) -> None:
        """Write the ledger whole, by replacement, so a crash cannot truncate it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def today(now: datetime | None = None) -> str:
    """The date a reading is made or retrieved, as provenance records it."""
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Stored transcripts
# --------------------------------------------------------------------------- #
#: How a transcript this tool stores begins, so the next call can tell its
#: provenance and that it is a machine reading.
HEADER_PREFIX = "Machine transcription by "
_XML_START = re.compile(r"^\s*(<\?xml|<PcGts|<alto|<!--)", re.I)


def is_layout_xml(text: str) -> bool:
    """Whether a note holds PAGE or ALTO XML rather than a reading."""
    return bool(_XML_START.match(text or ""))


_ENGINE_LABELS = {
    "tesseract": "Tesseract (Gramps Web)",
    "transkribus": "Transkribus",
    "loc": "the Library of Congress's OCR",
    "internet_archive": "the Internet Archive's OCR",
    "pdf_text_layer": "the PDF's own text layer",
}


def header(provenance: dict) -> str:
    """The first line of a transcript note: who read it, with what, and when."""
    engine = str(provenance.get("engine") or "")
    label = _ENGINE_LABELS.get(engine, engine)
    if engine == "transkribus":
        label += f", model {provenance.get('model_id')} ({provenance.get('model')})"
    elif engine == "tesseract":
        label += f", language {provenance.get('lang')}"
    parts = [label]
    if provenance.get("url"):
        parts.append(str(provenance["url"]))
    if provenance.get("page_of"):
        parts.append("page {} of {}".format(*provenance["page_of"]))
    parts.append(str(provenance.get("date")))
    return (
        HEADER_PREFIX
        + ", ".join(parts)
        + ". A finding aid, not evidence: read the image before citing it."
    )


def parse_header(text: str) -> dict | None:
    """The provenance a stored transcript's first line records, if this tool wrote it."""
    first = (text or "").split("\n", 1)[0]
    if not first.startswith(HEADER_PREFIX):
        return None
    rest = first[len(HEADER_PREFIX) :]
    engine = next((e for e, label in _ENGINE_LABELS.items() if rest.startswith(label)), None)
    date = re.search(r"(\d{4}-\d{2}-\d{2})\. A finding aid", first)
    model = re.search(r"model (\d+) \((.*?)\)", first)
    page = re.search(r", page (\d+) of \d+,", first)
    return {
        "engine": engine or "unknown",
        "model": model.group(2) if model else None,
        "model_id": int(model.group(1)) if model else None,
        "date": date.group(1) if date else None,
        "page": int(page.group(1)) if page else None,
    }


def body_of(text: str) -> str:
    """A stored transcript without its header line."""
    if (text or "").startswith(HEADER_PREFIX):
        return text.split("\n", 1)[1].lstrip("\n") if "\n" in text else ""
    return text or ""


def xml_with_provenance(xml: str, provenance: dict) -> str:
    """PAGE XML with a comment saying where it came from, after any declaration."""
    comment = (
        f"<!-- PAGE XML from Transkribus, model {provenance.get('model_id')} "
        f"({provenance.get('model')}), job {provenance.get('process_id')}, "
        f"{provenance.get('date')}; "
        "stored by gramps-evidence-mcp ocr_media. A finding aid, not evidence. -->"
    )
    match = re.match(r"\s*<\?xml[^>]*\?>", xml)
    if match:
        return xml[: match.end()] + "\n" + comment + xml[match.end() :]
    return comment + "\n" + xml
