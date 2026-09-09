"""MCP-protocol tests: drive sql-mcp through fastmcp's Client, the same way an
MCP client (Continue) would. Deterministic: no LLM, no network, in-process."""
import asyncio
import sys

import pytest
from fastmcp import Client
from mcp.shared.exceptions import McpError
from mcp.types import CancelledNotification, CancelledNotificationParams

from sql_mcp import server
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


def test_mcp_cancellation_reaps_sql_without_disconnect(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    script = tmp_path / "formatter.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).touch()\n"
        "time.sleep(30)\n", encoding="utf-8",
    )
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def recording_spawn(*args, **kwargs):
        proc = await spawn(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_spawn)
    monkeypatch.setattr(server, "sqruff_bin", lambda: sys.executable)
    monkeypatch.setattr(server, "_base_args", lambda *_: [str(script), str(ready)])

    async def scenario():
        async with Client(mcp) as client:
            request_id = client.session._request_id
            call = asyncio.create_task(client.call_tool("format", {"sql": "select 1"}))
            try:
                async with asyncio.timeout(5):
                    while not ready.exists():
                        await asyncio.sleep(.01)
                await client.session.send_notification(CancelledNotification(
                    params=CancelledNotificationParams(requestId=request_id),
                ))
                with pytest.raises(McpError, match="cancelled"):
                    await call
                assert await client.list_tools()
                async with asyncio.timeout(5):
                    while processes[0].returncode is None:
                        await asyncio.sleep(.01)
            finally:
                for proc in processes:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()

    asyncio.run(scenario())
    assert len(processes) == 1 and processes[0].returncode is not None


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
