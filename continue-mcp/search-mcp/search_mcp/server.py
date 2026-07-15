"""
search-mcp — replace Continue's built-in Grep/Glob search with ripgrep.

Shells out to the `rg` binary (install it with `uv tool install ripgrep`, see
README) and returns compact, structured results the agent can act on directly.
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
import json
import os
import shutil
from typing import Optional

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

mcp = FastMCP("search")


def _result(summary: str, data: dict, block: str = "", lang: str = "") -> ToolResult:
    """content is what Continue's UI shows (summary + optional fenced block);
    structured_content is what the model/tests read via res.data."""
    md = summary
    if block.strip():
        md += f"\n\n```{lang}\n{block}\n```"
    return ToolResult(content=[TextContent(type="text", text=md)], structured_content=data)

DEFAULT_TIMEOUT = float(os.environ.get("SEARCH_MCP_TIMEOUT", "30"))
MAX_RESULTS_CAP = int(os.environ.get("SEARCH_MCP_MAX_RESULTS", "1000"))


def _resolve(path: str) -> str:
    """Relative paths resolve against MCP_WORKSPACE (falls back to server cwd),
    so they mean the same thing no matter where Continue launched this process."""
    if os.path.isabs(path):
        return path
    base = os.path.abspath(os.environ.get("MCP_WORKSPACE") or os.getcwd())
    return os.path.join(base, path)

# --- workspace jail (default ON) --------------------------------------------
# The recommended tool policy runs this server on Automatic — no human approval
# per call — so a prompt-injected "read ~/.ssh/id_rsa" must fail closed, not
# silently succeed. Every path is realpath'd (a symlink inside the workspace
# can't tunnel out) and must live under the workspace root or an extra root
# from MCP_JAIL_EXTRA (os.pathsep-separated). MCP_JAIL=0 disables. The
# sanctioned escape hatch for a legitimate out-of-workspace file is the shell
# tool, which is approval-gated by policy.
def _jail_roots() -> list[str]:
    if os.environ.get("MCP_JAIL", "1").strip().lower() in ("0", "false", "off", "no"):
        return []
    roots = [os.path.abspath(os.environ.get("MCP_WORKSPACE") or os.getcwd())]
    for extra in (os.environ.get("MCP_JAIL_EXTRA") or "").split(os.pathsep):
        if extra.strip():
            roots.append(os.path.abspath(extra.strip()))
    return [os.path.normcase(os.path.realpath(r)) for r in roots]


def jail_error(path: str) -> str | None:
    """None if `path` is allowed; else a model-facing refusal that names the
    escalation paths (ask the user / approval-gated shell)."""
    roots = _jail_roots()
    if not roots:
        return None
    real = os.path.normcase(os.path.realpath(path))
    for root in roots:
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            return None
    return (
        f"path is outside the workspace jail: {path} (workspace: "
        f"{os.environ.get('MCP_WORKSPACE') or os.getcwd()}). This tool only "
        "touches the workspace (MCP_JAIL_EXTRA adds roots; MCP_JAIL=0 "
        "disables). For a legitimate outside file, ask the user or use a "
        "shell command, which requires approval."
    )


# --- locate the rg binary --------------------------------------------------
def rg_bin() -> str:
    """Find the ripgrep binary. Honor RIPGREP_BIN, else PATH."""
    b = os.environ.get("RIPGREP_BIN") or shutil.which("rg")
    if not b:
        raise RuntimeError(
            "ripgrep (`rg`) was not found on PATH. Install it with:\n"
            "    uv tool install ripgrep\n"
            "or point RIPGREP_BIN at the rg binary."
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
    args = ["--files"]
    if hidden:
        args.append("--hidden")
    if no_ignore:
        args.append("--no-ignore")
    for g in glob or []:
        args += ["-g", g]
    args.append(path)
    return args


# --- subprocess plumbing ---------------------------------------------------
async def _collect_json(proc, out: list, max_results: int) -> bool:
    """Stream rg --json, append match/context rows to `out`, stop at the cap.
    Returns True if we truncated."""
    matches = 0
    assert proc.stdout is not None
    async for raw in proc.stdout:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t not in ("match", "context"):
            continue
        d = obj["data"]
        row = {
            "file": d["path"]["text"],
            "line": d.get("line_number"),
            "text": d["lines"]["text"].rstrip("\n"),
            "kind": t,
        }
        if t == "match":
            subs = d.get("submatches") or []
            if subs:
                row["column"] = subs[0]["start"] + 1
            matches += 1
        out.append(row)
        if matches >= max_results:
            return True
    return False


async def _run_capped(args: list[str], max_results: int, timeout: float) -> dict:
    rg = rg_bin()
    proc = await asyncio.create_subprocess_exec(
        rg, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out: list = []
    timed_out = False
    truncated = False
    try:
        truncated = await asyncio.wait_for(
            _collect_json(proc, out, max_results), timeout
        )
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        if proc.returncode is None:
            proc.kill()
        err = b""
        try:
            _, err = await asyncio.wait_for(proc.communicate(), 5)
        except (asyncio.TimeoutError, ValueError):
            pass
    # rg exit codes: 0 = matches, 1 = none, 2 = real error.
    error = None
    if proc.returncode == 2 and err:
        error = err.decode("utf-8", "replace").strip()
    return {
        "matches": out,
        "count": sum(1 for r in out if r["kind"] == "match"),
        "truncated": truncated,
        "timed_out": timed_out,
        "error": error,
    }


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
) -> ToolResult:
    """Search file contents with ripgrep (regex, gitignore-aware). Returns matching
    lines as {file, line, column, text}; capped at max_results and flagged truncated
    if the cap is hit. Use `glob` (e.g. ['*.py']) to scope by file type."""
    path = _resolve(path)
    if err := jail_error(path):
        return _result(f"❌ {err}",
                       {"matches": [], "count": 0, "truncated": False,
                        "timed_out": False, "error": err})
    cap = max(1, min(max_results, MAX_RESULTS_CAP))
    args = build_grep_args(
        pattern, path, ignore_case, glob, multiline, context, hidden, no_ignore
    )
    data = await _run_capped(args, cap, DEFAULT_TIMEOUT)
    n = data["count"]
    flags = [label for key, label in (
        ("truncated", "truncated"), ("timed_out", "timed out"), ("error", "error"),
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
    rg = rg_bin()
    args = build_files_args(glob, path, hidden, no_ignore)
    proc = await asyncio.create_subprocess_exec(
        rg, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    paths: list[str] = []
    truncated = False
    timed_out = False
    assert proc.stdout is not None
    try:
        async def _read() -> bool:
            async for raw in proc.stdout:
                paths.append(raw.decode("utf-8", "replace").rstrip("\n"))
                if len(paths) >= cap:
                    return True
            return False

        truncated = await asyncio.wait_for(_read(), DEFAULT_TIMEOUT)
    except asyncio.TimeoutError:
        timed_out = True  # a timeout is not a complete listing — say so
    finally:
        if proc.returncode is None:
            proc.kill()
        try:
            await asyncio.wait_for(proc.communicate(), 5)
        except (asyncio.TimeoutError, ValueError):
            pass
    data = {"files": paths, "count": len(paths), "truncated": truncated,
            "timed_out": timed_out}
    flags = [f for f, on in (("truncated", truncated), ("timed out", timed_out)) if on]
    summary = f"{data['count']} file(s)" + (f" [{', '.join(flags)}]" if flags else "")
    block = "\n".join(data["files"])
    return _result(summary, data, block)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
