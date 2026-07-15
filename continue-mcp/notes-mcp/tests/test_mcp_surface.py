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
    assert {t.name for t in tools} == {"list", "read", "search", "write", "delete"}


def test_search_finds_lines_across_notes(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))

    async def scenario():
        async with Client(mcp) as c:
            await c.call_tool("write", {
                "name": "alpha", "content": "Alpha note\nthe magic token lives here\n",
            })
            await c.call_tool("write", {"name": "beta", "content": "Beta note\n"})
            hit = await c.call_tool("search", {"query": "MAGIC TOKEN"})
            miss = await c.call_tool("search", {"query": "no-such-string"})
            return hit.data, miss.data

    hit, miss = asyncio.run(scenario())
    assert hit["count"] == 1
    assert hit["matches"][0]["name"] == "alpha"
    assert miss["count"] == 0


def test_invalid_name_is_structured_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))

    async def scenario():
        async with Client(mcp) as c:
            return (await c.call_tool("read", {"name": "../escape"})).data

    res = asyncio.run(scenario())
    assert res["ok"] is False and "invalid note name" in res["error"]


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
    for name in ('list', 'read', 'search'):
        ann = tools[name].annotations
        assert ann and ann.readOnlyHint is True, f"{name} should be readOnlyHint"
