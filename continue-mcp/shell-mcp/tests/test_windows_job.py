"""Native Windows ownership guarantees that a taskkill-only tree cannot provide."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from shell_mcp import windows_job


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
        "from shell_mcp import windows_job\n"
        "job = windows_job.WindowsJob()\n"
        "subprocess.Popen([sys.executable, '-I', windows_job.__file__, job.name,\n"
        "                  'native', sys.executable, *sys.argv[1:]])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen([sys.executable, str(owner), str(child), str(ready), str(trigger), str(sentinel)])
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
