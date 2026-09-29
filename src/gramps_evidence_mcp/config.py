"""Configuration from environment variables and a TOML file.

Credentials come from the environment only, never from the TOML file, so the
TOML file is safe to share.

Environment variables, all prefixed ``GRAMPS_MCP_``:

===================  =========================================================
``API_URL``          Base URL of the gramps-webapi.
``USERNAME``         API user. Use a dedicated account, not a human login.
``PASSWORD``         That user's password.
``CONFIG``           Path to the TOML file. Default ``./gramps_mcp.toml``.
``EXPOSE_PRIVATE``   Override the TOML ``expose_private`` flag.
``TRANSPORT``        ``stdio`` (default) or ``http``, for Streamable HTTP.
``HOST``, ``PORT``   Where ``http`` listens. Default ``127.0.0.1:8090``.
===================  =========================================================

The TOML file holds non-secret settings: the reference GEDCOM list, the privacy
flag, cache location, request timeout. See ``gramps_mcp.example.toml``.
"""

from __future__ import annotations

import os
import tomllib  # stdlib (Python >= 3.11, which this project requires)
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ENV_PREFIX = "GRAMPS_MCP_"


@dataclass
class ReferenceFileConfig:
    """One legacy GEDCOM file in the read-only reference layer.

    Attributes
    ----------
    path : Path
        Absolute path to the GEDCOM file.
    label : str
        Short name reported with every answer drawn from this file.
    trust : str
        Provenance note travelling with every answer from this file.
    """

    path: Path
    label: str
    trust: str = ""


@dataclass
class Config:
    """Resolved server configuration.

    Attributes
    ----------
    api_url, username, password : str
        Gramps Web connection, from the environment.
    expose_private : bool
        Disable privacy filtering when True.
    cache_dir : Path
        Root for parsed-GEDCOM caches.
    reference_files : list of ReferenceFileConfig
        Legacy GEDCOMs served by the reference layer.
    unsourced_attribute : str
        Attribute name stamped on events recorded without a citation.
    request_timeout : float
        HTTP timeout in seconds for gramps-webapi calls.
    """

    api_url: str
    username: str
    password: str

    expose_private: bool = False
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "gramps-evidence-mcp")
    reference_files: list[ReferenceFileConfig] = field(default_factory=list)
    unsourced_attribute: str = "UNSOURCED"
    request_timeout: float = 30.0

    @property
    def gedcom_cache_dir(self) -> Path:
        """Path: Directory holding parsed-GEDCOM caches."""
        return self.cache_dir / "gedcom"


def _env(name: str) -> str | None:
    """Read ``GRAMPS_MCP_<name>`` from the environment."""
    return os.environ.get(ENV_PREFIX + name)


def _as_bool(val: str | None, default: bool) -> bool:
    """Parse a truthy environment string, falling back to ``default``."""
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_dotenv_from_cwd() -> None:
    """Read ``.env`` from the working directory, if there is one.

    Real environment variables win. Only the working directory is consulted:
    ``load_dotenv()`` with no path searches upward from the *calling
    module's* location instead, which for an installed package is
    ``site-packages``. It would ignore the ``.env`` beside the user and could
    read an unrelated one from a parent directory.
    """
    load_dotenv(Path.cwd() / ".env")


#: What ``GRAMPS_MCP_TRANSPORT`` accepts, mapped to the SDK's transport names.
TRANSPORTS = {"stdio": "stdio", "http": "streamable-http", "streamable-http": "streamable-http"}


def transport_settings() -> tuple[str, dict]:
    """Resolve the transport to serve on from the environment.

    Returns
    -------
    tuple of (str, dict)
        The SDK transport name and its keyword options: none for stdio, host
        and port for Streamable HTTP.

    Raises
    ------
    ConfigError
        On an unknown transport or a port that is not a number.
    """
    load_dotenv_from_cwd()
    name = (_env("TRANSPORT") or "stdio").strip().lower()
    if name not in TRANSPORTS:
        raise ConfigError(
            f"{ENV_PREFIX}TRANSPORT={name!r} is not a transport. Use 'stdio' "
            "(the default, for a client that launches the server) or 'http'."
        )
    transport = TRANSPORTS[name]
    if transport == "stdio":
        return transport, {}
    port = _env("PORT") or "8090"
    if not port.isdigit():
        raise ConfigError(f"{ENV_PREFIX}PORT={port!r} is not a port number.")
    return transport, {"host": _env("HOST") or "127.0.0.1", "port": int(port)}


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from the environment and a TOML file.

    A ``.env`` in the working directory is read if present; real environment
    variables win. See :func:`load_dotenv_from_cwd`.

    Parameters
    ----------
    config_path : Path, optional
        TOML file to read. Defaults to ``$GRAMPS_MCP_CONFIG`` or
        ``./gramps_mcp.toml``. A missing file is not an error.

    Returns
    -------
    Config
        Fully resolved configuration.

    Raises
    ------
    ConfigError
        If a required connection variable is unset. Raised at startup rather
        than on the first tool call.
    """
    load_dotenv_from_cwd()

    api_url = _env("API_URL")
    username = _env("USERNAME")
    password = _env("PASSWORD")

    missing = [
        ENV_PREFIX + n
        for n, v in (("API_URL", api_url), ("USERNAME", username), ("PASSWORD", password))
        if not v
    ]
    if missing:
        raise ConfigError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Set them (a .env file works) to point gramps-evidence-mcp at your "
            "Gramps Web instance. See README 'Configuration'."
        )
    assert api_url and username and password  # narrows the type

    path = config_path or Path(_env("CONFIG") or "gramps_mcp.toml")
    data: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)

    cfg = Config(
        api_url=api_url.rstrip("/"),
        username=username,
        password=password,
    )

    server = data.get("server", {})
    cfg.expose_private = _as_bool(_env("EXPOSE_PRIVATE"), server.get("expose_private", False))
    if "cache_dir" in server:
        cfg.cache_dir = Path(server["cache_dir"]).expanduser()
    cfg.unsourced_attribute = server.get("unsourced_attribute", cfg.unsourced_attribute)
    cfg.request_timeout = float(server.get("request_timeout", cfg.request_timeout))

    base = path.parent if path.exists() else Path.cwd()
    for entry in data.get("reference", []):
        if "path" not in entry:
            continue
        p = Path(entry["path"]).expanduser()
        if not p.is_absolute():
            p = (base / p).resolve()
        cfg.reference_files.append(
            ReferenceFileConfig(
                path=p,
                label=entry.get("label", p.stem),
                trust=entry.get("trust", ""),
            )
        )
    return cfg


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""
