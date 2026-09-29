"""Regenerate the tool-schema snapshot.

Run after a deliberate change to a tool name or parameter list::

    uv run python -m tests.regen_tool_snapshot
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from gramps_evidence_mcp.server import mcp

SNAPSHOT = Path(__file__).parent / "fixtures" / "tool_schema.json"


async def main() -> None:
    """Write the current tool names and parameters to the snapshot file."""
    tools = sorted(await mcp.list_tools(), key=lambda t: t.name)
    data = {t.name: sorted((t.input_schema or {}).get("properties", {}) or {}) for t in tools}
    SNAPSHOT.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(data)} tools to {SNAPSHOT}")


if __name__ == "__main__":
    asyncio.run(main())
