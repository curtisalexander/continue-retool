"""MCP-protocol tests: drive edit-mcp through fastmcp's Client, the same way an
MCP client (Continue) would. Deterministic: no LLM, no network, in-process."""
import asyncio
import errno
import os

from fastmcp import Client

from edit_mcp import server
from edit_mcp.server import mcp


def _call(tool: str, args: dict):
    async def scenario():
        async with Client(mcp) as c:
            return await c.call_tool(tool, args)

    return asyncio.run(scenario())


def test_numeric_conflict_hash_limit_is_defaulted_and_clamped(monkeypatch):
    monkeypatch.setenv("EDIT_TEST_LIMIT", "bad")
    assert server._env_int("EDIT_TEST_LIMIT", 20, 0, 100) == 20
    monkeypatch.setenv("EDIT_TEST_LIMIT", "-1")
    assert server._env_int("EDIT_TEST_LIMIT", 20, 0, 100) == 0
    monkeypatch.setenv("EDIT_TEST_LIMIT", "999")
    assert server._env_int("EDIT_TEST_LIMIT", 20, 0, 100) == 100


def test_diff_result_uses_fence_longer_than_content_backticks():
    diff = " unchanged ``` here\n+added ````` value"
    rendered = server._result("edited", {"ok": True}, diff).content[0].text
    assert rendered == f"edited\n\n{'`' * 6}diff\n{diff}\n{'`' * 6}"


def test_tools_advertised():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(scenario())
    assert {t.name for t in tools} == {
        "edit", "multi_edit", "create_file", "delete_file", "move_file",
    }


# House-style conformance, enforced mechanically (see rules/rule-rule.md).
DESCRIPTION_BUDGET_CHARS = 1000  # ~250 tokens; catches runaway growth


def test_descriptions_present_and_within_budget():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    for t in asyncio.run(scenario()):
        assert t.description, f"{t.name} has no description"
        assert len(t.description) <= DESCRIPTION_BUDGET_CHARS, (
            f"{t.name} description is {len(t.description)} chars "
            f"(budget {DESCRIPTION_BUDGET_CHARS})"
        )


def test_every_tool_advertises_expected_authority():
    async def scenario():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = {t.name: t for t in asyncio.run(scenario())}
    expected = {
        "edit": {"destructiveHint": True},
        "multi_edit": {"destructiveHint": True},
        "create_file": {"destructiveHint": True},
        "delete_file": {"destructiveHint": True, "idempotentHint": True},
        "move_file": {"destructiveHint": True},
    }
    assert set(tools) == set(expected)
    for name, hints in expected.items():
        ann = tools[name].annotations
        assert ann, f"{name} should advertise authority annotations"
        for hint, value in hints.items():
            assert getattr(ann, hint) is value, f"{name} should set {hint}={value}"


def test_edit_missing_file_is_structured_error(tmp_path):
    """File-not-found comes back as {ok: false}, the same failure shape as a
    match error — never a raised protocol-level exception."""
    res = _call("edit", {
        "path": str(tmp_path / "nope.txt"), "old_string": "a", "new_string": "b",
    })
    assert res.data["ok"] is False
    assert "not found" in res.data["error"]


def test_edit_cp1252_file_round_trips(tmp_path):
    """A non-UTF-8 (cp1252) file must be editable — and written back in its own
    encoding, not transcoded or crashed on."""
    f = tmp_path / "legacy.txt"
    f.write_bytes("café — “legacy”\nplain line\n".encode("cp1252"))
    res = _call("edit", {
        "path": str(f), "old_string": "plain line", "new_string": "edited line",
    })
    assert res.data["ok"] is True
    assert res.data["encoding"] == "cp1252"
    raw = f.read_bytes()
    assert "edited line" in raw.decode("cp1252")
    assert raw.decode("cp1252").startswith("café — “legacy”")


def test_unencodable_legacy_edit_preserves_original_bytes(tmp_path):
    f = tmp_path / "legacy.txt"
    original = "café — legacy\n".encode("cp1252")
    f.write_bytes(original)
    res = _call("edit", {
        "path": str(f), "old_string": "legacy", "new_string": "emoji 😀",
    })
    assert res.data["ok"] is False
    assert "could not safely write" in res.data["error"]
    assert f.read_bytes() == original


def test_atomic_replace_failure_preserves_original(tmp_path, monkeypatch):
    from edit_mcp import server

    f = tmp_path / "important.txt"
    f.write_text("before\n", encoding="utf-8")

    def fail_replace(_src, _dest):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(server.os, "replace", fail_replace)
    res = _call("edit", {
        "path": str(f), "old_string": "before", "new_string": "after",
    })
    assert res.data["ok"] is False
    assert f.read_text(encoding="utf-8") == "before\n"
    assert not list(tmp_path.glob(".important.txt.*.tmp"))


def test_atomic_edit_preserves_permission_bits(tmp_path):
    if os.name == "nt":
        return
    f = tmp_path / "script.sh"
    f.write_text("echo before\n", encoding="utf-8")
    f.chmod(0o751)
    res = _call("edit", {
        "path": str(f), "old_string": "before", "new_string": "after",
    })
    assert res.data["ok"] is True
    assert f.stat().st_mode & 0o777 == 0o751


def test_atomic_edit_follows_safe_symlink_without_replacing_it(tmp_path):
    if os.name == "nt":
        return
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    res = _call("edit", {
        "path": str(link), "old_string": "before", "new_string": "after",
    })
    assert res.data["ok"] is True
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "after\n"


def test_concurrent_content_change_aborts_without_overwriting(tmp_path, monkeypatch):
    from edit_mcp import server

    f = tmp_path / "shared.txt"
    f.write_text("before\n", encoding="utf-8")
    original_match = server.find_and_replace

    def concurrent_change(*args, **kwargs):
        result = original_match(*args, **kwargs)
        f.write_text("external edit\n", encoding="utf-8")
        return result

    monkeypatch.setattr(server, "find_and_replace", concurrent_change)
    res = _call("edit", {
        "path": str(f), "old_string": "before", "new_string": "after",
    })
    assert res.data["ok"] is False
    assert "changed after it was read" in res.data["error"]
    assert f.read_text(encoding="utf-8") == "external edit\n"


def test_metadata_only_change_with_same_content_is_allowed(tmp_path, monkeypatch):
    from edit_mcp import server

    f = tmp_path / "shared.txt"
    f.write_text("before\n", encoding="utf-8")
    original_match = server.find_and_replace

    def metadata_change(*args, **kwargs):
        result = original_match(*args, **kwargs)
        f.write_text("before\n", encoding="utf-8")
        return result

    monkeypatch.setattr(server, "find_and_replace", metadata_change)
    res = _call("edit", {
        "path": str(f), "old_string": "before", "new_string": "after",
    })
    assert res.data["ok"] is True
    assert f.read_text(encoding="utf-8") == "after\n"


def test_edit_dry_run_leaves_file_untouched(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("keep me\n", encoding="utf-8")
    res = _call("edit", {
        "path": str(f), "old_string": "keep", "new_string": "change",
        "dry_run": True,
    })
    assert res.data["ok"] is True and res.data["dry_run"] is True
    assert "change" in res.data["diff"]
    assert f.read_text(encoding="utf-8") == "keep me\n"


def test_delete_file(tmp_path):
    f = tmp_path / "gone.txt"
    f.write_text("x\n", encoding="utf-8")
    res = _call("delete_file", {"path": str(f)})
    assert res.data["ok"] is True and not f.exists()
    res2 = _call("delete_file", {"path": str(f)})
    assert res2.data["ok"] is False


def test_move_file_and_overwrite_guard(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("payload\n", encoding="utf-8")
    dest = tmp_path / "sub" / "dest.txt"
    res = _call("move_file", {"path": str(src), "new_path": str(dest)})
    assert res.data["ok"] is True
    assert not src.exists() and dest.read_text(encoding="utf-8") == "payload\n"
    # refuse to clobber without overwrite
    src2 = tmp_path / "src2.txt"
    src2.write_text("other\n", encoding="utf-8")
    res2 = _call("move_file", {"path": str(src2), "new_path": str(dest)})
    assert res2.data["ok"] is False
    assert dest.read_text(encoding="utf-8") == "payload\n"


def test_move_file_eacces_preserves_existing_destination(tmp_path, monkeypatch):
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("source\n", encoding="utf-8")
    dest.write_text("keep\n", encoding="utf-8")

    def denied(_src, _dest):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(server.os, "replace", denied)
    res = _call("move_file", {
        "path": str(src), "new_path": str(dest), "overwrite": True,
    })
    assert res.data["ok"] is False
    assert "permission denied" in res.data["error"]
    assert src.read_text(encoding="utf-8") == "source\n"
    assert dest.read_text(encoding="utf-8") == "keep\n"


def test_move_file_exdev_uses_cross_device_fallback(tmp_path, monkeypatch):
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("payload\n", encoding="utf-8")
    real_move = server.shutil.move
    fallback_calls = []

    def cross_device(_src, _dest):
        raise OSError(errno.EXDEV, "cross-device link")

    def tracked_move(move_src, move_dest):
        fallback_calls.append((move_src, move_dest))
        return real_move(move_src, move_dest)

    monkeypatch.setattr(server.os, "replace", cross_device)
    monkeypatch.setattr(server.shutil, "move", tracked_move)
    res = _call("move_file", {
        "path": str(src), "new_path": str(dest), "overwrite": True,
    })
    assert res.data["ok"] is True
    assert fallback_calls == [(str(src), str(dest))]
    assert not src.exists()
    assert dest.read_text(encoding="utf-8") == "payload\n"


def test_move_file_concurrent_destination_creation_does_not_clobber(
    tmp_path, monkeypatch
):
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("source\n", encoding="utf-8")
    real_link = server.os.link

    def concurrent_create(link_src, link_dest):
        dest.write_text("concurrent\n", encoding="utf-8")
        return real_link(link_src, link_dest)

    monkeypatch.setattr(server.os, "link", concurrent_create)
    res = _call("move_file", {"path": str(src), "new_path": str(dest)})
    assert res.data["ok"] is False
    assert src.read_text(encoding="utf-8") == "source\n"
    assert dest.read_text(encoding="utf-8") == "concurrent\n"


def test_move_file_no_replace_cross_device_commits_without_clobber(
    tmp_path, monkeypatch
):
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("payload\n", encoding="utf-8")
    src.chmod(0o640)
    real_link = server.os.link

    def cross_device_source(link_src, link_dest):
        if os.fspath(link_src) == os.fspath(src):
            raise OSError(errno.EXDEV, "cross-device link")
        return real_link(link_src, link_dest)

    monkeypatch.setattr(server.os, "link", cross_device_source)
    res = _call("move_file", {"path": str(src), "new_path": str(dest)})
    assert res.data["ok"] is True
    assert not src.exists()
    assert dest.read_text(encoding="utf-8") == "payload\n"
    if os.name != "nt":
        assert dest.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".dest.txt.*.tmp"))


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


def test_create_file_concurrent_destination_creation_does_not_clobber(
    tmp_path, monkeypatch
):
    f = tmp_path / "race.txt"
    real_link = server.os.link

    def concurrent_create(temp_path, target):
        f.write_text("concurrent\n", encoding="utf-8")
        return real_link(temp_path, target)

    monkeypatch.setattr(server.os, "link", concurrent_create)
    res = _call("create_file", {"path": str(f), "content": "requested\n"})
    assert res.data["ok"] is False
    assert f.read_text(encoding="utf-8") == "concurrent\n"
    assert not list(tmp_path.glob(".race.txt.*.tmp"))


def test_relative_path_resolves_against_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    (tmp_path / "rel.txt").write_text("aaa\n", encoding="utf-8")
    res = _call("edit", {
        "path": "rel.txt", "old_string": "aaa", "new_string": "bbb",
    })
    assert res.data["ok"] is True
    assert (tmp_path / "rel.txt").read_text(encoding="utf-8") == "bbb\n"


# --- workspace jail (default ON; conftest pins MCP_WORKSPACE to tmp_path) ----
def test_jail_blocks_outside_edit_and_create(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    victim = outside / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    res = _call("edit", {"path": str(victim), "old_string": "keep", "new_string": "x"})
    assert res.data["ok"] is False and "workspace jail" in res.data["error"]
    assert victim.read_text(encoding="utf-8") == "keep\n"
    res2 = _call("create_file", {"path": str(outside / "new.txt"), "content": "x"})
    assert res2.data["ok"] is False and "workspace jail" in res2.data["error"]


def test_jail_blocks_move_dest_outside(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside-move")
    src = tmp_path / "inside.txt"
    src.write_text("payload\n", encoding="utf-8")
    res = _call("move_file", {"path": str(src), "new_path": str(outside / "out.txt")})
    assert res.data["ok"] is False and "workspace jail" in res.data["error"]
    assert src.exists()


def test_jail_blocks_delete_outside(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside-del")
    f = outside / "keep.txt"
    f.write_text("x\n", encoding="utf-8")
    res = _call("delete_file", {"path": str(f)})
    assert res.data["ok"] is False and "workspace jail" in res.data["error"]
    assert f.exists()


def test_jail_opt_out_allows_outside(tmp_path, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("MCP_JAIL", "0")
    outside = tmp_path_factory.mktemp("outside-optout")
    f = outside / "e.txt"
    f.write_text("aaa\n", encoding="utf-8")
    res = _call("edit", {"path": str(f), "old_string": "aaa", "new_string": "bbb"})
    assert res.data["ok"] is True


# --- no-op edits must fail loudly -------------------------------------------
def test_identical_old_and_new_is_rejected(tmp_path):
    """Otherwise this reports '1 replacement, exact match' with an empty diff and
    the model believes a change landed that never happened."""
    f = tmp_path / "a.txt"
    f.write_text("hello world\n", encoding="utf-8")
    res = _call("edit", {"path": str(f), "old_string": "hello", "new_string": "hello"})
    assert res.data["ok"] is False
    assert "identical" in res.data["error"]
    assert f.read_text(encoding="utf-8") == "hello world\n"


# --- models that double-encode the edits array ------------------------------
def test_multi_edit_accepts_edits_as_a_json_string(tmp_path):
    """Several models emit `edits` as a JSON string rather than an array; without
    coercion the call dies in schema validation before the tool ever runs."""
    f = tmp_path / "a.txt"
    f.write_text("alpha beta\n", encoding="utf-8")
    res = _call("multi_edit", {
        "path": str(f),
        "edits": '[{"old_string": "alpha", "new_string": "ALPHA"},'
                 ' {"old_string": "beta", "new_string": "BETA"}]',
    })
    assert res.data["ok"] is True
    assert f.read_text(encoding="utf-8") == "ALPHA BETA\n"


def test_multi_edit_rejects_unparseable_edits_string_with_a_useful_message(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\n", encoding="utf-8")
    res = _call("multi_edit", {"path": str(f), "edits": "not json at all"})
    assert res.data["ok"] is False
    assert "valid JSON" in res.data["error"]


def test_multi_edit_still_accepts_a_real_list(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\n", encoding="utf-8")
    res = _call("multi_edit", {
        "path": str(f), "edits": [{"old_string": "alpha", "new_string": "OMEGA"}],
    })
    assert res.data["ok"] is True
    assert f.read_text(encoding="utf-8") == "OMEGA\n"


# --- Unicode-robust path resolution -----------------------------------------
def test_edit_finds_nfd_file_from_nfc_request(tmp_path):
    """The same byte-identical-but-different problem matcher.py solves for
    content, applied to the filename."""
    import unicodedata
    f = tmp_path / unicodedata.normalize("NFD", "café.txt")
    f.write_text("old\n", encoding="utf-8")
    res = _call("edit", {
        "path": str(tmp_path / unicodedata.normalize("NFC", "café.txt")),
        "old_string": "old", "new_string": "new",
    })
    assert res.data["ok"] is True
    assert f.read_text(encoding="utf-8") == "new\n"


def test_edit_finds_macos_screenshot_with_narrow_nbsp(tmp_path):
    import unicodedata

    real = unicodedata.normalize("NFD", "Capture d’écran à 3.42.11 PM.txt")
    f = tmp_path / real
    f.write_text("old\n", encoding="utf-8")
    asked = unicodedata.normalize("NFC", "Capture d'écran à 3.42.11 PM.txt")
    res = _call("edit", {
        "path": str(tmp_path / asked), "old_string": "old", "new_string": "new",
    })
    assert res.data["ok"] is True
    assert f.read_text(encoding="utf-8") == "new\n"


def test_create_file_uses_the_literal_path_not_a_unicode_variant(tmp_path):
    """Variant resolution is for paths that must already exist. A file being
    created must land at exactly the name asked for."""
    import unicodedata
    (tmp_path / unicodedata.normalize("NFD", "café.txt")).write_text("x\n", encoding="utf-8")
    asked = tmp_path / unicodedata.normalize("NFC", "café-new.txt")
    res = _call("create_file", {"path": str(asked), "content": "fresh\n"})
    assert res.data["ok"] is True
    assert unicodedata.normalize("NFC", res.data["path"]) == unicodedata.normalize("NFC", str(asked))
