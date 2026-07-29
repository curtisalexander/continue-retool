from __future__ import annotations
import importlib.util
import os
from pathlib import Path
from typing import Any, cast
import pytest

SCRIPT = Path(__file__).parents[1] / "install-workspace.py"
SPEC = importlib.util.spec_from_file_location("install_workspace", SCRIPT)
assert SPEC and SPEC.loader
installer = cast(Any, importlib.util.module_from_spec(SPEC))
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
    async def handshake(name, uv, workspace, env):
        calls.append((name, uv, workspace, env))
    monkeypatch.setattr(installer, "_handshake", handshake)
    installer.check(str(tmp_path), ["fs"], "/tools/uv")
    assert calls == [("fs", os.path.abspath("/tools/uv"), tmp_path.resolve(), {"MCP_WORKSPACE": str(tmp_path.resolve())})]

def test_install_stamps_absolute_paths_and_is_idempotent(tmp_path: Path):
    detected = installer.detect_shell_env()
    installer.install(str(tmp_path), ["shell", "fs"], "/tools/uv")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.yaml")}
    installer.install(str(tmp_path), ["shell", "fs"], "/tools/uv")
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.yaml")}
    text = (tmp_path / ".continue/mcpServers/shell.yaml").read_text()
    assert installer._quote(str(installer.KIT_DIR)) in text
    assert installer._quote(str(tmp_path.resolve())) in text
    assert installer._quote(os.path.abspath("/tools/uv")) in text
    assert '"--no-sync"' in text
    for name, value in detected.items():
        assert f"{name}: {installer._quote(value)}" in text

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
        if path.name.startswith(".fs.yaml.tmp-"):
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


def test_shell_detection_override_path_fallback_and_default(tmp_path: Path, monkeypatch):
    bash = tmp_path / "odd bash"
    pwsh = tmp_path / "pwsh"
    bash.write_text("")
    pwsh.write_text("")
    monkeypatch.setattr(installer, "_is_windows", lambda: False)
    monkeypatch.setenv("SHELL_MCP_BASH", str(tmp_path / "stale"))
    monkeypatch.setattr(installer.shutil, "which", lambda command: str(bash) if command == "bash" else (str(pwsh) if command == "pwsh" else None))
    detected = installer.detect_shell_env()
    assert detected["SHELL_MCP_BASH"] == str(bash.resolve())
    assert detected["SHELL_MCP_PWSH"] == str(pwsh.resolve())
    assert detected["SHELL_MCP_DEFAULT_SHELL"] == "bash"


def test_windows_default_order(monkeypatch):
    monkeypatch.setattr(installer, "_is_windows", lambda: True)
    available = {"SHELL_MCP_POWERSHELL": "/powershell", "SHELL_MCP_CMD": "/cmd"}
    monkeypatch.setattr(installer, "_find_interpreter", lambda name, commands, known: available.get(name))
    assert installer.detect_shell_env()["SHELL_MCP_DEFAULT_SHELL"] == "powershell"


def test_shell_detection_fails_without_platform_default(monkeypatch):
    monkeypatch.setattr(installer, "_is_windows", lambda: False)
    monkeypatch.setattr(installer, "_find_interpreter", lambda *args: None)
    with pytest.raises(RuntimeError, match="no usable default"):
        installer.detect_shell_env()


def test_owned_generated_file_is_upgraded_but_user_file_is_refused(tmp_path: Path):
    target = tmp_path / ".continue/mcpServers/fs.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(f"{installer.OWNERSHIP_MARKER}1\nold: true\n")
    installer.install(str(tmp_path), ["fs"], "/tools/uv")
    assert target.read_text().startswith(f"{installer.OWNERSHIP_MARKER}{installer.OWNERSHIP_VERSION}\n")
    target.write_text("# user configuration\n")
    with pytest.raises(RuntimeError, match="refusing"):
        installer.install(str(tmp_path), ["fs"], "/tools/uv")


def test_unmarked_legacy_generated_file_is_safely_upgraded(tmp_path: Path):
    target = tmp_path / ".continue/mcpServers/fs.yaml"
    target.parent.mkdir(parents=True)
    legacy = installer.render_config("fs", "/tools/uv", tmp_path)
    legacy = "\n".join(legacy.splitlines()[1:]) + "\n"
    target.write_text(legacy, encoding="utf-8")
    installer.install(str(tmp_path), ["fs"], "/tools/uv")
    assert target.read_text(encoding="utf-8").startswith(installer.OWNERSHIP_MARKER)


def test_minimal_doctor_environment_does_not_copy_secret(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PATH", "/safe-path")
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-copy")
    config_env = {"MCP_WORKSPACE": str(tmp_path), "SHELL_MCP_BASH": "/bin/bash"}
    doctor_env = {**installer._minimal_base_env(), **config_env}
    assert doctor_env["PATH"] == "/safe-path"
    assert doctor_env["MCP_WORKSPACE"] == str(tmp_path)
    assert "UNRELATED_SECRET" not in doctor_env


def test_backup_cleanup_failure_never_removes_installed_configs(tmp_path: Path, monkeypatch):
    installer.install(str(tmp_path), ["shell", "fs"], "/tools/uv")
    for path in tmp_path.rglob("*.yaml"):
        path.write_text(f"{installer.OWNERSHIP_MARKER}1\nold: true\n")
    original_unlink = Path.unlink

    def fail_backup_cleanup(path, *args, **kwargs):
        if ".bak-" in path.name:
            raise PermissionError("simulated sharing violation")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_cleanup)
    installer.install(str(tmp_path), ["shell", "fs"], "/tools/uv")
    for path in tmp_path.rglob("*.yaml"):
        assert path.read_text(encoding="utf-8").startswith(
            f"{installer.OWNERSHIP_MARKER}{installer.OWNERSHIP_VERSION}\n"
        )


def test_new_target_is_rolled_back_if_temporary_cleanup_fails(tmp_path: Path, monkeypatch):
    original_unlink = Path.unlink

    def fail_temporary_unlink(path, *args, **kwargs):
        if ".tmp-" in path.name:
            raise PermissionError("simulated temporary sharing violation")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)
    with pytest.raises(PermissionError, match="temporary sharing violation"):
        installer.install(str(tmp_path), ["fs"], "/tools/uv")
    assert not (tmp_path / ".continue/mcpServers/fs.yaml").exists()


def test_failed_rollback_retains_backup_for_recovery(tmp_path: Path, monkeypatch):
    installer.install(str(tmp_path), ["shell", "fs"], "/tools/uv")
    directory = tmp_path / ".continue/mcpServers"
    for path in directory.glob("*.yaml"):
        path.write_text(f"{installer.OWNERSHIP_MARKER}1\nold: true\n")
    original_replace = os.replace

    def fail_publish_and_restore(source, destination):
        source = Path(source)
        destination = Path(destination)
        if ".fs.yaml.tmp-" in source.name:
            raise PermissionError("simulated publish failure")
        if ".shell.yaml.bak-" in source.name:
            raise PermissionError("simulated rollback failure")
        return original_replace(source, destination)

    monkeypatch.setattr(installer.os, "replace", fail_publish_and_restore)
    with pytest.raises(PermissionError, match="publish failure"):
        installer.install(str(tmp_path), ["shell", "fs"], "/tools/uv")
    backups = list(directory.glob(".*.bak-*"))
    assert any("shell.yaml.bak" in path.name for path in backups)
    assert any(path.read_text(encoding="utf-8").endswith("old: true\n") for path in backups)
