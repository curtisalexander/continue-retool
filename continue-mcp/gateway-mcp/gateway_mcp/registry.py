"""
registry.py — the pure, dependency-free core of the gateway.

The gateway's job is progressive disclosure: instead of loading N full tool
schemas into the model's context up front (each ~550–1,400 tokens), it exposes
three meta-tools — search / describe / call — and discloses a real tool's schema
only when the model asks for it.

This module holds the parts that don't touch MCP or the network: building the
catalog of downstream tools, summarizing descriptions to one line, and ranking
tools against a query. Kept stdlib-only so it's trivially testable.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


@dataclass
class Tool:
    """One downstream tool, as the gateway knows it."""
    name: str            # display name shown to the model, e.g. "shell.start"
    server: str          # which downstream MCP server owns it, e.g. "shell"
    tool: str            # the raw tool name on that server, e.g. "start"
    description: str     # full description (returned by describe())
    schema: dict         # full JSON input schema (returned by describe())
    summary: str = ""    # one-line summary (returned by search())
    keywords: list = field(default_factory=list)


def summarize(description: str | None, limit: int = 140) -> str:
    """Collapse a description to a single short line for search() results."""
    if not description:
        return ""
    s = " ".join(description.split())
    for end in (". ", "! ", "? "):
        i = s.find(end)
        if 0 < i <= limit:
            return s[: i + 1]
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def build_catalog(raw_tools: list[dict]) -> "Catalog":
    """Build a Catalog from downstream tool listings.

    Each raw entry: {server, tool, description, input_schema}."""
    entries = []
    for r in raw_tools:
        server, tool = r["server"], r["tool"]
        entries.append(
            Tool(
                name=f"{server}.{tool}",
                server=server,
                tool=tool,
                description=r.get("description") or "",
                schema=r.get("input_schema") or {},
                summary=summarize(r.get("description")),
                keywords=[server, *re.split(r"[_\-.]", tool)],
            )
        )
    return Catalog(entries)


class Catalog:
    def __init__(self, entries: list[Tool]) -> None:
        self._by_name = {e.name: e for e in entries}

    def all(self) -> list[Tool]:
        return list(self._by_name.values())

    def get(self, name: str) -> Tool | None:
        return self._by_name.get(name)

    def resolve(self, name: str) -> Tool | None:
        """Exact, else case-insensitive match."""
        e = self._by_name.get(name)
        if e:
            return e
        low = name.lower()
        for k, v in self._by_name.items():
            if k.lower() == low:
                return v
        return None

    def suggest(self, name: str, n: int = 3) -> list[str]:
        return difflib.get_close_matches(name, list(self._by_name), n=n, cutoff=0.3)

    def __len__(self) -> int:
        return len(self._by_name)


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _score(entry: Tool, q_tokens: set[str], q_raw: str) -> int:
    name = entry.name.lower()
    score = 0
    if q_raw and q_raw in name:
        score += 10                      # strong: query is a substring of the name
    name_tokens = _tokens(entry.name + " " + " ".join(entry.keywords))
    all_tokens = name_tokens | _tokens(entry.summary)
    score += 3 * len(q_tokens & name_tokens)   # name/keyword hits weigh more
    score += 1 * len(q_tokens & all_tokens)     # summary hits weigh less
    return score


def rank_tools(catalog: Catalog, query: str, limit: int = 15) -> list[Tool]:
    """Return the best-matching tools for a query. Empty query -> all, name-sorted."""
    entries = catalog.all()
    if not query.strip():
        return sorted(entries, key=lambda e: e.name)[:limit]
    q_raw = query.lower().strip()
    q_tokens = _tokens(query)
    scored = [(s, e) for e in entries if (s := _score(e, q_tokens, q_raw)) > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return [e for _, e in scored[:limit]]
