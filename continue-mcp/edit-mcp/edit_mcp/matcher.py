"""
matcher.py — robust old/new string replacement, ported from the engineering in
Pi's edit tool (badlogic/pi-mono, packages/coding-agent/src/core/tools/edit-diff.ts:
fuzzyFindText / normalizeForFuzzyMatch / stripBom / detectLineEnding).

The whole point: models emit `old_string` that *looks* identical to what's on disk
but differs in bytes — curly quotes vs straight, en/em dashes vs hyphen, NBSP vs
space, NFC vs NFD accents (macOS paste!), a stray trailing space, or CRLF vs LF.
Exact `str.replace` then fails on anything non-ASCII. This module fixes that.

Strategy (two-tier, matching Pi):
  1. EXACT match on the raw text — if it hits, nothing is normalized and every
     byte is preserved.
  2. FUZZY fallback — normalize both sides (NFKC, per-line trailing-trim, smart
     quotes, dashes, exotic spaces), find the match in normalized space, then map
     it back to real LINE RANGES. Only the touched lines are rewritten; every
     untouched line is copied verbatim from the original, so exotic characters
     elsewhere in the file are never disturbed.

Deliberately dependency-free (stdlib only) so it's trivially unit-testable.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from bisect import bisect_right

# --- character classes we fold during fuzzy matching (mirrors Pi) ----------
_SMART_SINGLE = re.compile(r"[‘’‚‛]")           # ‘ ’ ‚ ‛  -> '
_SMART_DOUBLE = re.compile(r"[“”„‟]")           # “ ” „ ‟  -> "
_DASHES = re.compile(r"[‐‑‒–—―−]")  # 7 dashes -> -
_SPACES = re.compile(
    r"[        　]"      # 9 spaces -> space
)


class EditError(Exception):
    """Raised for empty/absent/ambiguous matches, with a model-friendly message."""


# --- primitives ------------------------------------------------------------
def strip_bom(text: str) -> tuple[str, str]:
    """Split a leading UTF-8 BOM off. The model never includes the invisible BOM
    in old_string, so we set it aside and re-attach on write."""
    if text.startswith("﻿"):
        return "﻿", text[1:]
    return "", text


def detect_line_ending(text: str) -> str:
    """CRLF if any \\r\\n present, else CR if any lone \\r, else LF."""
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _to_lf(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def normalize_for_fuzzy(text: str) -> str:
    """NFKC -> per-line trailing-whitespace trim -> fold quotes/dashes/spaces.
    Order matches Pi. Operates on a single logical string (may contain \\n)."""
    text = unicodedata.normalize("NFKC", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _SMART_SINGLE.sub("'", text)
    text = _SMART_DOUBLE.sub('"', text)
    text = _DASHES.sub("-", text)
    text = _SPACES.sub(" ", text)
    return text


# --- line-offset helpers for mapping normalized matches back to lines ------
def _line_starts(lines: list[str]) -> list[int]:
    starts, off = [], 0
    for ln in lines:
        starts.append(off)
        off += len(ln) + 1  # +1 for the joining newline
    return starts


def _locate(starts: list[int], offset: int) -> tuple[int, int]:
    i = bisect_right(starts, offset) - 1
    return i, offset - starts[i]


def _fuzzy_norm_lines(content_lf: str) -> tuple[list[str], list[str], str]:
    orig_lines = content_lf.split("\n")
    norm_lines = [normalize_for_fuzzy(ln) for ln in orig_lines]
    return orig_lines, norm_lines, "\n".join(norm_lines)


def _fuzzy_count(content_lf: str, old_lf: str) -> int:
    _, _, norm_content = _fuzzy_norm_lines(content_lf)
    norm_old = normalize_for_fuzzy(old_lf)
    if not norm_old:
        return 0
    return norm_content.count(norm_old)


def _fuzzy_replace_first(content_lf: str, old_lf: str, new_lf: str) -> str | None:
    """Replace the first fuzzy occurrence, preserving unchanged lines verbatim.
    Returns None if there's no fuzzy match."""
    orig_lines, norm_lines, norm_content = _fuzzy_norm_lines(content_lf)
    norm_old = normalize_for_fuzzy(old_lf)
    idx = norm_content.find(norm_old)
    if idx == -1:
        return None
    end = idx + len(norm_old)
    starts = _line_starts(norm_lines)
    ls, cs = _locate(starts, idx)
    le, ce = _locate(starts, end)
    # The touched line-range [ls, le] is rebuilt from normalized text; the edited
    # region is swapped for new_lf. Everything outside [ls, le] stays original.
    prefix = norm_lines[ls][:cs]
    suffix = norm_lines[le][ce:]
    rebuilt = orig_lines[:ls] + [prefix + new_lf + suffix] + orig_lines[le + 1:]
    return "\n".join(rebuilt)


def _closest_hint(content_lf: str, old_lf: str, max_lines: int = 5000) -> str | None:
    """Best-effort 'did you mean' diff for a no-match, à la difflib>0.9 tools."""
    content_lines = content_lf.split("\n")[:max_lines]
    old_lines = old_lf.split("\n")
    n = max(1, len(old_lines))
    best = (0.0, 0, "")
    for i in range(0, max(1, len(content_lines) - n + 1)):
        window = "\n".join(content_lines[i:i + n])
        ratio = difflib.SequenceMatcher(None, old_lf, window).ratio()
        if ratio > best[0]:
            best = (ratio, i, window)
    if best[0] < 0.5:
        return None
    diff = difflib.unified_diff(
        old_lf.splitlines(), best[2].splitlines(),
        fromfile="your old_string", tofile=f"file near line {best[1] + 1}",
        lineterm="", n=1,
    )
    return f"Closest match ~{best[0]:.0%} near line {best[1] + 1}:\n" + "\n".join(diff)


def _finish(bom: str, result_lf: str, eol: str) -> str:
    return bom + result_lf.replace("\n", eol)


# --- the public entry point ------------------------------------------------
def find_and_replace(
    content: str, old: str, new: str, replace_all: bool = False
) -> tuple[str, str, int]:
    """Return (new_content, strategy, count) where strategy is 'exact' or 'fuzzy'.

    Raises EditError on empty old_string, no match, or a non-unique match when
    replace_all is False."""
    if old == "":
        raise EditError("old_string must not be empty.")

    bom, body = strip_bom(content)
    eol = detect_line_ending(body)
    c, o, nw = _to_lf(body), _to_lf(old), _to_lf(new)

    # 1) EXACT — preserves every byte, no normalization applied.
    exact = c.count(o)
    if exact > 0:
        if not replace_all and exact > 1:
            raise EditError(
                f"old_string is not unique: {exact} exact matches. Add surrounding "
                f"context to disambiguate, or pass replace_all=true."
            )
        result = c.replace(o, nw) if replace_all else c.replace(o, nw, 1)
        return _finish(bom, result, eol), "exact", (exact if replace_all else 1)

    # 2) FUZZY — normalized match, unchanged lines preserved verbatim.
    fuzzy = _fuzzy_count(c, o)
    if fuzzy == 0:
        hint = _closest_hint(c, o)
        raise EditError("old_string not found." + (f"\n{hint}" if hint else ""))
    if not replace_all and fuzzy > 1:
        raise EditError(
            f"old_string is not unique: {fuzzy} fuzzy matches. Add surrounding "
            f"context to disambiguate, or pass replace_all=true."
        )

    if not replace_all:
        result = _fuzzy_replace_first(c, o, nw)
        assert result is not None
        return _finish(bom, result, eol), "fuzzy", 1

    result, count = c, 0
    for _ in range(fuzzy):  # bounded by the pre-counted occurrences
        nxt = _fuzzy_replace_first(result, o, nw)
        if nxt is None:
            break
        result, count = nxt, count + 1
    return _finish(bom, result, eol), "fuzzy", count


def apply_edits(content: str, edits: list[dict]) -> tuple[str, list[dict]]:
    """Apply a list of {old_string, new_string, replace_all?} edits sequentially
    to `content`. Each edit sees the result of the previous one (which naturally
    prevents overlap bugs). Returns (new_content, per-edit results)."""
    results = []
    current = content
    for i, e in enumerate(edits):
        current, strategy, count = find_and_replace(
            current, e["old_string"], e["new_string"], e.get("replace_all", False)
        )
        results.append({"index": i, "strategy": strategy, "replacements": count})
    return current, results
