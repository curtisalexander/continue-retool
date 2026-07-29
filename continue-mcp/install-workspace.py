#!/usr/bin/env python3
"""Install the selected continue-mcp servers into one Continue workspace."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

KIT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(KIT_DIR))
from continue_mcp_common.metadata import load_servers  # noqa: E402

SERVERS = {item["name"]: item for item in load_servers()}
DEFAULT_SERVERS = tuple(item["name"] for item in load_servers() if item["default"])
OWNERSHIP_VERSION = 2
OWNERSHIP_MARKER = "# continue-mcp installer generated file; ownership-version: "
OWNED_MARKERS = {f"{OWNERSHIP_MARKER}{version}" for version in (1, 2)}


def _quote(value: str) -> str:
    """Render a JSON-style scalar, valid YAML and safe for arbitrary paths."""
    import json
    return json.dumps(value, ensure_ascii=False)


def _usable_file(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value).expanduser()
    try:
        if path.is_file():
            return str(path.resolve())
    except OSError:
        pass
    return None


def _find_interpreter(env_name: str, commands: tuple[str, ...], known: tuple[str, ...]) -> str | None:
    """Find an interpreter, preferring a valid override over PATH and known paths."""
    override = _usable_file(os.environ.get(env_name))
    if override:
        return override
    for command in commands:
        found = _usable_file(shutil.which(command))
        if found:
            return found
    for candidate in known:
        found = _usable_file(candidate)
        if found:
            return found
    return None


def _is_windows() -> bool:
    return os.name == "nt"


def detect_shell_env() -> dict[str, str]:
    """Return deterministic, absolute shell paths for the current platform."""
    windows = _is_windows()
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    pwsh_known = (
        str(Path(program_files) / "PowerShell" / "7" / "pwsh.exe"),
        r"C:\Program Files\PowerShell\7\pwsh.exe",
    ) if windows else ("/usr/bin/pwsh", "/usr/local/bin/pwsh")
    powershell_known = (
        str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
    ) if windows else ("/usr/bin/powershell", "/usr/local/bin/powershell")
    bash_known = (
        str(Path(program_files) / "Git" / "bin" / "bash.exe"),
        r"C:\Program Files\Git\bin\bash.exe",
    ) if windows else ("/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash")
    cmd_known = (str(Path(system_root) / "System32" / "cmd.exe"),) if windows else ()
    specifications = (
        ("SHELL_MCP_PWSH", ("pwsh.exe", "pwsh") if windows else ("pwsh",), pwsh_known),
        ("SHELL_MCP_POWERSHELL", ("powershell.exe", "powershell") if windows else ("powershell",), powershell_known),
        ("SHELL_MCP_BASH", ("bash.exe", "bash") if windows else ("bash",), bash_known),
        ("SHELL_MCP_CMD", ("cmd.exe", "cmd") if windows else ("cmd",), cmd_known),
    )
    detected = {
        name: path
        for name, commands, known in specifications
        if (path := _find_interpreter(name, commands, known)) is not None
    }
    preferred = (
        (("SHELL_MCP_PWSH", "pwsh"), ("SHELL_MCP_POWERSHELL", "powershell"),
         ("SHELL_MCP_CMD", "cmd"))
        if windows else (("SHELL_MCP_BASH", "bash"),)
    )
    default = next((shell for name, shell in preferred if name in detected), None)
    if default is None:
        platform = "Windows" if windows else "non-Windows"
        raise RuntimeError(f"no usable default shell interpreter found for {platform}")
    detected["SHELL_MCP_DEFAULT_SHELL"] = default
    return detected


def render_config(name: str, uv: str, workspace: Path, shell_env: dict[str, str] | None = None) -> str:
    args = ["run", "--no-sync", "--project", str(KIT_DIR), f"{name}-mcp"]
    rendered_args = ", ".join(_quote(value) for value in args)
    env = {"MCP_WORKSPACE": str(workspace)}
    if name == "shell":
        env.update(shell_env if shell_env is not None else detect_shell_env())
    rendered_env = "".join(f"      {key}: {_quote(value)}\n" for key, value in env.items())
    return (
        f"{OWNERSHIP_MARKER}{OWNERSHIP_VERSION}\n"
        f"name: {name}\nversion: 0.0.1\nschema: v1\nmcpServers:\n"
        f"  - name: {name}\n    command: {_quote(uv)}\n"
        f"    args: [{rendered_args}]\n    connectionTimeout: 120000\n"
        f"    env:\n{rendered_env}"
    )


def expected_outputs(project: str | os.PathLike[str], selected: list[str], uv: str) -> dict[Path, str]:
    project_path = Path(project)
    if not project_path.is_dir():
        raise RuntimeError(f"project does not exist or is not a directory: {project_path}")
    workspace = project_path.resolve()
    shell_env = detect_shell_env() if "shell" in selected else None
    return {
        workspace / ".continue" / "mcpServers" / f"{name}.yaml":
            render_config(name, os.path.abspath(uv), workspace, shell_env)
        for name in selected
    }


def sync_deps(uv: str) -> None:
    subprocess.run([uv, "sync", "--locked", "--project", str(KIT_DIR)], check=True)


def _is_owned(data: bytes) -> bool:
    first_line = data.splitlines()[0].decode("utf-8", errors="replace") if data else ""
    return first_line in OWNED_MARKERS or _is_legacy_generated(data)


def _is_legacy_generated(data: bytes) -> bool:
    """Recognize the exact unmarked shape emitted before ownership-version 1."""
    try:
        lines = data.decode("utf-8").splitlines()
        if len(lines) != 10 or not lines[0].startswith("name: "):
            return False
        name = lines[0].removeprefix("name: ")
        static = [
            "version: 0.0.1", "schema: v1", "mcpServers:",
            f"  - name: {name}",
        ]
        if lines[1:5] != static or name not in SERVERS:
            return False
        command = json.loads(lines[5].removeprefix("    command: "))
        args = json.loads(lines[6].removeprefix("    args: "))
        workspace = json.loads(lines[9].removeprefix("      MCP_WORKSPACE: "))
        return (
            isinstance(command, str)
            and args[:2] == ["run", "--no-sync"]
            and len(args) == 5
            and args[2] == "--project"
            and args[4] == f"{name}-mcp"
            and lines[7] == "    connectionTimeout: 120000"
            and lines[8] == "    env:"
            and isinstance(workspace, str)
        )
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, IndexError):
        return False


class _ExistingFile(NamedTuple):
    stat: os.stat_result
    data: bytes


def _preflight(outputs: dict[Path, str], workspace: Path) -> dict[Path, _ExistingFile]:
    """Reject unsafe or conflicting targets before sync or file creation."""
    differing: list[Path] = []
    existing: dict[Path, _ExistingFile] = {}
    for path, text in outputs.items():
        try:
            target = path.lstat()
        except FileNotFoundError:
            target = None
        if target is not None:
            if not stat.S_ISREG(target.st_mode):
                raise RuntimeError(f"refusing non-regular output file: {path}")
            data = path.read_bytes()
            existing[path] = _ExistingFile(target, data)
            if data != text.encode("utf-8") and not _is_owned(data):
                differing.append(path)

        current = path.parent
        while current != workspace:
            if current.exists() or current.is_symlink():
                try:
                    current.resolve().relative_to(workspace)
                except ValueError as exc:
                    raise RuntimeError(
                        f"output path escapes project through symlink: {path}"
                    ) from exc
            if current.parent == current:
                raise RuntimeError(f"output path escapes project: {path}")
            current = current.parent
    if differing:
        raise RuntimeError(
            "refusing to overwrite differing existing file(s): "
            + ", ".join(map(str, differing))
        )
    return existing


def preflight_install(project: str, selected: list[str], uv: str) -> None:
    outputs = expected_outputs(project, selected, uv)  # render before any write
    workspace = Path(project).resolve()
    _preflight(outputs, workspace)


def install(project: str, selected: list[str], uv: str) -> None:
    outputs = expected_outputs(project, selected, uv)
    workspace = Path(project).resolve()
    existing = _preflight(outputs, workspace)
    committed: list[tuple[Path, Path | None]] = []
    temporaries: list[Path] = []
    backups: list[Path] = []
    transaction_succeeded = False
    try:
        for path, text in outputs.items():
            if path in existing and path.read_bytes() == text.encode("utf-8"):
                print(f"unchanged {path}")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{len(temporaries)}")
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            temporaries.append(temporary)
            prior = existing.get(path)
            backup = None
            if prior is not None:
                current = path.lstat()
                if (
                    not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino)
                    != (prior.stat.st_dev, prior.stat.st_ino)
                    or path.read_bytes() != prior.data
                ):
                    raise RuntimeError(f"output file changed after preflight: {path}")
                os.chmod(temporary, stat.S_IMODE(prior.stat.st_mode))
                backup = path.with_name(f".{path.name}.bak-{os.getpid()}-{len(committed)}")
                # Keep the live configuration present while creating rollback
                # state. Publishing is then one atomic destination replace.
                with backup.open("xb") as backup_file:
                    backup_file.write(prior.data)
                    backup_file.flush()
                    os.fsync(backup_file.fileno())
                backups.append(backup)
                os.chmod(backup, stat.S_IMODE(prior.stat.st_mode))
                current = path.lstat()
                if (
                    (current.st_dev, current.st_ino)
                    != (prior.stat.st_dev, prior.stat.st_ino)
                    or path.read_bytes() != prior.data
                ):
                    raise RuntimeError(f"output file changed while staging: {path}")
                os.replace(temporary, path)
            else:
                os.link(temporary, path)  # atomic creation without overwriting a raced file
                committed.append((path, None))
                temporary.unlink()
            if backup is not None:
                committed.append((path, backup))
            print(f"updated {path}" if backup else f"created {path}")
        transaction_succeeded = True
    except BaseException:
        for path, backup in reversed(committed):
            try:
                if backup is None:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(backup, path)
            except OSError:
                # Preserve any remaining backup for manual recovery. Never turn
                # a rollback failure into deletion of the only old copy.
                pass
        raise
    finally:
        for temporary in temporaries:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        # Cleanup is not part of the transaction. A sharing violation or
        # antivirus lock must never roll back successfully installed config.
        for backup in backups:
            if not transaction_succeeded:
                continue
            try:
                backup.unlink()
            except OSError:
                pass


def _minimal_base_env() -> dict[str, str]:
    names = (
        ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
        if _is_windows() else ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
    )
    return {name: os.environ[name] for name in names if name in os.environ}


def _config_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    in_env = False
    for line in text.splitlines():
        if line == "    env:":
            in_env = True
        elif in_env and line.startswith("      "):
            key, value = line.strip().split(":", 1)
            env[key] = json.loads(value.strip())
        elif in_env:
            break
    return env


async def _handshake(name: str, uv: str, workspace: Path, env: dict[str, str]) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=uv,
        args=["run", "--no-sync", "--project", str(KIT_DIR), f"{name}-mcp"],
        env={**_minimal_base_env(), **env},
        cwd=str(workspace),
        keep_alive=False,
    )
    async with asyncio.timeout(120):
        async with Client(transport, init_timeout=120, timeout=120) as client:
            await client.list_tools()


def check(project: str, selected: list[str], uv: str) -> None:
    workspace = Path(project).resolve()
    outputs = expected_outputs(workspace, selected, uv)
    failures = [str(path) for path, text in outputs.items() if not path.is_file() or path.read_text(encoding="utf-8") != text]
    if failures:
        raise RuntimeError("missing or stale installed configuration: " + ", ".join(failures))
    for name in selected:
        path = workspace / ".continue" / "mcpServers" / f"{name}.yaml"
        asyncio.run(_handshake(name, os.path.abspath(uv), workspace, _config_env(outputs[path])))
        print(f"ok {name}-mcp")


def _selection(value: str | None, with_sql: bool) -> list[str]:
    selected = [name.strip() for name in value.split(",")] if value is not None else list(DEFAULT_SERVERS)
    if with_sql:
        selected.append("sql")
    selected = list(dict.fromkeys(selected))
    if any(not name for name in selected):
        raise ValueError("unknown server(s): <empty>")
    unknown = set(selected) - SERVERS.keys()
    if unknown:
        raise ValueError("unknown server(s): " + ", ".join(sorted(unknown)))
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--only", help="comma-separated explicit server subset")
    group.add_argument("--with-sql", action="store_true", help="add optional sql-mcp")
    parser.add_argument("--no-sync", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        selected = _selection(args.only, args.with_sql)
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("uv is not available on PATH")
        if args.check:
            check(args.project, selected, uv)
        else:
            preflight_install(args.project, selected, uv)
            if not args.no_sync:
                sync_deps(uv)
            install(args.project, selected, uv)
    except (ImportError, OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"continue-mcp install failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
