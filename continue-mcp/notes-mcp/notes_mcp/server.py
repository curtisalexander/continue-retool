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
  notes.search(query)               -> matching lines across all notes
  notes.write(name, content, append?)
  notes.delete(name)

Run:  uv run notes-mcp
"""
from __future__ import annotations

import json
import ntpath
import os
import re
import stat
import tempfile
import time
from typing import List

from fastmcp import FastMCP
from fastmcp.tools import ToolResult

from continue_mcp_common.config import env_int as _env_int
from continue_mcp_common.results import result as _result

mcp = FastMCP("notes")


NOTES_DIRNAME = ".continue-notes"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_HOOK = 100


# Bound output and the work needed to produce it. MAX_NOTE_BYTES also bounds an
# atomic append, while MAX_SEARCH_BYTES prevents a small result from requiring a
# scan of an arbitrarily large collection.
MAX_READ_BYTES = _env_int("NOTES_MCP_MAX_READ_BYTES", 50 * 1024, 1024, 1024 * 1024)
MAX_NOTE_BYTES = _env_int("NOTES_MCP_MAX_NOTE_BYTES", 4 * 1024 * 1024, 1024, 16 * 1024 * 1024)
MAX_MATCHES = _env_int("NOTES_MCP_MAX_MATCHES", 200, 1, 5000)
MAX_LINE_CHARS = _env_int("NOTES_MCP_MAX_LINE_CHARS", 500, 40, 10000)
MAX_INDEX_ENTRIES = _env_int("NOTES_MCP_MAX_INDEX_ENTRIES", 200, 1, 5000)
MAX_INDEX_BYTES = _env_int("NOTES_MCP_MAX_INDEX_BYTES", 50 * 1024, 1024, 1024 * 1024)
MAX_INDEX_SCAN_ENTRIES = _env_int(
    "NOTES_MCP_MAX_INDEX_SCAN_ENTRIES", 2000, MAX_INDEX_ENTRIES, 50000
)
MAX_SEARCH_BYTES = _env_int(
    "NOTES_MCP_MAX_SEARCH_BYTES", 2 * 1024 * 1024, 1024, 32 * 1024 * 1024
)
MAX_QUERY_BYTES = 1024


def _workspace() -> str:
    return os.path.realpath(os.path.abspath(os.environ.get("MCP_WORKSPACE") or os.getcwd()))


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
    """Return the real notes directory, confined to the real repository root."""
    configured = os.environ.get("NOTES_MCP_DIRNAME", NOTES_DIRNAME)
    parts = re.split(r"[\\/]", configured)
    if (
        not configured
        or configured == "."
        or os.path.isabs(configured)
        or ntpath.isabs(configured)
        or ".." in parts
    ):
        raise ValueError(
            "invalid NOTES_MCP_DIRNAME: use a relative directory inside the repository"
        )
    root = os.path.realpath(repo_root())
    candidate = os.path.realpath(os.path.join(root, configured))
    try:
        contained = os.path.commonpath((root, candidate)) == root
    except ValueError:
        contained = False
    if not contained:
        raise ValueError("NOTES_MCP_DIRNAME resolves outside the repository")
    return candidate


def note_path(name: str) -> str:
    """Validate the name (no separators, no traversal) and map it to a file."""
    if not _NAME.match(name) or ".." in name:
        raise ValueError(
            f"invalid note name {name!r}: use letters, digits, '.', '_', '-' "
            "(no path separators)"
        )
    directory = notes_dir()
    lexical = os.path.join(directory, name + ".md")
    # A note symlink makes later open/replace/delete operations ambiguous and can
    # redirect them after the directory itself was checked. Reject it explicitly.
    if os.path.islink(lexical):
        raise ValueError(f"unsafe note {name!r}: symbolic links are not supported")
    resolved = os.path.realpath(lexical)
    if os.path.commonpath((directory, resolved)) != directory:
        raise ValueError(f"unsafe note {name!r}: path resolves outside notes directory")
    return resolved


def hook_line(text: str) -> str:
    """First non-empty line, markdown heading markers stripped, length-capped."""
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s if len(s) <= MAX_HOOK else s[: MAX_HOOK - 1] + "…"
    return ""


def _bad_name(e: ValueError) -> ToolResult:
    """Invalid names come back as the same structured {ok: false} shape as any
    other failure — never a raised protocol-level exception."""
    return _result(f"❌ {e}", {"ok": False, "error": str(e)})


def _failure(error: Exception | str) -> ToolResult:
    message = str(error)
    return _result(f"❌ {message}", {"ok": False, "error": message})


def _clip_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: str, data: bytes) -> None:
    """Durably replace path with encoded data, preserving ordinary mode bits."""
    parent = os.path.dirname(path)
    mode = None
    if os.path.exists(path):
        mode = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    fd, temp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=parent)
    try:
        if mode is not None:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            else:
                os.chmod(temp_path, mode)
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        temp_path = ""
        _fsync_dir(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def _open_read(path: str):
    """Open a note without following a symlink introduced after validation."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    return os.fdopen(fd, "rb")


# --- tools -----------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
async def list() -> ToolResult:
    """List all notes as a cheap index: {name, hook, age_days}. Call at the start
    of a task, then read(name) for anything relevant — don't guess from hooks."""
    try:
        d = notes_dir()
    except ValueError as e:
        return _failure(e)
    if not os.path.isdir(d):
        data = {"notes": [], "count": 0, "dir": d}
        summary = f"{data['count']} note(s) in {data['dir']}"
        block = "\n".join(f"{n['name']} — {n['hook']} ({n['age_days']}d)" for n in data["notes"])
        return _result(summary, data, block)
    now = time.time()
    out = []
    index_bytes = 0
    scanned = 0
    skipped = 0
    truncated = False
    try:
        entries = os.scandir(d)
    except OSError as e:
        return _failure(f"cannot list notes directory: {e}")
    with entries:
        for entry in entries:
            scanned += 1
            if scanned > MAX_INDEX_SCAN_ENTRIES:
                truncated = True
                break
            fn = entry.name
            if not fn.endswith(".md") or not _NAME.fullmatch(fn[:-3]):
                continue
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    skipped += 1
                    continue
                with _open_read(entry.path) as f:
                    head = f.read(2048)
                item = {
                    "name": fn[:-3],
                    "hook": hook_line(head.decode("utf-8", errors="replace")),
                    "age_days": round((now - entry.stat(follow_symlinks=False).st_mtime) / 86400, 1),
                }
            except OSError:
                skipped += 1
                continue
            item_bytes = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            if len(out) >= MAX_INDEX_ENTRIES or index_bytes + item_bytes > MAX_INDEX_BYTES:
                truncated = True
                break
            index_bytes += item_bytes
            out.append(item)
    out.sort(key=lambda note: note["name"])
    data = {"notes": out, "count": len(out), "dir": d,
            "truncated": truncated, "skipped": skipped}
    summary = f"{data['count']} note(s) in {data['dir']}"
    block = "\n".join(f"{n['name']} — {n['hook']} ({n['age_days']}d)" for n in data["notes"])
    return _result(summary, data, block)


@mcp.tool(annotations={"readOnlyHint": True})
async def read(name: str) -> ToolResult:
    """Read one note (a name from list()). Capped at 50KB; an oversized note is
    truncated with a pointer to read the rest with fs.read on the returned path."""
    try:
        p = note_path(name)
    except ValueError as e:
        return _bad_name(e)
    if not os.path.isfile(p):
        data = {"ok": False, "error": f"no note named {name!r}"}
        return _result(f"❌ {data['error']}", data)
    try:
        with _open_read(p) as f:
            raw = f.read(MAX_READ_BYTES + 1)
    except OSError as e:
        return _failure(f"cannot read note {name!r}: {e}")
    truncated = len(raw) > MAX_READ_BYTES
    if truncated:
        # Truncate bytes first, then choose a complete line and UTF-8 sequence.
        raw = raw[:MAX_READ_BYTES].rsplit(b"\n", 1)[0]
    content = raw.decode("utf-8", errors="replace")
    content, encoding_clipped = _clip_utf8(content, MAX_READ_BYTES)
    truncated = truncated or encoding_clipped
    data = {"ok": True, "name": name, "path": p, "content": content,
            "truncated": truncated}
    block = content
    if truncated:
        block += (
            f"\n\n[Note exceeds {MAX_READ_BYTES // 1024}KB and was truncated. "
            f"Read the rest with fs.read on {p}.]"
        )
    summary = f"note: {name}" + (" (truncated)" if truncated else "")
    return _result(summary, data, block=block, lang="markdown")


@mcp.tool
async def write(name: str, content: str, append: bool = False) -> ToolResult:
    """Create or update a note (markdown). Make the FIRST line a one-line summary —
    it becomes the hook shown by list(). append adds to the end instead of replacing."""
    try:
        p = note_path(name)
    except ValueError as e:
        return _bad_name(e)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # Re-resolve after directory creation so a pre-existing or concurrently
        # introduced escaping directory symlink is caught before mutation.
        p = note_path(name)
        new_data = (content if content.endswith("\n") else content + "\n").encode("utf-8")
        if append and os.path.isfile(p):
            with _open_read(p) as f:
                old_data = f.read(MAX_NOTE_BYTES + 1)
            if old_data:
                new_data = old_data + b"\n" + new_data
        if len(new_data) > MAX_NOTE_BYTES:
            return _failure(
                f"note exceeds NOTES_MCP_MAX_NOTE_BYTES ({MAX_NOTE_BYTES} bytes)"
            )
        _atomic_write(p, new_data)
    except (OSError, UnicodeError, ValueError) as e:
        return _failure(f"cannot write note {name!r}: {e}")
    data = {"ok": True, "name": name, "path": p}
    return _result(f"saved note {data['name']} → {data['path']}", data)


@mcp.tool(annotations={"readOnlyHint": True})
async def search(query: str) -> ToolResult:
    """Case-insensitive substring search across all notes. Returns matching lines
    as {name, line, text}, capped at 200 matches (long lines clipped to 500 chars);
    truncated flags the cap. Use it when the list() hooks aren't enough."""
    if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        return _failure(f"query exceeds {MAX_QUERY_BYTES} bytes")
    try:
        d = notes_dir()
    except ValueError as e:
        return _failure(e)
    matches: List[dict] = []
    q = query.lower()
    truncated = False
    line_clipped = False
    scanned_bytes = 0
    skipped = 0
    if q and os.path.isdir(d):
        try:
            filenames = []
            with os.scandir(d) as entries:
                for entry in entries:
                    if len(filenames) >= MAX_INDEX_SCAN_ENTRIES:
                        truncated = True
                        break
                    filenames.append(entry.name)
            filenames.sort()
        except OSError as e:
            return _failure(f"cannot list notes directory: {e}")
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            p = os.path.join(d, fn)
            try:
                if os.path.islink(p) or not os.path.isfile(p):
                    skipped += 1
                    continue
                with _open_read(p) as f:
                    i = 0
                    while scanned_bytes < MAX_SEARCH_BYTES:
                        remaining = MAX_SEARCH_BYTES - scanned_bytes
                        raw_line = f.readline(remaining + 1)
                        if not raw_line:
                            break
                        if len(raw_line) > remaining:
                            truncated = True
                            break
                        scanned_bytes += len(raw_line)
                        i += 1
                        line = raw_line.decode("utf-8", errors="replace")
                        if q in line.lower():
                            text = line.rstrip("\r\n")
                            if len(text) > MAX_LINE_CHARS:
                                text = text[:MAX_LINE_CHARS] + f"…[+{len(text) - MAX_LINE_CHARS} chars]"
                                line_clipped = True
                            matches.append({"name": fn[:-3], "line": i, "text": text})
                            if len(matches) >= MAX_MATCHES:
                                truncated = True
                                break
                    if (
                        scanned_bytes >= MAX_SEARCH_BYTES
                        and os.fstat(f.fileno()).st_size > f.tell()
                    ):
                        truncated = True
            except OSError:
                skipped += 1
            if truncated:
                break
    data = {"query": query, "matches": matches, "count": len(matches),
            "truncated": truncated, "line_clipped": line_clipped,
            "scanned_bytes": scanned_bytes, "skipped": skipped}
    flags = [lbl for on, lbl in ((truncated, "truncated"),
                                 (line_clipped, "long lines clipped")) if on]
    summary = f"{data['count']} match(es) for {query!r}" + (f" [{', '.join(flags)}]" if flags else "")
    block = "\n".join(f"{m['name']}:{m['line']}: {m['text']}" for m in matches)
    return _result(summary, data, block)


@mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True})
async def delete(name: str) -> ToolResult:
    """Delete a note that is wrong or no longer needed."""
    try:
        p = note_path(name)
    except ValueError as e:
        return _bad_name(e)
    if not os.path.isfile(p):
        data = {"ok": False, "error": f"no note named {name!r}"}
        return _result(f"❌ {data['error']}", data)
    try:
        os.remove(p)
        _fsync_dir(os.path.dirname(p))
    except OSError as e:
        return _failure(f"cannot delete note {name!r}: {e}")
    data = {"ok": True, "name": name}
    return _result(f"deleted note {data['name']}", data)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
