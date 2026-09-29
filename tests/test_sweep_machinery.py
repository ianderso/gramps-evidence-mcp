"""Tests for the sweep's own argument builder and drift guard.

A whole-surface sweep is only as good as the arguments it invokes tools with.
When those arguments stop being valid, the tool returns a tidy error envelope,
the sweep's "an envelope came back" assertion passes, and the behaviour under
test quietly stops being tested. Nothing fails when that happens, so the
machinery is tested here rather than trusted.
"""

from __future__ import annotations

import pytest

from .conftest import (
    LOCAL_VALIDATION_ERRORS,
    assert_reached_body,
    valid_args,
)


class _Tool:
    """A stand-in for a registered tool, carrying only what the builder reads."""

    def __init__(self, name: str, schema: dict):
        self.name = name
        self.input_schema = schema


def _schema(required: list[str], **props) -> dict:
    return {"type": "object", "properties": props, "required": required}


# --------------------------------------------------------------------------- #
# The drift guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", sorted(LOCAL_VALIDATION_ERRORS))
async def test_the_guard_fires_on_every_local_validation_error(code):
    """Each of these means the sweep never reached the tool's body."""
    with pytest.raises(AssertionError) as exc:
        assert_reached_body("some_tool", {"error": code, "message": "nope"})
    assert "did not test it" in str(exc.value)
    assert "some_tool" in str(exc.value)


async def test_the_guard_names_the_fix():
    """An assertion nobody can act on is a worse failure than none."""
    with pytest.raises(AssertionError) as exc:
        assert_reached_body("query_records", {"error": "no_criteria"})
    assert "ARGUMENT_HINTS" in str(exc.value)


async def test_the_guard_allows_a_genuine_api_failure_through():
    """An injected API error is the thing a sweep is trying to observe."""
    assert_reached_body("get_person", {"error": "api", "status": 500})


async def test_the_guard_allows_a_successful_result():
    """Not every sweep injects a failure."""
    assert_reached_body("db_stats", {"person": 10})


# --------------------------------------------------------------------------- #
# The builder
# --------------------------------------------------------------------------- #
async def test_only_required_properties_are_supplied():
    """Filling in optional parameters would test a different call each time."""
    tool = _Tool(
        "t",
        _schema(
            ["needed"],
            needed={"type": "string"},
            optional={"type": "string"},
        ),
    )
    assert set(valid_args(tool)) == {"needed"}


async def test_an_enum_takes_its_first_member():
    """A generic string would be rejected by a constrained parameter."""
    tool = _Tool("t", _schema(["kind"], kind={"enum": ["census", "birth"]}))
    assert valid_args(tool)["kind"] == "census"


@pytest.mark.parametrize(
    ("name", "spec", "check"),
    [
        ("object_type", {"type": "string"}, lambda v: v == "person"),
        ("target_type", {"type": "string"}, lambda v: v == "person"),
        ("file_path", {"type": "string"}, lambda v: v.startswith("/")),
        ("destination", {"type": "string"}, lambda v: v.startswith("/")),
        ("image_url", {"type": "string"}, lambda v: v.startswith("https://")),
        ("birth_year", {"type": "integer"}, lambda v: v == 1900),
        ("start_date", {"type": "string"}, lambda v: v == "1900"),
    ],
)
async def test_parameter_names_drive_plausible_values(name, spec, check):
    """Names carry meaning the schema does not.

    A parameter called file_path wants a path and one called object_type
    wants a Gramps type; a generic string satisfies neither, and the tool
    rejects it before doing any work.
    """
    tool = _Tool("t", _schema([name], **{name: spec}))
    assert check(valid_args(tool)[name])


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("integer", 1), ("number", 1.0), ("boolean", False), ("array", [])],
)
async def test_types_without_a_name_hint_fall_back_to_the_schema(kind, expected):
    """The schema is the second source of truth after the name."""
    tool = _Tool("t", _schema(["v"], v={"type": kind}))
    assert valid_args(tool)["v"] == expected


async def test_a_nested_model_gets_a_valid_instance():
    """A bare {} fails the model's own validator before the tool runs."""
    tool = _Tool("t", _schema(["citation"], citation={"$ref": "#/CitationInput"}))
    citation = valid_args(tool)["citation"]
    assert citation["source_title"]
    assert citation["page"]


async def test_overrides_win_over_everything_derived():
    """A caller testing a specific case must be able to force a value."""
    tool = _Tool("t", _schema(["object_type"], object_type={"type": "string"}))
    assert valid_args(tool, object_type="family")["object_type"] == "family"


async def test_a_tool_with_no_required_parameters_needs_no_arguments():
    """db_stats and its like take nothing; that is not a gap to fill."""
    assert valid_args(_Tool("db_stats", _schema([]))) == {}


async def test_an_empty_string_default_is_not_used_as_a_value():
    """A value of "" is what makes a tool answer no_criteria.

    Most optional string parameters default to "", so a builder that trusted
    defaults would supply nothing useful and stop at the tool's own checks.
    """
    tool = _Tool("t", _schema(["surname"], surname={"type": "string", "default": ""}))
    assert valid_args(tool)["surname"] != ""


async def test_a_meaningful_default_beats_the_generic_fallback():
    """A declared default is the tool's own idea of a sensible value."""
    tool = _Tool("t", _schema(["limit"], limit={"type": "integer", "default": 50}))
    assert valid_args(tool)["limit"] == 50
