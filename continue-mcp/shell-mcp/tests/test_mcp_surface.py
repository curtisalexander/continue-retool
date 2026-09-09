"""MCP-protocol tests: drive shell-mcp through fastmcp's Client, the same way
an MCP client (Continue) would — start a job, poll it, read output, all over
the MCP boundary. Deterministic: no LLM, no network, in-process transport."""
import asyncio
import re
import shutil
import sys

import pytest

from fastmcp import Client
from mcp.shared.exceptions import McpError
from mcp.types import CancelledNotification, CancelledNotificationParams

from shell_mcp import server
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
# every tool advertises its authority via annotations.
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


def test_shell_schema_and_description_make_interpreter_selection_explicit():
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    for name in ("run", "start"):
        shell_schema = tools[name].parameters["properties"]["shell"]["anyOf"][0]
        assert shell_schema["enum"] == ["bash", "pwsh", "powershell", "cmd"]
        assert "already invokes" in tools[name].description
        assert "default:" in tools[name].description


def test_every_tool_advertises_expected_authority():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = {t.name: t for t in asyncio.run(scenario())}
    expected = {
        "start": {"openWorldHint": True},
        "output": {"readOnlyHint": True},
        "poll": {"readOnlyHint": True},
        "kill": {"destructiveHint": True, "idempotentHint": True},
        "list_jobs": {"readOnlyHint": True},
        "run": {"openWorldHint": True},
        "send": {"openWorldHint": True},
    }
    assert set(tools) == set(expected)
    for name, hints in expected.items():
        ann = tools[name].annotations
        assert ann, f"{name} should advertise authority annotations"
        for hint, value in hints.items():
            assert getattr(ann, hint) is value, f"{name} should set {hint}={value}"


def test_run_over_mcp(shell_case, tmp_path):
    sh = shell_case.name
    script = tmp_path / "via mcp.py"
    script.write_text("print('via-mcp')\n", encoding="utf-8")

    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool("run", {
                "cmd": shell_case.invoke(PY, script),
                "shell": sh,
                "timeout": 15,
            })

    res = asyncio.run(scenario())
    assert res.data["exit_code"] == 0
    assert "via-mcp" in res.data["stdout"]
    assert res.data["state"] == "exited"


def test_rendered_content_contains_complete_recovery_contract_over_mcp():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool("run", {
                "cmd": f'"{PY}" -c "print(\'content-contract\')"',
                "shell": sh, "timeout": 15,
            })
    res = asyncio.run(scenario())
    text = "\n".join(block.text for block in res.content if block.type == "text")
    assert "content-contract" in text
    assert "job=j" in text
    assert "stdout_cursor=" in text and "stderr_cursor=" in text
    assert "encoding=" in text


def test_failure_sets_mcp_is_error_and_keeps_structured_content():
    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool("run", {"cmd": "bash nested", "shell": "bash"}, raise_on_error=False)
    res = asyncio.run(scenario())
    assert res.is_error is True
    assert res.structured_content
    assert res.structured_content["ok"] is False
    assert res.structured_content["error_type"] == "validation"


def test_content_only_incremental_output_uses_exact_rendered_cursors(tmp_path):
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    producer = tmp_path / "two_chunks.py"
    release = tmp_path / "release-second-chunk"
    producer.write_text(
        "import pathlib, sys, time\n"
        "print('async-chunk-one', flush=True)\n"
        "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.01)\n"
        "print('async-chunk-two', flush=True)\n",
        encoding="utf-8",
    )

    def rendered(result):
        return "\n".join(block.text for block in result.content if block.type == "text")

    def cursors(text):
        match = re.search(r"stdout_cursor=(\d+) stderr_cursor=(\d+)", text)
        assert match, text
        return int(match.group(1)), int(match.group(2))

    async def scenario():
        async with Client(mcp) as c:
            started = await c.call_tool("start", {
                "cmd": f'"{PY}" "{producer}" "{release}"', "shell": sh, "timeout": 15,
            })
            job_match = re.search(r"\bjob=(j\d+)\b", rendered(started))
            assert job_match
            jid = job_match[1]
            first_text = ""
            for _ in range(100):
                first_text = rendered(await c.call_tool("output", {
                    "job_id": jid, "since_stdout": 0, "since_stderr": 0,
                }))
                if "async-chunk-one" in first_text:
                    break
                await asyncio.sleep(0.01)
            out_cursor, err_cursor = cursors(first_text)
            release.touch()
            second_text = ""
            for _ in range(200):
                second_text = rendered(await c.call_tool("output", {
                    "job_id": jid,
                    "since_stdout": out_cursor,
                    "since_stderr": err_cursor,
                }))
                if "async-chunk-two" in second_text:
                    break
                await asyncio.sleep(0.01)
            return first_text, second_text, cursors(second_text)

    first, second, second_cursors = asyncio.run(scenario())
    assert "\nasync-chunk-one\n" in first and "async-chunk-two" not in first
    assert "\nasync-chunk-two\n" in second and "async-chunk-one" not in second
    assert second_cursors[0] > cursors(first)[0]


def test_client_call_cancellation_kills_tree_without_disconnect(tmp_path, shell_case):
    sh = shell_case.name
    ready = tmp_path / "child-ready"
    marker = tmp_path / "cancelled-child-side-effect"
    trigger = tmp_path / "allow-side-effect"
    child = tmp_path / "delayed_marker.py"
    child.write_text(
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).touch()\n"
        "while not pathlib.Path(sys.argv[3]).exists(): time.sleep(.01)\n"
        "pathlib.Path(sys.argv[2]).touch()\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, *sys.argv[1:]])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    async def scenario():
        server.JOBS.clear()
        async with Client(mcp) as c:
            request_id = c.session._request_id
            call = asyncio.create_task(c.call_tool("run", {
                "cmd": shell_case.invoke(PY, parent, child, ready, marker, trigger),
                "shell": sh, "timeout": 60,
            }))
            async with asyncio.timeout(5):
                while not ready.exists():
                    await asyncio.sleep(0.01)
            job = next(reversed(server.JOBS.values()))
            # Emit the MCP cancellation notification for this real call while
            # preserving the client session.
            await c.session.send_notification(CancelledNotification(
                params=CancelledNotificationParams(
                    requestId=request_id, reason="cancellation regression test",
                )
            ))
            with pytest.raises(McpError, match="cancelled"):
                await call
            # Prove the still-connected client did not rely on lifespan shutdown.
            assert await c.list_tools()
            async with asyncio.timeout(5):
                while (
                    job.state == "running"
                    or job._reaper_task is None
                    or not job._reaper_task.done()
                ):
                    await asyncio.sleep(0.01)
            trigger.touch()
            await asyncio.sleep(1)
            return job

    job = asyncio.run(scenario())
    assert job.state == "killed" and job.proc.returncode is not None
    assert job._reaper_task and job._reaper_task.done()
    assert not marker.exists()


def test_start_poll_output_lifecycle_over_mcp(shell_case, tmp_path):
    sh = shell_case.name
    script = tmp_path / "mcp lifecycle.py"
    script.write_text("print('lifecycle')\n", encoding="utf-8")

    async def scenario():
        async with Client(mcp) as c:
            started = await c.call_tool("start", {
                "cmd": shell_case.invoke(PY, script),
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


def test_client_shutdown_kills_running_job():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        async with Client(mcp) as c:
            started = await c.call_tool("start", {
                "cmd": f'"{PY}" -c "import time; time.sleep(30)"',
                "shell": sh,
                "timeout": 60,
            })
            job_id = started.data["job_id"]
        return server.JOBS[job_id]

    job = asyncio.run(scenario())
    assert job.state == "killed"
    assert job.proc.returncode is not None


def test_unknown_job_is_a_tool_error():
    from fastmcp.exceptions import ToolError

    async def scenario():
        async with Client(mcp) as c:
            await c.call_tool("poll", {"job_id": "nope"})

    with pytest.raises(ToolError):
        asyncio.run(scenario())
