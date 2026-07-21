"""Golden tests for notes-mcp. Run: uv run --extra test pytest -q"""
import asyncio
import os
import stat

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
    return asyncio.run(server.write(name, content, **kw)).structured_content


def _read(name):
    return asyncio.run(server.read(name)).structured_content


def _list():
    return asyncio.run(server.list()).structured_content


def _delete(name):
    return asyncio.run(server.delete(name)).structured_content


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


@pytest.mark.parametrize("configured", ["../outside", "/tmp/outside", r"C:\outside"])
def test_notes_dir_rejects_absolute_and_traversal_config(tmp_path, monkeypatch, configured):
    _in_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("NOTES_MCP_DIRNAME", configured)
    res = _write("n", "must stay contained")
    assert res["ok"] is False
    assert "NOTES_MCP_DIRNAME" in res["error"]


def test_notes_dir_rejects_escaping_symlink(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / ".continue-notes").symlink_to(outside, target_is_directory=True)
    res = _write("n", "must stay contained")
    assert res["ok"] is False
    assert "outside the repository" in res["error"]
    assert not (outside / "n.md").exists()


def test_note_symlink_is_rejected_for_all_mutations(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    notes = tmp_path / ".continue-notes"
    notes.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-note.md"
    outside.write_text("keep me\n")
    (notes / "linked.md").symlink_to(outside)
    assert _read("linked")["ok"] is False
    assert _write("linked", "replacement")["ok"] is False
    assert _delete("linked")["ok"] is False
    assert outside.read_text() == "keep me\n"


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


def test_list_index_is_bounded_and_reports_partial_result(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "MAX_INDEX_ENTRIES", 2)
    for name in ("a", "b", "c"):
        _write(name, f"hook for {name}")
    res = _list()
    assert res["count"] == 2
    assert res["truncated"] is True


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


def test_write_is_atomic_and_preserves_original_on_replace_failure(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("n", "original")
    path = tmp_path / ".continue-notes" / "n.md"

    def fail_replace(src, dst):
        raise OSError("injected replacement failure")

    monkeypatch.setattr(server.os, "replace", fail_replace)
    res = _write("n", "replacement")
    assert res["ok"] is False
    assert path.read_bytes() == b"original\n"
    assert sorted(p.name for p in path.parent.iterdir()) == ["n.md"]


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_atomic_write_preserves_permissions(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("n", "original")
    path = tmp_path / ".continue-notes" / "n.md"
    path.chmod(0o640)
    assert _write("n", "replacement")["ok"] is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_atomic_write_preserves_permissions_without_fchmod(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("n", "original")
    path = tmp_path / ".continue-notes" / "n.md"
    path.chmod(0o640)
    preserved_mode = stat.S_IMODE(path.stat().st_mode)
    chmod_calls = []
    real_chmod = os.chmod

    def record_chmod(target, mode):
        chmod_calls.append((target, mode))
        real_chmod(target, mode)

    monkeypatch.delattr(server.os, "fchmod", raising=False)
    monkeypatch.setattr(server.os, "chmod", record_chmod)

    assert _write("n", "replacement")["ok"] is True
    assert path.read_text(encoding="utf-8") == "replacement\n"
    assert len(chmod_calls) == 1
    assert chmod_calls[0][1] == preserved_mode


def test_encoding_failure_preserves_original(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("n", "original")
    path = tmp_path / ".continue-notes" / "n.md"
    res = _write("n", "bad surrogate: \ud800")
    assert res["ok"] is False
    assert path.read_bytes() == b"original\n"


def test_write_and_append_are_bounded(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "MAX_NOTE_BYTES", 16)
    assert _write("n", "small")["ok"] is True
    assert _write("n", "x" * 16)["ok"] is False
    assert _write("n", "y" * 12, append=True)["ok"] is False
    assert _read("n")["content"] == "small\n"


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
    """Invalid names come back as structured {ok: false} — the same failure
    shape as every other error, never a raised exception."""
    _in_repo(tmp_path, monkeypatch)
    res = asyncio.run(server.write(bad, "x")).structured_content
    assert res["ok"] is False
    assert "invalid note name" in res["error"]


def test_hook_truncated():
    long = "y" * 300
    assert len(server.hook_line(long)) == server.MAX_HOOK


def _search(query):
    return asyncio.run(server.search(query)).structured_content


# --- output caps: a runaway note or query must not flood context ------------
def test_read_caps_oversized_note_and_points_at_fs_read(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("big", "line of text\n" * 100_000)
    res = _read("big")
    assert res["ok"] is True
    assert res["truncated"] is True
    assert len(res["content"].encode("utf-8")) <= server.MAX_READ_BYTES
    assert res["content"].endswith("text")            # truncated on a line boundary
    md = asyncio.run(server.read("big")).content[0].text
    assert res["path"] in md and "fs.read" in md      # escape hatch named


def test_read_small_note_is_untouched(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("small", "just a little note\n")
    res = _read("small")
    assert res["truncated"] is False
    assert res["content"] == "just a little note\n"


def test_read_limit_counts_utf8_bytes_not_characters(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "MAX_READ_BYTES", 16)
    _write("unicode", "界界界\n界界界\n")
    res = _read("unicode")
    assert res["truncated"] is True
    assert len(res["content"].encode("utf-8")) <= 16


def test_search_caps_match_count(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("many", "needle\n" * 1000)
    res = _search("needle")
    assert res["count"] == server.MAX_MATCHES
    assert res["truncated"] is True


def test_search_clips_long_lines_and_flags_it(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("wide", "needle " + "Z" * 5000 + "\n")
    res = _search("needle")
    assert res["count"] == 1
    assert res["line_clipped"] is True
    assert len(res["matches"][0]["text"]) < 600


def test_search_clean_result_has_no_flags(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    _write("a", "find me here\n")
    res = _search("find me")
    assert res["count"] == 1
    assert res["truncated"] is False and res["line_clipped"] is False


def test_search_scan_work_is_byte_bounded(tmp_path, monkeypatch):
    _in_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "MAX_SEARCH_BYTES", 32)
    _write("many", "ordinary line\n" * 20 + "needle\n")
    res = _search("needle")
    assert res["count"] == 0
    assert res["truncated"] is True
    assert res["scanned_bytes"] <= 32
