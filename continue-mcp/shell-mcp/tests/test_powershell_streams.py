"""Integration coverage for byte streams through real PowerShell interpreters."""

import asyncio
import os
import sys
import time

import pytest

from shell_mcp import server


def _require_powershell(shell_case):
    if shell_case.name not in ("pwsh", "powershell"):
        pytest.skip("PowerShell-only stream behavior")


async def _wait(job_id, seconds=15):
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        snapshot = (await server.poll(job_id)).structured_content
        if snapshot["state"] != "running":
            return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(f"PowerShell job {job_id} did not finish")
        await asyncio.sleep(0.05)


def test_native_pipeline_input_is_utf8_without_bom(shell_case, tmp_path):
    _require_powershell(shell_case)
    reader = tmp_path / "stdin hex.py"
    reader.write_text(
        "import sys\nprint(sys.stdin.buffer.read().hex())\n", encoding="utf-8"
    )
    text = "Grüße 日本語 🚀"
    command = f"{shell_case.quote(text)} | {shell_case.invoke(sys.executable, reader)}"

    result = asyncio.run(
        server.run(command, shell=shell_case.name, timeout=15, encoding="utf-8")
    ).structured_content

    expected = (text + os.linesep).encode("utf-8").hex()
    assert result["state"] == "exited" and result["exit_code"] == 0
    assert result["stdout"].strip() == expected
    assert not result["stdout"].strip().startswith("efbbbf")


def test_native_split_utf8_streams_survive_cursors_and_spill(shell_case, tmp_path, monkeypatch):
    _require_powershell(shell_case)
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    child = tmp_path / "split streams.py"
    child.write_text(
        "import sys, time\n"
        "out = ('🚀' + 'O' * 80).encode()\n"
        "err = ('😀' + 'E' * 80).encode()\n"
        "for stream, data in ((sys.stdout.buffer, out[:2]), (sys.stderr.buffer, err[:2]), "
        "(sys.stdout.buffer, out[2:]), (sys.stderr.buffer, err[2:])):\n"
        "    stream.write(data); stream.flush(); time.sleep(.15)\n",
        encoding="utf-8",
    )

    async def scenario():
        started = (await server.start(
            shell_case.invoke(sys.executable, child),
            shell=shell_case.name,
            timeout=15,
            encoding="utf-8",
        )).structured_content
        jid = started["job_id"]
        # Keep this integration test small while exercising the real spill path.
        server.JOBS[jid].stdout.cap = 32
        server.JOBS[jid].stderr.cap = 32
        out_cursor = err_cursor = 0
        out_parts, err_parts = [], []
        while server.JOBS[jid].state == server.JobState.RUNNING:
            snap = (await server.output(
                jid, since_stdout=out_cursor, since_stderr=err_cursor
            )).structured_content
            out_parts.append(snap["stdout"])
            err_parts.append(snap["stderr"])
            out_cursor, err_cursor = snap["stdout_cursor"], snap["stderr_cursor"]
            await asyncio.sleep(0.04)
        await _wait(jid)
        final = (await server.output(
            jid, since_stdout=out_cursor, since_stderr=err_cursor
        )).structured_content
        out_parts.append(final["stdout"])
        err_parts.append(final["stderr"])
        return final, "".join(out_parts), "".join(err_parts)

    final, stdout, stderr = asyncio.run(scenario())
    assert stdout.count("🚀") == 1 and stdout.endswith("O" * 16)
    assert stderr.count("😀") == 1 and stderr.endswith("E" * 16)
    assert "52 bytes truncated" in stdout and "52 bytes truncated" in stderr
    assert final["stdout_cursor"] == 84 and final["stderr_cursor"] == 84
    assert "�" not in stdout + stderr
    assert final["stdout_full_output"] and final["stderr_full_output"]
    with open(final["stdout_full_output"], encoding="utf-8") as stream:
        assert stream.read() == "🚀" + "O" * 80
    with open(final["stderr_full_output"], encoding="utf-8") as stream:
        assert stream.read() == "😀" + "E" * 80


@pytest.mark.parametrize(
    "command",
    [
        "Read-Host 'must not wait'",
        "$ConfirmPreference='Low'; Remove-Item -LiteralPath $env:SHELL_MCP_CONFIRM_TARGET -Confirm",
    ],
    ids=("read-host", "confirmation"),
)
def test_noninteractive_prompts_fail_promptly(shell_case, tmp_path, command):
    _require_powershell(shell_case)
    target = tmp_path / "confirm target.txt"
    target.write_text("keep", encoding="utf-8")
    started = time.monotonic()
    result = asyncio.run(server.run(
        command,
        shell=shell_case.name,
        timeout=5,
        env={"SHELL_MCP_CONFIRM_TARGET": str(target)},
    )).structured_content

    assert time.monotonic() - started < 4
    assert result["state"] == "exited", result
    assert result["exit_code"] != 0, result
    if "Remove-Item" in command:
        assert target.exists()


def test_interactive_read_host_and_native_stdin(shell_case, tmp_path):
    _require_powershell(shell_case)
    native = tmp_path / "native stdin.py"
    native.write_text(
        "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )

    async def run_with_input(command, text):
        started = (await server.start(
            command, shell=shell_case.name, timeout=15, interactive=True,
            encoding="utf-8",
        )).structured_content
        await server.send(started["job_id"], text, eof=True)
        await _wait(started["job_id"])
        return (await server.output(started["job_id"])).structured_content

    async def scenario():
        host = await run_with_input("$v = Read-Host 'value'; Write-Output ('got:' + $v)", "Grüße 🚀\n")
        raw = await run_with_input(shell_case.invoke(sys.executable, native), "日本語 😀\n")
        return host, raw

    host, raw = asyncio.run(scenario())
    assert host["exit_code"] == 0 and "got:Grüße 🚀" in host["stdout"]
    assert raw["exit_code"] == 0 and raw["stdout"] == "日本語 😀\n"
