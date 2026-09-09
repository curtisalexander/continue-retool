"""
fs-mcp — line-ranged file reads and directory listings for Continue.dev,
replacing the built-in Read file / List dir tools.

Why: the built-in read tool's behavior pushes the agent into writing its own
throwaway PowerShell/Python scripts to inspect files. These two tools make the
direct path the easy path: numbered, line-ranged reads with hard caps on BOTH
lines and total bytes (a huge file can't flood the context window, and a merely
*wide* one can't either), and a depth-limited listing that always skips .git.

Tools:
  fs.read(path, start_line?, limit?)   -> numbered lines, capped, with range info
  fs.list(path, depth?, include_hidden?) -> entries {path, type, size}, capped

Run:  uv run fs-mcp
"""
from __future__ import annotations

import codecs
import io
import os
from typing import List, Optional

from fastmcp import FastMCP
from fastmcp.tools import ToolResult

from continue_mcp_common.config import env_int as _env_int
from continue_mcp_common.paths import jail_error
from continue_mcp_common.paths import resolve_existing as _resolve_existing
from continue_mcp_common.results import result as _result
from continue_mcp_common.text import detect_reader

mcp = FastMCP("fs")


DEFAULT_LIMIT = _env_int("FS_MCP_DEFAULT_LIMIT", 2000, 1, 10_000)
MAX_LINE_CHARS = _env_int("FS_MCP_MAX_LINE_CHARS", 2000, 40, 100_000)
# Numbered-content byte cap (the protocol also carries a summary and a structured
# copy). The line and per-line caps MULTIPLY (2000 lines x 2000 chars
# is ~4MB), so on their own they don't bound the result at all — a wide file still
# floods the context window. This is the cap that actually binds, and it's why
# read reports truncated_by: whichever limit hits first wins.
MAX_BYTES = _env_int("FS_MCP_MAX_BYTES", 50 * 1024, 1024, 4 * 1024 * 1024)
MAX_ENTRIES = _env_int("FS_MCP_MAX_ENTRIES", 500, 1, 5000)
MAX_DEPTH = 20                                                            # recursion ceiling
MAX_LIST_ERRORS = 20                                                      # bounded diagnostics
MAX_SCANNED_ENTRIES = 5000                                                # internal work ceiling
ALWAYS_SKIP = {".git"}
SNIFF_BYTES = 8192  # binary-detection window


# --- Unicode-robust path resolution ----------------------------------------
# Same failure this kit fixes for file CONTENT (see edit-mcp/matcher.py), applied
# to the FILENAME: the model emits a path that looks identical to what's on disk
# but differs in bytes, and a plain isfile() answers "file not found". The three
# that actually bite, all from pasting a name out of a macOS UI:
#   NFC vs NFD  — HFS+/APFS store decomposed ("é" = e + U+0301); models emit NFC
#   ' vs U+2019 — screenshot names use the curly apostrophe ("Capture d'écran")
#   NBSP + AM/PM — macOS screenshots put U+202F, not a space, before AM/PM
# Ported from pi's packages/coding-agent/src/core/tools/path-utils.ts.
# Bytes that appear in real text: printable ASCII, tab/LF/CR/FF/BS/ESC, and
# everything >= 0x80 (which may be UTF-8 or a legacy code page — either way it's
# text, so a cp1252 file must not be mistaken for a binary).
_TEXT_BYTES = bytes({7, 8, 9, 10, 12, 13, 27}) + bytes(range(0x20, 0x100))


def _is_binary(path: str) -> bool:
    """Binary if the first 8KB holds a NUL, or if >30% of it is non-text bytes.
    This is the file(1)/git heuristic. NUL alone isn't enough — compressed and
    encrypted blobs often have none — and without the ratio test a PNG decodes
    to replacement-character mojibake that reports ok: true, so the model burns
    context on garbage it has no way to identify as binary."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(SNIFF_BYTES)
    except OSError:
        return False
    if not chunk:
        return False  # empty file is a fine, if boring, text file
    if chunk.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return False
    if b"\x00" in chunk:
        return True
    nontext = chunk.translate(None, delete=_TEXT_BYTES)
    if len(nontext) / len(chunk) > 0.30:
        return True
    # Everything >= 0x80 counted as text above, which is what keeps a cp1252 file
    # out of the binary bucket — but it also lets a NUL-free compressed blob pass.
    # Split the two by density: legacy-encoded prose is mostly ASCII with the odd
    # accent, while a blob is ~50% high bytes. So: not valid UTF-8 AND high-byte
    # dense == binary. The incremental decoder tolerates a multibyte character
    # straddling the end of the sniff window, which is not a decode failure.
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(chunk, False)
    except UnicodeDecodeError:
        high = sum(1 for b in chunk if b >= 0x80)
        return high / len(chunk) > 0.30
    return False


# --- defense-in-depth workspace path scoping (default ON) -------------------
# Realpath containment rejects straightforward paths outside MCP_WORKSPACE or
# MCP_JAIL_EXTRA roots. It is not a sandbox or a complete TOCTOU guarantee.
# --- tools -----------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
async def read(
    path: str,
    start_line: int = 1,
    limit: Optional[int] = None,
    encoding: Optional[str] = None,
    start_column: int = 1,
    content_only: bool = False,
) -> ToolResult:
    """Read saved disk text as "LINENO<TAB>text", not unsaved editor buffers.
    start_line and start_column are 1-based; columns count Unicode characters.
    Content is capped at 50KiB and limit lines (default 2000), plus metadata.
    Wide lines continue horizontally: pass BOTH returned start_line/start_column
    values until truncated=false. content_only omits line numbers, not metadata.
    Line endings are displayed as LF. Pass encoding for a known external codec,
    such as the encoding reported with a shell spill path."""
    path = _resolve_existing(path)
    if err := jail_error(path):
        return _result(f"❌ {err}", {"ok": False, "path": path, "error": err,
                                         "error_type": "jail"})
    if not os.path.isfile(path):
        data = {"ok": False, "path": path, "error": f"file not found: {path}",
                "error_type": "not_found"}
        return _result(f"❌ {data['error']}", data)
    if encoding is None and _is_binary(path):
        size = os.path.getsize(path)
        err = (
            f"binary file ({size} bytes) — not decodable as text. Use a shell "
            f"command if you need to inspect it (e.g. `file`, `xxd | head`)."
        )
        return _result(f"❌ {err}", {"ok": False, "path": path, "error": err,
                                     "error_type": "binary", "binary": True, "size": size})
    limit = max(1, limit if limit is not None else DEFAULT_LIMIT)
    start = max(1, start_line)
    column = max(1, start_column)

    rows: List[str] = []
    observed_lines = 0
    budget = MAX_BYTES
    truncated_by: Optional[str] = None
    reached_eof = False
    # Detect with bounded memory, then stream only through the requested page.
    # Legacy encoding detection may scan the file, but a multi-GB log is never
    # retained merely to return a small page.
    try:
        with open(path, "rb") as raw:
            decoded = detect_reader(raw, codec=encoding)
    except (LookupError, UnicodeError) as exc:
        error = f"could not decode {path}: {exc}"
        return _result(
            f"❌ {error}",
            {"ok": False, "path": path, "error": error, "error_type": "decode"},
        )
    with open(path, "rb") as raw:
        raw.seek(len(decoded.bom))
        errors = "replace" if decoded.had_errors else "strict"
        text = io.TextIOWrapper(raw, encoding=decoded.codec, errors=errors, newline=None)
        completed = 0
        last_line = 0
        next_line = next_column = None
        for observed_lines, ln in enumerate(text, start=1):
            if observed_lines < start:
                continue
            if completed >= limit:
                truncated_by, next_line, next_column = "lines", observed_lines, 1
                break
            ln = ln.rstrip("\n")
            offset = column - 1 if observed_lines == start else 0
            prefix = "" if content_only else f"{observed_lines}\t"
            separator = 1 if rows else 0
            room = budget - len(prefix.encode("utf-8")) - separator
            candidate = ln[offset: offset + MAX_LINE_CHARS]
            # Preserve whole-line pagination where possible. Only split the
            # first row of a page, so a following request always has room to
            # make progress even at a multibyte character boundary.
            if rows and len(candidate.encode("utf-8")) > room:
                truncated_by, next_line, next_column = "bytes", observed_lines, offset + 1
                break
            byte_limited = False
            if offset >= len(ln):
                piece = ""
                line_done = True
            else:
                # Bound both characters and encoded bytes without splitting a
                # Unicode character. Leave room for an optional line prefix.
                piece_chars = []
                used = 0
                for char in candidate:
                    size = len(char.encode("utf-8"))
                    if used + size > room:
                        byte_limited = True
                        break
                    piece_chars.append(char)
                    used += size
                piece = "".join(piece_chars)
                line_done = offset + len(piece) >= len(ln)
            row = prefix + piece
            cost = len(row.encode("utf-8")) + separator
            if cost > budget:
                truncated_by, next_line, next_column = "bytes", observed_lines, offset + 1
                break
            rows.append(row)
            last_line = observed_lines
            budget -= cost
            if not line_done:
                truncated_by = "bytes" if byte_limited else "characters"
                next_line, next_column = observed_lines, offset + len(piece) + 1
                break
            completed += 1
            column = 1
        else:
            reached_eof = True
    end = last_line
    total_lines = observed_lines if reached_eof else None
    data = {
        "ok": True,
        "path": path,
        "content": "\n".join(rows),
        "content_only": content_only,
        "start_line": start if rows else 0,
        "start_column": max(1, start_column),
        "end_line": end,
        "total_lines": total_lines,
        "total_lines_exact": reached_eof,
        "total_lines_at_least": observed_lines,
        "lines_scanned": observed_lines,
        "truncated": not reached_eof,
        "truncated_by": truncated_by if not reached_eof else None,
        "next_start_line": next_line if not reached_eof else None,
        "next_start_column": next_column if not reached_eof else None,
        "encoding": decoded.codec,
        "bom": decoded.bom.hex() if decoded.bom else None,
        "had_errors": decoded.had_errors,
        "decode_loss": decoded.loss,
    }
    total_label = str(total_lines) if reached_eof else f"at least {observed_lines}"
    summary = (f"{data['path']} · lines {data['start_line']}–{data['end_line']} "
               f"of {total_label} · start_column={data['start_column']} · "
               f"encoding={decoded.codec} bom={data['bom'] or 'none'} "
               f"decode_loss={str(decoded.loss).lower()} · "
               f"truncated={str(data['truncated']).lower()}")
    block = data["content"]
    if data["truncated"]:
        why = {"bytes": f"{MAX_BYTES}-byte content limit",
               "characters": f"{MAX_LINE_CHARS}-character line segment limit",
               "lines": f"{limit}-line limit"}[truncated_by]
        continuation = (f"start_line={data['next_start_line']}, "
                        f"start_column={data['next_start_column']}")
        summary += f" (truncated: {why} — read on with {continuation})"
        # The hint goes in the fenced block too, not just the summary: it has to
        # survive in the payload the model reads back, next to where it ran out.
        block += (
            f"\n\n[Showing lines {data['start_line']}-{end} of {total_label} ({why}). "
            f"Use {continuation} to continue.]"
        )
    return _result(summary, data, block)


@mcp.tool(annotations={"readOnlyHint": True})
async def list(path: str = ".", depth: int = 1, include_hidden: bool = False) -> ToolResult:
    """List a directory as {path, type, size} entries, dirs first, capped at 500.
    depth > 1 recurses that many levels; hidden files and .git are skipped unless
    include_hidden is set (.git is always skipped)."""
    path = _resolve_existing(path)
    if err := jail_error(path):
        return _result(f"❌ {err}", {"ok": False, "path": path, "error": err,
                                         "error_type": "jail"})
    if not os.path.isdir(path):
        data = {"ok": False, "path": path, "error": f"not a directory: {path}"}
        return _result(f"❌ {data['error']}", data)
    requested_depth = max(1, depth)
    depth = min(requested_depth, MAX_DEPTH)
    entries: List[dict] = []
    errors: List[dict] = []
    truncated = requested_depth > depth
    skipped = 0
    error_count = 0
    scanned = 0
    scan_capped = False

    def record_error(entry_path: str, error: OSError, entry_skipped: bool = True) -> None:
        nonlocal error_count, skipped
        error_count += 1
        if entry_skipped:
            skipped += 1
        if len(errors) < MAX_LIST_ERRORS:
            errors.append({"path": os.path.relpath(entry_path, path), "error": str(error)})

    def walk(dir_path: str, level: int) -> None:
        nonlocal scanned, truncated, scan_capped
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            return
        try:
            with os.scandir(dir_path) as iterator:
                children = []
                for child in iterator:
                    if scanned >= MAX_SCANNED_ENTRIES:
                        truncated = True
                        scan_capped = True
                        break
                    scanned += 1
                    if child.name in ALWAYS_SKIP or (child.name.startswith(".") and not include_hidden):
                        continue
                    children.append(child)
        except OSError as e:
            record_error(dir_path, e)
            return
        typed_children = []
        for child in children:
            try:
                is_dir = child.is_dir(follow_symlinks=False)
            except OSError as e:
                record_error(child.path, e)
                continue
            typed_children.append((not is_dir, child.name.lower(), child, is_dir))
        typed_children.sort(key=lambda item: (item[0], item[1]))
        for _, _, child, is_dir in typed_children:
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                return
            rel = os.path.relpath(child.path, path)
            entry: dict = {"path": rel + (os.sep if is_dir else ""),
                           "type": "dir" if is_dir else "file"}
            if not is_dir:
                try:
                    entry["size"] = child.stat(follow_symlinks=False).st_size
                except OSError as e:
                    entry["size"] = None
                    record_error(child.path, e, entry_skipped=False)
            entries.append(entry)
            if is_dir and level < depth:
                walk(child.path, level + 1)

    walk(path, 1)
    partial = error_count > 0
    data = {"ok": True, "path": path, "entries": entries,
            "count": len(entries), "truncated": truncated, "partial": partial,
            "requested_depth": requested_depth, "depth": depth,
            "depth_capped": requested_depth > depth, "skipped": skipped,
            "scanned": scanned, "scan_capped": scan_capped, "errors": errors,
            "errors_truncated": error_count > len(errors)}
    summary = (
        f"{data['count']} entr(ies) in {data['path']}"
        + (" (truncated)" if data['truncated'] else "")
        + (f" (scan limit: examined {scanned} entries, including hidden entries; narrow path)" if scan_capped else "")
        + (f" (partial: {error_count} inaccessible)" if partial else "")
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
