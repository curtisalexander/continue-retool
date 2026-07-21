from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "install-workspace.py"
SPEC = importlib.util.spec_from_file_location("install_workspace", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_install_serializes_adversarial_paths_and_writes_manifest(tmp_path: Path):
    project = tmp_path / "project # one 'quoted'"
    project.mkdir()
    uv = 'C:/Program Files/uv #1/uv"tool.exe'

    installer.install(str(project), ["hello"], uv)

    config_path = project / ".continue/mcpServers/hello.yaml"
    config = installer._parse_installed_config(str(config_path))
    assert config["command"] == uv
    assert config["args"] == [
        "run", "--no-sync", "--project",
        installer._slashes(installer.KIT_DIR),
        "hello-mcp",
    ]
    assert config["env"]["MCP_WORKSPACE"] == installer._slashes(str(project))

    manifest = json.loads((project / installer.MANIFEST_REL).read_text())
    record = manifest["files"][".continue/mcpServers/hello.yaml"]
    assert len(record["installed_sha256"]) == 64
    assert record["previous"] is None


def test_selective_install_uninstall_restores_empty_project(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()

    installer.install(str(project), ["hello"], "/path/to/uv")
    installer.uninstall(str(project), ["hello"], False)

    assert list(project.iterdir()) == []


def test_gateway_install_stamps_downstreams_and_uninstalls(tmp_path: Path):
    project = tmp_path / "gateway project # 'quoted'"
    project.mkdir()
    uv = os.path.abspath('/opt/uv tools/uv')

    installer.install(str(project), ["hello", "sql"], uv, gateway=True)

    yaml_path = project / ".continue/mcpServers/gateway.yaml"
    yaml_config = installer._parse_installed_config(str(yaml_path))
    assert yaml_config["command"] == uv
    assert yaml_config["args"][1:3] == ["--no-sync", "--project"]
    gateway_config_path = project / installer.GATEWAY_CONFIG_REL
    assert yaml_config["env"]["GATEWAY_CONFIG"] == installer._slashes(
        str(gateway_config_path)
    )
    downstreams = json.loads(gateway_config_path.read_text())["servers"]
    assert set(downstreams) == {"hello", "sql"}
    for name, spec in downstreams.items():
        assert spec["command"] == uv
        assert spec["args"] == [
                "run", "--no-sync", "--project",
                installer._slashes(installer.KIT_DIR),
            f"{name}-mcp",
        ]
        assert spec["env"]["MCP_WORKSPACE"] == installer._slashes(str(project))

    manifest = json.loads((project / installer.MANIFEST_REL).read_text())
    assert ".continue/mcpServers/gateway.yaml" in manifest["files"]
    assert ".continue/gateway.config.json" in manifest["files"]
    assert not (project / ".continue/mcpServers/hello.yaml").exists()

    installer.uninstall(str(project), ["hello", "sql"], False, gateway=True)
    assert list(project.iterdir()) == []


def test_gateway_install_rejects_duplicate_direct_registration(tmp_path: Path):
    project = tmp_path / "project"
    direct = project / ".continue/mcpServers/sql.yaml"
    direct.parent.mkdir(parents=True)
    direct.write_text("user config\n")

    with pytest.raises(RuntimeError, match="already registered directly"):
        installer.install(str(project), ["sql"], "/opt/uv", gateway=True)

    assert direct.read_text() == "user config\n"
    assert not (project / ".continue/mcpServers/gateway.yaml").exists()


def test_direct_install_rejects_server_already_behind_gateway(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    installer.install(str(project), ["sql"], "/opt/uv", gateway=True)

    with pytest.raises(RuntimeError, match="already configured behind the gateway"):
        installer.install(str(project), ["sql"], "/opt/uv")

    assert not (project / ".continue/mcpServers/sql.yaml").exists()


def test_hybrid_uninstall_keeps_shared_rules_until_last_registration(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    installer.install(str(project), ["sql"], "/opt/uv", gateway=True)
    installer.install(str(project), ["hello"], "/opt/uv")

    installer.uninstall(str(project), ["sql"], False, gateway=True)
    assert (project / ".continue/mcpServers/hello.yaml").exists()
    assert (project / ".continue/rules/notes.md").exists()

    installer.uninstall(str(project), ["hello"], False)
    assert list(project.iterdir()) == []


def test_full_uninstall_restores_exact_initial_files_and_modes(tmp_path: Path):
    project = tmp_path / "project"
    target = project / ".continue/mcpServers/hello.yaml"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original user config\xff\n")
    target.chmod(0o640)
    (project / "keep.txt").write_text("keep me")
    before = _snapshot(project)

    installer.install(str(project), list(installer.SERVERS), "/path/to/uv")
    installer.install(str(project), list(installer.SERVERS), "/path/to/uv")
    installer.uninstall(str(project), list(installer.SERVERS), True)

    assert _snapshot(project) == before
    assert not (project / installer.MANIFEST_REL).exists()
    assert not (project / installer.BACKUP_DIR_REL).exists()


def test_reinstall_refuses_to_overwrite_local_edit(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    installer.install(str(project), ["hello"], "/path/to/uv")
    target = project / ".continue/mcpServers/hello.yaml"
    target.write_text("local edit\n")

    with pytest.raises(RuntimeError, match="locally modified"):
        installer.install(str(project), ["hello"], "/path/to/uv")

    assert target.read_text() == "local edit\n"


def test_uninstall_retains_modified_installed_file(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    installer.install(str(project), ["hello"], "/path/to/uv")
    target = project / ".continue/mcpServers/hello.yaml"
    target.write_text("local edit\n")

    installer.uninstall(str(project), ["hello"], False)

    assert target.read_text() == "local edit\n"
    manifest = json.loads((project / installer.MANIFEST_REL).read_text())
    assert ".continue/mcpServers/hello.yaml" in manifest["files"]


def test_atomic_replacement_failure_preserves_original(tmp_path: Path, monkeypatch):
    target = tmp_path / "owned.yaml"
    target.write_bytes(b"original")
    real_replace = installer.os.replace

    def fail_target(src, dest):
        if os.fspath(dest) == os.fspath(target):
            raise OSError("injected replacement failure")
        return real_replace(src, dest)

    monkeypatch.setattr(installer.os, "replace", fail_target)
    with pytest.raises(OSError, match="injected"):
        installer._atomic_write(str(target), b"replacement")

    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob(".owned.yaml.*.tmp"))


def test_install_rejects_symlinked_continue_directory(tmp_path: Path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    try:
        (project / ".continue").symlink_to(outside, target_is_directory=True)
    except OSError as e:
        pytest.skip(f"symlink creation unavailable: {e}")

    with pytest.raises(RuntimeError, match="escapes project"):
        installer.install(str(project), ["hello"], "/path/to/uv")

    assert list(outside.iterdir()) == []


def test_sync_deps_runs_one_root_sync_for_multiple_selected_servers(monkeypatch):
    calls = []

    class Completed:
        returncode = 0
        stdout = "Resolved 80 packages in 3ms\n"
        stderr = ""

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/tools/uv")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return Completed()

    monkeypatch.setattr(installer.subprocess, "run", run)

    assert installer.sync_deps(["hello", "fs", "sql"]) == 0
    assert calls == [
        (["/tools/uv", "sync", "--project", installer.KIT_DIR],
         {"capture_output": True, "text": True})
    ]


def test_policy_checklist_mentions_only_registered_servers():
    checklist = installer.policy_checklist(["hello", "fs"])
    assert "hello.*" in checklist
    assert "fs.*" in checklist
    assert "shell.*" not in checklist
    assert "search.*" not in checklist
    assert "sql.*" not in checklist


def test_doctor_launches_exact_installed_configuration(tmp_path: Path, monkeypatch):
    project = tmp_path / "project # doctor"
    project.mkdir()
    command = 'C:/Tools # stable/uv"quoted.exe'
    installer.install(str(project), ["hello"], command)
    calls = []

    def handshake(cmd, *, env=None, cwd=None, timeout=30.0):
        calls.append((cmd, env, cwd, timeout))
        return True, "ok"

    monkeypatch.setattr(installer, "_mcp_handshake", handshake)
    failures = installer.doctor(str(project), ["hello"])

    assert failures == 0
    assert len(calls) == 1
    cmd, env, cwd, timeout = calls[0]
    assert cmd[0] == command
    assert cmd[1:3] == ["run", "--no-sync"]
    assert env["MCP_WORKSPACE"] == installer._slashes(str(project))
    assert cwd is None
    assert timeout == 120


def test_doctor_rejects_broken_installed_args(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    installer.install(str(project), ["hello"], "/path/to/uv")
    target = project / ".continue/mcpServers/hello.yaml"
    target.write_text(target.read_text().replace(
        'args: ["run",', 'args: [not valid JSON,'
    ))
    called = False

    def handshake(*args, **kwargs):
        nonlocal called
        called = True
        return True, "unexpected"

    monkeypatch.setattr(installer, "_mcp_handshake", handshake)
    assert installer.doctor(str(project), ["hello"]) > 0
    assert not called


def test_gateway_doctor_probes_installed_downstream_catalog(tmp_path: Path, monkeypatch):
    project = tmp_path / "gateway doctor"
    project.mkdir()
    command = os.path.abspath("/tools/uv")
    installer.install(str(project), ["hello", "sql"], command, gateway=True)
    calls = []

    def handshake(cmd, *, env=None, cwd=None, timeout=30.0, gateway_servers=()):
        calls.append((cmd, env, cwd, timeout, gateway_servers))
        return True, "catalog reached"

    monkeypatch.setattr(installer, "_mcp_handshake", handshake)
    failures = installer.doctor(str(project), ["hello", "sql"], gateway=True)

    assert failures == 0
    assert len(calls) == 1
    cmd, env, cwd, timeout, gateway_servers = calls[0]
    assert cmd[0] == command
    assert env["GATEWAY_CONFIG"] == installer._slashes(
        str(project / installer.GATEWAY_CONFIG_REL)
    )
    assert cwd is None
    assert timeout == 120
    assert gateway_servers == ("hello", "sql")
