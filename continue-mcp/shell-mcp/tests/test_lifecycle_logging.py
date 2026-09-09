"""Regression tests for start ownership, log isolation and async capture."""
import asyncio
import codecs
import os
import sys
import threading

import anyio
import pytest

from shell_mcp import server


@pytest.mark.parametrize("cancel", [False, True, "anyio"], ids=["shutdown", "cancel", "anyio-cancel"])
def test_pending_spawn_remains_owned(shell_case, monkeypatch, cancel):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        spawn = asyncio.create_subprocess_exec

        async def paused_spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return await spawn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", paused_spawn)
        old_ids = set(server.JOBS)
        scope = anyio.CancelScope()

        async def begin():
            with scope:
                return await server._start(
                    shell_case.invoke(sys.executable, "-c", "import time; time.sleep(30)"),
                    shell=shell_case.name,
                )

        start = asyncio.create_task(begin())
        await asyncio.wait_for(entered.wait(), 5)
        if cancel:
            if cancel == "anyio":
                scope.cancel()
            else:
                start.cancel()
            closing = start
        else:
            closing = asyncio.create_task(server._shutdown_jobs())
        await asyncio.sleep(0)
        assert not closing.done()
        if not cancel:
            with pytest.raises(ValueError, match="shutting down"):
                await server._start("echo rejected", shell=shell_case.name)
        release.set()
        if cancel is True:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 10)
        else:
            await asyncio.wait_for(closing, 10)
            await start
        jobs = [job for jid, job in server.JOBS.items() if jid not in old_ids]
        assert len(jobs) == 1
        assert jobs[0].state == "killed"
        assert jobs[0].proc.returncode is not None
        assert jobs[0]._reaper_task.done()
        assert not server._start_operations

    asyncio.run(scenario())


def test_shutdown_removes_owned_spills_and_lifespan_reopens_admission(shell_case, tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    unrelated = tmp_path / ".continue-mcp" / "logs" / "other-instance" / "j1.log"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"preserve")

    async def scenario():
        started = await server._start(shell_case.invoke(
            sys.executable, "-c", "import time; time.sleep(.1); print('0123456789abcdef')"
        ), shell=shell_case.name)
        job = server.JOBS[started["job_id"]]
        job.stdout.cap = 8
        await job._reaper_task
        path = job.stdout.spill_path
        assert path and os.path.isfile(path)
        await server._shutdown_jobs()
        assert not os.path.exists(path)
        assert not os.path.exists(os.path.dirname(path))
        assert unrelated.read_bytes() == b"preserve"
        async with server.lifespan(None):
            result = await server.run("echo restarted", shell=shell_case.name)
            assert result.structured_content["stdout"].strip() == "restarted"

    asyncio.run(scenario())


def test_spills_are_exclusive_and_instance_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    first_root = server._spill_root()
    monkeypatch.setattr(server, "_INSTANCE_ID", "second-instance")
    second_root = server._spill_root()
    assert first_root != second_root
    first = server.RingBuffer(cap=8, spill_target=os.path.join(first_root, "j1.log"))
    other = server.RingBuffer(cap=8, spill_target=os.path.join(second_root, "j1.log"))
    collision = server.RingBuffer(cap=8, spill_target=first.spill_target)
    first.write(b"first-owner-output")
    first.close()
    other.write(b"other-owner-output")
    other.close()
    collision.write(b"must-not-overwrite")
    collision.remove_spill()
    assert collision.spill_error and collision.spill_path is None
    assert open(first.spill_path, "rb").read() == b"first-owner-output"
    if os.name != "nt":
        assert os.stat(first_root).st_mode & 0o777 == 0o700
        assert os.stat(first.spill_path).st_mode & 0o777 == 0o600
    first.remove_spill()
    assert open(other.spill_path, "rb").read() == b"other-owner-output"
    other.remove_spill()


def test_slow_write_does_not_block_loop_and_cancel_joins_writer(tmp_path, monkeypatch):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        buf = server.RingBuffer(cap=8, spill_target=str(tmp_path / "out.log"))
        capture = buf._capture_spill

        def slow_capture(chunk):
            entered.set()
            assert release.wait(5)
            capture(chunk)

        monkeypatch.setattr(buf, "_capture_spill", slow_capture)
        writer = asyncio.create_task(buf.awrite(b"0123456789abcdef"))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            assert entered.is_set()
            # Event loop is responsive while a real worker is blocked on IO.
            assert "truncated" in buf.text()
            writer.cancel()
            await asyncio.sleep(0)
            assert not writer.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await writer
        buf.close()
        assert (tmp_path / "out.log").read_bytes() == b"0123456789abcdef"

    asyncio.run(scenario())


def test_short_writes_are_retried_and_failed_sink_keeps_draining():
    class Sink:
        def __init__(self):
            self.data = bytearray()
            self.fail = False

        def write(self, data):
            if self.fail:
                raise OSError("disk failed")
            self.data.extend(data[:3])
            return min(3, len(data))

        def close(self):
            pass

    sink = Sink()
    buf = server.RingBuffer(cap=8, spill_target="unused")
    buf._spill_file = sink
    buf.write(b"0123456789")
    assert sink.data == b"0123456789"
    sink.fail = True
    buf.write(b"failure")
    buf.write(b"last")
    assert buf.total == 21 and buf.text().endswith("last")
    assert "disk failed" in buf.spill_error


def test_initial_bom_is_display_only_and_each_stream_is_independent(tmp_path):
    out = server.RingBuffer(cap=32, spill_target=str(tmp_path / "out.log"))
    err = server.RingBuffer()
    out.write(codecs.BOM_UTF8[:2])
    assert out.read_incremental(0) == ("", 0)
    err.write(codecs.BOM_UTF8 + "error🚀".encode())
    assert err.read_incremental(0) == ("error🚀", 12)
    out.write(codecs.BOM_UTF8[2:] + "A\ufeffB".encode())
    assert out.read_incremental(0) == ("A\ufeffB", 8)
    out.write(b"x" * 80)
    out.close()
    assert out.text().startswith("A\ufeffB")
    assert out.read_incremental(0)[1] == 88
    assert (tmp_path / "out.log").read_bytes() == codecs.BOM_UTF8 + "A\ufeffB".encode() + b"x" * 80


def test_completed_duration_and_powershell_transcript(shell_case):
    async def scenario():
        snap = (await server.run(shell_case.invoke(sys.executable, "-c", "print(42)"),
                                 shell=shell_case.name)).structured_content
        before = snap["runtime_ms"]
        await asyncio.sleep(.05)
        status = await server.poll(snap["job_id"])
        assert status.structured_content["runtime_ms"] == before
        listed = await server.list_jobs()
        job = next(j for j in listed.structured_content["jobs"] if j["job_id"] == snap["job_id"])
        assert job["runtime_ms"] == before
        prompt = "PS>" if shell_case.name in ("powershell", "pwsh") else "$"
        assert f"{prompt} " in server._console_text("command", snap)
        assert f"{prompt} " in status.content[0].text

    asyncio.run(scenario())
