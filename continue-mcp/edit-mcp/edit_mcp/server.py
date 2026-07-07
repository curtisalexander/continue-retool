"""
edit-mcp — a robust file-edit tool for Continue.dev, replacing the built-in
Create/Edit file tools.

The matching engine (matcher.py) is ported from Pi's edit tool and fixes the
class of failure you hit constantly: `old_string` that looks identical but differs
in bytes (smart quotes, em-dashes, NBSP, NFC vs NFD accents, trailing whitespace,
CRLF). Exact match is tried first (byte-perfect); a normalized fuzzy fallback
catches the rest while leaving untouched lines exactly as they were.

Tools:
  edit(path, old_string, new_string, replace_all?)  -> replaces built-in "Edit file"
  multi_edit(path, edits)                            -> several edits, one write
  create_file(path, content, overwrite?)             -> replaces built-in "Create file"

Run:  uv run edit-mcp
"""
from __future__ import annotations

import difflib
import os

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from .matcher import EditError, apply_edits, find_and_replace

mcp = FastMCP("edit")


def _result(summary: str, data: dict, diff: str = "") -> ToolResult:
    """Return a ToolResult so Continue's UI shows a rendered summary + diff
    (content) while the model still gets the structured fields (res.data)."""
    md = summary
    if diff.strip():
        md += "\n\n```diff\n" + diff + "\n```"
    return ToolResult(
        content=[TextContent(type="text", text=md)],
        structured_content=data,
    )


def _resolve(path: str) -> str:
    """Relative paths resolve against MCP_WORKSPACE (falls back to server cwd),
    so they mean the same thing no matter where Continue launched this process."""
    if os.path.isabs(path):
        return path
    base = os.path.abspath(os.environ.get("MCP_WORKSPACE") or os.getcwd())
    return os.path.join(base, path)


# --- file IO that preserves bytes we don't touch ---------------------------
def _read(path: str) -> str:
    # newline="" stops Python from translating line endings; the matcher handles
    # CRLF/BOM itself so what we read is what's really on disk.
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def _preview(before: str, after: str, path: str, max_lines: int = 40) -> str:
    diff = difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=2,
    )
    lines = list(diff)
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... (+{len(lines) - max_lines} more diff lines)"]
    return "\n".join(lines)


# --- tools -----------------------------------------------------------------
@mcp.tool
async def edit(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> ToolResult:
    """Replace old_string with new_string in a file. Matches exactly first, then
    falls back to a Unicode-normalized match (smart quotes, dashes, NBSP, accents,
    trailing whitespace, CRLF) so non-ASCII regions still match. old_string must be
    unique unless replace_all is set."""
    path = _resolve(path)
    try:
        before = _read(path)
    except FileNotFoundError:
        raise EditError(f"file not found: {path}")
    try:
        after, strategy, count = find_and_replace(before, old_string, new_string, replace_all)
    except EditError as e:
        return _result(f"❌ edit failed: {e}", {"ok": False, "path": path, "error": str(e)})
    _write(path, after)
    diff = _preview(before, after, path)
    data = {
        "ok": True,
        "path": path,
        "strategy": strategy,          # "exact" or "fuzzy"
        "replacements": count,
        "diff": diff,
    }
    return _result(f"Edited {path} — {count} replacement(s), {strategy} match", data, diff)


@mcp.tool
async def multi_edit(path: str, edits: list[dict]) -> ToolResult:
    """Apply several edits to one file in a single write. `edits` is a list of
    {old_string, new_string, replace_all?}, applied in order (each sees the prior
    result). All must succeed or the file is left unchanged."""
    path = _resolve(path)
    try:
        before = _read(path)
    except FileNotFoundError:
        raise EditError(f"file not found: {path}")
    try:
        after, results = apply_edits(before, edits)
    except EditError as e:
        return _result(f"❌ multi_edit failed: {e}", {"ok": False, "path": path, "error": str(e)})
    _write(path, after)
    diff = _preview(before, after, path)
    data = {"ok": True, "path": path, "edits": results, "diff": diff}
    return _result(f"Applied {len(results)} edit(s) to {path}", data, diff)


@mcp.tool
async def create_file(path: str, content: str, overwrite: bool = False) -> ToolResult:
    """Create a new file with the given content. Fails if the file exists unless
    overwrite is set. Creates parent directories as needed."""
    path = _resolve(path)
    if os.path.exists(path) and not overwrite:
        return _result(
            f"❌ file exists: {path} (pass overwrite=true to replace)",
            {"ok": False, "path": path, "error": "file exists (pass overwrite=true to replace)"},
        )
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    _write(path, content)
    n = len(content.encode("utf-8"))
    diff = _preview("", content, path)
    return _result(f"Created {path} ({n} bytes)", {"ok": True, "path": path, "bytes": n}, diff)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
