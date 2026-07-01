"""
Tests for the gateway's pure catalog/ranking core. Run: uv run pytest (from gateway-mcp/)

These cover the logic that decides *which* tool the model is shown — no MCP, no
downstream servers, no network required.
"""
import pytest

from gateway_mcp.registry import build_catalog, rank_tools, summarize

RAW = [
    {"server": "shell", "tool": "start",
     "description": "Start a shell command in the background. Returns a job_id; poll with output()/poll(), stop with kill().",
     "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
    {"server": "shell", "tool": "run",
     "description": "Convenience: start a command and wait up to timeout for it to finish.", "input_schema": {}},
    {"server": "shell", "tool": "kill",
     "description": "Kill a running job and its whole process tree.", "input_schema": {}},
    {"server": "search", "tool": "grep",
     "description": "Search file contents with ripgrep (regex, gitignore-aware).", "input_schema": {}},
    {"server": "search", "tool": "files",
     "description": "List files visible to ripgrep, optionally filtered by glob like '*.py'.", "input_schema": {}},
    {"server": "edit", "tool": "edit",
     "description": "Replace old_string with new_string in a file (exact then fuzzy).", "input_schema": {}},
]


@pytest.fixture
def catalog():
    return build_catalog(RAW)


def test_catalog_builds_dotted_names(catalog):
    assert catalog.get("shell.start") is not None
    assert catalog.get("shell.start").server == "shell"
    assert catalog.get("shell.start").tool == "start"
    assert len(catalog) == 6


def test_describe_payload_has_schema(catalog):
    e = catalog.get("shell.start")
    assert e.schema["properties"]["cmd"]["type"] == "string"


def test_search_result_is_summary_not_schema(catalog):
    # search() surfaces only summaries — the token-saving point
    e = catalog.get("shell.start")
    assert e.summary and len(e.summary) <= 141
    assert "job_id" in e.summary or "background" in e.summary


@pytest.mark.parametrize("query,expected_top", [
    ("run a command in the background", "shell."),
    ("search code for a regex", "search.grep"),
    ("find python files", "search.files"),
    ("kill process tree", "shell.kill"),
])
def test_ranking_surfaces_the_right_tool(catalog, query, expected_top):
    hits = rank_tools(catalog, query, limit=3)
    assert hits, f"no hits for {query!r}"
    assert hits[0].name.startswith(expected_top)


def test_empty_query_lists_all_sorted(catalog):
    hits = rank_tools(catalog, "", limit=100)
    assert len(hits) == 6
    assert [h.name for h in hits] == sorted(h.name for h in hits)


def test_limit_is_respected(catalog):
    assert len(rank_tools(catalog, "", limit=2)) == 2


def test_resolve_is_case_insensitive(catalog):
    assert catalog.resolve("SHELL.START").name == "shell.start"


def test_suggest_on_typo(catalog):
    assert "shell.start" in catalog.suggest("shell.strt")


def test_summarize_truncates_and_single_lines():
    long = "First sentence is here. " + "x " * 200
    s = summarize(long)
    assert s == "First sentence is here."
    assert "\n" not in summarize("a\nb\nc")
