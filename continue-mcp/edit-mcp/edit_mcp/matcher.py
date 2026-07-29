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


def _normalize_with_provenance(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize text and retain the source span for every output character.

    Characters that interact under NFKC are one source cluster. This includes
    combining sequences, Hangul Jamo, and compatibility forms such as voiced
    half-width Kana. Consequently partial matches within a normalization
    expansion always replace the complete original cluster.
    """
    output: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        end = index + 1
        if text[index] != "\n":
            while end < len(text) and text[end] != "\n":
                following = text[end]
                decomposition = unicodedata.normalize("NFKD", following)
                if unicodedata.combining(following) or (
                    decomposition and unicodedata.combining(decomposition[0])
                ):
                    end += 1
                    continue
                current = unicodedata.normalize("NFKC", text[index:end])
                interacts = unicodedata.normalize(
                    "NFKC", current + following
                ) != current + unicodedata.normalize("NFKC", following)
                if not interacts:
                    break
                end += 1
        normalized = unicodedata.normalize("NFKC", text[index:end])
        normalized = _SMART_SINGLE.sub("'", normalized)
        normalized = _SMART_DOUBLE.sub('"', normalized)
        normalized = _DASHES.sub("-", normalized)
        normalized = _SPACES.sub(" ", normalized)
        output.extend(normalized)
        spans.extend([(index, end)] * len(normalized))
        index = end

    # Match normalize_for_fuzzy's per-line rstrip while preserving provenance
    # for all retained characters (especially the newline itself).
    trimmed_output: list[str] = []
    trimmed_spans: list[tuple[int, int]] = []
    line_output: list[str] = []
    line_spans: list[tuple[int, int]] = []
    for char, span in zip(output, spans, strict=True):
        if char == "\n":
            while line_output and line_output[-1].isspace():
                line_output.pop()
                line_spans.pop()
            trimmed_output.extend(line_output)
            trimmed_spans.extend(line_spans)
            trimmed_output.append(char)
            trimmed_spans.append(span)
            line_output, line_spans = [], []
        else:
            line_output.append(char)
            line_spans.append(span)
    while line_output and line_output[-1].isspace():
        line_output.pop()
        line_spans.pop()
    trimmed_output.extend(line_output)
    trimmed_spans.extend(line_spans)
    return "".join(trimmed_output), trimmed_spans


def _match_spans(content: str, needle: str) -> list[tuple[int, int]]:
    """Non-overlapping [start, end) matches, all from one immutable snapshot."""
    if not needle:
        return []
    spans = []
    offset = 0
    while (start := content.find(needle, offset)) != -1:
        end = start + len(needle)
        spans.append((start, end))
        offset = end
    return spans


def _fuzzy_replace(
    content_lf: str, old_lf: str, new_lf: str, replace_all: bool
) -> tuple[str, int] | None:
    """Replace fuzzy matches found in the original normalized snapshot.

    Replacements are applied back-to-front within each touched line group, so
    inserted text is never searched again. Lines outside those groups are copied
    verbatim from the original content.
    """
    norm_content, provenance = _normalize_with_provenance(content_lf)
    norm_old = normalize_for_fuzzy(old_lf)
    spans = _match_spans(norm_content, norm_old)
    if not spans:
        return None
    if not replace_all:
        spans = spans[:1]

    original_spans: list[tuple[int, int]] = []
    for start, end in spans:
        source_span = (provenance[start][0], provenance[end - 1][1])
        # Distinct normalized matches can fall within one expansion. Applying
        # both would overlap in source space, so replace that source cluster once.
        if not original_spans or source_span[0] >= original_spans[-1][1]:
            original_spans.append(source_span)
    result = content_lf
    for start, end in reversed(original_spans):
        result = result[:start] + new_lf + result[end:]
    return result, len(original_spans)


def _closest_hint(content_lf: str, old_lf: str, max_lines: int = 5000) -> str | None:
    """Best-effort 'did you mean' diff for a no-match, à la difflib>0.9 tools."""
    content_lines = content_lf.split("\n")[:max_lines]
    old_lines = old_lf.split("\n")
    n = max(1, len(old_lines))
    # One matcher, seq2 fixed: set_seq2 caches its index, and the cheap
    # upper-bound ratios skip most windows before the O(len²) ratio() runs —
    # this loop fires on every failed match, when latency hurts most.
    sm = difflib.SequenceMatcher(None)
    sm.set_seq2(old_lf)
    best = (0.0, 0, "")
    for i in range(0, max(1, len(content_lines) - n + 1)):
        window = "\n".join(content_lines[i:i + n])
        sm.set_seq1(window)
        if sm.real_quick_ratio() <= best[0] or sm.quick_ratio() <= best[0]:
            continue
        ratio = sm.ratio()
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


def _lf_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize separators for matching while retaining raw source spans."""
    output: list[str] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        if text[i] == "\r":
            end = i + 2 if i + 1 < len(text) and text[i + 1] == "\n" else i + 1
            output.append("\n")
            spans.append((i, end))
            i = end
        else:
            output.append(text[i])
            spans.append((i, i + 1))
            i += 1
    return "".join(output), spans


def _replacement(text: str, consumed: str, body: str, start: int) -> str:
    """Give replacement breaks consumed separators in order, then a stable default."""
    separators = re.findall(r"\r\n|\r|\n", consumed)
    all_separators = list(re.finditer(r"\r\n|\r|\n", body))
    nearby = next((m.group() for m in reversed(all_separators) if m.start() < start), None)
    if nearby is None:
        nearby = next((m.group() for m in all_separators if m.start() >= start), "\n")
    pieces = re.split(r"\r\n|\r|\n", text)
    result = pieces[0]
    for index, piece in enumerate(pieces[1:]):
        result += (separators[index] if index < len(separators) else nearby) + piece
    return result


def _apply(body: str, spans: list[tuple[int, int]], new: str) -> str:
    result = body
    for start, end in reversed(spans):
        result = result[:start] + _replacement(new, body[start:end], body, start) + result[end:]
    return result


# --- the public entry point ------------------------------------------------
def find_and_replace(
    content: str, old: str, new: str, replace_all: bool = False
) -> tuple[str, str, int]:
    """Return (new_content, strategy, count) where strategy is 'exact' or 'fuzzy'.

    Raises EditError on empty old_string, no match, or a non-unique match when
    replace_all is False."""
    if old == "":
        raise EditError("old_string must not be empty.")
    if old == new:
        # Otherwise this reports "1 replacement, exact match" with an empty diff
        # and the model believes the edit landed. A no-op is always a mistake in
        # the caller's reasoning, so it has to fail loudly. (Pi raises the same.)
        raise EditError(
            "old_string and new_string are identical — this edit would change "
            "nothing. Check whether the change is already applied."
        )

    bom, body = strip_bom(content)
    c, raw_spans = _lf_with_spans(body)
    o = _to_lf(old)

    # 1) EXACT — preserves every byte, no normalization applied.
    exact = c.count(o)
    if exact > 0:
        if not replace_all and exact > 1:
            raise EditError(
                f"old_string is not unique: {exact} exact matches. Add surrounding "
                f"context to disambiguate, or pass replace_all=true."
            )
        matches = _match_spans(c, o)
        if not replace_all:
            matches = matches[:1]
        source = [(raw_spans[start][0], raw_spans[end - 1][1]) for start, end in matches]
        return bom + _apply(body, source, new), "exact", len(source)

    # 2) FUZZY — normalized match, unchanged lines preserved verbatim.
    norm_old = normalize_for_fuzzy(o)
    norm_content, provenance = _normalize_with_provenance(c)
    normalized_spans = _match_spans(norm_content, norm_old)
    source_spans: list[tuple[int, int]] = []
    for start, end in normalized_spans:
        span = (provenance[start][0], provenance[end - 1][1])
        if not source_spans or span[0] >= source_spans[-1][1]:
            source_spans.append(span)
    fuzzy = len(source_spans)
    if fuzzy == 0:
        hint = _closest_hint(c, o)
        raise EditError("old_string not found." + (f"\n{hint}" if hint else ""))
    if not replace_all and fuzzy > 1:
        raise EditError(
            f"old_string is not unique: {fuzzy} fuzzy matches. Add surrounding "
            f"context to disambiguate, or pass replace_all=true."
        )

    if not replace_all:
        source_spans = source_spans[:1]
    raw_source = [
        (raw_spans[start][0], raw_spans[end - 1][1]) for start, end in source_spans
    ]
    return bom + _apply(body, raw_source, new), "fuzzy", len(raw_source)
