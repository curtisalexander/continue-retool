"""Native Windows ownership guarantees that a taskkill-only tree cannot provide."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from shell_mcp import server, windows_job


def test_windows_launcher_bypasses_venv_redirector(monkeypatch):
    calls = []
    closed = []
    base = os.path.abspath("base-python.exe")
    venv = os.path.abspath("venv-python.exe")
    monkeypatch.setattr(sys, "_base_executable", base)
    monkeypatch.setattr(sys, "executable", venv)
    monkeypatch.setattr(server, "IS_WINDOWS", True)
    monkeypatch.setattr(server, "build_argv", lambda *_: ["shell.exe", "-c", "echo test"])
    monkeypatch.setattr(windows_job, "WindowsJob", lambda: SimpleNamespace(
        name="test-job", close=lambda: closed.append(True),
    ))

    async def record_spawn(*args, **kwargs):
        calls.append(args)
        raise OSError("stop after recording launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_spawn)
    with pytest.raises(OSError, match="stop after recording launch"):
        asyncio.run(server._start("echo test", shell="bash"))
    assert calls == [(base, "-I", os.path.abspath(windows_job.__file__),
                      "test-job", "bash", "shell.exe", "-c", "echo test")]
    assert closed == [True]


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job Objects")
def test_owner_death_kills_descendant_without_cooperative_cleanup(tmp_path):
    ready, trigger, sentinel = (tmp_path / name for name in ("ready", "trigger", "sentinel"))
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib, sys, time\n"
        "ready, trigger, sentinel = map(pathlib.Path, sys.argv[1:])\n"
        "ready.touch()\n"
        "while not trigger.exists(): time.sleep(.01)\n"
        "sentinel.touch()\n",
        encoding="utf-8",
    )
    # The owner holds a handle but is not itself in the job. Kill it abruptly,
    # without finally/lifespan cleanup, while its launcher and child are alive.
    owner = tmp_path / "owner.py"
    owner.write_text(
        "import subprocess, sys, time\n"
        f"sys.path.insert(0, {str(Path(windows_job.__file__).parents[1])!r})\n"
        "from shell_mcp import windows_job\n"
        "job = windows_job.WindowsJob()\n"
        "subprocess.Popen([sys._base_executable, '-I', windows_job.__file__, job.name,\n"
        "                  'native', sys.executable, *sys.argv[1:]])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    # No venv redirector around the owner: only our Job Object may terminate
    # descendants when the owner dies, not an incidental redirector job.
    proc = subprocess.Popen([sys._base_executable, str(owner), str(child), str(ready), str(trigger), str(sentinel)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), "launcher did not successfully assign itself before spawning"
    finally:
        proc.kill()
        proc.wait(timeout=5)
    # Let the kernel finish termination before offering the side effect.
    time.sleep(.2)
    trigger.touch()
    time.sleep(.5)
    assert not sentinel.exists()


def test_windows_job_structure_layout():
    # Fixed-width DWORD and pointer-sized SIZE_T fields, not Linux c_ulong.
    import ctypes
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(windows_job._BasicLimits) == 64
        assert ctypes.sizeof(windows_job._ExtendedLimits) == 144
    assert Path(windows_job.__file__).is_file()
