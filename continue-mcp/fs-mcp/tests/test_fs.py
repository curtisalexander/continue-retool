"""Golden tests for fs-mcp. Run: uv run --extra test pytest -q"""
import asyncio
import os

import pytest

from fs_mcp import server


def _read(path, **kw):
    return asyncio.run(server.read(str(path), **kw)).structured_content


def _list(path, **kw):
    return asyncio.run(server.list(str(path), **kw)).structured_content


# --- read -------------------------------------------------------------------
def test_read_numbers_lines(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    res = _read(f)
    assert res["ok"] is True
    assert res["content"] == "1\talpha\n2\tbeta\n3\tgamma"
    assert res["total_lines"] == 3
    assert res["truncated"] is False


def test_read_line_range_pages(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 101)), encoding="utf-8")
    res = _read(f, start_line=41, limit=10)
    assert res["start_line"] == 41 and res["end_line"] == 50
    assert res["content"].startswith("41\tline41")
    assert res["content"].endswith("50\tline50")
    assert res["truncated"] is True
    assert res["total_lines"] == 100


def test_read_default_limit_caps(tmp_path):
    f = tmp_path / "huge.txt"
    f.write_text("x\n" * 5000, encoding="utf-8")
    res = _read(f)
    assert res["end_line"] == 2000
    assert res["truncated"] is True


def test_read_long_lines_are_clipped(tmp_path):
    f = tmp_path / "wide.txt"
    f.write_text("y" * 10_000, encoding="utf-8")
    res = _read(f)
    assert "…[+" in res["content"]
    assert len(res["content"]) < 3000


def test_read_bom_and_crlf(tmp_path):
    f = tmp_path / "win.txt"
    f.write_bytes(b"\xef\xbb\xbffirst\r\nsecond\r\n")
    res = _read(f)
    assert res["content"] == "1\tfirst\n2\tsecond"  # BOM stripped, CRLF handled


def test_read_missing_file(tmp_path):
    res = _read(tmp_path / "nope.txt")
    assert res["ok"] is False
    assert "not found" in res["error"]


def test_read_start_past_eof(tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("only\n", encoding="utf-8")
    res = _read(f, start_line=10)
    assert res["ok"] is True
    assert res["content"] == ""
    assert res["truncated"] is False


# --- list -------------------------------------------------------------------
def _tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "deep").mkdir()
    (tmp_path / "src" / "deep" / "leaf.txt").write_text("y", encoding="utf-8")
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (tmp_path / ".hidden").write_text("h", encoding="utf-8")


def test_list_depth_one_dirs_first(tmp_path):
    _tree(tmp_path)
    res = _list(tmp_path)
    paths = [e["path"] for e in res["entries"]]
    assert paths[0].startswith("src")           # dirs sort first
    assert "README.md" in paths
    assert not any(".git" in p for p in paths)  # .git always skipped
    assert not any(".hidden" in p for p in paths)
    assert not any("deep" in p for p in paths)  # depth=1 doesn't recurse


def test_list_recurses_to_depth(tmp_path):
    _tree(tmp_path)
    res = _list(tmp_path, depth=3)
    paths = [e["path"] for e in res["entries"]]
    assert any(p.endswith("leaf.txt") for p in paths)


def test_list_include_hidden(tmp_path):
    _tree(tmp_path)
    res = _list(tmp_path, include_hidden=True)
    paths = [e["path"] for e in res["entries"]]
    assert ".hidden" in paths
    assert not any(p.startswith(".git") for p in paths)  # still skipped


def test_list_caps_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_ENTRIES", 5)
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("", encoding="utf-8")
    res = _list(tmp_path)
    assert res["count"] == 5
    assert res["truncated"] is True


def test_list_sizes_reported(tmp_path):
    (tmp_path / "sized.bin").write_bytes(b"12345")
    res = _list(tmp_path)
    entry = next(e for e in res["entries"] if e["path"] == "sized.bin")
    assert entry["size"] == 5


def test_list_not_a_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("", encoding="utf-8")
    res = _list(f)
    assert res["ok"] is False


# --- workspace resolution -----------------------------------------------------
def test_relative_path_resolves_against_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    (tmp_path / "rel.txt").write_text("found\n", encoding="utf-8")
    res = _read("rel.txt")
    assert res["ok"] is True
    assert res["content"] == "1\tfound"
    res = _list(".")
    assert any(e["path"] == "rel.txt" for e in res["entries"])


# --- workspace jail (default ON; conftest pins MCP_WORKSPACE to tmp_path) ----
def test_jail_blocks_outside_read(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    secret = outside / "secret.txt"
    secret.write_text("s3cret\n", encoding="utf-8")
    res = _read(secret)
    assert res["ok"] is False
    assert "workspace jail" in res["error"]


def test_jail_blocks_outside_list(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside-list")
    res = _list(outside)
    assert res["ok"] is False
    assert "workspace jail" in res["error"]


def test_jail_blocks_relative_escape(tmp_path):
    res = _read("../escape.txt")
    assert res["ok"] is False
    assert "workspace jail" in res["error"]


def test_jail_blocks_symlink_escape(tmp_path, tmp_path_factory):
    """A symlink INSIDE the workspace must not read a target OUTSIDE it —
    paths are realpath'd before the containment check."""
    if os.name == "nt":
        pytest.skip("symlink creation needs privileges on Windows")
    outside = tmp_path_factory.mktemp("outside-link")
    target = outside / "target.txt"
    target.write_text("tunneled\n", encoding="utf-8")
    link = tmp_path / "innocent.txt"
    os.symlink(target, link)
    res = _read(link)
    assert res["ok"] is False
    assert "workspace jail" in res["error"]


def test_jail_opt_out(tmp_path, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("MCP_JAIL", "0")
    outside = tmp_path_factory.mktemp("outside-optout")
    f = outside / "ok.txt"
    f.write_text("fine\n", encoding="utf-8")
    res = _read(f)
    assert res["ok"] is True


def test_jail_extra_root_allows(tmp_path, tmp_path_factory, monkeypatch):
    outside = tmp_path_factory.mktemp("extra-root")
    monkeypatch.setenv("MCP_JAIL_EXTRA", str(outside))
    f = outside / "ok.txt"
    f.write_text("fine\n", encoding="utf-8")
    res = _read(f)
    assert res["ok"] is True
