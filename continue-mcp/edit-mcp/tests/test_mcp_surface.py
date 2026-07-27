"""MCP-surface contract for the intentionally narrow edit server."""
import asyncio
from fastmcp import Client
from edit_mcp.server import mcp

def test_exact_tool_surface_and_authority():
    async def scenario():
        async with Client(mcp) as client:
            return await client.list_tools()
    tools = {tool.name: tool for tool in asyncio.run(scenario())}
    assert set(tools) == {"edit", "create_file"}
    assert all(tool.description and len(tool.description) <= 1000 for tool in tools.values())
    assert all(tool.annotations.destructiveHint is True for tool in tools.values())

def test_edit_and_create_over_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    async def scenario():
        async with Client(mcp) as client:
            created = await client.call_tool("create_file", {"path": "a.txt", "content": "hello — world   "})
            edited = await client.call_tool("edit", {"path": "a.txt", "old_string": "hello - world", "new_string": "done"})
            return created.data, edited.data
    created, edited = asyncio.run(scenario())
    assert created["ok"] and edited["ok"]
    assert (tmp_path / "a.txt").read_text() == "done   "
