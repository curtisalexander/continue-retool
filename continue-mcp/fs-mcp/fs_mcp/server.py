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

mcp = FastMCP("fs")

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


# --- tools -----------------------------------------------------------------
@mcp.tool
async def read(path: str, start_line: int = 1, limit: Optional[int] = None) -> dict:
    """Read a file as numbered lines: "LINENO<TAB>text". start_line is 1-based;
    limit caps the line count (default 2000). Use the returned total_lines to
    page through big files with follow-up calls."""
    path = _resolve(path)
    if not os.path.isfile(path):
        return {"ok": False, "path": path, "error": f"file not found: {path}"}
    limit = max(1, limit if limit is not None else DEFAULT_LIMIT)
    start = max(1, start_line)

    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        text = f.read()
    lines = text.splitlines()
    total = len(lines)

    window = lines[start - 1 : start - 1 + limit]
    numbered = []
    for i, ln in enumerate(window, start=start):
        if len(ln) > MAX_LINE_CHARS:
            ln = ln[:MAX_LINE_CHARS] + f"…[+{len(ln) - MAX_LINE_CHARS} chars]"
        numbered.append(f"{i}\t{ln}")
    end = start - 1 + len(window)
    return {
        "ok": True,
        "path": path,
        "content": "\n".join(numbered),
        "start_line": start if window else 0,
        "end_line": end,
        "total_lines": total,
        "truncated": end < total,
    }


@mcp.tool
async def list(path: str = ".", depth: int = 1, include_hidden: bool = False) -> dict:
    """List a directory as {path, type, size} entries, dirs first, capped at 500.
    depth > 1 recurses that many levels; hidden files and .git are skipped unless
    include_hidden is set (.git is always skipped)."""
    path = _resolve(path)
    if not os.path.isdir(path):
        return {"ok": False, "path": path, "error": f"not a directory: {path}"}
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
    return {"ok": True, "path": path, "entries": entries,
            "count": len(entries), "truncated": truncated}


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
