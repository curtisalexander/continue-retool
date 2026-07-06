"""MCP-protocol tests: drive notes-mcp through fastmcp's Client, the same way
an MCP client (Continue) would. Deterministic: no LLM, no network, in-process."""
import asyncio

from fastmcp import Client

from notes_mcp.server import mcp


def _repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))


def test_tools_advertised():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(scenario())
    assert {t.name for t in tools} == {"list", "read", "write", "delete"}


def test_full_cycle_over_mcp(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch)

    async def scenario():
        async with Client(mcp) as c:
            await c.call_tool("write", {
                "name": "task-state",
                "content": "Fix half done\n\nNext: rerun suite\n",
            })
            idx = await c.call_tool("list", {})
            note = await c.call_tool("read", {"name": "task-state"})
            gone = await c.call_tool("delete", {"name": "task-state"})
            return idx.data, note.data, gone.data

    idx, note, gone = asyncio.run(scenario())
    assert idx["count"] == 1
    assert idx["notes"][0]["hook"] == "Fix half done"
    assert "Next: rerun suite" in note["content"]
    assert gone["ok"] is True
