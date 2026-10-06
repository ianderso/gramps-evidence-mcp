"""Configuration loading: where ``.env`` is read from."""

from __future__ import annotations

import pytest

from gramps_evidence_mcp.config import ConfigError, load_config

_VARS = (
    "GRAMPS_MCP_API_URL",
    "GRAMPS_MCP_USERNAME",
    "GRAMPS_MCP_PASSWORD",
    "GRAMPS_MCP_CONFIG",
    "GRAMPS_MCP_EXPOSE_PRIVATE",
)


def test_env_file_is_read_from_the_working_directory(tmp_path, monkeypatch):
    """Not from beside the installed package, where no user keeps one."""
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(
        "GRAMPS_MCP_API_URL=http://gramps.example.org:5000/\n"
        "GRAMPS_MCP_USERNAME=mcp\n"
        "GRAMPS_MCP_PASSWORD=pw\n"
    )
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.api_url == "http://gramps.example.org:5000"
    assert cfg.username == "mcp"


def test_real_environment_variables_win_over_the_env_file(tmp_path, monkeypatch):
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(
        "GRAMPS_MCP_API_URL=http://from-file\nGRAMPS_MCP_USERNAME=mcp\nGRAMPS_MCP_PASSWORD=pw\n"
    )
    monkeypatch.setenv("GRAMPS_MCP_API_URL", "http://from-env")
    monkeypatch.chdir(tmp_path)
    assert load_config().api_url == "http://from-env"


def _connection(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRAMPS_MCP_API_URL", "http://gramps.example.org")
    monkeypatch.setenv("GRAMPS_MCP_USERNAME", "mcp")
    monkeypatch.setenv("GRAMPS_MCP_PASSWORD", "pw")


def test_transkribus_is_optional_and_off_by_default(tmp_path, monkeypatch):
    _connection(monkeypatch, tmp_path)
    cfg = load_config()
    assert not cfg.transkribus_configured
    assert cfg.transkribus_page_budget == 0
    assert cfg.transkribus_api_url == "https://transkribus.eu/processing/v1"


def test_transkribus_settings_are_read_and_the_password_kept_out_of_repr(tmp_path, monkeypatch):
    _connection(monkeypatch, tmp_path)
    monkeypatch.setenv("GRAMPS_MCP_TRANSKRIBUS_USERNAME", "reader@example.org")
    monkeypatch.setenv("GRAMPS_MCP_TRANSKRIBUS_PASSWORD", "tk-secret")
    monkeypatch.setenv("GRAMPS_MCP_TRANSKRIBUS_PAGE_BUDGET", "20")
    monkeypatch.setenv("GRAMPS_MCP_TRANSKRIBUS_API_URL", "https://example.org/v2/")
    cfg = load_config()
    assert cfg.transkribus_configured
    assert cfg.transkribus_page_budget == 20
    assert cfg.transkribus_api_url == "https://example.org/v2"
    assert "tk-secret" not in repr(cfg)


@pytest.mark.parametrize(
    ("name", "value", "says"),
    [
        ("GRAMPS_MCP_TRANSKRIBUS_USERNAME", "reader@example.org", "both"),
        ("GRAMPS_MCP_TRANSKRIBUS_PAGE_BUDGET", "ten", "not a number of pages"),
        ("GRAMPS_MCP_TRANSKRIBUS_PAGE_BUDGET", "-1", "not a number of pages"),
    ],
)
def test_a_half_set_account_or_a_bad_budget_is_refused_at_startup(
    tmp_path, monkeypatch, name, value, says
):
    _connection(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert says in str(exc.value)
