"""
Golden tests for shell-mcp. Run:  uv run pytest  (from shell-mcp/)

Covers the promises made in continue-mcp-toolkit.md §2:
  - stdout / stderr capture and exit codes
  - server-enforced timeout that KILLS and REPORTS (state == "timeout")
  - the #1 bug: kill must terminate the whole PROCESS TREE, not just the top proc
  - incremental output via cursors
  - the RingBuffer cap and the shell-selection mapping (pure unit tests)

Design note: each subprocess test runs its whole scenario inside a single
asyncio.run(...) so the server's background reader/watchdog tasks (created on the
running loop inside start()) stay alive for the duration of the test.
"""
import asyncio
import shutil
import sys

import pytest

from shell_mcp import server
from shell_mcp.server import IS_WINDOWS, RingBuffer, build_argv

PY = sys.executable


def default_shell():
    """A shell we can rely on existing on this host, or None to skip."""
    if IS_WINDOWS:
        return "cmd" if shutil.which("cmd") else None
    return "bash" if shutil.which("bash") else None


# --- pure unit tests (fast, no subprocess) --------------------------------
def test_build_argv_bash():
    assert build_argv("echo hi", "bash") == ["bash", "-lc", "echo hi"]


def test_build_argv_pwsh():
    assert build_argv("Get-ChildItem", "pwsh") == [
        "pwsh", "-NoProfile", "-Command", "Get-ChildItem",
    ]


def test_build_argv_unknown_shell_raises():
    with pytest.raises(ValueError):
        build_argv("whatever", "fish")


def test_ring_buffer_caps_and_marks_truncation():
    rb = RingBuffer(cap=100)
    rb.write(b"A" * 500)
    text = rb.text()
    assert "truncated" in text          # middle-truncation marker present
    assert len(rb) == 500               # logical length preserved for cursors


# --- subprocess behavior ---------------------------------------------------
def test_run_captures_stdout_and_exit_code():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        return await server.run(f'"{PY}" -c "print(\'hello-out\')"', shell=sh, timeout=15)

    res = asyncio.run(scenario())
    assert res["exit_code"] == 0
    assert "hello-out" in res["stdout"]
    assert res["state"] == "exited"


def test_run_captures_stderr():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        code = "import sys; sys.stderr.write('err-here')"
        return await server.run(f'"{PY}" -c "{code}"', shell=sh, timeout=15)

    res = asyncio.run(scenario())
    assert "err-here" in res["stderr"]


def test_timeout_kills_and_reports():
    """A command that outlives its timeout must be killed and reported as timeout,
    not left running and not raising past the model."""
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        started = await server.start(
            f'"{PY}" -c "import time; time.sleep(10)"', shell=sh, timeout=1
        )
        jid = started["job_id"]
        for _ in range(60):  # up to ~6s for the 1s watchdog to fire
            st = await server.poll(jid)
            if st["state"] != "running":
                break
            await asyncio.sleep(0.2)
        return await server.poll(jid)

    st = asyncio.run(scenario())
    assert st["state"] == "timeout"


def test_kill_terminates_process_tree(tmp_path):
    """The load-bearing test. A parent process spawns a grandchild that, after a
    delay, writes a sentinel file. Killing the JOB must take down the whole tree,
    so the sentinel is never written."""
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    sentinel = tmp_path / "grandchild_ran.txt"
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "sentinel = sys.argv[1]\n"
        "# grandchild: wait, then prove it survived by touching the sentinel\n"
        "code = \"import time, sys; time.sleep(3); open(sys.argv[1], 'w').close()\"\n"
        "subprocess.Popen([sys.executable, '-c', code, sentinel])\n"
        "time.sleep(30)  # keep the parent (and the job) alive\n"
    )

    async def scenario():
        started = await server.start(
            f'"{PY}" "{parent}" "{sentinel}"', shell=sh, timeout=60
        )
        jid = started["job_id"]
        await asyncio.sleep(1.0)          # let the grandchild spawn
        killed = await server.kill(jid)
        await asyncio.sleep(4.0)          # outlast the grandchild's 3s timer
        return killed

    killed = asyncio.run(scenario())
    assert killed["state"] == "killed"
    assert not sentinel.exists(), (
        "grandchild outlived the kill — process-group/tree kill is broken"
    )


def test_incremental_output_cursor():
    """output(since=cursor) returns only new bytes — the incremental-injection path."""
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        started = await server.start(
            f'"{PY}" -c "print(1); print(2); print(3)"', shell=sh, timeout=15
        )
        jid = started["job_id"]
        for _ in range(60):
            st = await server.poll(jid)
            if st["state"] != "running":
                break
            await asyncio.sleep(0.1)
        first = await server.output(jid, since_stdout=0)
        again = await server.output(jid, since_stdout=first["stdout_cursor"])
        return first, again

    first, again = asyncio.run(scenario())
    assert "1" in first["stdout"] and "3" in first["stdout"]
    assert again["stdout"] == ""          # cursor consumed everything


def test_default_cwd_is_workspace(tmp_path, monkeypatch):
    """With MCP_WORKSPACE set and no cwd argument, commands run in the
    workspace — not wherever Continue happened to launch the server."""
    import os
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))

    async def scenario():
        return await server.run(
            f'"{PY}" -c "import os; print(os.getcwd())"', shell=sh, timeout=15
        )

    res = asyncio.run(scenario())
    assert os.path.realpath(res["stdout"].strip()) == os.path.realpath(str(tmp_path))
