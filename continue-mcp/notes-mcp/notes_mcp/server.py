"""
notes-mcp — repo-local agent memory: an index plus one markdown file per note.

Notes are the agent's working memory (task state, discoveries, corrections) —
facts, not policy. Policy belongs in Continue rules; the companion rule in
../rules/notes.md is what makes the agent actually consult these tools.

Storage is LOCAL TO THE CURRENT REPO, never the home directory: notes live in
<repo-root>/.continue-notes/ (dirname configurable via NOTES_MCP_DIRNAME). The
repo root is found by walking up from MCP_WORKSPACE (or the server's cwd) to
the nearest .git; with no repo, the workspace itself is used. Plain .md files:
greppable, hand-editable, committable if a note graduates to shared truth.

Tools (progressive disclosure — the index is cheap, contents load on demand):
  notes.list()                      -> [{name, hook, age_days}]
  notes.read(name)                  -> full content of one note
  notes.write(name, content, append?)
  notes.delete(name)

Run:  uv run notes-mcp
"""
from __future__ import annotations

import os
import re
import time

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

mcp = FastMCP("notes")


def _result(summary: str, data: dict, block: str = "", lang: str = "") -> ToolResult:
    """content is what Continue's UI shows (summary + optional fenced block);
    structured_content is what the model/tests read via res.data."""
    md = summary
    if block.strip():
        md += f"\n\n```{lang}\n{block}\n```"
    return ToolResult(content=[TextContent(type="text", text=md)], structured_content=data)

NOTES_DIRNAME = os.environ.get("NOTES_MCP_DIRNAME", ".continue-notes")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_HOOK = 100


def _workspace() -> str:
    return os.path.abspath(os.environ.get("MCP_WORKSPACE") or os.getcwd())


def repo_root() -> str:
    """Nearest enclosing git repo root, walking up from the workspace; the
    workspace itself if there is no repo. Notes never leave the repo."""
    start = _workspace()
    cur = start
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return start
        cur = parent


def notes_dir() -> str:
    return os.path.join(repo_root(), NOTES_DIRNAME)


def note_path(name: str) -> str:
    """Validate the name (no separators, no traversal) and map it to a file."""
    if not _NAME.match(name) or ".." in name:
        raise ValueError(
            f"invalid note name {name!r}: use letters, digits, '.', '_', '-' "
            "(no path separators)"
        )
    return os.path.join(notes_dir(), name + ".md")


def hook_line(text: str) -> str:
    """First non-empty line, markdown heading markers stripped, length-capped."""
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s if len(s) <= MAX_HOOK else s[: MAX_HOOK - 1] + "…"
    return ""


# --- tools -----------------------------------------------------------------
@mcp.tool
async def list() -> ToolResult:
    """List all notes as a cheap index: {name, hook, age_days}. Call at the start
    of a task, then read(name) for anything relevant — don't guess from hooks."""
    d = notes_dir()
    if not os.path.isdir(d):
        data = {"notes": [], "count": 0, "dir": d}
        summary = f"{data['count']} note(s) in {data['dir']}"
        block = "\n".join(f"{n['name']} — {n['hook']} ({n['age_days']}d)" for n in data["notes"])
        return _result(summary, data, block)
    now = time.time()
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".md"):
            continue
        p = os.path.join(d, fn)
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(2048)
            age_days = round((now - os.path.getmtime(p)) / 86400, 1)
        except OSError:
            continue
        out.append({"name": fn[:-3], "hook": hook_line(head), "age_days": age_days})
    data = {"notes": out, "count": len(out), "dir": d}
    summary = f"{data['count']} note(s) in {data['dir']}"
    block = "\n".join(f"{n['name']} — {n['hook']} ({n['age_days']}d)" for n in data["notes"])
    return _result(summary, data, block)


@mcp.tool
async def read(name: str) -> ToolResult:
    """Read one note in full (a name from list())."""
    p = note_path(name)
    if not os.path.isfile(p):
        data = {"ok": False, "error": f"no note named {name!r}"}
        return _result(f"❌ {data['error']}", data)
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        data = {"ok": True, "name": name, "content": f.read()}
    return _result(f"note: {data['name']}", data, block=data["content"], lang="markdown")


@mcp.tool
async def write(name: str, content: str, append: bool = False) -> ToolResult:
    """Create or update a note (markdown). Make the FIRST line a one-line summary —
    it becomes the hook shown by list(). append adds to the end instead of replacing."""
    p = note_path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if append and os.path.isfile(p) and os.path.getsize(p) > 0:
        content = "\n" + content
    with open(p, "a" if append else "w", encoding="utf-8") as f:
        f.write(content if content.endswith("\n") else content + "\n")
    data = {"ok": True, "name": name, "path": p}
    return _result(f"saved note {data['name']} → {data['path']}", data)


@mcp.tool
async def delete(name: str) -> ToolResult:
    """Delete a note that is wrong or no longer needed."""
    p = note_path(name)
    if not os.path.isfile(p):
        data = {"ok": False, "error": f"no note named {name!r}"}
        return _result(f"❌ {data['error']}", data)
    os.remove(p)
    data = {"ok": True, "name": name}
    return _result(f"deleted note {data['name']}", data)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
