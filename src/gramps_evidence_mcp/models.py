"""Pydantic models for MCP tool inputs and outputs.

These are the tool-facing contract, deliberately simpler than the raw Gramps
object model; :mod:`gramps_evidence_mcp.mapping` translates between them and the
gramps-webapi wire format.

Docstrings and ``Field`` descriptions in this module are published in the tool
JSON schema, so they are written for the model calling the tool rather than for
a developer reading the source.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class Gender(str, Enum):
    """Gramps gender. Maps to Person gender ints in the mapping layer."""

    female = "female"  # Gramps 0
    male = "male"  # Gramps 1
    unknown = "unknown"  # Gramps 2


class Confidence(str, Enum):
    """Gramps citation confidence levels (int 0..4 on the wire)."""

    very_low = "very_low"  # 0 - unreliable/estimated
    low = "low"  # 1 - questionable
    normal = "normal"  # 2 - secondary evidence
    high = "high"  # 3 - direct/primary but not original
    very_high = "very_high"  # 4 - original record, primary information


class _StrictInput(BaseModel):
    """Base for tool inputs: a key the model does not define is refused.

    Pydantic's default is to ignore it, and on a write that loses data
    without a word -- ``{"type": "Birth", "when": "1790"}`` wrote a birth
    with no date, and ``{"source_title": "X", "pages": "p. 4"}`` a citation
    with no page, both reported as success. The tool arguments themselves
    refuse unknown names (see ``refuse_unknown_arguments`` in the server);
    this closes the same gap one level down.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _name_the_unknown(cls, data: Any) -> Any:
        # A ValueError here reaches the caller naming the field it came
        # from, so a caller that guessed a key can correct itself.
        if isinstance(data, dict):
            accepted = sorted(f.alias or n for n, f in cls.model_fields.items())
            if unknown := sorted(set(data) - set(accepted)):
                raise ValueError(
                    f"{cls.__name__} has no field "
                    f"{', '.join(repr(u) for u in unknown)}. It takes: "
                    f"{', '.join(accepted)}."
                )
        return data


class NameParts(_StrictInput):
    """A person's primary name, broken into Gramps name components."""

    given: str = Field(default="", description="Given/first name(s), e.g. 'John Robert'.")
    surname: str = Field(default="", description="Family name / surname.")
    prefix: str = Field(default="", description="Surname prefix, e.g. 'van', 'de la'.")
    suffix: str = Field(default="", description="Suffix, e.g. 'Jr.', 'III'.")
    title: str = Field(default="", description="Title, e.g. 'Dr.', 'Rev.'.")
    nick: str = Field(default="", description="Nickname.")
    call: str = Field(default="", description="Call name (the given name actually used).")


class NameMatch(_StrictInput):
    """Which of a person's names. Every field given must match, ignoring case.

    get_person lists the names: the primary, and each alternate with its index.
    """

    given: str | None = Field(default=None, description="Given name(s) as stored.")
    surname: str | None = Field(default=None, description="Surname as stored.")
    type: str | None = Field(
        default=None,
        description="Name type: 'Birth Name', 'Also Known As', 'Married Name', ...",
    )
    primary: bool | None = Field(
        default=None,
        description="True: the primary name only. False: alternate names only.",
    )
    index: int | None = Field(
        default=None,
        ge=0,
        description="Position among the alternate names, as get_person lists them. "
        "Only needed to tell identical duplicates apart.",
    )

    @model_validator(mode="after")
    def _says_something(self) -> NameMatch:
        if (
            self.primary is None
            and self.index is None
            and not any((self.given, self.surname, self.type))
        ):
            raise ValueError(
                "NameMatch needs at least one of: given, surname, type, primary, index."
            )
        return self


class CitationInput(_StrictInput):
    """How to cite a fact. Either reference an existing citation, or create one.

    Precedence:
    1. ``citation`` -> reuse that exact citation.
    2. else ``source`` -> create a new citation on that existing source.
    3. else ``source_title`` -> create a new source AND a citation on it.

    ``page`` is where in the source the fact appears ("Vol 3, p. 45; line 12").
    ``confidence`` is your assessment of how strongly the source supports the fact.
    """

    citation: str | None = Field(
        default=None,
        description="Existing citation to reuse: handle or gramps_id "
        "(e.g. 'C0001'). Only when it supports this exact claim -- a citation "
        "carries one confidence.",
    )
    source: str | None = Field(
        default=None,
        description="Existing source to cite: handle or gramps_id "
        "(e.g. 'S0001'). A new citation is created on it.",
    )
    source_title: str | None = Field(
        default=None,
        description="Title of a NEW source to create, e.g. 'Ohio Birth Certificate #12345'.",
    )
    source_author: str | None = Field(default=None, description="Author of the new source.")
    source_pubinfo: str | None = Field(
        default=None, description="Publication info of the new source."
    )
    page: str = Field(
        default="",
        description="Where in the source the fact appears, e.g. 'p. 45, entry 12'.",
    )
    confidence: Confidence = Field(
        default=Confidence.normal, description="Confidence the source supports this fact."
    )
    date: str | None = Field(
        default=None, description="Date the source was recorded/accessed (free text)."
    )
    note: str | None = Field(default=None, description="Free-text note attached to the citation.")

    @model_validator(mode="after")
    def _at_least_one_target(self) -> CitationInput:
        if not any((self.citation, self.source, self.source_title)):
            raise ValueError(
                "CitationInput needs one of: citation (an existing citation), "
                "source (an existing source), or source_title (to create one)."
            )
        return self


class EventInput(_StrictInput):
    """A dated/placed fact about a person or family."""

    type: str = Field(
        description="Gramps event type, e.g. 'Birth', 'Death', 'Marriage', "
        "'Baptism', 'Burial', 'Residence', 'Occupation', 'Census'.",
    )
    date: str | None = Field(
        default=None,
        description="Date, Gramps style: '1899', '12 JAN 1899', 'ABT 1900', "
        "'BEF 1950'; 'BET 1898 AND 1901' happened once within the range; "
        "'FROM 1864 TO 1865' lasted the whole span; 'FROM 1880' or 'TO 1890' is "
        "open at one end.",
    )
    place: str | None = Field(
        default=None,
        description="Place reference. Resolved in order: existing handle/"
        "gramps_id, exact title ('Columbus, Ohio, USA'), then exact "
        "unique name ('Columbus' — errors if several places share it); only a "
        "string matching nothing creates a new place. Prefer the gramps_id or "
        "full title of an existing place.",
    )
    description: str | None = Field(default=None, description="Free-text description.")
    citation: CitationInput | None = Field(
        default=None, description="Citation supporting this event (see require_citation)."
    )
    allow_new_type: bool = Field(
        default=False,
        description="Accept a type that is neither a Gramps standard type nor one of "
        "the tree's custom types, creating it as a new custom type. Only when meant: a "
        "near-miss is refused with the closest names.",
    )
    _type_resolved: bool = PrivateAttr(default=False)


class VitalEventInput(EventInput):
    """A birth, death or marriage given with the person or family it belongs to.

    The parameter already names the event, so its type may be left out;
    ``add_person`` and ``add_family`` fill it in.
    """

    type: str | None = Field(
        default=None,
        description="Leave out: the parameter names the event (Birth, Death or Marriage).",
    )


class CitationEdit(_StrictInput):
    """One row of update_citations: which citation, and what to set on it."""

    citation: str = Field(description="Citation handle or gramps_id, e.g. 'C0001'.")
    page: str | None = Field(default=None, description="New locator. Omit to keep.")
    confidence: Confidence | None = Field(default=None, description="New grading. Omit to keep.")
    expect_page_prefix: str | None = Field(
        default=None,
        description="Refuse this row unless the live page still starts with this, so a "
        "page edited since the sweep was planned is reported, not overwritten.",
    )


class RepositoryLink(_StrictInput):
    """One row of link_repositories."""

    source: str = Field(description="Source handle or gramps_id, e.g. 'S0001'.")
    repository: str = Field(description="Repository handle or gramps_id, e.g. 'R0001'.")
    call_number: str | None = Field(default=None, description="Call number in that repository.")
    media_type: str = Field(
        default="Unknown",
        description="Medium of the source there: 'Book', 'Microfilm', 'Electronic', ...",
    )


class WriteResult(BaseModel):
    """Shape returned by a write tool. Tools return plain dicts matching it."""

    handle: str
    gramps_id: str | None = None
    object_type: str
    message: str
