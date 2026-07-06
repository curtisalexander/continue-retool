"""MCP-protocol tests: drive fs-mcp through fastmcp's Client, the same way an
MCP client (Continue) would. Deterministic: no LLM, no network, in-process."""
import asyncio

from fastmcp import Client

from fs_mcp.server import mcp


def test_tools_advertised():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(scenario())
    assert {t.name for t in tools} == {"read", "list"}


def test_read_over_mcp(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\n", encoding="utf-8")

    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool("read", {"path": str(f), "start_line": 2})

    res = asyncio.run(scenario())
    assert res.data["ok"] is True
    assert res.data["content"] == "2\ttwo"


def test_list_over_mcp(tmp_path):
    (tmp_path / "x.py").write_text("", encoding="utf-8")

    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool("list", {"path": str(tmp_path)})

    res = asyncio.run(scenario())
    assert res.data["ok"] is True
    assert res.data["entries"][0]["path"] == "x.py"
