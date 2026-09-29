"""Entry point: ``python -m gramps_evidence_mcp`` / the ``gramps-evidence-mcp`` console script."""

from __future__ import annotations

from .server import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()
