from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


def _transport(module: str, workspace: Path) -> StdioTransport:
    return StdioTransport(
        command=sys.executable,
        args=["-c", f"from {module}.server import main; main()"],
        env={**os.environ, "MCP_WORKSPACE": str(workspace)},
        cwd=str(workspace),
        keep_alive=False,
    )


def test_unicode_round_trip_over_real_stdio_transport(tmp_path: Path) -> None:
    """Exercise JSON framing and two real server processes, not in-process MCP."""
    path = tmp_path / "café-日本語-🚀.txt"
    content = "composed café\ncombining café\n日本語 and 🚀\n"

    async def scenario() -> tuple[dict, dict]:
        async with Client(_transport("edit_mcp", tmp_path)) as edit_client:
            created = await edit_client.call_tool(
                "create_file", {"path": str(path), "content": content}
            )
        async with Client(_transport("fs_mcp", tmp_path)) as fs_client:
            read = await fs_client.call_tool("read", {"path": str(path)})
        return created.data, read.data

    created, read = asyncio.run(scenario())
    assert created["ok"] is True
    assert read["encoding"] == "utf-8" and read["decode_loss"] is False
    assert "café" in read["content"]
    assert "café" in read["content"]
    assert "日本語 and 🚀" in read["content"]
