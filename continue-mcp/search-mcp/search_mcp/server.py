"""
search-mcp — replace Continue's built-in Grep/Glob search with ripgrep.

Shells out to the `rg` binary (bring your own, or opt into the bundled build —
see README and rg_bin() below) and returns compact, structured results the agent
can act on directly.
Two tools:

  search.grep(pattern, ...)   -> content search (regex, gitignore-aware)  -> replaces "Grep search"
  search.files(glob, ...)     -> list files by glob                        -> replaces "Glob search"

Why this beats the built-ins: native ripgrep speed, gitignore-awareness for free,
terse structured output (fewer tokens than the built-in tool prompts), and a hard
result cap so a broad query can't flood the context window.

Run:  uv run search-mcp
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import shutil
from asyncio.subprocess import PIPE
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Optional, TypeVar

from fastmcp import FastMCP
from fastmcp.tools import ToolResult

from continue_mcp_common.config import env_float as _env_float
from continue_mcp_common.config import env_int as _env_int
from continue_mcp_common.paths import jail_error
from continue_mcp_common.paths import resolve_path as _resolve
from continue_mcp_common.results import result as _result
from continue_mcp_common.text import decode_content, decode_filename

mcp = FastMCP("search")


DEFAULT_TIMEOUT = _env_float("SEARCH_MCP_TIMEOUT", 30.0, 0.1, 300.0)
MAX_RESULTS_CAP = _env_int("SEARCH_MCP_MAX_RESULTS", 1000, 1, 10_000)
# Per-match line cap (chars). rg --json emits the WHOLE matching line, and
# --max-columns is ignored in --json mode (verified: a 200KB line still comes
# back at 200KB), so a match in minified JS / a one-line lockfile would otherwise
# dump the entire line into context. Pi truncates to 500 for the same reason.
MAX_LINE_CHARS = _env_int("SEARCH_MCP_MAX_LINE_CHARS", 500, 40, 10_000)
# Ceiling on ONE rg --json record. asyncio's StreamReader splits on newlines with
# a default 64KB buffer and raises ValueError past it — so a single long matching
# line used to crash grep outright. Raise it well past any real minified line,
# and still fail closed (not a traceback) if a record somehow exceeds even this.
MAX_RECORD_BYTES = _env_int(
    "SEARCH_MCP_MAX_RECORD", 8 * 1024 * 1024, 64 * 1024, 64 * 1024 * 1024
)
MAX_ERROR_BYTES = 64 * 1024

T = TypeVar("T")
SearchEncoding = Literal["utf-8", "utf-16le", "utf-16be", "windows-1252"]


# --- defense-in-depth workspace path scoping (default ON) -------------------
# Realpath containment rejects straightforward paths outside MCP_WORKSPACE or
# MCP_JAIL_EXTRA roots. It is not a sandbox or a complete TOCTOU guarantee.
# --- locate the rg binary --------------------------------------------------
def rg_bin() -> str:
    """Find the ripgrep binary. Resolution order:
      1. RIPGREP_BIN — an explicit path to a system/own rg (the escape hatch;
         trusted as-is, so no third-party wheel is ever forced on you).
      2. `rg` on PATH — a system install, or the bundled `ripgrep-bin` wheel once
         it's on PATH (via `uv tool install` globally, or this server's `rg` extra).
    Missing rg raises a message that names every fix AND the third-party caveat,
    so the fix is discoverable from the error alone."""
    b = os.environ.get("RIPGREP_BIN") or shutil.which("rg")
    if not b:
        raise RuntimeError(
            "ripgrep (`rg`) was not found — search needs it. Pick ONE fix:\n"
            "  1. Install a system rg:   brew install ripgrep  (or apt/choco/etc.)\n"
            "  2. Global prebuilt rg:    uv tool install ripgrep-bin\n"
            "  3. Into the toolkit venv: uv sync --project <…>/continue-mcp --extra rg\n"
            "  4. Point at an existing rg: set RIPGREP_BIN=/abs/path/to/rg\n"
            "Options 2 and 3 install `ripgrep-bin`, a THIRD-PARTY repackage "
            "(Bing-su/pip-binary-factory) of ripgrep's OFFICIAL release binaries — "
            "not published by ripgrep's author. Prefer 1 or 4 to avoid it."
        )
    return b


# --- pure arg builders (unit-testable without rg) --------------------------
def build_grep_args(
    pattern: str,
    path: str = ".",
    ignore_case: bool = False,
    glob: Optional[list[str]] = None,
    multiline: bool = False,
    context: int = 0,
    hidden: bool = False,
    no_ignore: bool = False,
    encoding: Optional[SearchEncoding] = None,
) -> list[str]:
    args = ["--json"]
    if ignore_case:
        args.append("-i")
    if multiline:
        args += ["--multiline", "--multiline-dotall"]
    if context > 0:
        args += ["-C", str(context)]
    if hidden:
        args.append("--hidden")
    if no_ignore:
        args.append("--no-ignore")
    if encoding:
        args += ["--encoding", encoding]
    for g in glob or []:
        args += ["-g", g]
    args += ["--", pattern, path]
    return args


def build_files_args(
    glob: Optional[list[str]] = None,
    path: str = ".",
    hidden: bool = False,
    no_ignore: bool = False,
) -> list[str]:
    args = ["--files", "--null"]
    if hidden:
        args.append("--hidden")
    if no_ignore:
        args.append("--no-ignore")
    for g in glob or []:
        args += ["-g", g]
    args.append(path)
    return args


# --- subprocess plumbing ---------------------------------------------------
def _clip(text: str) -> tuple[str, bool]:
    """Clip a matching line to MAX_LINE_CHARS. Returns (text, was_clipped)."""
    if len(text) <= MAX_LINE_CHARS:
        return text, False
    return text[:MAX_LINE_CHARS] + f"…[+{len(text) - MAX_LINE_CHARS} chars]", True


async def _collect_json(
    stdout: asyncio.StreamReader, out: list, max_results: int, flags: dict
) -> bool:
    """Stream rg --json, append match/context rows to `out`, stop at the cap.
    Sets flags['line_clipped'] if any line was clipped. Returns True if we hit
    the match cap."""
    matches = 0
    async for raw in stdout:
        try:
            obj = json.loads(raw)
        except UnicodeDecodeError:
            flags["decode_error"] = True
            continue
        except json.JSONDecodeError:
            flags["decode_error"] = True
            continue
        t = obj.get("type")
        if t not in ("match", "context"):
            continue
        try:
            d = obj["data"]
            line_bytes = _json_data_bytes(d["lines"])
            path_value = _json_data_value(d["path"])
            decoded = decode_content(line_bytes)
            text, clipped = _clip(decoded.text.rstrip("\r\n"))
        except (KeyError, TypeError, ValueError, binascii.Error):
            flags["decode_error"] = True
            continue
        if clipped:
            flags["line_clipped"] = True
        row = {
            "file": decode_filename(path_value),
            "line": d.get("line_number"),
            "text": text,
            "kind": t,
        }
        if decoded.codec != "utf-8" or decoded.had_errors or decoded.loss:
            row["encoding"] = decoded.codec
            row["decode_loss"] = decoded.loss
        if flags.get("source_encoding"):
            row["source_encoding"] = flags["source_encoding"]
        if t == "match":
            subs = d.get("submatches") or []
            if subs:
                try:
                    byte_offset = int(subs[0]["start"])
                    if byte_offset < 0 or byte_offset > len(line_bytes):
                        raise ValueError("invalid submatch byte offset")
                    prefix = line_bytes[:byte_offset]
                    # Use the complete-content policy independently on the exact
                    # raw byte prefix. Character count, unlike byte count, is
                    # encoding-dependent.
                    char_column = len(decode_content(prefix).text) + 1
                    row["byte_column"] = byte_offset + 1
                    row["column"] = char_column
                except (KeyError, TypeError, ValueError):
                    flags["decode_error"] = True
            matches += 1
        out.append(row)
        if matches >= max_results:
            return True
    return False


def _json_data_value(value: object) -> bytes | str:
    """Read ripgrep's JSON Data union without assuming either representation."""
    if not isinstance(value, dict):
        raise TypeError("ripgrep Data must be an object")
    text = value.get("text")
    if isinstance(text, str):
        return text
    encoded = value.get("bytes")
    if isinstance(encoded, str):
        return base64.b64decode(encoded, validate=True)
    raise ValueError("ripgrep Data has neither text nor bytes")


def _json_data_bytes(value: object) -> bytes:
    data = _json_data_value(value)
    return data.encode("utf-8") if isinstance(data, str) else data


async def _collect_stderr(stream: asyncio.StreamReader) -> tuple[str, bool]:
    """Drain stderr concurrently while retaining only a bounded diagnostic."""
    kept = bytearray()
    clipped = False
    while chunk := await stream.read(8192):
        remaining = MAX_ERROR_BYTES - len(kept)
        if remaining > 0:
            kept.extend(chunk[:remaining])
        if len(chunk) > remaining:
            clipped = True
    text = kept.decode("utf-8", "replace").strip()
    if clipped:
        text += f"\n[stderr clipped at {MAX_ERROR_BYTES} bytes]"
    return text, clipped


async def _finish_process(proc, stderr_task) -> str:
    """Stop a capped/timed-out child and collect its concurrently drained stderr."""
    if proc.returncode is None:
        proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        pass
    try:
        error, _ = await asyncio.wait_for(stderr_task, 5)
        return error
    except asyncio.TimeoutError:
        stderr_task.cancel()
        return "stderr drain timed out"


@dataclass(frozen=True)
class _ProcessRun:
    value: object | None
    returncode: int | None
    stderr: str
    timed_out: bool
    collector_error: Exception | None


async def _run_process(
    args: list[str], timeout: float,
    collector: Callable[[asyncio.StreamReader], Awaitable[T]],
    *, limit: int | None = None,
) -> _ProcessRun:
    """Run rg with bounded output collection and consistently reap the child."""
    kwargs = {"limit": limit} if limit is not None else {}
    proc = await asyncio.create_subprocess_exec(
        rg_bin(), *args,
        stdout=PIPE,
        stderr=PIPE,
        **kwargs,
    )
    assert proc.stdout is not None and proc.stderr is not None
    stderr_task = asyncio.create_task(_collect_stderr(proc.stderr))
    value = None
    timed_out = False
    collector_error = None
    try:
        value = await asyncio.wait_for(collector(proc.stdout), timeout)
    except asyncio.TimeoutError:
        timed_out = True
    except Exception as exc:
        collector_error = exc
    finally:
        # EOF normally means rg has exited; let asyncio observe its real status
        # before the capped/timeout cleanup path decides the child is still live.
        if value is not None and not value and not timed_out and collector_error is None:
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
        stderr = await _finish_process(proc, stderr_task)
    return _ProcessRun(value, proc.returncode, stderr, timed_out, collector_error)


async def _run_capped(
    args: list[str], max_results: int, timeout: float,
    source_encoding: Optional[SearchEncoding] = None,
) -> dict:
    out: list = []
    flags: dict = {"source_encoding": source_encoding} if source_encoding else {}
    run = await _run_process(
        args, timeout,
        lambda stdout: _collect_json(stdout, out, max_results, flags),
        limit=MAX_RECORD_BYTES,
    )
    return _capped_result(run, out, flags, timeout)


def _capped_result(
    run: _ProcessRun, out: list, flags: dict, timeout: float
) -> dict:
    timed_out = run.timed_out
    oversize = isinstance(run.collector_error, ValueError)
    collector_failed = run.collector_error is not None
    truncated = bool(run.value) or collector_failed or bool(flags.get("decode_error"))
    # rg exit codes: 0 = matches, 1 = none, 2 = real error.
    error = None
    error_type = None
    if oversize:
        cap = (f"{MAX_RECORD_BYTES / (1024 * 1024):.0f}MB"
               if MAX_RECORD_BYTES >= 1024 * 1024 else f"{MAX_RECORD_BYTES // 1024}KB")
        error = (
            f"a matching line exceeded {cap} and was skipped; results are partial. "
            "Refine the pattern or use the shell tool."
        )
        error_type = "decode"
    elif run.collector_error is not None:
        error = f"could not parse ripgrep output; results are partial: {run.collector_error}"
        error_type = "decode"
    elif run.returncode == 2:
        error = run.stderr or "ripgrep exited with code 2"
        error_type = "process"
    elif flags.get("decode_error"):
        error = "could not decode one or more ripgrep records; results are partial"
        error_type = "decode"
    elif timed_out:
        error = f"ripgrep timed out after {timeout}s; results are partial"
        error_type = "timeout"
    return {
        "ok": error is None,
        "matches": out,
        "count": sum(1 for r in out if r["kind"] == "match"),
        "truncated": truncated,
        "line_clipped": bool(flags.get("line_clipped")),
        "timed_out": timed_out,
        "error": error,
        "error_type": error_type,
    }


async def _run_files(args: list[str], cap: int, timeout: float) -> dict:
    paths: list[str] = []
    async def _collect_files(stdout: asyncio.StreamReader) -> bool:
        pending = bytearray()
        while chunk := await stdout.read(8192):
            pending.extend(chunk)
            while (end := pending.find(0)) >= 0:
                paths.append(decode_filename(bytes(pending[:end])))
                del pending[: end + 1]
                if len(paths) >= cap:
                    return True
            if len(pending) > MAX_RECORD_BYTES:
                raise ValueError("a file path exceeded the record limit")
        # Defensive support for producers that omit the final delimiter.
        if pending:
            paths.append(decode_filename(bytes(pending)))
            if len(paths) >= cap:
                return True
        return False

    run = await _run_process(args, timeout, _collect_files)
    collector_error = run.collector_error
    truncated = bool(run.value) or collector_error is not None
    timed_out = run.timed_out
    error = (
        run.stderr or "ripgrep exited with code 2"
    ) if run.returncode == 2 else None
    error_type = "process" if error else None
    if collector_error is not None:
        error = f"could not decode a file record; results are partial: {collector_error}"
        error_type = "decode"
    elif timed_out:
        error = f"ripgrep timed out after {timeout}s; results are partial"
        error_type = "timeout"
    return {
        "ok": error is None,
        "files": paths,
        "count": len(paths),
        "truncated": truncated,
        "timed_out": timed_out,
        "error": error,
        "error_type": error_type,
    }


def _subprocess_failure(kind: str, error: str, *, files: bool) -> dict:
    data = {
        "ok": False,
        "count": 0,
        "truncated": False,
        "timed_out": kind == "timeout",
        "error": error,
        "error_type": kind,
    }
    if files:
        data["files"] = []
    else:
        data.update({"matches": [], "line_clipped": False})
    return data


# --- tools -----------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
async def grep(
    pattern: str,
    path: str = ".",
    ignore_case: bool = False,
    glob: Optional[list[str]] = None,
    multiline: bool = False,
    context: int = 0,
    hidden: bool = False,
    no_ignore: bool = False,
    max_results: int = 200,
    encoding: Optional[SearchEncoding] = None,
) -> ToolResult:
    """Search file contents with ripgrep (regex, gitignore-aware). Returns matching
    lines as {file, line, column, text}; capped at max_results and flagged truncated
    if the cap is hit. Long matching lines are clipped to 500 chars (line_clipped
    flags it). Use `glob` (e.g. ['*.py']) to scope by file type. For a known
    legacy file, set encoding='windows-1252' so non-ASCII patterns match."""
    path = _resolve(path)
    if err := jail_error(path):
        return _result(f"❌ {err}",
                       {"matches": [], "count": 0, "truncated": False,
                        "line_clipped": False, "timed_out": False, "error": err})
    cap = max(1, min(max_results, MAX_RESULTS_CAP))
    args = build_grep_args(
        pattern, path, ignore_case, glob, multiline, context, hidden, no_ignore,
        encoding,
    )
    try:
        # Ripgrep transcodes an explicit source encoding to UTF-8 JSON. Preserve
        # that provenance even though the returned line bytes are now UTF-8.
        data = await _run_capped(
            args, cap, DEFAULT_TIMEOUT, source_encoding=encoding
        )
    except (OSError, RuntimeError) as exc:
        data = _subprocess_failure("spawn", str(exc), files=False)
    n = data["count"]
    flags = [label for key, label in (
        ("truncated", "truncated"), ("line_clipped", "long lines clipped"),
        ("timed_out", "timed out"), ("error", "error"),
    ) if data.get(key)]
    summary = f"{n} match(es) for {pattern!r}" + (f" [{', '.join(flags)}]" if flags else "")
    block = "\n".join(f"{r['file']}:{r['line']}: {r['text']}" for r in data["matches"])
    return _result(summary, data, block)


@mcp.tool(annotations={"readOnlyHint": True})
async def files(
    glob: Optional[list[str]] = None,
    path: str = ".",
    hidden: bool = False,
    no_ignore: bool = False,
    max_results: int = 500,
) -> ToolResult:
    """List files visible to ripgrep, optionally filtered by glob (e.g. ['*.ts',
    '!**/dist/**']). Respects .gitignore unless no_ignore is set. Returns file paths,
    capped at max_results."""
    path = _resolve(path)
    if err := jail_error(path):
        return _result(f"❌ {err}",
                       {"files": [], "count": 0, "truncated": False,
                        "timed_out": False, "error": err})
    cap = max(1, min(max_results, MAX_RESULTS_CAP))
    args = build_files_args(glob, path, hidden, no_ignore)
    try:
        data = await _run_files(args, cap, DEFAULT_TIMEOUT)
    except (OSError, RuntimeError) as exc:
        data = _subprocess_failure("spawn", str(exc), files=True)
    flags = [label for key, label in (
        ("truncated", "truncated"), ("timed_out", "timed out"),
        ("error", "error"),
    ) if data.get(key)]
    summary = f"{data['count']} file(s)" + (f" [{', '.join(flags)}]" if flags else "")
    block = "\n".join(data["files"])
    return _result(summary, data, block)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
