"""
Tests for search-mcp. Run:  uv run pytest  (from search-mcp/)

Unit tests (arg builders) run anywhere. Integration tests actually invoke `rg`
and are skipped if ripgrep isn't installed (see README: a system rg, or
`uv tool install ripgrep-bin`).
"""
import asyncio
import shutil

import pytest

from search_mcp import server
from search_mcp.server import build_files_args, build_grep_args

HAVE_RG = shutil.which("rg") is not None
needs_rg = pytest.mark.skipif(not HAVE_RG, reason="ripgrep (rg) not installed")


# --- pure unit tests (no rg needed) ---------------------------------------
def test_grep_args_basic():
    args = build_grep_args("TODO", path="src")
    assert args[0] == "--json"
    assert args[-3:] == ["--", "TODO", "src"]


def test_grep_args_flags_and_globs():
    args = build_grep_args(
        "err", ignore_case=True, glob=["*.py", "!**/build/**"], hidden=True
    )
    assert "-i" in args
    assert "--hidden" in args
    # each glob is passed as its own -g <glob> pair
    gi = [i for i, a in enumerate(args) if a == "-g"]
    assert len(gi) == 2
    assert args[gi[0] + 1] == "*.py"


def test_grep_args_multiline_and_context():
    args = build_grep_args("a.*b", multiline=True, context=3)
    assert "--multiline" in args and "--multiline-dotall" in args
    assert args[args.index("-C") + 1] == "3"


def test_files_args_globs():
    args = build_files_args(glob=["*.ts"], path="app")
    assert args[0] == "--files"
    assert "-g" in args and "*.ts" in args
    assert args[-1] == "app"


# --- integration tests (need rg) ------------------------------------------
@needs_rg
def test_grep_finds_match(tmp_path):
    (tmp_path / "a.txt").write_text("hello\nNEEDLE here\nbye\n")
    (tmp_path / "b.txt").write_text("nothing\n")
    res = asyncio.run(server.grep("NEEDLE", path=str(tmp_path)))
    assert res.structured_content["count"] == 1
    hit = next(r for r in res.structured_content["matches"] if r["kind"] == "match")
    assert hit["file"].endswith("a.txt")
    assert hit["line"] == 2
    assert "NEEDLE" in hit["text"]
    assert res.structured_content["truncated"] is False


@needs_rg
def test_grep_respects_max_results(tmp_path):
    (tmp_path / "many.txt").write_text("x\n" * 50)
    res = asyncio.run(server.grep("x", path=str(tmp_path), max_results=5))
    assert res.structured_content["count"] == 5
    assert res.structured_content["truncated"] is True


@needs_rg
def test_grep_glob_filters_by_type(tmp_path):
    (tmp_path / "keep.py").write_text("target\n")
    (tmp_path / "skip.md").write_text("target\n")
    res = asyncio.run(server.grep("target", path=str(tmp_path), glob=["*.py"]))
    assert res.structured_content["count"] == 1
    assert res.structured_content["matches"][0]["file"].endswith("keep.py")


@needs_rg
def test_files_lists_by_glob(tmp_path):
    (tmp_path / "one.py").write_text("")
    (tmp_path / "two.py").write_text("")
    (tmp_path / "note.md").write_text("")
    res = asyncio.run(server.files(glob=["*.py"], path=str(tmp_path)))
    assert res.structured_content["count"] == 2
    assert all(p.endswith(".py") for p in res.structured_content["files"])


@needs_rg
def test_relative_path_resolves_against_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("WORKSPACE_NEEDLE\n")
    res = asyncio.run(server.grep("WORKSPACE_NEEDLE", path="sub"))
    assert res.structured_content["count"] == 1


# --- workspace jail (default ON; conftest pins MCP_WORKSPACE to tmp_path) ----
# The jail check runs before rg is even located, so these need no ripgrep.
def _grep(path):
    return asyncio.run(server.grep("x", path=str(path))).structured_content


def test_jail_blocks_outside_grep(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    res = _grep(outside)
    assert res["count"] == 0
    assert "workspace jail" in res["error"]


def test_jail_blocks_outside_files(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside-files")
    res = asyncio.run(server.files(path=str(outside))).structured_content
    assert res["count"] == 0
    assert "workspace jail" in res["error"]


def test_jail_opt_out(tmp_path, tmp_path_factory, monkeypatch):
    if not HAVE_RG:
        pytest.skip("ripgrep (rg) not installed")
    monkeypatch.setenv("MCP_JAIL", "0")
    outside = tmp_path_factory.mktemp("outside-optout")
    (outside / "a.txt").write_text("needle\n", encoding="utf-8")
    res = asyncio.run(server.grep("needle", path=str(outside))).structured_content
    assert res["count"] == 1


# --- long lines must not crash or flood (regression) ------------------------
@needs_rg
def test_grep_survives_a_giant_matching_line(tmp_path):
    """rg --json emits the WHOLE matching line, and asyncio's StreamReader used to
    raise ValueError past its 64KB buffer — so a match in minified JS / a one-line
    lockfile crashed grep outright. It must now return a clipped result instead."""
    (tmp_path / "app.min.js").write_text("var x=1;needle=" + "A" * 500_000 + ";\n")
    res = asyncio.run(server.grep("needle", path=str(tmp_path)))
    d = res.structured_content
    assert d["count"] == 1                 # no crash, no traceback
    assert d["error"] is None
    assert d["line_clipped"] is True
    assert len(d["matches"][0]["text"]) < 600  # clipped, not 500KB


@needs_rg
def test_grep_clips_only_long_lines_and_flags_it(tmp_path):
    # newline="" — write_text's default translates \n to \r\n on Windows,
    # which rg reports as part of the line text and breaks the exact match
    # below for a reason unrelated to what this test checks.
    (tmp_path / "mix.txt").write_text(
        "short match\n" + "match " + "B" * 2000 + "\n", newline=""
    )
    res = asyncio.run(server.grep("match", path=str(tmp_path)))
    d = res.structured_content
    assert d["count"] == 2
    assert d["line_clipped"] is True
    short = next(m for m in d["matches"] if m["line"] == 1)
    assert short["text"] == "short match"  # a short line is left exactly as-is


@needs_rg
def test_grep_degrades_when_a_record_exceeds_the_hard_ceiling(tmp_path, monkeypatch):
    """Past even the raised buffer, grep must report a partial result, not raise."""
    monkeypatch.setattr(server, "MAX_RECORD_BYTES", 128 * 1024)
    (tmp_path / "huge.txt").write_text("needle=" + "Z" * 1_000_000 + "\n")
    res = asyncio.run(server.grep("needle", path=str(tmp_path)))
    d = res.structured_content
    assert d["truncated"] is True
    assert d["error"] and "exceeded" in d["error"]
