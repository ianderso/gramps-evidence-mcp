"""An MCP server over a self-hosted Gramps Web tree, built around citations.

The server is a REST client of gramps-webapi and never opens the Gramps
database files, so the API server serializes concurrent access. See
``docs/ARCHITECTURE.md``.
"""

__version__ = "1.0.1"
