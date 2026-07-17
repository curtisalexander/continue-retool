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
import json
import os
import re
import shutil
import unicodedata
from typing import Union

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


# --- Unicode-robust path resolution ----------------------------------------
# matcher.py fixes "the model's old_string looks identical but differs in bytes"
# for file CONTENT. The same thing happens to the FILENAME, where it surfaces as
# a bogus "file not found". The three that actually bite, all from pasting a name
# out of a macOS UI:
#   NFC vs NFD  — HFS+/APFS store decomposed ("é" = e + U+0301); models emit NFC
#   ' vs U+2019 — screenshot names use the curly apostrophe ("Capture d'écran")
#   NBSP + AM/PM — macOS screenshots put U+202F, not a space, before AM/PM
# Ported from pi's packages/coding-agent/src/core/tools/path-utils.ts. Kept in
# sync with fs-mcp's copy by hand — each server stays standalone by design.
_NARROW_NBSP = " "
_AMPM = re.compile(r" (AM|PM)\.", re.IGNORECASE)


def _path_variants(path: str) -> list[str]:
    """Byte-different spellings of `path` to try when the literal one misses.
    Deduped, literal path excluded.

    The three transforms are independent, so this takes their cross product
    rather than a fixed ladder. Pi hand-picks four rungs (nfd, curly, nfd+curly,
    am/pm) and so can't match a name that needs three at once — which is exactly
    the French macOS screenshot its own comment cites: curly apostrophe AND
    narrow NBSP AND NFD. The extra combinations cost only stat calls, and only
    on the miss path, where we were about to fail anyway."""
    quoted = [path, path.replace("'", "’")]
    spaced = []
    for p in quoted:
        spaced.append(p)
        alt = _AMPM.sub(_NARROW_NBSP + r"\1.", p)
        if alt != p:
            spaced.append(alt)
    seen = {path}
    out = []
    for p in spaced:
        for form in (p, unicodedata.normalize("NFD", p), unicodedata.normalize("NFC", p)):
            if form not in seen:
                seen.add(form)
                out.append(form)
    return out


def _resolve_existing(path: str) -> str:
    """_resolve, then fall back to Unicode variants if nothing is there. Only for
    paths that must ALREADY exist (edit/delete/move source) — never for a path
    being created, where the literal spelling the caller asked for is the answer."""
    resolved = _resolve(path)
    if os.path.exists(resolved):
        return resolved
    for variant in _path_variants(resolved):
        if os.path.exists(variant):
            return variant
    return resolved


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


# --- file IO that preserves bytes we don't touch ---------------------------
def _read(path: str) -> tuple[str, str]:
    """Returns (content, encoding).

    Concurrency note: the tools below do a synchronous read-modify-write —
    `before, enc = _read(path)` ... `_write(path, after, enc)` with NO `await`
    between the read and the write. That's what makes same-file edits safe under
    asyncio without a mutation queue (Pi needs one because Node fs is async).
    Keep it that way: an `await` inserted between _read and _write here reopens
    the lost-update race and would require serializing writes per path.

    UTF-8 first; a corporate cp1252/latin-1 file
    — the very environment this tool targets — must not blow up with a raw
    UnicodeDecodeError, and must be written back in ITS encoding, not silently
    transcoded to UTF-8. latin-1 is the final fallback (any byte decodes, and
    the read→write round-trip preserves every byte). Line endings and BOM are
    the matcher's job, so no newline translation happens here."""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise AssertionError("unreachable: latin-1 decodes any byte string")


def _write(path: str, content: str, encoding: str = "utf-8") -> None:
    with open(path, "w", encoding=encoding, newline="") as f:
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
    before, encoding = _read(path)
    try:
        after, strategy, count = find_and_replace(before, old_string, new_string, replace_all)
    except EditError as e:
        return _result(f"❌ edit failed: {e}", {"ok": False, "path": path, "error": str(e)})
    if not dry_run:
        _write(path, after, encoding)
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
    before, encoding = _read(path)
    try:
        after, results = apply_edits(before, _coerce_edits(edits))
    except EditError as e:
        return _result(f"❌ multi_edit failed: {e}", {"ok": False, "path": path, "error": str(e)})
    if not dry_run:
        _write(path, after, encoding)
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
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    _write(path, content)
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
