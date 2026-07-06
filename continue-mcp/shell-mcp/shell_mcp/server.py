"""
shell-mcp — a background-job terminal runner for Continue.dev (and any MCP client).

Implements the shape described in continue-mcp-toolkit.md §2–3:
  - background-job model:  start -> poll/output -> kill   (never blocks the transport)
  - bash OR powershell/pwsh/cmd, selected explicitly per call
  - stdout AND stderr captured concurrently (no pipe-buffer deadlock)
  - cancel/kill of the WHOLE process tree (process group / new session)
  - server-enforced timeout that kills + reports partial output

Pure-async Python (FastMCP option "B"). Tree-kill is `setsid` + `killpg` on
Unix/macOS and `taskkill /T /F` on Windows.

Run:  uv run shell-mcp
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from fastmcp import FastMCP

mcp = FastMCP("shell")

# --- configuration ---------------------------------------------------------
DEFAULT_TIMEOUT = float(os.environ.get("SHELL_MCP_DEFAULT_TIMEOUT", "120"))
MAX_BUFFER_BYTES = int(os.environ.get("SHELL_MCP_MAX_BUFFER", str(256 * 1024)))
IS_WINDOWS = sys.platform.startswith("win")


# --- shell selection (§2b) -------------------------------------------------
def build_argv(cmd: str, shell: Optional[str]) -> list[str]:
    """Map a shell name + a command string to an argv. We pass the whole command
    to the shell via -c / -Command rather than tokenizing it ourselves."""
    shell = (shell or ("pwsh" if IS_WINDOWS else "bash")).lower()
    if shell == "bash":
        return ["bash", "-lc", cmd]
    if shell == "pwsh":
        return ["pwsh", "-NoProfile", "-Command", cmd]
    if shell == "powershell":
        return ["powershell.exe", "-NoProfile", "-Command", cmd]
    if shell == "cmd":
        return ["cmd.exe", "/c", cmd]
    raise ValueError(f"unknown shell: {shell!r} (use bash|pwsh|powershell|cmd)")


# --- a capped buffer so a runaway command can't blow the context window ----
class RingBuffer:
    """Keeps head + tail bytes with a truncation marker in the middle."""

    def __init__(self, cap: int = MAX_BUFFER_BYTES) -> None:
        self.cap = cap
        self._data = bytearray()
        self._dropped = 0

    def write(self, chunk: bytes) -> None:
        self._data.extend(chunk)
        if len(self._data) > self.cap:
            overflow = len(self._data) - self.cap
            # drop from the middle: keep head/2 and tail/2
            keep = self.cap // 2
            self._dropped += overflow
            self._data = self._data[:keep] + self._data[-keep:]

    def text(self) -> str:
        s = self._data.decode("utf-8", "replace")
        if self._dropped:
            mid = len(s) // 2
            s = f"{s[:mid]}\n...[{self._dropped} bytes truncated]...\n{s[mid:]}"
        return s

    def __len__(self) -> int:
        return len(self._data) + self._dropped


# --- the job registry ------------------------------------------------------
@dataclass
class Job:
    job_id: str
    cmd: str
    proc: asyncio.subprocess.Process
    started: float
    stdout: RingBuffer = field(default_factory=RingBuffer)
    stderr: RingBuffer = field(default_factory=RingBuffer)
    state: str = "running"          # running | exited | killed | timeout
    exit_code: Optional[int] = None
    _readers: list[asyncio.Task] = field(default_factory=list)
    _timeout_task: Optional[asyncio.Task] = None


JOBS: dict[str, Job] = {}
_counter = 0


def _next_id() -> str:
    global _counter
    _counter += 1
    return f"j{_counter}"


async def _drain(stream: asyncio.StreamReader, buf: RingBuffer) -> None:
    """Concurrently drain one pipe so the child never blocks on a full buffer."""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        buf.write(chunk)


def _kill_tree(job: Job, sig: int = signal.SIGTERM) -> None:
    """Kill the whole process group, not just the top process (§2b, the #1 bug)."""
    proc = job.proc
    if proc.returncode is not None:
        return
    try:
        if IS_WINDOWS:
            # taskkill /T walks and kills the whole child tree.
            import subprocess
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _watch_timeout(job: Job, timeout: float) -> None:
    await asyncio.sleep(timeout)
    if job.proc.returncode is None:
        job.state = "timeout"
        _kill_tree(job, signal.SIGKILL if not IS_WINDOWS else signal.SIGTERM)


# --- tools (§2c) -----------------------------------------------------------
@mcp.tool
async def start(
    cmd: str,
    shell: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> dict:
    """Start a shell command in the background. Returns instantly with a job_id;
    poll it with output()/poll(), stop it with kill(). shell = bash|pwsh|powershell|cmd."""
    shell_name = (shell or ("pwsh" if IS_WINDOWS else "bash")).lower()
    argv = build_argv(cmd, shell_name)  # validates shell_name even on the cmd path
    # cwd defaults to the workspace, and relative cwd resolves against it — the
    # server's own cwd (wherever Continue launched it) is never the implicit base.
    workspace = os.environ.get("MCP_WORKSPACE")
    if cwd is None:
        cwd = workspace
    elif not os.path.isabs(cwd) and workspace:
        cwd = os.path.join(os.path.abspath(workspace), cwd)
    # new session/group so we can kill the whole tree later
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # setsid -> own process group

    common: dict = dict(
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        **kwargs,
    )
    if IS_WINDOWS and shell_name == "cmd":
        # cmd.exe parses its command line with its own rules; the \"-escaping
        # that list-based spawning applies breaks any quoted command. Hand
        # cmd.exe the raw string instead (COMSPEC /c passthrough).
        proc = await asyncio.create_subprocess_shell(cmd, **common)
    else:
        proc = await asyncio.create_subprocess_exec(*argv, **common)
    job = Job(job_id=_next_id(), cmd=cmd, proc=proc, started=time.monotonic())
    job._readers = [
        asyncio.create_task(_drain(proc.stdout, job.stdout)),
        asyncio.create_task(_drain(proc.stderr, job.stderr)),
    ]
    job._timeout_task = asyncio.create_task(
        _watch_timeout(job, timeout if timeout is not None else DEFAULT_TIMEOUT)
    )
    asyncio.create_task(_reap(job))
    JOBS[job.job_id] = job
    return {"job_id": job.job_id, "state": job.state}


async def _reap(job: Job) -> None:
    """Wait for exit, finalize state/exit_code once readers have drained."""
    rc = await job.proc.wait()
    await asyncio.gather(*job._readers, return_exceptions=True)
    if job._timeout_task:
        job._timeout_task.cancel()
    job.exit_code = rc
    if job.state == "running":
        job.state = "exited"


def _snapshot(job: Job, since_out: int = 0, since_err: int = 0) -> dict:
    out = job.stdout.text()
    err = job.stderr.text()
    return {
        "job_id": job.job_id,
        "state": job.state,
        "exit_code": job.exit_code,
        "runtime_ms": int((time.monotonic() - job.started) * 1000),
        "stdout": out[since_out:],
        "stderr": err[since_err:],
        "stdout_cursor": len(out),
        "stderr_cursor": len(err),
    }


@mcp.tool
async def output(job_id: str, since_stdout: int = 0, since_stderr: int = 0) -> dict:
    """Return new stdout/stderr for a job since the given cursors (incremental
    injection). Pass the returned *_cursor values back on the next call to stream."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"no such job: {job_id}")
    return _snapshot(job, since_stdout, since_stderr)


@mcp.tool
async def poll(job_id: str) -> dict:
    """Lightweight status check: state, exit_code, runtime. No output payload."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"no such job: {job_id}")
    return {
        "job_id": job.job_id,
        "state": job.state,
        "exit_code": job.exit_code,
        "runtime_ms": int((time.monotonic() - job.started) * 1000),
    }


@mcp.tool
async def kill(job_id: str) -> dict:
    """Kill a running job and its whole process tree."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"no such job: {job_id}")
    if job.proc.returncode is None:
        job.state = "killed"
        _kill_tree(job, signal.SIGKILL if not IS_WINDOWS else signal.SIGTERM)
    return {"job_id": job.job_id, "state": job.state}


@mcp.tool
async def list_jobs() -> list[dict]:
    """List all known jobs and their states."""
    return [
        {
            "job_id": j.job_id,
            "cmd": j.cmd,
            "state": j.state,
            "runtime_ms": int((time.monotonic() - j.started) * 1000),
        }
        for j in JOBS.values()
    ]


@mcp.tool
async def run(cmd: str, shell: Optional[str] = None, timeout: float = 30.0) -> dict:
    """Convenience: start a command and wait up to `timeout` for it to finish.
    For quick one-liners. Long jobs should use start()/output() instead."""
    started = await start(cmd, shell=shell, timeout=timeout)
    job = JOBS[started["job_id"]]
    deadline = time.monotonic() + timeout
    while job.state == "running" and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    return _snapshot(job)


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
