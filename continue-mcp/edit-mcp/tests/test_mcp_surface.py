"""MCP-surface contract for the intentionally narrow edit server."""
import asyncio
import pytest
from fastmcp import Client
from edit_mcp.server import edit, mcp

def test_exact_tool_surface_and_authority():
    async def scenario():
        async with Client(mcp) as client:
            return await client.list_tools()
    tools = {tool.name: tool for tool in asyncio.run(scenario())}
    assert set(tools) == {"edit", "create_file"}
    assert all(tool.description and len(tool.description) <= 1000 for tool in tools.values())
    assert all(tool.annotations.destructiveHint is True for tool in tools.values())

def test_edit_and_create_over_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    async def scenario():
        async with Client(mcp) as client:
            created = await client.call_tool("create_file", {"path": "a.txt", "content": "hello — world   "})
            edited = await client.call_tool("edit", {"path": "a.txt", "old_string": "hello - world", "new_string": "done"})
            return created.data, edited.data
    created, edited = asyncio.run(scenario())
    assert created["ok"] and edited["ok"]
    assert (tmp_path / "a.txt").read_text() == "done   "


def test_cp1252_edit_and_unrepresentable_failure_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    path = tmp_path / "legacy.txt"
    path.write_bytes("cost €\n".encode("cp1252"))
    original = path.read_bytes()
    successful = asyncio.run(
        edit(str(path), "cost €", "price €")
    ).structured_content
    assert successful["encoding"] == "cp1252"
    assert path.read_bytes().decode("cp1252") == "price €\n"

    before_failure = path.read_bytes()
    failed = asyncio.run(
        edit(str(path), "price €", "snowman ☃")
    ).structured_content
    assert failed["ok"] is False
    assert path.read_bytes() == before_failure
    assert original != before_failure


@pytest.mark.parametrize(
    ("bom", "codec"), [(b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")]
)
def test_utf16_bom_edit_round_trip(tmp_path, monkeypatch, bom, codec):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    path = tmp_path / "wide.txt"
    path.write_bytes(bom + "alpha\r\nbeta\n".encode(codec))
    result = asyncio.run(
        edit(str(path), "alpha\nbeta", "ALPHA\nBETA")
    ).structured_content
    assert result["ok"] is True and result["encoding"] == codec
    raw = path.read_bytes()
    assert raw.startswith(bom)
    assert raw[len(bom):].decode(codec) == "ALPHA\r\nBETA\n"


def test_malformed_bom_edit_fails_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    path = tmp_path / "broken.txt"
    original = b"\xff\xfea"
    path.write_bytes(original)
    result = asyncio.run(edit(str(path), "a", "b")).structured_content
    assert result["ok"] is False
    assert path.read_bytes() == original
