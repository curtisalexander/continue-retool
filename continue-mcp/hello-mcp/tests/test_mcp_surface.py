"""MCP-protocol tests: exercise the server through fastmcp's Client, the same
way an MCP client (Continue) would — list tools, call them, check results.
Deterministic by design: no LLM, no network, in-process transport."""
import asyncio

from fastmcp import Client

from hello_mcp.server import mcp


def test_tools_advertised():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(scenario())
    assert {t.name for t in tools} == {"ping", "echo", "whoami"}
    # every description is present (it's what the model sees)
    assert all(t.description for t in tools)


def test_ping_and_echo_round_trip():
    async def scenario():
        async with Client(mcp) as c:
            pong = await c.call_tool("ping", {})
            echo = await c.call_tool("echo", {"text": "continue-mcp"})
            return pong, echo

    pong, echo = asyncio.run(scenario())
    assert pong.data == "pong"
    assert echo.data == "continue-mcp"


def test_whoami_reports_host():
    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool("whoami", {})

    res = asyncio.run(scenario())
    assert set(res.data) >= {"system", "machine", "python", "cwd", "resolved_base"}


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
    for name in ('ping', 'echo', 'whoami'):
        ann = tools[name].annotations
        assert ann and ann.readOnlyHint is True, f"{name} should be readOnlyHint"
