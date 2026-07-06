"""Golden tests for notes-mcp. Run: uv run --extra test pytest -q"""
import asyncio
import os

import pytest

from notes_mcp import server


def _in_repo(tmp_path, monkeypatch, subdir=""):
    """Make tmp_path a git repo and point the workspace at it (or a subdir)."""
    (tmp_path / ".git").mkdir()
    ws = tmp_path / subdir if subdir else tmp_path
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCP_WORKSPACE", str(ws))
    return tmp_path


def _write(name, content, **kw):
    return asyncio.run(server.write(name, content, **kw))


def _read(name):
    return asyncio.run(server.read(name))


def _list():
    return asyncio.run(server.list())


def _delete(name):
    return asyncio.run(server.delete(name))


# --- storage location -------------------------------------------------------
def test_notes_live_at_repo_root(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch, subdir="src/deep")
    res = _write("first", "hello note\n")
    assert res["ok"] is True
    # written at the REPO ROOT's .continue-notes, not under src/deep
    assert res["path"] == str(tmp_path / ".continue-notes" / "first.md")
    assert (tmp_path / ".continue-notes" / "first.md").exists()


def test_no_repo_falls_back_to_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "loose"
    ws.mkdir()
    monkeypatch.setenv("MCP_WORKSPACE", str(ws))
    res = _write("n", "x\n")
    assert res["path"].startswith(str(ws))


def test_never_home_directory(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    res = _write("n", "x\n")
    assert not res["path"].startswith(os.path.expanduser("~") + os.sep) or \
        res["path"].startswith(str(tmp_path))


# --- write / read / list / delete -------------------------------------------
def test_round_trip(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("state", "Refactor half done\n\nRemaining: matcher.py\n")
    res = _read("state")
    assert res["ok"] is True
    assert "Remaining: matcher.py" in res["content"]


def test_list_index_shape(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("alpha", "# Alpha summary line\n\ndetails\n")
    _write("beta", "plain first line\nmore\n")
    res = _list()
    assert res["count"] == 2
    by_name = {n["name"]: n for n in res["notes"]}
    assert by_name["alpha"]["hook"] == "Alpha summary line"  # heading stripped
    assert by_name["beta"]["hook"] == "plain first line"
    assert all(n["age_days"] >= 0 for n in res["notes"])


def test_list_empty_when_no_dir(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    res = _list()
    assert res == {"notes": [], "count": 0, "dir": str(tmp_path / ".continue-notes")}


def test_append(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("log", "first entry")
    _write("log", "second entry", append=True)
    content = _read("log")["content"]
    assert content == "first entry\n\nsecond entry\n"


def test_overwrite_replaces(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("n", "old\n")
    _write("n", "new\n")
    assert _read("n")["content"] == "new\n"


def test_delete(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("gone", "x\n")
    assert _delete("gone")["ok"] is True
    assert _read("gone")["ok"] is False
    assert _delete("gone")["ok"] is False


def test_missing_note_read(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    res = _read("never-written")
    assert res["ok"] is False


# --- name safety --------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "../escape", "a/b", "a\\b", ".hidden", "", "with space", "..",
])
def test_bad_names_rejected(tmp_path, monkeypatch, bad):
    _in_repo(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(server.write(bad, "x"))


def test_hook_truncated():
    long = "y" * 300
    assert len(server.hook_line(long)) == server.MAX_HOOK
