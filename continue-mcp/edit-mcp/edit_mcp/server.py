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
import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from typing import Union

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from continue_mcp_common.config import env_int as _env_int
from continue_mcp_common.paths import jail_error
from continue_mcp_common.paths import resolve_existing as _resolve_existing
from continue_mcp_common.paths import resolve_path as _resolve

from .matcher import EditError, apply_edits, find_and_replace

mcp = FastMCP("edit")


# Hashing is the strongest cheap conflict check for ordinary source files. Keep
# the second read bounded; unusually large files use the stat fingerprint only.
CONFLICT_HASH_MAX_BYTES = _env_int(
    "EDIT_MCP_CONFLICT_HASH_MAX_BYTES", 1024 * 1024, 0, 64 * 1024 * 1024
)


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


# --- Unicode-robust path resolution ----------------------------------------
# matcher.py fixes "the model's old_string looks identical but differs in bytes"
# for file CONTENT. The same thing happens to the FILENAME, where it surfaces as
# a bogus "file not found". The three that actually bite, all from pasting a name
# out of a macOS UI:
#   NFC vs NFD  — HFS+/APFS store decomposed ("é" = e + U+0301); models emit NFC
#   ' vs U+2019 — screenshot names use the curly apostrophe ("Capture d'écran")
#   NBSP + AM/PM — macOS screenshots put U+202F, not a space, before AM/PM
# --- workspace jail (default ON) --------------------------------------------
# The recommended tool policy runs this server on Automatic — no human approval
# per call — so a prompt-injected "read ~/.ssh/id_rsa" must fail closed, not
# silently succeed. Every path is realpath'd (a symlink inside the workspace
# can't tunnel out) and must live under the workspace root or an extra root
# from MCP_JAIL_EXTRA (os.pathsep-separated). MCP_JAIL=0 disables. The
# sanctioned escape hatch for a legitimate out-of-workspace file is the shell
# tool, which is approval-gated by policy.
# --- file IO that preserves bytes we don't touch ---------------------------
class FileConflictError(Exception):
    """The destination changed after this operation read it."""


@dataclass(frozen=True)
class FileVersion:
    stat_key: tuple[int, int, int, int, int]
    digest: bytes | None


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _digest(raw: bytes) -> bytes:
    return hashlib.blake2b(raw, digest_size=16).digest()


def _read(path: str) -> tuple[str, str, FileVersion]:
    """Returns (content, encoding, version).

    Concurrency note: the tools below do a synchronous read-modify-write —
    `_read(path)` ... `_write(path, ...)` with NO `await` between the read and
    the write. That's what makes same-file edits safe under asyncio without a
    mutation queue (Pi needs one because Node fs is async).
    Keep it that way: an `await` inserted between _read and _write here reopens
    the lost-update race and would require serializing writes per path.

    UTF-8 first; a corporate cp1252/latin-1 file
    — the very environment this tool targets — must not blow up with a raw
    UnicodeDecodeError, and must be written back in ITS encoding, not silently
    transcoded to UTF-8. latin-1 is the final fallback (any byte decodes, and
    the read→write round-trip preserves every byte). Line endings and BOM are
    the matcher's job, so no newline translation happens here."""
    with open(path, "rb") as f:
        before = os.fstat(f.fileno())
        raw = f.read()
        after = os.fstat(f.fileno())
    if _stat_key(before) != _stat_key(after):
        raise FileConflictError(f"file changed while it was being read: {path}")
    version = FileVersion(
        stat_key=_stat_key(after),
        digest=_digest(raw) if len(raw) <= CONFLICT_HASH_MAX_BYTES else None,
    )
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc, version
        except UnicodeDecodeError:
            continue
    raise AssertionError("unreachable: latin-1 decodes any byte string")


def _sync_parent(path: str) -> None:
    """Best-effort directory fsync so a completed rename survives a crash."""
    if os.name == "nt":
        return
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


def _verify_unchanged(path: str, expected: FileVersion) -> None:
    """Cheap bounded optimistic-concurrency check immediately before replace."""
    try:
        if expected.digest is None:
            current = _stat_key(os.stat(path))
            unchanged = current == expected.stat_key
        else:
            # Ordinary source files take this stronger path. The extra read is
            # capped by CONFLICT_HASH_MAX_BYTES; metadata-only changes are okay.
            with open(path, "rb") as f:
                raw = f.read(CONFLICT_HASH_MAX_BYTES + 1)
            unchanged = len(raw) <= CONFLICT_HASH_MAX_BYTES and _digest(raw) == expected.digest
    except FileNotFoundError:
        unchanged = False
    if not unchanged:
        raise FileConflictError(
            f"file changed after it was read: {path}; reread it and retry the edit"
        )


def _write(
    path: str,
    content: str,
    encoding: str = "utf-8",
    expected_version: FileVersion | None = None,
) -> None:
    """Encode first, then atomically replace `path` from a sibling temp file.

    A sibling stays on the destination filesystem, which is required for atomic
    os.replace(). Resolve the final symlink so editing a safe in-workspace link
    updates its target rather than replacing the link itself.
    """
    payload = content.encode(encoding)  # fail before touching the destination
    target = os.path.realpath(path)
    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    previous_mode = None
    try:
        previous_mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        pass

    fd, temp_path = tempfile.mkstemp(
        dir=parent, prefix=f".{os.path.basename(target)}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if previous_mode is not None:
            os.chmod(temp_path, previous_mode)
        if expected_version is not None:
            _verify_unchanged(target, expected_version)
        os.replace(temp_path, target)
        _sync_parent(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.remove(temp_path)
        except OSError:
            pass


def _write_error(path: str, exc: OSError | UnicodeError | FileConflictError) -> ToolResult:
    error = f"could not safely write {path}: {exc}"
    return _result(f"❌ {error}", {"ok": False, "path": path, "error": error})


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
    dry_run: bool = False,
) -> ToolResult:
    """Replace old_string with new_string in a file. Matches exactly first, then
    falls back to a Unicode-normalized match (smart quotes, dashes, NBSP, accents,
    trailing whitespace, CRLF) so non-ASCII regions still match. old_string must be
    unique unless replace_all is set. dry_run previews the diff without writing."""
    path = _resolve_existing(path)
    if err := jail_error(path):
        return _result(f"❌ {err}", {"ok": False, "path": path, "error": err})
    if not os.path.isfile(path):
        return _result(f"❌ file not found: {path}",
                       {"ok": False, "path": path, "error": f"file not found: {path}"})
    try:
        before, encoding, version = _read(path)
    except (OSError, FileConflictError) as e:
        return _write_error(path, e)
    try:
        after, strategy, count = find_and_replace(before, old_string, new_string, replace_all)
    except EditError as e:
        return _result(f"❌ edit failed: {e}", {"ok": False, "path": path, "error": str(e)})
    if not dry_run:
        try:
            _write(path, after, encoding, expected_version=version)
        except (OSError, UnicodeError, FileConflictError) as e:
            return _write_error(path, e)
    diff = _preview(before, after, path)
    data = {
        "ok": True,
        "path": path,
        "strategy": strategy,          # "exact" or "fuzzy"
        "replacements": count,
        "encoding": encoding,
        "dry_run": dry_run,
        "diff": diff,
    }
    verb = "Would edit (dry run)" if dry_run else "Edited"
    return _result(f"{verb} {path} — {count} replacement(s), {strategy} match", data, diff)


def _coerce_edits(edits: Union[list[dict], str]) -> list[dict]:
    """Accept `edits` as a JSON string as well as a list. Several models emit the
    array double-encoded (Pi hard-codes the same workaround, naming Opus 4.6 and
    GLM-5.1); without this the call dies in schema validation before the tool ever
    runs, and the model gets a pydantic dump instead of anything actionable."""
    if isinstance(edits, str):
        try:
            parsed = json.loads(edits)
        except json.JSONDecodeError as e:
            raise EditError(
                f"edits was a string but not valid JSON ({e}). Pass a list of "
                f"{{old_string, new_string, replace_all?}} objects."
            ) from e
        if not isinstance(parsed, list):
            raise EditError(f"edits must be a list, got {type(parsed).__name__}.")
        edits = parsed
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            raise EditError(f"edits[{i}] must be an object, got {type(e).__name__}.")
        for key in ("old_string", "new_string"):
            if key not in e:
                raise EditError(f"edits[{i}] is missing {key}.")
    return edits


@mcp.tool
async def multi_edit(
    path: str, edits: Union[list[dict], str], dry_run: bool = False
) -> ToolResult:
    """Apply several edits to one file in a single write. `edits` is a list of
    {old_string, new_string, replace_all?}, applied in order (each sees the prior
    result). All must succeed or the file is left unchanged. dry_run previews only."""
    path = _resolve_existing(path)
    if err := jail_error(path):
        return _result(f"❌ {err}", {"ok": False, "path": path, "error": err})
    if not os.path.isfile(path):
        return _result(f"❌ file not found: {path}",
                       {"ok": False, "path": path, "error": f"file not found: {path}"})
    try:
        before, encoding, version = _read(path)
    except (OSError, FileConflictError) as e:
        return _write_error(path, e)
    try:
        after, results = apply_edits(before, _coerce_edits(edits))
    except EditError as e:
        return _result(f"❌ multi_edit failed: {e}", {"ok": False, "path": path, "error": str(e)})
    if not dry_run:
        try:
            _write(path, after, encoding, expected_version=version)
        except (OSError, UnicodeError, FileConflictError) as e:
            return _write_error(path, e)
    diff = _preview(before, after, path)
    data = {"ok": True, "path": path, "edits": results, "encoding": encoding,
            "dry_run": dry_run, "diff": diff}
    verb = "Would apply (dry run)" if dry_run else "Applied"
    return _result(f"{verb} {len(results)} edit(s) to {path}", data, diff)


@mcp.tool
async def create_file(path: str, content: str, overwrite: bool = False) -> ToolResult:
    """Create a new file with the given content. Fails if the file exists unless
    overwrite is set. Creates parent directories as needed."""
    path = _resolve(path)
    if err := jail_error(path):
        return _result(f"❌ {err}", {"ok": False, "path": path, "error": err})
    if os.path.exists(path) and not overwrite:
        return _result(
            f"❌ file exists: {path} (pass overwrite=true to replace)",
            {"ok": False, "path": path, "error": "file exists (pass overwrite=true to replace)"},
        )
    try:
        _write(path, content)
    except (OSError, UnicodeError) as e:
        return _write_error(path, e)
    n = len(content.encode("utf-8"))
    diff = _preview("", content, path)
    return _result(f"Created {path} ({n} bytes)", {"ok": True, "path": path, "bytes": n}, diff)


@mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True})
async def delete_file(path: str) -> ToolResult:
    """Delete one file (not a directory). Gives file deletion its own policy
    lane instead of routing rm through the shell tool."""
    path = _resolve_existing(path)
    if err := jail_error(path):
        return _result(f"❌ {err}", {"ok": False, "path": path, "error": err})
    if not os.path.isfile(path):
        return _result(f"❌ file not found: {path}",
                       {"ok": False, "path": path, "error": f"file not found: {path}"})
    os.remove(path)
    return _result(f"Deleted {path}", {"ok": True, "path": path})


@mcp.tool(annotations={"destructiveHint": True})
async def move_file(path: str, new_path: str, overwrite: bool = False) -> ToolResult:
    """Move or rename a file. Fails if the destination exists unless overwrite
    is set. Creates destination parent directories as needed."""
    src = _resolve_existing(path)
    dest = _resolve(new_path)  # literal: the destination is being created
    for p in (src, dest):
        if err := jail_error(p):
            return _result(f"❌ {err}", {"ok": False, "path": p, "error": err})
    if not os.path.isfile(src):
        return _result(f"❌ file not found: {src}",
                       {"ok": False, "path": src, "error": f"file not found: {src}"})
    if os.path.exists(dest) and not overwrite:
        return _result(
            f"❌ destination exists: {dest} (pass overwrite=true to replace)",
            {"ok": False, "path": dest,
             "error": "destination exists (pass overwrite=true to replace)"},
        )
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    try:
        os.replace(src, dest)      # atomic when src/dest share a filesystem
    except OSError:                # cross-device (EXDEV): copy + delete
        if os.path.exists(dest):
            os.remove(dest)
        shutil.move(src, dest)
    data = {"ok": True, "from": src, "to": dest}
    return _result(f"Moved {src} → {dest}", data)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
