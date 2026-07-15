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


# House-style conformance, enforced mechanically (see rules/rule-rule.md).
DESCRIPTION_BUDGET_CHARS = 1000  # ~250 tokens; catches runaway growth


def test_descriptions_present_and_within_budget():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    for t in asyncio.run(scenario()):
        assert t.description, f"{t.name} has no description"
        assert len(t.description) <= DESCRIPTION_BUDGET_CHARS, (
            f"{t.name} description is {len(t.description)} chars "
            f"(budget {DESCRIPTION_BUDGET_CHARS})"
        )


def test_read_only_tools_are_annotated():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = {t.name: t for t in asyncio.run(scenario())}
    for name in ('format', 'lint'):
        ann = tools[name].annotations
        assert ann and ann.readOnlyHint is True, f"{name} should be readOnlyHint"
