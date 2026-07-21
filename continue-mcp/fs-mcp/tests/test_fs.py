"""Golden tests for fs-mcp. Run: uv run --extra test pytest -q"""
import asyncio
import os

import pytest

from fs_mcp import server


def _read(path, **kw):
    return asyncio.run(server.read(str(path), **kw)).structured_content


def _list(path, **kw):
    return asyncio.run(server.list(str(path), **kw)).structured_content


def test_numeric_environment_limits_are_defaulted_and_clamped(monkeypatch):
    monkeypatch.setenv("FS_TEST_LIMIT", "invalid")
    assert server._env_int("FS_TEST_LIMIT", 20, 1, 100) == 20
    monkeypatch.setenv("FS_TEST_LIMIT", "-5")
    assert server._env_int("FS_TEST_LIMIT", 20, 1, 100) == 1
    monkeypatch.setenv("FS_TEST_LIMIT", "999999")
    assert server._env_int("FS_TEST_LIMIT", 20, 1, 100) == 100


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
    assert res["total_lines"] is None
    assert res["total_lines_exact"] is False
    assert res["total_lines_at_least"] == 51
    assert res["lines_scanned"] == 51


def test_read_default_limit_caps(tmp_path):
    f = tmp_path / "huge.txt"
    f.write_text("x\n" * 5000, encoding="utf-8")
    res = _read(f)
    assert res["end_line"] == 2000
    assert res["truncated"] is True
    assert res["lines_scanned"] == 2001


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
    assert res["total_lines"] == 1
    assert res["total_lines_exact"] is True


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


def test_list_caps_internal_directory_scan_work(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_SCANNED_ENTRIES", 4)
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("", encoding="utf-8")
    res = _list(tmp_path)
    assert res["scanned"] == 4
    assert res["count"] == 4
    assert res["truncated"] is True


def test_list_caps_requested_recursion_depth(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_DEPTH", 2)
    (tmp_path / "one" / "two" / "three").mkdir(parents=True)
    res = _list(tmp_path, depth=10_000)
    paths = [e["path"] for e in res["entries"]]
    assert res["requested_depth"] == 10_000
    assert res["depth"] == 2 and res["depth_capped"] is True
    assert res["truncated"] is True
    assert any("two" in path for path in paths)
    assert not any("three" in path for path in paths)


def test_list_reports_inaccessible_subdirectory_as_partial(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (tmp_path / "visible.txt").write_text("ok", encoding="utf-8")
    real_scandir = server.os.scandir

    def guarded_scandir(path):
        if os.fspath(path) == os.fspath(blocked):
            raise PermissionError("access denied for test")
        return real_scandir(path)

    monkeypatch.setattr(server.os, "scandir", guarded_scandir)
    res = _list(tmp_path, depth=2)
    assert res["ok"] is True and res["partial"] is True
    assert res["skipped"] == 1
    assert res["errors"][0]["path"] == "blocked"
    assert "access denied for test" in res["errors"][0]["error"]
    assert any(entry["path"] == "visible.txt" for entry in res["entries"])


def test_list_bounds_inaccessible_entry_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_LIST_ERRORS", 2)
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    real_scandir = server.os.scandir

    def guarded_scandir(path):
        if os.fspath(path) != os.fspath(tmp_path):
            raise PermissionError(f"blocked {os.path.basename(path)}")
        return real_scandir(path)

    monkeypatch.setattr(server.os, "scandir", guarded_scandir)
    res = _list(tmp_path, depth=2)
    assert res["skipped"] == 3
    assert len(res["errors"]) == 2
    assert res["errors_truncated"] is True


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


# --- byte cap: the line and per-line caps multiply, so bytes must bind --------
def test_read_byte_cap_binds_on_wide_files(tmp_path):
    """2000 lines x 1500 chars is ~3MB and passes both the line and per-line cap.
    Only the total-byte cap keeps that out of the context window."""
    f = tmp_path / "wide.txt"
    f.write_text(("x" * 1500 + "\n") * 3000, encoding="utf-8")
    res = _read(f)
    assert len(res["content"].encode("utf-8")) <= server.MAX_BYTES + 2000
    assert res["truncated"] is True
    assert res["truncated_by"] == "bytes"
    assert res["total_lines"] is None
    assert res["total_lines_exact"] is False
    assert res["lines_scanned"] < 100


def test_read_small_page_does_not_count_rest_of_large_file(tmp_path):
    f = tmp_path / "million.txt"
    f.write_text("x\n" * 100_000, encoding="utf-8")
    res = _read(f, limit=10)
    assert res["content"].endswith("10\tx")
    assert res["lines_scanned"] == 11
    assert res["total_lines"] is None
    assert res["total_lines_at_least"] == 11


def test_read_reports_line_limit_when_lines_bind_first(tmp_path):
    f = tmp_path / "narrow.txt"
    f.write_text("x\n" * 5000, encoding="utf-8")
    res = _read(f)
    assert res["end_line"] == 2000
    assert res["truncated_by"] == "lines"


def test_read_next_start_line_pages_to_the_end(tmp_path):
    f = tmp_path / "wide.txt"
    f.write_text(("y" * 1000 + "\n") * 200, encoding="utf-8")
    seen, start, hops = 0, 1, 0
    while True:
        res = _read(f, start_line=start)
        seen += res["end_line"] - res["start_line"] + 1
        hops += 1
        if not res["truncated"]:
            break
        start = res["next_start_line"]
        assert hops < 20, "paging failed to terminate"
    assert seen == 200  # every line delivered exactly once, no gap or overlap


def test_read_emits_first_line_even_if_it_busts_the_budget(tmp_path, monkeypatch):
    """A read must always make progress; returning zero lines would wedge the
    caller re-requesting the same start_line forever. Only reachable with a
    small FS_MCP_MAX_BYTES, since MAX_LINE_CHARS clips any line long before
    it can exhaust the default 50KB on its own."""
    monkeypatch.setattr(server, "MAX_BYTES", 64)
    f = tmp_path / "lines.txt"
    f.write_text("z" * 400 + "\nsecond\n", encoding="utf-8")
    res = _read(f)
    assert res["start_line"] == 1 and res["end_line"] == 1
    assert res["truncated"] is True and res["next_start_line"] == 2


def test_read_truncation_hint_reaches_the_model(tmp_path):
    f = tmp_path / "wide.txt"
    f.write_text(("q" * 1200 + "\n") * 500, encoding="utf-8")
    md = asyncio.run(server.read(str(f))).content[0].text
    assert f"start_line={_read(f)['next_start_line']}" in md


# --- binary files ------------------------------------------------------------
def test_read_refuses_binary_instead_of_returning_mojibake(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 4)
    res = _read(f)
    assert res["ok"] is False and res["binary"] is True
    assert "�" not in res["error"]


def test_read_does_not_mistake_legacy_encoded_text_for_binary(tmp_path):
    """cp1252 prose is invalid UTF-8 but is emphatically text — the corporate
    files this kit exists to handle must not be refused as binary."""
    f = tmp_path / "cp.txt"
    f.write_bytes("Grüße, naïve café — résumé\n".encode("cp1252"))
    assert _read(f)["ok"] is True


def test_read_allows_empty_and_emoji_files(tmp_path):
    empty = tmp_path / "e.txt"
    empty.write_text("", encoding="utf-8")
    assert _read(empty)["ok"] is True
    emoji = tmp_path / "u.txt"
    emoji.write_text("héllo 🎉 世界\n", encoding="utf-8")
    assert _read(emoji)["ok"] is True


# --- Unicode-robust path resolution -----------------------------------------
def test_read_finds_nfd_file_from_nfc_request(tmp_path):
    """macOS stores filenames decomposed; models emit composed."""
    import unicodedata
    f = tmp_path / unicodedata.normalize("NFD", "café-résumé.txt")
    f.write_text("accented\n", encoding="utf-8")
    res = _read(tmp_path / unicodedata.normalize("NFC", "café-résumé.txt"))
    assert res["ok"] is True and res["content"] == "1\taccented"


def test_read_finds_macos_screenshot_needing_all_three_transforms(tmp_path):
    """Curly apostrophe AND narrow NBSP before PM AND NFD accents, at once —
    the combination Pi's fixed variant ladder can't reach."""
    import unicodedata
    real = unicodedata.normalize("NFD", "Capture d’écran à 3.42.11 PM.txt")
    (tmp_path / real).write_text("shot\n", encoding="utf-8")
    asked = unicodedata.normalize("NFC", "Capture d'écran à 3.42.11 PM.txt")
    assert _read(tmp_path / asked)["ok"] is True


def test_missing_file_still_reports_the_path_that_was_asked_for(tmp_path):
    res = _read(tmp_path / "nope.txt")
    assert res["ok"] is False and "nope.txt" in res["error"]
