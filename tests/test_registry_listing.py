"""Tests over the MCP Registry listing: ``server.json`` and the README marker.

The registry lists what ``server.json`` says, and confirms that the PyPI
package is this project's by finding ``mcp-name: <name>`` in the README that
PyPI holds for that exact version. Nothing else fails if the three drift
apart; the release would, after PyPI had already accepted it.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from gramps_evidence_mcp import __version__
from gramps_evidence_mcp.config import ENV_PREFIX

ROOT = Path(__file__).parent.parent
SERVER_JSON = json.loads((ROOT / "server.json").read_text())
PACKAGE = SERVER_JSON["packages"][0]

#: Read by the server but meaningless to a listing that launches it over
#: stdio: they choose the HTTP transport and where it listens.
_HTTP_ONLY = {"GRAMPS_MCP_TRANSPORT", "GRAMPS_MCP_HOST", "GRAMPS_MCP_PORT"}


def test_server_json_carries_the_package_version_twice():
    """The listing names a version, and so does the package it points at."""
    assert SERVER_JSON["version"] == __version__
    assert PACKAGE["version"] == __version__


def test_server_json_points_at_this_package_on_pypi():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert PACKAGE["registryType"] == "pypi"
    assert PACKAGE["identifier"] == project["name"]
    assert PACKAGE["transport"] == {"type": "stdio"}


def test_the_readme_carries_the_registry_marker_for_this_name():
    """Without it the registry refuses the listing as someone else's package.

    The token must end at a boundary: ``mcp-name: x/y.`` does not match.
    """
    marker = re.escape(f"mcp-name: {SERVER_JSON['name']}")
    assert re.search(marker + r"(\s|-->|<)", (ROOT / "README.md").read_text())


def test_server_json_declares_every_setting_the_server_reads():
    """A client configures the server from this list, so a gap is a setting nobody sets."""
    source = (ROOT / "src/gramps_evidence_mcp/config.py").read_text()
    read = {ENV_PREFIX + name for name in re.findall(r'_env\("([A-Z_]+)"\)', source)}
    declared = {v["name"] for v in PACKAGE["environmentVariables"]}
    assert declared == read - _HTTP_ONLY


def test_only_the_passwords_are_secret():
    """A client masks a secret; the URLs and user names are not."""
    secret = {v["name"] for v in PACKAGE["environmentVariables"] if v.get("isSecret")}
    assert secret == {"GRAMPS_MCP_PASSWORD", "GRAMPS_MCP_TRANSKRIBUS_PASSWORD"}


def test_the_server_reports_its_version_when_a_session_starts():
    """Clients show it; an empty string names no release at all."""
    from gramps_evidence_mcp.server import mcp

    options = mcp._lowlevel_server.create_initialization_options()
    assert options.server_version == __version__


def test_the_description_fits_the_registry_limit():
    """The MCP Registry refuses a description over 100 characters, after PyPI has the release."""
    assert len(SERVER_JSON["description"]) <= 100
