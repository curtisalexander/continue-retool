"""MCP-protocol tests: drive edit-mcp through fastmcp's Client, the same way an
MCP client (Continue) would. Deterministic: no LLM, no network, in-process."""
import asyncio

from fastmcp import Client

from edit_mcp.server import mcp


def _call(tool: str, args: dict):
    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool(tool, args)

    return asyncio.run(scenario())


def test_tools_advertised():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(scenario())
    assert {t.name for t in tools} == {"edit", "multi_edit", "create_file"}


def test_edit_exact_match(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    res = _call("edit", {
        "path": str(f), "old_string": "beta", "new_string": "BETA",
    })
    assert res.data["ok"] is True
    assert res.data["strategy"] == "exact"
    assert f.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_edit_fuzzy_smart_quotes(tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("say “hello” now\n", encoding="utf-8")  # curly quotes on disk
    res = _call("edit", {
        "path": str(f),
        "old_string": 'say "hello" now',   # model emits straight quotes
        "new_string": 'say "goodbye" now',
    })
    assert res.data["ok"] is True
    assert res.data["strategy"] == "fuzzy"
    assert "goodbye" in f.read_text(encoding="utf-8")


def test_edit_no_match_reports_error(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("nothing here\n", encoding="utf-8")
    res = _call("edit", {
        "path": str(f), "old_string": "absent", "new_string": "x",
    })
    assert res.data["ok"] is False
    assert "not found" in res.data["error"]


def test_create_file_and_overwrite_guard(tmp_path):
    f = tmp_path / "new" / "file.txt"
    res = _call("create_file", {"path": str(f), "content": "hi\n"})
    assert res.data["ok"] is True
    assert f.read_text(encoding="utf-8") == "hi\n"
    # second create without overwrite must refuse
    res2 = _call("create_file", {"path": str(f), "content": "clobber\n"})
    assert res2.data["ok"] is False
    assert f.read_text(encoding="utf-8") == "hi\n"


def test_relative_path_resolves_against_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    (tmp_path / "rel.txt").write_text("aaa\n", encoding="utf-8")
    res = _call("edit", {
        "path": "rel.txt", "old_string": "aaa", "new_string": "bbb",
    })
    assert res.data["ok"] is True
    assert (tmp_path / "rel.txt").read_text(encoding="utf-8") == "bbb\n"
