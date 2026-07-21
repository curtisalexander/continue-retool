"""
shell-mcp — a background-job terminal runner for Continue.dev (and any MCP client).

Implements the shape described in the historical toolkit design §2–3:
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
import codecs
import locale
import math
import os
import shutil
import signal
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from continue_mcp_common.config import env_float as _env_float
from continue_mcp_common.config import env_int as _env_int

# --- configuration ---------------------------------------------------------
DEFAULT_TIMEOUT = _env_float("SHELL_MCP_DEFAULT_TIMEOUT", 120.0, 1.0, 86_400.0)
MAX_BUFFER_BYTES = _env_int(
    "SHELL_MCP_MAX_BUFFER", 256 * 1024, 1024, 16 * 1024 * 1024
)
MAX_FINISHED_JOBS = _env_int("SHELL_MCP_MAX_FINISHED", 20, 1, 1000)
MAX_RUNNING_JOBS = _env_int("SHELL_MCP_MAX_RUNNING", 8, 1, 128)
MIN_TIMEOUT = 0.1
MAX_TIMEOUT = 86_400.0
IS_WINDOWS = sys.platform.startswith("win")


def _validate_timeout(timeout: float | None, default: float) -> float:
    value = default if timeout is None else timeout
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a number of seconds")
    value = float(value)
    if not math.isfinite(value) or not MIN_TIMEOUT <= value <= MAX_TIMEOUT:
        raise ValueError(
            f"timeout must be finite and between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds"
        )
    return value


@asynccontextmanager
async def lifespan(_app):
    try:
        yield
    finally:
        await _shutdown_jobs()


mcp = FastMCP("shell", lifespan=lifespan)

# Where full output goes when the ring buffer has to drop the middle. Pi spills
# to the system tmpdir, but that would be unreadable HERE: fs-mcp/search-mcp are
# workspace-jailed, so a /tmp path is one the model is told to read and then
# can't. Inside the workspace it stays greppable by the very tools this kit ships.
# SHELL_MCP_SPILL=0 turns spilling off.
SPILL_DIR = os.environ.get("SHELL_MCP_SPILL_DIR") or os.path.join(".continue-mcp", "logs")
SPILL_ENABLED = os.environ.get("SHELL_MCP_SPILL", "1").strip().lower() not in (
    "0", "false", "off", "no",
)


def _spill_root() -> str:
    base = os.path.abspath(os.environ.get("MCP_WORKSPACE") or os.getcwd())
    return SPILL_DIR if os.path.isabs(SPILL_DIR) else os.path.join(base, SPILL_DIR)


# --- output decoding: right encoding per platform --------------------------
# stdout/stderr come back as raw bytes. UTF-8 is correct for bash and pwsh 7+
# (emoji and all). But cmd.exe and Windows PowerShell 5.1 emit the console/OEM
# code page (cp437/cp850/cp1252), which UTF-8 decoding would mangle. So: decode
# UTF-8 when the bytes are valid UTF-8, else fall back to the platform code page.
# Force one explicitly with SHELL_MCP_ENCODING (e.g. "cp1252", "cp850", "utf-8").
_ENCODING_OVERRIDE = os.environ.get("SHELL_MCP_ENCODING")


def _fallback_encoding() -> str:
    if _ENCODING_OVERRIDE:
        return _ENCODING_OVERRIDE
    if IS_WINDOWS:
        try:
            import ctypes  # the OEM code page is what cmd/PowerShell 5.1 pipe out
            return "cp" + str(ctypes.windll.kernel32.GetOEMCP())
        except Exception:
            return locale.getpreferredencoding(False) or "cp1252"
    return "utf-8"


_FALLBACK_ENCODING = _fallback_encoding()


def decode_output(data: bytes) -> str:
    """Decode child output: UTF-8 if it's valid UTF-8, else the platform code
    page (never raises — invalid bytes in the fallback are replaced)."""
    if _ENCODING_OVERRIDE:
        return data.decode(_ENCODING_OVERRIDE, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(_FALLBACK_ENCODING, errors="replace")


def _decode_slice(data: bytes | bytearray) -> str:
    """decode_output for an arbitrary byte slice of a stream. A byte cursor can
    split a multibyte UTF-8 character, which strict decoding would misread as
    'not UTF-8' and push the whole slice to the code-page fallback. So: skip
    orphaned continuation bytes at the start, and retry without a partial
    character at the end, before falling back."""
    b = bytes(data)
    if _ENCODING_OVERRIDE:
        return decode_output(b)
    i = 0
    while i < min(len(b), 3) and (b[i] & 0xC0) == 0x80:
        i += 1
    b = b[i:]
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        for trim in (1, 2, 3):
            if trim >= len(b):
                break
            try:
                return b[:-trim].decode("utf-8")
            except UnicodeDecodeError:
                continue
        return decode_output(b)


def _incomplete_utf8_suffix(data: bytes | bytearray) -> int:
    """Return the byte length of a valid-but-incomplete UTF-8 suffix.

    Invalid data is intentionally *not* retained: decode_output() will handle it
    with the configured platform fallback. Only an otherwise-valid UTF-8 slice
    ending partway through a character needs to wait for the next pipe read.
    """
    b = bytes(data)
    try:
        b.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data" and exc.end == len(b):
            return len(b) - exc.start
    return 0


def _incomplete_output_suffix(data: bytes | bytearray) -> int:
    """Return bytes held by the configured codec's incremental decoder."""
    if not _ENCODING_OVERRIDE:
        return _incomplete_utf8_suffix(data)
    decoder = codecs.getincrementaldecoder(_ENCODING_OVERRIDE)(errors="strict")
    try:
        decoder.decode(bytes(data), final=False)
    except UnicodeDecodeError:
        return 0
    pending, _ = decoder.getstate()
    return len(pending)


# --- shell selection + interpreter resolution (§2b) ------------------------
# We spawn the interpreter as argv[0], so it has to be *found* first. Relying on
# a bare name ("pwsh") means an OS PATH lookup against whatever environment the
# server inherited — and a GUI-launched VS Code often has a stale/thin PATH that
# lacks pwsh (installed to Program Files, not a guaranteed dir) or where pwsh 7
# isn't installed at all (only Windows PowerShell 5.1 ships). That's what drives
# a client to `where pwsh` and hard-code the path. So we resolve the interpreter
# ourselves, robustly, and hand create_subprocess_exec an absolute path.
#
# name -> (executable to locate, args that wrap the command string)
_INTERP: dict[str, tuple[str, list[str]]] = {
    "bash":       ("bash",           ["-lc"]),
    "pwsh":       ("pwsh",           ["-NoProfile", "-Command"]),
    "powershell": ("powershell.exe", ["-NoProfile", "-Command"]),
    "cmd":        ("cmd.exe",        ["/c"]),
}


def _known_locations(shell: str) -> list[str]:
    """Fixed install paths to try when PATH lookup misses (the stale-GUI-PATH
    case). These are where each interpreter actually lives on a default box."""
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    return {
        "pwsh": [os.path.join(pf, "PowerShell", "7", "pwsh.exe")],
        "powershell": [os.path.join(
            sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")],
        "cmd": [os.path.join(sysroot, "System32", "cmd.exe")],
        "bash": ["/bin/bash", "/usr/bin/bash",
                 os.path.join(pf, "Git", "bin", "bash.exe")],
    }.get(shell, [])


def resolve_interpreter(shell: str, exe_name: Optional[str] = None) -> Optional[str]:
    """Locate an interpreter binary so the client never has to `where` it and
    hard-code a path. Order:
      1. SHELL_MCP_<SHELL> env override — trusted as-is (the installer stamps an
         absolute path here from a real terminal, mirroring how command: uv is
         stamped; a stale stamp is a re-run-the-installer situation, like uv).
      2. PATH lookup (shutil.which).
      3. known install locations (patches a stale/thin inherited PATH).
    Returns None if nothing resolves."""
    if exe_name is None:
        exe_name = _INTERP.get(shell, (shell, []))[0]
    override = os.environ.get(f"SHELL_MCP_{shell.upper()}")
    if override:
        return override
    found = shutil.which(exe_name)
    if found:
        return found
    for cand in _known_locations(shell):
        if os.path.isfile(cand):
            return cand
    return None


def _default_shell() -> str:
    """Pick a default interpreter that actually exists. On Windows that's pwsh
    (PowerShell 7) when present, else powershell (5.1, always installed) — never
    a hard default at an interpreter that may be absent. Override with
    SHELL_MCP_DEFAULT_SHELL (the installer stamps the one it detected)."""
    forced = os.environ.get("SHELL_MCP_DEFAULT_SHELL")
    if forced:
        return forced.lower()
    if IS_WINDOWS:
        return "pwsh" if resolve_interpreter("pwsh") else "powershell"
    return "bash"


if IS_WINDOWS:
    # The cmd path spawns via create_subprocess_shell (raw-string passthrough),
    # which builds "%ComSpec% /c <cmd>" from OUR environment — it never sees the
    # interpreter build_argv resolved. Pointing ComSpec at that resolution makes
    # the SHELL_MCP_CMD stamp / known-location fallback apply to cmd too.
    _cmd_exe = resolve_interpreter("cmd")
    if _cmd_exe:
        os.environ["ComSpec"] = _cmd_exe


def build_argv(cmd: str, shell: Optional[str]) -> list[str]:
    """Map a shell name + a command string to an argv, with argv[0] resolved to
    an absolute interpreter path. We pass the whole command to the shell via
    -c / -Command rather than tokenizing it ourselves."""
    shell = (shell or _default_shell()).lower()
    if shell not in _INTERP:
        raise ValueError(f"unknown shell: {shell!r} (use bash|pwsh|powershell|cmd)")
    exe_name, wrap = _INTERP[shell]
    exe = resolve_interpreter(shell, exe_name)
    if exe is None:
        raise ValueError(
            f"interpreter for shell={shell!r} ({exe_name}) not found — not on "
            f"PATH, not in known install locations, and SHELL_MCP_{shell.upper()} "
            f"is unset. Install it or choose a different shell= "
            f"(bash|pwsh|powershell|cmd); do NOT prefix cmd with an interpreter "
            f"name or an absolute path."
        )
    return [exe, *wrap, cmd]


# --- a capped buffer so a runaway command can't blow the context window ----
class RingBuffer:
    """Capped stream buffer: keeps the head (start of the stream) and the most
    recent tail, dropping the middle once the cap is exceeded.

    Offsets are STABLE LOGICAL BYTE POSITIONS counted from the start of the
    stream, so a reader's cursor stays valid across truncation — read_from()
    returns whatever of [offset:] still exists, with a marker standing in for
    any bytes that were dropped in between. (Character-offset cursors into the
    decoded text would shift every time the buffer truncated.)

    Dropped bytes are not lost: once the cap is passed the whole stream is also
    written to a spill file, and the truncation marker names it. Until then
    chunks are held in `_pending` so the head can still be flushed to the file
    retroactively — a job that stays under the cap never touches the disk."""

    def __init__(self, cap: int = MAX_BUFFER_BYTES, spill_target: str | None = None) -> None:
        self.cap = cap
        self._head = bytearray()   # first bytes of the stream; frozen after 1st drop
        self._tail = bytearray()   # most recent bytes
        self._dropped = 0          # bytes dropped between head and tail
        self.total = 0             # logical bytes ever written
        self.spill_target = spill_target   # path to use IF we overflow; None = never
        self.spill_path: str | None = None  # set once the file actually exists
        self._spill_file = None
        self._pending: list[bytes] = []      # raw chunks not yet on disk
        self._closed = False

    def _open_spill(self) -> None:
        if self._spill_file is not None or not self.spill_target:
            return
        try:
            os.makedirs(os.path.dirname(self.spill_target), exist_ok=True)
            self._spill_file = open(self.spill_target, "wb")
        except OSError:
            self.spill_target = None  # read-only workspace: degrade, don't crash
            self._pending.clear()
            return
        self.spill_path = self.spill_target
        for chunk in self._pending:
            self._spill_file.write(chunk)
        self._pending.clear()

    def write(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.spill_target:
            if self._spill_file is not None:
                self._spill_file.write(chunk)
            else:
                self._pending.append(chunk)
        keep = self.cap // 2
        if not self._dropped:
            self._head.extend(chunk)
            if len(self._head) > self.cap:
                self._tail = self._head[-keep:]
                self._dropped = len(self._head) - 2 * keep
                del self._head[keep:]
                self._open_spill()  # first drop: from here the file is the only
            return                  # complete copy of the stream
        self._tail.extend(chunk)
        if len(self._tail) > keep:
            overflow = len(self._tail) - keep
            self._dropped += overflow
            del self._tail[:overflow]

    def close(self) -> None:
        """Called once the process exits. Under the cap nothing was dropped, so
        the pending chunks are simply discarded and no file is ever created."""
        self._pending.clear()
        if self._spill_file is not None:
            self._spill_file.close()
            self._spill_file = None
        self._closed = True

    def remove_spill(self) -> None:
        """Close and best-effort remove this buffer's full-output artifact.

        Spill logs live exactly as long as their retained job. Cleanup must not
        make starting a later command fail (for example, if an operator has
        already removed the file or made the log directory read-only).
        """
        self.close()
        path = self.spill_path
        if path is None:
            return
        try:
            os.remove(path)
        except OSError:
            pass
        self.spill_path = None

    def _read_range(self, offset: int, end: int) -> str:
        """Decode the retained portions of logical byte range ``[offset, end)``."""
        offset = max(0, min(offset, self.total))
        end = max(offset, min(end, self.total))
        if not self._dropped:
            return _decode_slice(self._head[offset:end])

        head_end = len(self._head)
        tail_start = self.total - len(self._tail)
        if offset >= tail_start:
            return _decode_slice(self._tail[offset - tail_start:end - tail_start])

        parts = []
        if offset < head_end:
            parts.append(_decode_slice(self._head[offset:min(end, head_end)]))
        gap_start = max(offset, head_end)
        gap_end = min(end, tail_start)
        if gap_end > gap_start:
            where = f" — full output: {self.spill_path}" if self.spill_path else ""
            parts.append(f"\n...[{gap_end - gap_start} bytes truncated{where}]...\n")
        if end > tail_start:
            parts.append(_decode_slice(self._tail[:end - tail_start]))
        return "".join(parts)

    def _incremental_end(self) -> int:
        """Last safe byte cursor while the producer may append more data."""
        if self._closed or not self.total:
            return self.total
        ending = self._tail if self._dropped else self._head
        return self.total - _incomplete_output_suffix(ending)

    def read_incremental(self, offset: int) -> tuple[str, int]:
        """Return decoded new output and the cursor safe to acknowledge.

        A pipe read may stop in the middle of a multibyte UTF-8 character. In
        that case its prefix remains unacknowledged, so the next poll decodes
        the complete character instead of losing it between two byte cursors.
        """
        end = self._incremental_end()
        return self._read_range(offset, end), end

    def read_from(self, offset: int) -> str:
        """Decoded text of the stream from logical byte `offset` to the end.
        Any part of that range that was dropped is replaced by one marker."""
        return self._read_range(offset, self.total)

    def text(self) -> str:
        return self.read_from(0)

    def __len__(self) -> int:
        return self.total


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
    _reaper_task: Optional[asyncio.Task] = None


JOBS: dict[str, Job] = {}
_counter = 0
_starting_jobs = 0


def _next_id() -> str:
    global _counter
    _counter += 1
    return f"j{_counter}"


def _prune_finished() -> None:
    """Cap retained completed jobs, buffers, and spill files.

    A timeout/kill changes state before the process and pipe readers necessarily
    finish, so only prune after the reaper has completed. Dict insertion order
    equals start order, which makes removal oldest-first.
    """
    finished = [
        j for j in JOBS.values()
        if j.state != "running"
        and j._reaper_task is not None
        and j._reaper_task.done()
    ]
    for j in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
        j.stdout.remove_spill()
        j.stderr.remove_spill()
        del JOBS[j.job_id]


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


async def _shutdown_jobs() -> None:
    """Kill and reap every child before the MCP server releases its transport."""
    running = [job for job in JOBS.values() if job.proc.returncode is None]
    for job in running:
        job.state = "killed"
        _kill_tree(job, signal.SIGKILL if not IS_WINDOWS else signal.SIGTERM)

    reapers = [job._reaper_task for job in running if job._reaper_task is not None]
    if reapers:
        try:
            await asyncio.wait_for(
                asyncio.gather(*reapers, return_exceptions=True), timeout=10.0
            )
        except asyncio.TimeoutError:
            for job in running:
                _kill_tree(job, signal.SIGKILL if not IS_WINDOWS else signal.SIGTERM)
            for task in reapers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*reapers, return_exceptions=True)


# --- rendering: echo the command + output as a terminal-style block --------
# Returning a ToolResult gives Continue's UI a readable transcript (content)
# while still handing the model the structured fields (structured_content /
# res.data). Without this the command and output are buried in escaped JSON.
def _console_text(cmd: str, snap: dict) -> str:
    parts = [f"$ {cmd}"]
    out = (snap.get("stdout") or "").rstrip("\n")
    err = (snap.get("stderr") or "").rstrip("\n")
    if out:
        parts.append(out)
    if err:
        parts.append("[stderr]\n" + err)
    state, ec = snap.get("state"), snap.get("exit_code")
    tail = f"[{state}]" + (f" exit {ec}" if ec is not None else "")
    if snap.get("job_id") and ec is None and not out and not err:
        tail += f" job={snap['job_id']}"
    parts.append(tail)
    return "```console\n" + "\n".join(parts) + "\n```"


def _shell_result(cmd: str, snap: dict) -> ToolResult:
    return ToolResult(
        content=[TextContent(type="text", text=_console_text(cmd, snap))],
        structured_content=snap,
    )


def _shell_failure(cmd: str, kind: str, error: str, **extra) -> ToolResult:
    data = {
        "ok": False,
        "state": "failed",
        "error": error,
        "error_type": kind,
        **extra,
    }
    text = f"❌ {kind}: {error}\n\n```console\n$ {cmd}\n[failed]\n```"
    return ToolResult(
        content=[TextContent(type="text", text=text)], structured_content=data
    )


def _start_failure_kind(exc: Exception) -> str:
    if isinstance(exc, OSError) or "interpreter" in str(exc).lower():
        return "spawn"
    return "validation"


# --- tools (§2c) -----------------------------------------------------------
async def _start(
    cmd: str,
    shell: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[dict[str, str]] = None,
    interactive: bool = False,
) -> dict:
    """Launch the process and register the job. Internal: run()/start() call this;
    only the @mcp.tool wrappers shape the ToolResult the client sees."""
    global _starting_jobs
    _prune_finished()
    effective_timeout = _validate_timeout(timeout, DEFAULT_TIMEOUT)
    running_jobs = sum(job.proc.returncode is None for job in JOBS.values())
    if running_jobs + _starting_jobs >= MAX_RUNNING_JOBS:
        raise ValueError(
            f"shell concurrency limit reached ({MAX_RUNNING_JOBS} running jobs); "
            "wait for or kill a job before starting another"
        )
    shell_name = (shell or _default_shell()).lower()
    argv = build_argv(cmd, shell_name)  # validates even on the cmd path
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
    if env:
        kwargs["env"] = {**os.environ, **env}

    common: dict = dict(
        # stdin defaults to DEVNULL, not inherit: the server's own stdin IS the
        # MCP transport, and a child that reads it would eat protocol bytes.
        stdin=asyncio.subprocess.PIPE if interactive else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        **kwargs,
    )
    # Reserve synchronously before the first await so concurrent start calls
    # cannot all pass the count while their subprocesses are being created.
    _starting_jobs += 1
    try:
        if IS_WINDOWS and shell_name == "cmd":
            # cmd.exe parses its command line with its own rules; the \"-escaping
            # that list-based spawning applies breaks any quoted command. Hand
            # cmd.exe the raw string instead (ComSpec /c passthrough — ComSpec is
            # pointed at the resolved cmd.exe at import time above).
            proc = await asyncio.create_subprocess_shell(cmd, **common)
        else:
            proc = await asyncio.create_subprocess_exec(*argv, **common)
    finally:
        _starting_jobs -= 1
    jid = _next_id()
    root = _spill_root() if SPILL_ENABLED else None
    job = Job(
        job_id=jid, cmd=cmd, proc=proc, started=time.monotonic(),
        stdout=RingBuffer(spill_target=os.path.join(root, f"{jid}-stdout.log") if root else None),
        stderr=RingBuffer(spill_target=os.path.join(root, f"{jid}-stderr.log") if root else None),
    )
    stdout = proc.stdout
    stderr = proc.stderr
    assert stdout is not None and stderr is not None
    job._readers = [
        asyncio.create_task(_drain(stdout, job.stdout)),
        asyncio.create_task(_drain(stderr, job.stderr)),
    ]
    job._timeout_task = asyncio.create_task(
        _watch_timeout(job, effective_timeout)
    )
    JOBS[job.job_id] = job
    job._reaper_task = asyncio.create_task(_reap(job))
    return {"job_id": job.job_id, "state": job.state}


@mcp.tool(annotations={"openWorldHint": True})
async def start(
    cmd: str,
    shell: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[dict[str, str]] = None,
    interactive: bool = False,
) -> ToolResult:
    """Start a shell command in the background. Returns instantly with a job_id;
    poll it with output()/poll(), stop it with kill(). `env` adds/overrides
    environment variables; interactive=true opens stdin for send().

    This tool IS a shell — pass `cmd` exactly as you'd type it at the prompt. Do
    NOT prefix it with an interpreter name or absolute path (don't pass
    `pwsh script.ps1`, `bash script.sh`, or a full path to pwsh.exe); pick the
    interpreter with the `shell` argument instead — the server locates it for you.
    shell = bash | pwsh | powershell | cmd (default: bash off Windows; on Windows
    pwsh if installed, else powershell). To run a script file, use the shell's
    own call syntax, e.g. PowerShell `& ./Deploy.ps1 -Env prod`, bash `./deploy.sh`."""
    try:
        data = await _start(
            cmd, shell=shell, cwd=cwd, timeout=timeout, env=env,
            interactive=interactive,
        )
    except (OSError, ValueError) as exc:
        return _shell_failure(cmd, _start_failure_kind(exc), str(exc))
    return _shell_result(cmd, data)


async def _reap(job: Job) -> None:
    """Wait for exit, finalize state/exit_code once readers have drained."""
    rc = await job.proc.wait()
    await asyncio.gather(*job._readers, return_exceptions=True)
    if job._timeout_task:
        job._timeout_task.cancel()
    job.stdout.close()
    job.stderr.close()
    job.exit_code = rc
    if job.state == "running":
        job.state = "exited"


def _last_lines(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _snapshot(job: Job, since_out: int = 0, since_err: int = 0) -> dict:
    """Cursors are logical byte offsets into each stream (stable across the
    RingBuffer's truncation), NOT character offsets into the decoded text."""
    stdout, stdout_cursor = job.stdout.read_incremental(since_out)
    stderr, stderr_cursor = job.stderr.read_incremental(since_err)
    timed_out = job.state == "timeout"
    return {
        "ok": not timed_out,
        "job_id": job.job_id,
        "state": job.state,
        "exit_code": job.exit_code,
        "runtime_ms": int((time.monotonic() - job.started) * 1000),
        "stdout": stdout,
        "stderr": stderr,
        "stdout_cursor": stdout_cursor,
        "stderr_cursor": stderr_cursor,
        # Only present once the buffer actually overflowed and spilled; a job
        # that fit in the buffer has nothing more to offer than what's above.
        "stdout_full_output": job.stdout.spill_path,
        "stderr_full_output": job.stderr.spill_path,
        "error": "command timed out" if timed_out else None,
        "error_type": "timeout" if timed_out else None,
    }


@mcp.tool(annotations={"readOnlyHint": True})
async def output(
    job_id: str, since_stdout: int = 0, since_stderr: int = 0, tail: int = 0
) -> ToolResult:
    """Return new stdout/stderr for a job since the given cursors (incremental
    injection). Pass the returned *_cursor values back on the next call to stream.
    tail=N ignores the cursors and returns only the last N lines of each stream."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"no such job: {job_id}")
    try:
        snap = _snapshot(job, since_stdout, since_stderr)
    except (UnicodeError, LookupError) as exc:
        return _shell_failure(
            job.cmd, "decode", f"could not decode command output: {exc}",
            job_id=job.job_id,
        )
    if tail > 0:
        snap["stdout"] = _last_lines(job.stdout.text(), tail)
        snap["stderr"] = _last_lines(job.stderr.text(), tail)
    return _shell_result(job.cmd, snap)


@mcp.tool(annotations={"readOnlyHint": True})
async def poll(job_id: str) -> ToolResult:
    """Lightweight status check: state, exit_code, runtime. No output payload."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"no such job: {job_id}")
    timed_out = job.state == "timeout"
    data = {
        "ok": not timed_out,
        "job_id": job.job_id,
        "state": job.state,
        "exit_code": job.exit_code,
        "runtime_ms": int((time.monotonic() - job.started) * 1000),
        "error": "command timed out" if timed_out else None,
        "error_type": "timeout" if timed_out else None,
    }
    tail = f"[{data['state']}]" + (f" exit {data['exit_code']}" if data['exit_code'] is not None else "")
    text = f"{data['job_id']}: {tail} · {data['runtime_ms']}ms · $ {job.cmd}"
    return ToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


@mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True})
async def kill(job_id: str) -> ToolResult:
    """Kill a running job and its whole process tree."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"no such job: {job_id}")
    if job.proc.returncode is None:
        job.state = "killed"
        _kill_tree(job, signal.SIGKILL if not IS_WINDOWS else signal.SIGTERM)
    data = {"job_id": job.job_id, "state": job.state}
    text = f"{data['job_id']}: [{data['state']}] · $ {job.cmd}"
    return ToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


@mcp.tool(annotations={"readOnlyHint": True})
async def list_jobs() -> ToolResult:
    """List all known jobs and their states."""
    jobs = [
        {
            "job_id": j.job_id,
            "cmd": j.cmd,
            "state": j.state,
            "runtime_ms": int((time.monotonic() - j.started) * 1000),
        }
        for j in JOBS.values()
    ]
    data = {"jobs": jobs, "count": len(jobs)}
    block = "\n".join(
        f"{j['job_id']}  [{j['state']}]  {j['runtime_ms']}ms  $ {j['cmd']}" for j in jobs
    )
    md = f"{len(jobs)} job(s)" + (f"\n\n```console\n{block}\n```" if block else "")
    return ToolResult(content=[TextContent(type="text", text=md)], structured_content=data)


@mcp.tool(annotations={"openWorldHint": True})
async def run(
    cmd: str,
    shell: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: float = 30.0,
    env: Optional[dict[str, str]] = None,
) -> ToolResult:
    """Convenience: start a command and wait up to `timeout` for it to finish.
    For quick one-liners; long jobs should use start()/output() instead.

    This tool IS a shell — pass `cmd` as you'd type it at the prompt, without an
    interpreter prefix or absolute path (no `pwsh script.ps1`, no full path to
    pwsh.exe); choose the interpreter with `shell` = bash | pwsh | powershell | cmd (default:
    bash off Windows; on Windows pwsh if installed, else powershell). The server
    locates the binary. Run a script with the shell's call syntax, e.g.
    `& ./Deploy.ps1`."""
    try:
        timeout = _validate_timeout(timeout, 30.0)
        started = await _start(cmd, shell=shell, cwd=cwd, timeout=timeout, env=env)
    except (OSError, ValueError) as exc:
        return _shell_failure(cmd, _start_failure_kind(exc), str(exc))
    job = JOBS[started["job_id"]]
    # Wait on the process itself, not a poll loop. The watchdog fires at
    # `timeout` and kills the tree; the margin here covers the kill landing.
    try:
        await asyncio.wait_for(job.proc.wait(), timeout + 10.0)
    except asyncio.TimeoutError:
        pass
    # Let the reaper/watchdog settle final state (drain readers, set exit_code)
    # so the snapshot can't report "running" for a process that just ended.
    for _ in range(100):
        if job.state != "running":
            break
        await asyncio.sleep(0.05)
    try:
        return _shell_result(cmd, _snapshot(job))
    except (UnicodeError, LookupError) as exc:
        return _shell_failure(
            cmd, "decode", f"could not decode command output: {exc}",
            job_id=job.job_id,
        )


@mcp.tool(annotations={"openWorldHint": True})
async def send(job_id: str, text: str, eof: bool = False) -> ToolResult:
    """Write text to the stdin of a job started with interactive=true (include
    any trailing newline yourself). eof=true closes stdin afterwards."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"no such job: {job_id}")
    stdin = job.proc.stdin
    if stdin is None:
        data = {"ok": False, "job_id": job_id,
                "error": "job has no stdin pipe (start it with interactive=true)"}
        return ToolResult(content=[TextContent(type="text", text=f"❌ {data['error']}")],
                          structured_content=data)
    if job.proc.returncode is not None:
        data = {"ok": False, "job_id": job_id, "error": "job already exited"}
        return ToolResult(content=[TextContent(type="text", text=f"❌ {data['error']}")],
                          structured_content=data)
    stdin.write(text.encode("utf-8"))
    await stdin.drain()
    if eof:
        stdin.close()
    data = {"ok": True, "job_id": job_id, "sent_bytes": len(text.encode("utf-8")),
            "eof": eof, "state": job.state}
    return ToolResult(
        content=[TextContent(type="text", text=f"{job_id}: sent {data['sent_bytes']} byte(s)"
                             + (" + EOF" if eof else ""))],
        structured_content=data,
    )


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
