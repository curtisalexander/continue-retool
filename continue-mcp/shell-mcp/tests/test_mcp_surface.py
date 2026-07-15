"""MCP-protocol tests: drive shell-mcp through fastmcp's Client, the same way
an MCP client (Continue) would — start a job, poll it, read output, all over
the MCP boundary. Deterministic: no LLM, no network, in-process transport."""
import asyncio
import shutil
import sys

import pytest

from fastmcp import Client

from shell_mcp.server import IS_WINDOWS, mcp

PY = sys.executable


def default_shell():
    if IS_WINDOWS:
        return "cmd" if shutil.which("cmd") else None
    return "bash" if shutil.which("bash") else None


def test_tools_advertised():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(scenario())
    assert {t.name for t in tools} == {
        "start", "output", "poll", "kill", "list_jobs", "run", "send",
    }


# House-style conformance, enforced mechanically (see rules/rule-rule.md):
# every tool describes itself, descriptions can't grow without bound, and
# read-only tools say so via annotations.
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
    for name in ("poll", "output", "list_jobs"):
        ann = tools[name].annotations
        assert ann and ann.readOnlyHint is True, f"{name} should be readOnlyHint"


def test_run_over_mcp():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool("run", {
                "cmd": f'"{PY}" -c "print(\'via-mcp\')"',
                "shell": sh,
                "timeout": 15,
            })

    res = asyncio.run(scenario())
    assert res.data["exit_code"] == 0
    assert "via-mcp" in res.data["stdout"]
    assert res.data["state"] == "exited"


def test_start_poll_output_lifecycle_over_mcp():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        async with Client(mcp) as c:
            started = await c.call_tool("start", {
                "cmd": f'"{PY}" -c "print(\'lifecycle\')"',
                "shell": sh,
                "timeout": 15,
            })
            jid = started.data["job_id"]
            for _ in range(100):
                st = await c.call_tool("poll", {"job_id": jid})
                if st.data["state"] != "running":
                    break
                await asyncio.sleep(0.1)
            out = await c.call_tool("output", {"job_id": jid})
            listing = await c.call_tool("list_jobs", {})
            return st.data, out.data, listing.data["jobs"]

    st, out, listing = asyncio.run(scenario())
    assert st["state"] == "exited" and st["exit_code"] == 0
    assert "lifecycle" in out["stdout"]
    assert any(j["job_id"] == st["job_id"] for j in listing)


def test_unknown_job_is_a_tool_error():
    from fastmcp.exceptions import ToolError

    async def scenario():
        async with Client(mcp) as c:
            await c.call_tool("poll", {"job_id": "nope"})

    with pytest.raises(ToolError):
        asyncio.run(scenario())
