"""
Golden tests for shell-mcp. Run:  uv run pytest  (from shell-mcp/)

Covers the shell lifecycle promises recorded in the historical toolkit design §2:
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
import os
import shutil
import subprocess
import sys
import time

import pytest

from shell_mcp import server
from shell_mcp.server import IS_WINDOWS, JobState, RingBuffer, build_argv

PY = sys.executable


def test_numeric_environment_limits_are_defaulted_and_clamped(monkeypatch):
    monkeypatch.setenv("SHELL_TEST_INT", "bad")
    assert server._env_int("SHELL_TEST_INT", 20, 1, 100) == 20
    monkeypatch.setenv("SHELL_TEST_INT", "0")
    assert server._env_int("SHELL_TEST_INT", 20, 1, 100) == 1
    monkeypatch.setenv("SHELL_TEST_FLOAT", "inf")
    assert server._env_float("SHELL_TEST_FLOAT", 30.0, 1.0, 300.0) == 30.0
    monkeypatch.setenv("SHELL_TEST_FLOAT", "999")
    assert server._env_float("SHELL_TEST_FLOAT", 30.0, 1.0, 300.0) == 300.0


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "5"])
def test_timeout_validation_rejects_invalid_values(timeout):
    with pytest.raises(ValueError, match="timeout must"):
        server._validate_timeout(timeout, 30.0)


def test_timeout_validation_accepts_bounds_and_default():
    assert server._validate_timeout(None, 30.0) == 30.0
    assert server._validate_timeout(server.MIN_TIMEOUT, 30.0) == server.MIN_TIMEOUT
    assert server._validate_timeout(server.MAX_TIMEOUT, 30.0) == server.MAX_TIMEOUT


def test_console_result_uses_fence_longer_than_output_backticks():
    output = "triple ``` and longer `````` runs"
    rendered = server._console_text(
        "printf output", {"stdout": output, "state": "done", "exit_code": 0}
    )
    fence = "`" * 7
    assert rendered == f"{fence}console\n$ printf output\n{output}\n[done] exit 0\n{fence}"


def test_console_result_keeps_triple_fence_for_normal_output():
    rendered = server._console_text(
        "echo hi", {"stdout": "hi\n", "state": "done", "exit_code": 0}
    )
    assert rendered == "```console\n$ echo hi\nhi\n[done] exit 0\n```"


def default_shell():
    """A shell we can rely on existing on this host, or None to skip."""
    if IS_WINDOWS:
        return "cmd" if shutil.which("cmd") else None
    return "bash" if shutil.which("bash") else None


# --- pure unit tests (fast, no subprocess) --------------------------------
# build_argv now resolves argv[0] to an absolute interpreter path. We pin that
# resolution with the SHELL_MCP_<SHELL> override so the argv is host-independent
# (the override is trusted as-is, exactly as the installer stamps it).
def test_job_state_is_string_compatible_and_constrained():
    assert JobState.RUNNING == "running"
    assert str(JobState.TIMEOUT) == "timeout"
    with pytest.raises(ValueError):
        JobState("unknown")


def test_build_argv_bash(monkeypatch):
    monkeypatch.setattr(server, "resolve_interpreter", lambda *_args: "/opt/bash")
    assert build_argv("echo hi", "bash") == ["/opt/bash", "-lc", "echo hi"]


def test_build_argv_pwsh(monkeypatch):
    monkeypatch.setattr(server, "resolve_interpreter", lambda *_args: r"C:/PS/pwsh.exe")
    assert build_argv("Get-ChildItem", "pwsh") == [
        r"C:/PS/pwsh.exe",
        "-NoProfile",
        "-Command",
        server._powershell_command("Get-ChildItem"),
    ]


def test_build_argv_windows_powershell_enables_utf8(monkeypatch):
    monkeypatch.setattr(
        server, "resolve_interpreter", lambda *_args: r"C:/Windows/powershell.exe"
    )
    argv = build_argv("Write-Output '🚀'", "powershell")
    assert argv[-1].startswith(server._POWERSHELL_UTF8_PREFIX)
    assert argv[-1] == server._powershell_command("Write-Output '🚀'")


def test_build_argv_unknown_shell_raises():
    with pytest.raises(ValueError):
        build_argv("whatever", "fish")


def test_resolve_interpreter_env_override_wins(monkeypatch):
    from shell_mcp.server import resolve_interpreter
    monkeypatch.setenv("SHELL_MCP_PWSH", PY)
    assert resolve_interpreter("pwsh") == os.path.abspath(PY)


def test_resolve_interpreter_stale_override_falls_back(monkeypatch):
    from shell_mcp.server import resolve_interpreter
    monkeypatch.setenv("SHELL_MCP_BASH", "/missing/stale/bash")
    monkeypatch.setattr(server.shutil, "which", lambda _name: PY)
    assert resolve_interpreter("bash") == PY


def test_resolve_interpreter_falls_through_to_which(monkeypatch):
    from shell_mcp.server import resolve_interpreter
    monkeypatch.delenv("SHELL_MCP_BASH", raising=False)
    sh = default_shell()
    if sh != "bash":
        pytest.skip("bash not resolvable on this host")
    resolved = resolve_interpreter("bash")
    assert resolved and resolved.lower().endswith(("bash", "bash.exe"))


def test_default_shell_honors_override(monkeypatch):
    from shell_mcp.server import _default_shell
    monkeypatch.setenv("SHELL_MCP_DEFAULT_SHELL", "cmd")
    monkeypatch.setattr(server, "resolve_interpreter", lambda *_args: "/cmd")
    assert _default_shell() == "cmd"


def test_build_argv_unresolved_interpreter_raises(monkeypatch):
    # An interpreter that resolves nowhere must raise a clear ValueError rather
    # than deferring to a raw FileNotFoundError from the subprocess spawn.
    monkeypatch.setattr(server, "resolve_interpreter", lambda *a, **k: None)
    with pytest.raises(ValueError, match="not found"):
        build_argv("echo hi", "pwsh")


def test_ring_buffer_caps_and_marks_truncation():
    rb = RingBuffer(cap=100)
    rb.write(b"A" * 500)
    text = rb.text()
    assert "truncated" in text          # middle-truncation marker present
    assert len(rb) == 500               # logical length preserved for cursors


def test_ring_buffer_cursor_stable_across_truncation():
    """A cursor taken BEFORE the buffer truncates must never re-serve consumed
    bytes or skip new ones — offsets are logical stream positions, not indexes
    into the (shifting) decoded text."""
    rb = RingBuffer(cap=100)
    rb.write(b"A" * 40)
    cursor = rb.total
    assert rb.read_from(0) == "A" * 40
    rb.write(b"B" * 300)                # forces head/tail truncation
    new = rb.read_from(cursor)
    assert "A" not in new               # already-consumed bytes never reappear
    assert "B" in new                   # new bytes (what survived) are delivered
    assert "truncated" in new           # the dropped middle is marked, not silent
    assert rb.read_from(rb.total) == ""  # end-cursor -> nothing
    # a cursor pointing into the dropped gap degrades to marker + tail, no error
    gap_read = rb.read_from(150)
    assert "truncated" in gap_read and gap_read.endswith("B" * 50)


def test_ring_buffer_cursor_mid_multibyte_char():
    """A byte cursor that splits a UTF-8 character must not push the slice into
    the code-page fallback (mojibake) — orphan continuation bytes are skipped."""
    rb = RingBuffer(cap=10_000)
    rb.write(("é" * 10).encode("utf-8"))
    out = rb.read_from(1)               # 1 lands inside the first 2-byte é
    assert out == "é" * 9


def test_incremental_cursor_retains_partial_multibyte_character():
    rb = RingBuffer(cap=10_000)
    encoded = "😀".encode("utf-8")

    rb.write(encoded[:2])
    first, cursor = rb.read_incremental(0)
    assert first == ""
    assert cursor == 0

    rb.write(encoded[2:])
    second, cursor = rb.read_incremental(cursor)
    assert second == "😀"
    assert cursor == len(encoded)


def test_incremental_cursor_releases_incomplete_bytes_when_stream_closes():
    rb = RingBuffer(cap=10_000)
    rb.write(b"ok\xe2")

    running, cursor = rb.read_incremental(0)
    assert running == "ok"
    assert cursor == 2

    rb.close()
    final, cursor = rb.read_incremental(cursor)
    assert final
    assert cursor == 3


def test_finished_jobs_are_pruned(monkeypatch):
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    monkeypatch.setattr(server, "MAX_FINISHED_JOBS", 2)

    async def scenario():
        server.JOBS.clear()
        for _ in range(5):
            await server.run(f'"{PY}" -c "print(1)"', shell=sh, timeout=15)
        return sum(1 for j in server.JOBS.values() if j.state != "running")

    finished = asyncio.run(scenario())
    assert finished <= 3  # prune runs on each start: 2 kept + the newest


def test_pruning_removes_spill_logs(tmp_path, monkeypatch):
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    monkeypatch.setattr(server, "MAX_FINISHED_JOBS", 1)
    monkeypatch.setattr(server, "SPILL_DIR", str(tmp_path))

    async def scenario():
        server.JOBS.clear()
        noisy = f'"{PY}" -c "print(\'x\' * 300000)"'
        first = (await server.run(noisy, shell=sh, timeout=15)).structured_content
        second = (await server.run(noisy, shell=sh, timeout=15)).structured_content
        first_paths = (first["stdout_full_output"], first["stderr_full_output"])
        assert first_paths[0] and os.path.exists(first_paths[0])
        await server.run(f'"{PY}" -c "print(1)"', shell=sh, timeout=15)
        return first["job_id"], second["job_id"], first_paths

    first_id, second_id, first_paths = asyncio.run(scenario())
    assert first_id not in server.JOBS
    assert second_id in server.JOBS
    assert all(path is None or not os.path.exists(path) for path in first_paths)


def test_pruning_waits_for_reaper_after_kill_state(monkeypatch):
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    monkeypatch.setattr(server, "MAX_FINISHED_JOBS", 1)

    async def scenario():
        server.JOBS.clear()
        started = await server._start(
            f'"{PY}" -c "import time; time.sleep(30)"', shell=sh, timeout=60
        )
        job = server.JOBS[started["job_id"]]
        job.state = JobState.KILLED
        server._prune_finished()
        retained_while_reaping = job.job_id in server.JOBS
        await server._shutdown_jobs()
        return retained_while_reaping

    assert asyncio.run(scenario()) is True


def test_concurrency_limit_reserves_slots_before_spawn(monkeypatch):
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    monkeypatch.setattr(server, "MAX_RUNNING_JOBS", 1)

    async def scenario():
        server.JOBS.clear()
        results = await asyncio.gather(
            server._start(
                f'"{PY}" -c "import time; time.sleep(30)"', shell=sh, timeout=60
            ),
            server._start(
                f'"{PY}" -c "import time; time.sleep(30)"', shell=sh, timeout=60
            ),
            return_exceptions=True,
        )
        await server._shutdown_jobs()
        return results

    results = asyncio.run(scenario())
    started = [result for result in results if isinstance(result, dict)]
    rejected = [result for result in results if isinstance(result, ValueError)]
    assert len(started) == 1
    assert len(rejected) == 1
    assert "concurrency limit reached" in str(rejected[0])
    job = server.JOBS[started[0]["job_id"]]
    assert job.state == "killed" and job.proc.returncode is not None


def test_shutdown_kills_and_reaps_every_running_job():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        server.JOBS.clear()
        ids = []
        for _ in range(2):
            started = await server._start(
                f'"{PY}" -c "import time; time.sleep(30)"', shell=sh, timeout=60
            )
            ids.append(started["job_id"])
        await server._shutdown_jobs()
        return [server.JOBS[job_id] for job_id in ids]

    jobs = asyncio.run(scenario())
    assert all(job.state == "killed" for job in jobs)
    assert all(job.proc.returncode is not None for job in jobs)
    assert all(job.stdout._spill_file is None for job in jobs)
    assert all(job.stderr._spill_file is None for job in jobs)


def test_run_honors_cwd(tmp_path):
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    import os

    async def scenario():
        return (await server.run(
            f'"{PY}" -c "import os; print(os.getcwd())"',
            shell=sh, cwd=str(tmp_path), timeout=15,
        )).structured_content

    res = asyncio.run(scenario())
    assert os.path.realpath(res["stdout"].strip()) == os.path.realpath(str(tmp_path))


def test_run_env_overlay():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        return (await server.run(
            f'"{PY}" -c "import os; print(os.environ[\'SHELL_MCP_TEST_VAR\'])"',
            shell=sh, timeout=15, env={"SHELL_MCP_TEST_VAR": "overlay-42"},
        )).structured_content

    res = asyncio.run(scenario())
    assert "overlay-42" in res["stdout"]


def test_interactive_send_reaches_stdin():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        started = (await server.start(
            f'"{PY}" -c "print(\'got:\' + input())"',
            shell=sh, timeout=15, interactive=True,
        )).structured_content
        jid = started["job_id"]
        await server.send(jid, "ping-from-test\n", eof=True)
        for _ in range(100):
            st = (await server.poll(jid)).structured_content
            if st["state"] != "running":
                break
            await asyncio.sleep(0.1)
        return (await server.output(jid)).structured_content

    res = asyncio.run(scenario())
    assert "got:ping-from-test" in res["stdout"]


def test_output_tail_mode():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        started = (await server.start(
            f'"{PY}" -c "[print(i) for i in range(10)]"', shell=sh, timeout=15,
        )).structured_content
        jid = started["job_id"]
        for _ in range(100):
            st = (await server.poll(jid)).structured_content
            if st["state"] != "running":
                break
            await asyncio.sleep(0.1)
        return (await server.output(jid, tail=2)).structured_content

    res = asyncio.run(scenario())
    lines = res["stdout"].splitlines()
    assert lines == ["8", "9"]


# --- subprocess behavior ---------------------------------------------------
def test_run_captures_stdout_and_exit_code():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        return (await server.run(f'"{PY}" -c "print(\'hello-out\')"', shell=sh, timeout=15)).structured_content

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
        return (await server.run(f'"{PY}" -c "{code}"', shell=sh, timeout=15)).structured_content

    res = asyncio.run(scenario())
    assert "err-here" in res["stderr"]


@pytest.mark.parametrize(
    "command",
    [
        "pwsh ./output.ps1",
        "bash script.sh",
        '"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -Command hi',
        "& 'C:\\Program Files\\PowerShell\\7\\pwsh.exe' -Command hi",
    ],
)
def test_redundant_interpreter_is_rejected_before_spawn(command, monkeypatch):
    called = False

    async def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("spawn must not be reached")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)
    result = asyncio.run(server.run(command, shell="bash"))
    assert result.structured_content["ok"] is False
    assert result.structured_content["error_type"] == "validation"
    assert "already invokes" in result.structured_content["error"]
    assert called is False


def test_windows_environment_overlay_is_case_insensitive_and_can_remove():
    result = server._child_environment(
        r"C:\PowerShell\pwsh.exe",
        {"path": r"C:\tools", "secret": None, "NEW": "yes"},
        windows=True,
        base={"Path": r"C:\base", "SECRET": "remove", "Keep": "value"},
    )
    assert [key for key in result if key.casefold() == "path"] == ["Path"]
    assert result["Path"] == r"C:\tools"
    assert not any(key.casefold() == "secret" for key in result)
    assert result["NEW"] == "yes" and result["Keep"] == "value"


@pytest.mark.parametrize(
    ("raw", "expected"), [(b"caf\xe9", "café"), (b"\x93hi\x94", "“hi”")]
)
def test_ring_buffer_fixed_cp1252_never_drops_bytes(raw, expected):
    rb = RingBuffer(cap=10_000, codec="cp1252")
    for byte in raw:
        rb.write(bytes([byte]))
    rb.close()
    assert rb.text() == expected


def test_ring_buffer_dbcs_poll_boundary_does_not_drop_character():
    raw = "あ".encode("cp932")
    rb = RingBuffer(cap=10_000, codec="cp932")
    rb.write(raw[:1])
    text, cursor = rb.read_incremental(0)
    assert text == "" and cursor == 0
    rb.write(raw[1:])
    rb.close()
    assert rb.read_incremental(cursor)[0] == "あ"


def test_ring_buffer_malformed_leading_byte_is_never_silently_skipped():
    rb = RingBuffer(cap=10_000, codec="utf-8")
    rb.write(b"a")
    _, cursor = rb.read_incremental(0)
    rb.write(b"\x80b")
    rb.close()
    assert rb.read_incremental(cursor)[0] == "\ufffdb"
    assert rb.decode_errors is True


def test_ring_buffer_finalizes_character_split_by_truncation_gap():
    rb = RingBuffer(cap=10, codec="utf-8")
    rb.write(b"aaaa\xe2" + b"x" * 20)
    text = rb.text()
    assert text.startswith("aaaa\ufffd\n...[15 bytes truncated")
    assert rb.decode_errors is True


def test_explicit_job_encoding_overrides_shell_default():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    raw = "café".encode("cp1252")
    code = f"import sys;sys.stdout.buffer.write(bytes.fromhex('{raw.hex()}'))"
    result = asyncio.run(
        server.run(f'"{PY}" -c "{code}"', shell=sh, encoding="cp1252")
    ).structured_content
    assert result["stdout"] == "café" and result["encoding"] == "cp1252"


def test_invalid_explicit_job_encoding_is_validation_failure():
    result = asyncio.run(server.run("echo hi", encoding="not-a-real-codec"))
    assert result.structured_content["ok"] is False
    assert result.structured_content["error_type"] == "validation"


def test_nonzero_exit_is_not_ok():
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    result = asyncio.run(server.run(f'"{PY}" -c "raise SystemExit(7)"', shell=sh))
    assert result.structured_content["exit_code"] == 7
    assert result.structured_content["ok"] is False


def test_descendant_inheriting_pipes_cannot_hang_completed_parent(tmp_path):
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    child_pid = tmp_path / "child.pid"
    script = tmp_path / "inherited_handle.py"
    script.write_text(
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n",
        encoding="utf-8",
    )

    async def scenario():
        started = time.monotonic()
        result = await server.run(
            f'"{PY}" "{script}" "{child_pid}"', shell=sh, timeout=10
        )
        return result.structured_content, time.monotonic() - started

    result, elapsed = asyncio.run(scenario())
    try:
        pid = int(child_pid.read_text(encoding="utf-8"))
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True
            )
        else:
            os.kill(pid, 9)
    except (FileNotFoundError, ProcessLookupError):
        pass
    assert result["state"] == "exited" and result["exit_code"] == 0
    assert elapsed < 3


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows PowerShell Unicode integration")
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_windows_powershell_variants_emit_utf8_unicode(shell):
    if not server.resolve_interpreter(shell):
        pytest.skip(f"{shell} is unavailable")
    result = asyncio.run(
        server.run("Write-Output 'Grüße 日本語 🚀'", shell=shell, timeout=15)
    ).structured_content
    assert result["ok"] is True and result["encoding"] == "utf-8"
    assert "Grüße 日本語 🚀" in result["stdout"]


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows cmd OEM integration")
def test_windows_cmd_uses_one_oem_codec():
    text = "Grüße café"
    raw = text.encode(server._FALLBACK_ENCODING)
    code = f"import sys;sys.stdout.buffer.write(bytes.fromhex('{raw.hex()}'))"
    result = asyncio.run(
        server.run(f'"{PY}" -c "{code}"', shell="cmd", timeout=15)
    ).structured_content
    assert result["encoding"] == server._FALLBACK_ENCODING
    assert result["stdout"] == text


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows PowerShell stdin integration")
def test_windows_pwsh_interactive_utf8_input():
    if not server.resolve_interpreter("pwsh"):
        pytest.skip("pwsh is unavailable")

    async def scenario():
        started = (await server.start(
            "$line=[Console]::In.ReadLine();[Console]::Out.Write($line)",
            shell="pwsh", timeout=15, interactive=True,
        )).structured_content
        await server.send(started["job_id"], "Grüße 日本語 🚀\n", eof=True)
        job = server.JOBS[started["job_id"]]
        assert job._reaper_task is not None
        await asyncio.wait_for(job._reaper_task, timeout=15)
        return (await server.output(started["job_id"])).structured_content

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["stdout"] == "Grüße 日本語 🚀"


@pytest.mark.skipif(not IS_WINDOWS, reason="PowerShell rendering regression")
def test_windows_pwsh_errors_do_not_emit_ansi_sequences():
    if not server.resolve_interpreter("pwsh"):
        pytest.skip("PowerShell 7 is unavailable")

    async def scenario():
        return await server.run("Write-Error 'plain-error'", shell="pwsh", timeout=15)

    result = asyncio.run(scenario())
    assert "plain-error" in result.structured_content["stderr"]
    assert "\x1b[" not in result.structured_content["stderr"]
    assert "\x1b[" not in result.content[0].text


def test_timeout_kills_and_reports():
    """A command that outlives its timeout must be killed and reported as timeout,
    not left running and not raising past the model."""
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def scenario():
        started = (await server.start(
            f'"{PY}" -c "import time; time.sleep(10)"', shell=sh, timeout=1
        )).structured_content
        jid = started["job_id"]
        for _ in range(60):  # up to ~6s for the 1s watchdog to fire
            st = (await server.poll(jid)).structured_content
            if st["state"] != "running":
                break
            await asyncio.sleep(0.2)
        return (await server.poll(jid)).structured_content

    st = asyncio.run(scenario())
    assert st["state"] == "timeout"
    assert st["ok"] is False and st["error_type"] == "timeout"


def test_spawn_failure_is_a_structured_result(monkeypatch):
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    async def fail_spawn(*_args, **_kwargs):
        raise FileNotFoundError("interpreter disappeared")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fail_spawn)
    res = asyncio.run(server.start("echo hi", shell=sh)).structured_content
    assert res["ok"] is False
    assert res["state"] == "failed"
    assert res["error_type"] == "spawn"


def test_job_codec_is_fixed_when_global_override_changes(monkeypatch):
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")
    expected_encoding = server._job_encoding(sh)

    async def scenario():
        result = await server.run(
            f'"{PY}" -c "print(1)"', shell=sh, timeout=15
        )
        job_id = result.structured_content["job_id"]
        monkeypatch.setattr(server, "_ENCODING_OVERRIDE", "not-a-codec")
        server.JOBS[job_id].stdout.write(b"more")
        return (await server.output(job_id)).structured_content

    res = asyncio.run(scenario())
    assert res["ok"] is True
    assert res["encoding"] == expected_encoding
    assert "more" in res["stdout"]


def test_kill_terminates_process_tree(tmp_path):
    """Killing the JOB must take down a confirmed-running grandchild."""
    sh = default_shell()
    if sh is None:
        pytest.skip("no usable shell on this host")

    sentinel = tmp_path / "grandchild_ran.txt"
    ready = tmp_path / "grandchild_ready.txt"
    trigger = tmp_path / "grandchild_trigger.txt"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import os, sys, time\n"
        "sentinel, ready, trigger = sys.argv[1:]\n"
        "open(ready, 'w').close()\n"
        "while not os.path.exists(trigger):\n"
        "    time.sleep(0.01)\n"
        "open(sentinel, 'w').close()\n"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "grandchild, sentinel, ready, trigger = sys.argv[1:]\n"
        "subprocess.Popen([sys.executable, grandchild, sentinel, ready, trigger])\n"
        "time.sleep(30)  # keep the parent (and the job) alive\n"
    )

    async def scenario():
        started = (await server.start(
            f'"{PY}" "{parent}" "{grandchild}" "{sentinel}" "{ready}" "{trigger}"',
            shell=sh, timeout=60,
        )).structured_content
        jid = started["job_id"]
        # Do not race kill against process creation: require the grandchild's
        # own readiness signal, with a bounded diagnostic timeout.
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
        killed = (await server.kill(jid)).structured_content
        job = server.JOBS[jid]
        assert job._reaper_task is not None
        await asyncio.wait_for(asyncio.shield(job._reaper_task), timeout=5)
        trigger.touch()
        # A surviving grandchild responds almost immediately; bound the
        # negative assertion rather than sleeping past an arbitrary child timer.
        try:
            async with asyncio.timeout(1):
                while not sentinel.exists():
                    await asyncio.sleep(0.01)
        except TimeoutError:
            pass
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
        started = (await server.start(
            f'"{PY}" -c "print(1); print(2); print(3)"', shell=sh, timeout=15
        )).structured_content
        jid = started["job_id"]
        for _ in range(60):
            st = (await server.poll(jid)).structured_content
            if st["state"] != "running":
                break
            await asyncio.sleep(0.1)
        first = (await server.output(jid, since_stdout=0)).structured_content
        again = (await server.output(jid, since_stdout=first["stdout_cursor"])).structured_content
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
        return (await server.run(
            f'"{PY}" -c "import os; print(os.getcwd())"', shell=sh, timeout=15
        )).structured_content

    res = asyncio.run(scenario())
    assert os.path.realpath(res["stdout"].strip()) == os.path.realpath(str(tmp_path))


def test_decode_output_utf8_and_fallback():
    from shell_mcp.server import decode_output
    assert decode_output("héllo 😀".encode("utf-8")) == "héllo 😀"   # valid utf-8 incl emoji
    # cp1252 smart-quote bytes are invalid utf-8 -> must not raise, must not be empty
    out = decode_output(b"\x93hi\x94")
    assert out and "hi" in out


# --- spill file: the dropped middle must stay recoverable --------------------
def test_ringbuffer_spills_full_stream_when_it_overflows(tmp_path):
    """The ring buffer drops the middle to stay bounded. Without a spill file
    those bytes are gone for good, so a truncated result is unrecoverable."""
    target = str(tmp_path / "logs" / "j1-stdout.log")
    buf = RingBuffer(cap=1024, spill_target=target)
    for i in range(500):
        buf.write(f"line {i}\n".encode())
    buf.close()
    assert buf.spill_path == target
    with open(target, "rb") as f:
        spilled = f.read()
    assert spilled.count(b"\n") == 500          # every line, head to tail
    assert b"line 250\n" in spilled             # including the dropped middle
    assert "line 250" not in buf.text()         # which is absent from the buffer


def test_ringbuffer_under_cap_never_touches_the_disk(tmp_path):
    target = str(tmp_path / "logs" / "j1-stdout.log")
    buf = RingBuffer(cap=4096, spill_target=target)
    buf.write(b"small output\n")
    buf.close()
    assert buf.spill_path is None
    assert not os.path.exists(target)


def test_ringbuffer_truncation_marker_names_the_spill_file(tmp_path):
    target = str(tmp_path / "logs" / "j1-stdout.log")
    buf = RingBuffer(cap=1024, spill_target=target)
    for i in range(500):
        buf.write(f"line {i}\n".encode())
    buf.close()
    assert target in buf.text()


def test_ringbuffer_degrades_quietly_when_the_spill_path_is_unwritable(tmp_path):
    """A read-only workspace must cost us the spill file, not the command."""
    blocker = tmp_path / "logs"
    blocker.write_text("I am a file, not a directory\n", encoding="utf-8")
    buf = RingBuffer(cap=1024, spill_target=str(blocker / "j1-stdout.log"))
    for i in range(500):
        buf.write(f"line {i}\n".encode())
    buf.close()
    assert buf.spill_path is None
    assert "line 499" in buf.text()  # buffer still works


def test_spill_file_lands_inside_the_workspace_so_scoped_tools_can_read_it(tmp_path, monkeypatch):
    """Pi spills to the system tmpdir; here fs/search are workspace-scoped, so a
    tmpdir path would be one the model is told to read and then cannot."""
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    root = server._spill_root()
    assert root.startswith(str(tmp_path))


@pytest.mark.skipif(default_shell() is None, reason="no usable shell on this host")
def test_run_reports_spill_path_for_a_noisy_command(tmp_path, monkeypatch):
    """End-to-end against the real 256KB default: 60k numbered lines is ~600KB,
    comfortably past the cap. (RingBuffer binds cap as a default argument at
    import, so patching server.MAX_BUFFER_BYTES here would do nothing.)"""
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))

    async def scenario():
        # No embedded newline in the code string: cmd.exe (default_shell() on
        # Windows) treats a raw \n as a command terminator, silently truncating
        # everything after it — the child would then exit 0 having printed
        # nothing, and this test would fail for the wrong reason.
        code = "import sys; [sys.stdout.write('line %d\\n' % i) for i in range(60000)]"
        res = await server.run(cmd=f'{PY} -c "{code}"', shell=default_shell(), timeout=60)
        return res.structured_content

    snap = asyncio.run(scenario())
    assert snap["state"] == "exited" and snap["exit_code"] == 0
    spill = snap["stdout_full_output"]
    assert spill and os.path.exists(spill)
    assert spill.startswith(str(tmp_path))     # inside the jail, so fs.read can open it
    with open(spill) as f:
        assert sum(1 for _ in f) == 60000      # including everything the buffer dropped
    assert "line 30000" not in snap["stdout"]  # the middle really was dropped
