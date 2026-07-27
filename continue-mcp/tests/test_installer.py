from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import pytest

SCRIPT = Path(__file__).parents[1] / "install-workspace.py"
SPEC = importlib.util.spec_from_file_location("install_workspace", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)

def test_defaults_and_sql_selection():
    assert installer._selection(None, False) == ["shell", "fs", "search", "edit"]
    assert installer._selection(None, True)[-1] == "sql"
    assert installer._selection("sql,fs", False) == ["sql", "fs"]
    assert installer._selection(" sql, fs ", False) == ["sql", "fs"]
    with pytest.raises(ValueError, match="empty"):
        installer._selection("fs, ", False)

def test_install_requires_existing_directory(tmp_path: Path):
    with pytest.raises(RuntimeError, match="does not exist or is not a directory"):
        installer.install(str(tmp_path / "missing"), ["fs"], "/tools/uv")

def test_install_rejects_escaping_continue_symlink(tmp_path: Path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".continue").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="escapes project"):
        installer.install(str(project), ["fs"], "/tools/uv")
    assert list(outside.iterdir()) == []

def test_check_uses_handshake_without_subprocess_dependency(tmp_path: Path, monkeypatch):
    installer.install(str(tmp_path), ["fs"], "/tools/uv")
    calls = []
    async def handshake(name, uv, workspace):
        calls.append((name, uv, workspace))
    monkeypatch.setattr(installer, "_handshake", handshake)
    installer.check(str(tmp_path), ["fs"], "/tools/uv")
    assert calls == [("fs", "/tools/uv", tmp_path.resolve())]

def test_install_stamps_absolute_paths_and_is_idempotent(tmp_path: Path):
    installer.install(str(tmp_path), ["shell", "fs"], "/tools/uv")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.yaml")}
    installer.install(str(tmp_path), ["shell", "fs"], "/tools/uv")
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.yaml")}
    text = (tmp_path / ".continue/mcpServers/shell.yaml").read_text()
    assert str(installer.KIT_DIR) in text and str(tmp_path.resolve()) in text
    assert '"/tools/uv"' in text and '"--no-sync"' in text

def test_refuses_differing_existing_file_before_any_write(tmp_path: Path):
    target = tmp_path / ".continue/mcpServers/fs.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("mine\n")
    with pytest.raises(RuntimeError, match="refusing"):
        installer.install(str(tmp_path), ["shell", "fs"], "/tools/uv")
    assert not (target.parent / "shell.yaml").exists()


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is unavailable on Windows")
def test_refuses_non_regular_output_without_reading_it(tmp_path: Path):
    target = tmp_path / ".continue/mcpServers/fs.yaml"
    target.parent.mkdir(parents=True)
    os.mkfifo(target)
    with pytest.raises(RuntimeError, match="non-regular"):
        installer.install(str(tmp_path), ["fs"], "/tools/uv")


def test_failed_multi_file_install_removes_files_created_by_that_run(tmp_path: Path, monkeypatch):
    original_open = Path.open

    def fail_second(path, *args, **kwargs):
        if path.name == "fs.yaml":
            raise OSError("simulated write failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_second)
    with pytest.raises(OSError, match="simulated"):
        installer.install(str(tmp_path), ["shell", "fs"], "/tools/uv")
    assert not (tmp_path / ".continue/mcpServers/shell.yaml").exists()


def test_target_created_after_preflight_is_not_overwritten(tmp_path: Path, monkeypatch):
    original_preflight = installer._preflight
    raced = tmp_path / ".continue/mcpServers/fs.yaml"

    def create_after_preflight(outputs, workspace):
        existing = original_preflight(outputs, workspace)
        raced.parent.mkdir(parents=True, exist_ok=True)
        raced.write_text("mine\n", encoding="utf-8")
        return existing

    monkeypatch.setattr(installer, "_preflight", create_after_preflight)
    with pytest.raises(FileExistsError):
        installer.install(str(tmp_path), ["fs"], "/tools/uv")
    assert raced.read_text(encoding="utf-8") == "mine\n"


def test_sync_is_one_locked_root_command(monkeypatch):
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    installer.sync_deps("/tools/uv")
    assert calls == [(["/tools/uv", "sync", "--locked", "--project", str(installer.KIT_DIR)], {"check": True})]
