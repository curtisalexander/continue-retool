"""Workspace resolution, Unicode path variants, and containment checks."""

from __future__ import annotations

import os
import re
import unicodedata

_NARROW_NBSP = " "
_AMPM = re.compile(r" (AM|PM)\.", re.IGNORECASE)
_DISABLED = {"0", "false", "off", "no"}


def resolve_path(path: str) -> str:
    """Resolve relative paths against MCP_WORKSPACE, falling back to cwd."""
    if os.path.isabs(path):
        return path
    base = os.path.abspath(os.environ.get("MCP_WORKSPACE") or os.getcwd())
    return os.path.join(base, path)


def path_variants(path: str) -> list[str]:
    """Return byte-different Unicode spellings to try after a literal miss."""
    quoted = [path, path.replace("'", "’")]
    spaced: list[str] = []
    for candidate in quoted:
        spaced.append(candidate)
        alternate = _AMPM.sub(_NARROW_NBSP + r"\1.", candidate)
        if alternate != candidate:
            spaced.append(alternate)
    seen = {path}
    variants: list[str] = []
    for candidate in spaced:
        for form in (
            candidate,
            unicodedata.normalize("NFD", candidate),
            unicodedata.normalize("NFC", candidate),
        ):
            if form not in seen:
                seen.add(form)
                variants.append(form)
    return variants


def resolve_existing(path: str) -> str:
    """Resolve a path and try Unicode-equivalent variants when it is missing."""
    resolved = resolve_path(path)
    if os.path.exists(resolved):
        return resolved
    for variant in path_variants(resolved):
        if os.path.exists(variant):
            return variant
    return resolved


def jail_roots() -> list[str]:
    """Return normalized allowed roots, or an empty list when jail is disabled."""
    if os.environ.get("MCP_JAIL", "1").strip().lower() in _DISABLED:
        return []
    roots = [os.path.abspath(os.environ.get("MCP_WORKSPACE") or os.getcwd())]
    for extra in (os.environ.get("MCP_JAIL_EXTRA") or "").split(os.pathsep):
        if extra.strip():
            roots.append(os.path.abspath(extra.strip()))
    return [os.path.normcase(os.path.realpath(root)) for root in roots]


def jail_error(path: str) -> str | None:
    """Return a model-facing refusal if path resolves outside the allowed roots."""
    roots = jail_roots()
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
