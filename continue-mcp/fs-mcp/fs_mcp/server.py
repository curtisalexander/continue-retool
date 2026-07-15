"""
fs-mcp — line-ranged file reads and directory listings for Continue.dev,
replacing the built-in Read file / List dir tools.

Why: the built-in read tool's behavior pushes the agent into writing its own
throwaway PowerShell/Python scripts to inspect files. These two tools make the
direct path the easy path: numbered, line-ranged reads with hard caps (a huge
file can't flood the context window), and a depth-limited listing that always
skips .git.

Tools:
  fs.read(path, start_line?, limit?)   -> numbered lines, capped, with range info
  fs.list(path, depth?, include_hidden?) -> entries {path, type, size}, capped

Run:  uv run fs-mcp
"""
from __future__ import annotations

import os
from typing import Optional

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

mcp = FastMCP("fs")


def _result(summary: str, data: dict, block: str = "", lang: str = "") -> ToolResult:
    """content is what Continue's UI shows (summary + optional fenced block);
    structured_content is what the model/tests read via res.data."""
    md = summary
    if block.strip():
        md += f"\n\n```{lang}\n{block}\n```"
    return ToolResult(content=[TextContent(type="text", text=md)], structured_content=data)

DEFAULT_LIMIT = int(os.environ.get("FS_MCP_DEFAULT_LIMIT", "2000"))     # lines per read
MAX_LINE_CHARS = int(os.environ.get("FS_MCP_MAX_LINE_CHARS", "2000"))   # per-line cap
MAX_ENTRIES = int(os.environ.get("FS_MCP_MAX_ENTRIES", "500"))          # per listing
ALWAYS_SKIP = {".git"}


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


# --- tools -----------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
async def read(path: str, start_line: int = 1, limit: Optional[int] = None) -> ToolResult:
    """Read a file as numbered lines: "LINENO<TAB>text". start_line is 1-based;
    limit caps the line count (default 2000). Use the returned total_lines to
    page through big files with follow-up calls."""
    path = _resolve(path)
    if err := jail_error(path):
        return _result(f"❌ {err}", {"ok": False, "path": path, "error": err})
    if not os.path.isfile(path):
        data = {"ok": False, "path": path, "error": f"file not found: {path}"}
        return _result(f"❌ {data['error']}", data)
    limit = max(1, limit if limit is not None else DEFAULT_LIMIT)
    start = max(1, start_line)
    stop = start + limit  # exclusive

    # Stream line by line — a multi-GB log must never be slurped into memory
    # to serve a 50-line window. Lines outside the window are only counted.
    numbered: list[str] = []
    total = 0
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for total, ln in enumerate(f, start=1):
            if start <= total < stop:
                ln = ln.rstrip("\n")
                if len(ln) > MAX_LINE_CHARS:
                    ln = ln[:MAX_LINE_CHARS] + f"…[+{len(ln) - MAX_LINE_CHARS} chars]"
                numbered.append(f"{total}\t{ln}")
    end = start - 1 + len(numbered)
    data = {
        "ok": True,
        "path": path,
        "content": "\n".join(numbered),
        "start_line": start if numbered else 0,
        "end_line": end,
        "total_lines": total,
        "truncated": end < total,
    }
    summary = (
        f"{data['path']} · lines {data['start_line']}–{data['end_line']} "
        f"of {data['total_lines']}"
        + (" (truncated)" if data['truncated'] else "")
    )
    return _result(summary, data, data["content"])


@mcp.tool(annotations={"readOnlyHint": True})
async def list(path: str = ".", depth: int = 1, include_hidden: bool = False) -> ToolResult:
    """List a directory as {path, type, size} entries, dirs first, capped at 500.
    depth > 1 recurses that many levels; hidden files and .git are skipped unless
    include_hidden is set (.git is always skipped)."""
    path = _resolve(path)
    if err := jail_error(path):
        return _result(f"❌ {err}", {"ok": False, "path": path, "error": err})
    if not os.path.isdir(path):
        data = {"ok": False, "path": path, "error": f"not a directory: {path}"}
        return _result(f"❌ {data['error']}", data)
    depth = max(1, depth)
    entries: list[dict] = []
    truncated = False

    def walk(dir_path: str, level: int) -> None:
        nonlocal truncated
        if truncated:
            return
        try:
            children = sorted(
                os.scandir(dir_path),
                key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()),
            )
        except PermissionError:
            return
        for child in children:
            name = child.name
            if name in ALWAYS_SKIP or (name.startswith(".") and not include_hidden):
                continue
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                return
            rel = os.path.relpath(child.path, path)
            is_dir = child.is_dir(follow_symlinks=False)
            entry: dict = {"path": rel + (os.sep if is_dir else ""),
                           "type": "dir" if is_dir else "file"}
            if not is_dir:
                try:
                    entry["size"] = child.stat(follow_symlinks=False).st_size
                except OSError:
                    entry["size"] = None
            entries.append(entry)
            if is_dir and level < depth:
                walk(child.path, level + 1)

    walk(path, 1)
    data = {"ok": True, "path": path, "entries": entries,
            "count": len(entries), "truncated": truncated}
    summary = (
        f"{data['count']} entr(ies) in {data['path']}"
        + (" (truncated)" if data['truncated'] else "")
    )
    block = "\n".join(
        f"{'d' if e['type'] == 'dir' else 'f'}  {e['path']}"
        + ("" if e['type'] == 'dir' or e.get('size') is None else f"  ({e['size']}b)")
        for e in data["entries"]
    )
    return _result(summary, data, block)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
