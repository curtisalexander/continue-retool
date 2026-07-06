"""MCP-protocol tests: drive search-mcp through fastmcp's Client, the same way
an MCP client (Continue) would. Deterministic: no LLM, no network, in-process.
Calls that reach ripgrep are skipped if rg isn't installed."""
import asyncio
import shutil

import pytest

from fastmcp import Client

from search_mcp.server import mcp

HAVE_RG = shutil.which("rg") is not None
needs_rg = pytest.mark.skipif(not HAVE_RG, reason="ripgrep (rg) not installed")


def test_tools_advertised():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(scenario())
    assert {t.name for t in tools} == {"grep", "files"}


@needs_rg
def test_grep_over_mcp(tmp_path):
    (tmp_path / "a.txt").write_text("hello\nNEEDLE here\n", encoding="utf-8")

    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool("grep", {
                "pattern": "NEEDLE", "path": str(tmp_path),
            })

    res = asyncio.run(scenario())
    assert res.data["count"] == 1
    assert res.data["matches"][0]["line"] == 2


@needs_rg
def test_files_over_mcp(tmp_path):
    (tmp_path / "x.py").write_text("", encoding="utf-8")
    (tmp_path / "y.md").write_text("", encoding="utf-8")

    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool("files", {
                "glob": ["*.py"], "path": str(tmp_path),
            })

    res = asyncio.run(scenario())
    assert res.data["count"] == 1
    assert res.data["files"][0].endswith("x.py")
