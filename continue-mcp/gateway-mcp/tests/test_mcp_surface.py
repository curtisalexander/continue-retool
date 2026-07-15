"""End-to-end MCP-protocol tests for the gateway: the gateway server is driven
through fastmcp's Client while it, in turn, spawns a real downstream MCP server
(tests/downstream_server.py) over stdio and aggregates its catalog.

This covers the full search -> describe -> call flow across a real process
boundary. Deterministic: no LLM, no network."""
import asyncio
import json
import os
import sys

from fastmcp import Client

DOWNSTREAM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downstream_server.py")


def _gateway_client(tmp_path, monkeypatch):
    """Point GATEWAY_CONFIG at a config that spawns the fixture server, then
    import the gateway fresh so its lifespan reads that config."""
    config = tmp_path / "gateway.config.json"
    config.write_text(json.dumps({
        "servers": {
            "demo": {"command": sys.executable, "args": [DOWNSTREAM]},
        }
    }), encoding="utf-8")
    monkeypatch.setenv("GATEWAY_CONFIG", str(config))
    from gateway_mcp.server import mcp
    return Client(mcp)


def test_search_describe_call_flow(tmp_path, monkeypatch):
    client = _gateway_client(tmp_path, monkeypatch)

    async def scenario():
        async with client as c:
            # only the three meta-tools are advertised
            tools = await c.list_tools()
            meta = {t.name for t in tools}

            found = await c.call_tool("search", {"query": "uppercase text"})
            desc = await c.call_tool("describe", {"name": "demo.upper"})
            result = await c.call_tool("call", {
                "name": "demo.upper", "arguments": {"text": "abc"},
            })
            summed = await c.call_tool("call", {
                "name": "demo.add", "arguments": {"a": 2, "b": 3},
            })
            # call now forwards the downstream result faithfully: the displayed
            # value rides in the content text (primitives arrive as text).
            return meta, found.data, desc.data, result.content[0].text, summed.content[0].text

    meta, found, desc, result, summed = asyncio.run(scenario())
    assert meta == {"search", "describe", "call"}
    assert any(t["name"] == "demo.upper" for t in found["tools"])
    assert desc["name"] == "demo.upper"
    assert "text" in desc["input_schema"].get("properties", {})
    assert result == "ABC"
    assert summed in (5, "5")  # downstream returns int; content may arrive as text


# House-style conformance, enforced mechanically (see rules/rule-rule.md).
DESCRIPTION_BUDGET_CHARS = 1000  # ~250 tokens; catches runaway growth


def test_descriptions_and_annotations(tmp_path, monkeypatch):
    client = _gateway_client(tmp_path, monkeypatch)

    async def scenario():
        async with client as c:
            return await c.list_tools()

    tools = {t.name: t for t in asyncio.run(scenario())}
    for t in tools.values():
        assert t.description, f"{t.name} has no description"
        assert len(t.description) <= DESCRIPTION_BUDGET_CHARS
    for name in ("search", "describe"):
        ann = tools[name].annotations
        assert ann and ann.readOnlyHint is True, f"{name} should be readOnlyHint"


def test_unknown_tool_suggests_alternatives(tmp_path, monkeypatch):
    client = _gateway_client(tmp_path, monkeypatch)

    async def scenario():
        async with client as c:
            bad = await c.call_tool("call", {"name": "demo.uppr", "arguments": {}})
            return bad.data

    bad = asyncio.run(scenario())
    assert "error" in bad
    assert "demo.upper" in bad.get("did_you_mean", [])
