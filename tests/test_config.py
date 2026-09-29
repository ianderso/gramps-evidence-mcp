"""Configuration loading: where ``.env`` is read from."""

from __future__ import annotations

from gramps_evidence_mcp.config import load_config

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
