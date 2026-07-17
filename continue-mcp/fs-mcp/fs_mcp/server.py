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
import os
import re
import unicodedata
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
# Total payload cap. The line and per-line caps MULTIPLY (2000 lines x 2000 chars
# is ~4MB), so on their own they don't bound the result at all — a wide file still
# floods the context window. This is the cap that actually binds, and it's why
# read reports truncated_by: whichever limit hits first wins.
MAX_BYTES = int(os.environ.get("FS_MCP_MAX_BYTES", str(50 * 1024)))
MAX_ENTRIES = int(os.environ.get("FS_MCP_MAX_ENTRIES", "500"))          # per listing
ALWAYS_SKIP = {".git"}
SNIFF_BYTES = 8192  # binary-detection window


def _resolve(path: str) -> str:
    """Relative paths resolve against MCP_WORKSPACE (falls back to server cwd),
    so they mean the same thing no matter where Continue launched this process."""
    if os.path.isabs(path):
        return path
    base = os.path.abspath(os.environ.get("MCP_WORKSPACE") or os.getcwd())
    return os.path.join(base, path)


# --- Unicode-robust path resolution ----------------------------------------
# Same failure this kit fixes for file CONTENT (see edit-mcp/matcher.py), applied
# to the FILENAME: the model emits a path that looks identical to what's on disk
# but differs in bytes, and a plain isfile() answers "file not found". The three
# that actually bite, all from pasting a name out of a macOS UI:
#   NFC vs NFD  — HFS+/APFS store decomposed ("é" = e + U+0301); models emit NFC
#   ' vs U+2019 — screenshot names use the curly apostrophe ("Capture d'écran")
#   NBSP + AM/PM — macOS screenshots put U+202F, not a space, before AM/PM
# Ported from pi's packages/coding-agent/src/core/tools/path-utils.ts.
_NARROW_NBSP = " "
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
    """_resolve, then fall back to Unicode variants if nothing is there. Returns
    the variant that exists, else the literal resolution (so the caller still
    reports "not found" against the path the model actually asked for)."""
    resolved = _resolve(path)
    if os.path.exists(resolved):
        return resolved
    for variant in _path_variants(resolved):
        if os.path.exists(variant):
            return variant
    return resolved


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
    limit caps the line count (default 2000). Output is also capped at 50KB total,
    whichever limit hits first. When the result is truncated it tells you the
    start_line to pass next; repeat until truncated is false to read the whole file."""
    path = _resolve_existing(path)
    if err := jail_error(path):
        return _result(f"❌ {err}", {"ok": False, "path": path, "error": err})
    if not os.path.isfile(path):
        data = {"ok": False, "path": path, "error": f"file not found: {path}"}
        return _result(f"❌ {data['error']}", data)
    if _is_binary(path):
        size = os.path.getsize(path)
        err = (
            f"binary file ({size} bytes) — not decodable as text. Use a shell "
            f"command if you need to inspect it (e.g. `file`, `xxd | head`)."
        )
        return _result(f"❌ {err}", {"ok": False, "path": path, "error": err,
                                     "binary": True, "size": size})
    limit = max(1, limit if limit is not None else DEFAULT_LIMIT)
    start = max(1, start_line)
    stop = start + limit  # exclusive

    # Stream line by line — a multi-GB log must never be slurped into memory
    # to serve a 50-line window. Lines outside the window are only counted, so
    # total_lines stays exact even when we stop collecting early.
    numbered: list[str] = []
    total = 0
    budget = MAX_BYTES
    truncated_by: Optional[str] = None
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for total, ln in enumerate(f, start=1):
            if start <= total < stop and truncated_by is None:
                ln = ln.rstrip("\n")
                if len(ln) > MAX_LINE_CHARS:
                    ln = ln[:MAX_LINE_CHARS] + f"…[+{len(ln) - MAX_LINE_CHARS} chars]"
                row = f"{total}\t{ln}"
                cost = len(row.encode("utf-8")) + 1  # +1 for the joining newline
                # Stop on whole lines only — never hand back half a line. The
                # first line alone can exceed the budget; emit it regardless so
                # a read always makes progress instead of returning nothing.
                if cost > budget and numbered:
                    truncated_by = "bytes"
                    continue
                budget -= cost
                numbered.append(row)
    end = start - 1 + len(numbered)
    if truncated_by is None and end < total:
        truncated_by = "lines"
    data = {
        "ok": True,
        "path": path,
        "content": "\n".join(numbered),
        "start_line": start if numbered else 0,
        "end_line": end,
        "total_lines": total,
        "truncated": end < total,
        "truncated_by": truncated_by if end < total else None,
        "next_start_line": end + 1 if end < total else None,
    }
    summary = (
        f"{data['path']} · lines {data['start_line']}–{data['end_line']} "
        f"of {data['total_lines']}"
    )
    block = data["content"]
    if data["truncated"]:
        why = f"{MAX_BYTES // 1024}KB limit" if truncated_by == "bytes" else f"{limit}-line limit"
        summary += f" (truncated: {why} — read on with start_line={data['next_start_line']})"
        # The hint goes in the fenced block too, not just the summary: it has to
        # survive in the payload the model reads back, next to where it ran out.
        block += (
            f"\n\n[Showing lines {data['start_line']}-{end} of {total} ({why}). "
            f"Use start_line={data['next_start_line']} to continue.]"
        )
    return _result(summary, data, block)


@mcp.tool(annotations={"readOnlyHint": True})
async def list(path: str = ".", depth: int = 1, include_hidden: bool = False) -> ToolResult:
    """List a directory as {path, type, size} entries, dirs first, capped at 500.
    depth > 1 recurses that many levels; hidden files and .git are skipped unless
    include_hidden is set (.git is always skipped)."""
    path = _resolve_existing(path)
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
