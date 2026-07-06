"""MCP-protocol tests: drive sql-mcp through fastmcp's Client, the same way an
MCP client (Continue) would. Deterministic: no LLM, no network, in-process."""
import asyncio

from fastmcp import Client

from sql_mcp.server import mcp


def test_tools_advertised():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(scenario())
    assert {t.name for t in tools} == {"format", "lint"}


def test_format_over_mcp():
    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool("format", {
                "sql": "SELECT ID FROM T WHERE ID IS NOT NULL;",
            })

    res = asyncio.run(scenario())
    assert res.data["ok"] is True
    assert "select id" in res.data["sql"]


def test_lint_over_mcp():
    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool("lint", {"sql": "SELECT A FROM b;"})

    res = asyncio.run(scenario())
    assert res.data["ok"] is True
    assert res.data["count"] > 0
