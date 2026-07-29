from __future__ import annotations

import asyncio
from pathlib import Path

from edit_mcp import server as edit_server
from fs_mcp import server as fs_server
from search_mcp import server as search_server


def test_cp1252_text_agrees_across_read_search_and_edit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    path = tmp_path / "legacy.txt"
    path.write_bytes("prefix “café”—résumé suffix\r\n".encode("cp1252"))

    read = asyncio.run(fs_server.read(str(path))).structured_content
    searched = asyncio.run(
        search_server.grep("café", path=str(path), encoding="windows-1252")
    ).structured_content
    edited = asyncio.run(
        edit_server.edit(str(path), "“café”—résumé", "“café”—updated")
    ).structured_content

    assert read["encoding"] == "cp1252"
    assert "“café”—résumé" in read["content"]
    assert searched["count"] == 1
    assert searched["matches"][0]["text"] == "prefix “café”—résumé suffix"
    assert searched["matches"][0]["source_encoding"] == "windows-1252"
    assert edited["ok"] is True and edited["encoding"] == "cp1252"
    assert path.read_bytes().decode("cp1252") == "prefix “café”—updated suffix\r\n"
