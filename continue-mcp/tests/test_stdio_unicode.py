from __future__ import annotations

import asyncio
import os
import re
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


def test_continue_content_only_pagination_over_stdio(tmp_path, monkeypatch):
    """Continue's pinned MCP adapter ignores structuredContent. Reconstruct
    all displayed lines using ONLY text content, including horizontal pages."""
    monkeypatch.setenv("FS_MCP_MAX_BYTES", "1024")
    monkeypatch.setenv("FS_MCP_MAX_LINE_CHARS", "40")
    path = tmp_path / "wide.txt"
    original = "short\n" + "α🙂界" * 43 + "TAIL\n\nlast\tcolumn"
    path.write_text(original, encoding="utf-8")

    async def scenario():
        pieces = {}
        args = {"path": str(path), "limit": 2}
        async with Client(_transport("fs_mcp", tmp_path)) as client:
            for _ in range(20):
                result = await client.call_tool("read", args)
                assert not result.is_error
                text = "\n".join(b.text for b in result.content if b.type == "text")
                assert "encoding=utf-8" in text and "decode_loss=false" in text
                for number, content in re.findall(r"^(\d+)\t(.*)$", text, re.MULTILINE):
                    pieces[int(number)] = pieces.get(int(number), "") + content
                continuation = re.search(r"read on with start_line=(\d+), start_column=(\d+)", text)
                if continuation is None:
                    assert "truncated=false" in text
                    return [pieces[n] for n in sorted(pieces)]
                args.update(start_line=int(continuation[1]), start_column=int(continuation[2]))
        raise AssertionError("pagination failed to terminate")

    assert asyncio.run(scenario()) == original.split("\n")


def test_application_failures_are_protocol_errors_over_stdio(tmp_path):
    async def scenario():
        async with Client(_transport("edit_mcp", tmp_path)) as client:
            missing = await client.call_tool("edit", {
                "path": "absent.txt", "old_string": "a", "new_string": "b",
            }, raise_on_error=False)
            assert missing.is_error
            assert "file not found" in missing.content[0].text
            await client.call_tool("create_file", {"path": "exists.txt", "content": "original"})
            conflict = await client.call_tool("create_file", {
                "path": "exists.txt", "content": "replacement",
            }, raise_on_error=False)
            assert conflict.is_error and "file exists" in conflict.content[0].text
        async with Client(_transport("fs_mcp", tmp_path)) as client:
            refusal = await client.call_tool("read", {"path": "../outside.txt"}, raise_on_error=False)
            assert refusal.is_error and "workspace path scope" in refusal.content[0].text

    asyncio.run(scenario())
    assert (tmp_path / "exists.txt").read_text() == "original"
