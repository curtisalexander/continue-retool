#!/usr/bin/env -S uv run --script
"""
install-workspace.py — wire this toolkit into a project in one command.

Copies each server's .continue/mcpServers/*.yaml into the target project,
stamping the absolute paths as it goes:
  * command     -> the absolute path to `uv` (so Continue doesn't depend on the
                   PATH it was launched with — GUI-launched VS Code often lacks
                   the shell PATH where uv lives)
  * --project   -> this toolkit checkout (where the script lives)
  * MCP_WORKSPACE -> the target project root
and copies the two rules (notes discovery + the rule rule) into
.continue/rules/. Stdlib only; works on macOS/Linux/Windows.

The yaml launches each server with `uv run --no-sync` so startup never touches
the network (the venv is already built by this installer). Without --no-sync,
`uv run` tries to sync against the index on every launch, which hangs behind a
corporate proxy and shows up in Continue as a connection timeout.

All servers share this toolkit's single uv project, lockfile, and environment.
After copying the selected configs this script runs one `uv sync` at the toolkit
root. That builds the venv now, at a terminal where failures are visible —
instead of lazily on first launch, when Continue spawns a server headless and a
slow first sync looks like a hang. Pass --no-sync to skip.

Corporate networks: `uv sync` reaches a package index, so behind a proxy set
UV_SYSTEM_CERTS=true (trust the OS cert store) and UV_DEFAULT_INDEX=<mirror>
before running this — or put system-certs/index in a user/system uv.toml. Both
apply to every uv call, so no per-directory pyproject edits. See README.

Re-running is safe: an installation manifest records the hash of every written
file and stores any pre-existing bytes in a private backup directory. A local
edit made after installation is never overwritten. Uninstall removes unchanged
installer-created files and restores unchanged installer-replaced files.

Usage (uv runs it via the shebang — `python3` isn't on PATH on Windows):
  uv run install-workspace.py /path/to/your/project
  uv run install-workspace.py /path/to/your/project --only shell,search,edit
  uv run install-workspace.py /path/to/your/project --gateway --only sql,notes
  uv run install-workspace.py /path/to/your/project --no-sync
  uv run install-workspace.py /path/to/your/project --check    # doctor: verify
  uv run install-workspace.py /path/to/your/project --uninstall
Or, on a Unix shell, directly: ./install-workspace.py /path/to/your/project

Doctor mode (--check) verifies an install end-to-end: uv present, the shared
project + venv, the stamped yamls in the project, detected interpreters,
and a LIVE MCP handshake using the installed command, arguments, environment,
workspace, working directory, and timeout (initialize + tools/list over stdio —
the same flow Continue performs at connect). It compresses the troubleshooting
checklist into one command; run it whenever a server shows "connection timed out".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, cast

from continue_mcp_common.metadata import load_servers

KIT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_METADATA = load_servers(include_gateway=False)
SERVERS = [server["name"] for server in SERVER_METADATA]
GATEWAY = "gateway"
RULES = ["notes.md", "rule-rule.md"]
GATEWAY_CONFIG_REL = os.path.join(".continue", "gateway.config.json")
MANIFEST_REL = os.path.join(".continue", ".continue-mcp-install.json")
BACKUP_DIR_REL = os.path.join(".continue", ".continue-mcp-backups")
MANIFEST_VERSION = 1

_POLICY_LINES = {server["name"]: server["policy"] for server in SERVER_METADATA}


def policy_checklist(names: list[str]) -> str:
    """Render guidance only for servers actually registered by this install."""
    lines = ["", "Next, in Continue's Agent tool settings:"]
    lines.extend(_POLICY_LINES[name] for name in SERVERS if name in names)
    if set(names) & {"fs", "search", "edit"}:
        lines.extend([
            "",
            "The Automatic grants for fs/search/edit are workspace-jailed by default:",
            "real paths outside MCP_WORKSPACE are refused. MCP_JAIL_EXTRA adds roots;",
            "MCP_JAIL=0 disables the jail. shell is the approval-gated escape hatch.",
        ])
    if "hello" in names:
        lines.extend([
            "",
            "Suggested first prompt:",
            "  Call hello.ping, then call hello.whoami and report MCP_WORKSPACE.",
            "A pong confirms MCP is live; the workspace should point at this project.",
        ])
    lines.extend([
        "",
        "For rarely used tools, re-run with --gateway: Continue sees only the",
        "gateway's three discovery tools while it owns the selected downstreams.",
    ])
    return "\n".join(lines)

CORP_NOTE = (
    "  Behind a corporate proxy? `uv sync` needs UV_SYSTEM_CERTS=true and\n"
    "  UV_DEFAULT_INDEX=<your mirror> set (or a user/system uv.toml). See README."
)


def _slashes(p: str) -> str:
    """Forward slashes everywhere: valid on Windows, and avoids backslash
    escapes inside double-quoted YAML strings."""
    return os.path.abspath(p).replace("\\", "/")


def _yaml_string(value: str) -> str:
    """JSON strings are valid YAML double-quoted scalars, including paths that
    contain spaces, #, quotes, colons, or Windows backslashes."""
    return json.dumps(value, ensure_ascii=False)


def stamp(text: str, server: str, workspace: str, uv_path: str) -> str:
    project_dir = _slashes(KIT_DIR)
    workspace = _slashes(workspace)
    lines = []
    for line in text.splitlines(keepends=True):
        indent = line[:len(line) - len(line.lstrip())]
        stripped = line.strip()
        newline = "\n" if line.endswith("\n") else ""
        if stripped.startswith("command:"):
            line = f"{indent}command: {_yaml_string(uv_path)}{newline}"
        elif stripped.startswith("args:"):
            args = ["run", "--no-sync", "--project", project_dir, f"{server}-mcp"]
            line = f"{indent}args: {json.dumps(args, ensure_ascii=False)}{newline}"
        elif stripped.startswith("MCP_WORKSPACE:"):
            line = f"{indent}MCP_WORKSPACE: {_yaml_string(workspace)}{newline}"
        elif stripped.startswith("GATEWAY_CONFIG:"):
            config = _slashes(os.path.join(workspace, GATEWAY_CONFIG_REL))
            line = f"{indent}GATEWAY_CONFIG: {_yaml_string(config)}{newline}"
        lines.append(line)
    text = "".join(lines)
    if "/absolute/path/to" in text:
        raise RuntimeError(f"{server}.yaml still has an unstamped placeholder")
    return text


# --- shell-mcp interpreter detection ---------------------------------------
# The shell server spawns an interpreter (pwsh/powershell/bash/cmd) per call and
# has to *find* it first. From GUI-launched VS Code its inherited PATH is often
# stale/thin (pwsh lives in Program Files, not a guaranteed dir; pwsh 7 may not
# be installed at all). So we resolve interpreters HERE — in a real terminal
# where PATH is correct — and stamp their absolute paths into the yaml env, the
# same tactic already used for uv in command:. The server still resolves at
# runtime when these are absent (manual install, or a shell added afterward).
_SHELL_INTERPRETERS = ["pwsh", "powershell", "cmd", "bash"]


def _interp_known_locations(shell: str) -> list[str]:
    """Fixed install paths to try when PATH lookup misses. Mirrors
    shell_mcp.server._known_locations — keep in sync by hand (this stdlib-only
    script can't import the server package before its venv exists)."""
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    return {
        "pwsh": [os.path.join(pf, "PowerShell", "7", "pwsh.exe")],
        "powershell": [os.path.join(
            sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")],
        "cmd": [os.path.join(sysroot, "System32", "cmd.exe")],
        "bash": ["/bin/bash", "/usr/bin/bash",
                 os.path.join(pf, "Git", "bin", "bash.exe")],
    }.get(shell, [])


def detect_interpreters() -> dict[str, str]:
    """Resolve which shells exist on THIS machine -> {shell: absolute path}.
    PATH first, then the known install locations — so a pwsh that lives in
    Program Files but not on PATH still gets stamped."""
    found: dict[str, str] = {}
    for shell in _SHELL_INTERPRETERS:
        exe = shutil.which(shell)
        if not exe:
            exe = next((c for c in _interp_known_locations(shell)
                        if os.path.isfile(c)), None)
        if exe:
            found[shell] = _slashes(exe)
    return found


def _default_shell_for(found: dict[str, str]) -> str:
    """Prefer a real interpreter: pwsh > powershell > cmd on Windows, bash off it."""
    order = ("pwsh", "powershell", "cmd") if os.name == "nt" else ("bash",)
    for s in order:
        if s in found:
            return s
    return next(iter(found), "bash")


def stamp_shell_interpreters(text: str) -> str:
    """Replace the __SHELL_INTERPRETERS__ marker line in shell.yaml with the
    detected interpreter paths + default shell, indented to match the marker."""
    marker = "# __SHELL_INTERPRETERS__"
    found = detect_interpreters()
    out: list[str] = []
    for line in text.splitlines():
        if marker in line:
            indent = line[: len(line) - len(line.lstrip())]
            if found:
                for shell, path in found.items():
                    out.append(f"{indent}SHELL_MCP_{shell.upper()}: {_yaml_string(path)}")
                out.append(f"{indent}SHELL_MCP_DEFAULT_SHELL: "
                           f"{_yaml_string(_default_shell_for(found))}")
                shells = ", ".join(found)
                print(f"  detected shells for shell-mcp: {shells} "
                      f"(default {_default_shell_for(found)})")
            else:
                out.append(f"{indent}# no shells detected at install; "
                           f"server resolves interpreters at runtime")
                print("  WARNING: no pwsh/powershell/bash/cmd found on PATH — "
                      "shell-mcp will resolve interpreters at runtime.",
                      file=sys.stderr)
        else:
            out.append(line)
    result = "\n".join(out)
    return result + "\n" if text.endswith("\n") else result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(path: str) -> None:
    """Best-effort directory durability; unsupported on some platforms."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: str, data: bytes, mode: int | None = None) -> None:
    """Durably replace a regular file using a sibling temporary file."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    if mode is None and os.path.isfile(path) and not os.path.islink(path):
        mode = stat.S_IMODE(os.stat(path).st_mode)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.",
                               suffix=".tmp", dir=parent)
    try:
        if mode is not None:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            else:  # Windows has chmod(path) but may not expose fchmod(fd).
                os.chmod(tmp, mode)
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _manifest_path(project: str) -> str:
    return os.path.join(project, MANIFEST_REL)


def _require_contained(project: str, path: str) -> str:
    project_real = os.path.realpath(project)
    path_real = os.path.realpath(path)
    try:
        contained = os.path.commonpath([project_real, path_real]) == project_real
    except ValueError:
        contained = False
    if not contained:
        raise RuntimeError(f"installer path escapes project: {path}")
    return path


def _from_manifest_rel(project: str, rel: str) -> str:
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        raise RuntimeError(f"invalid path in installation manifest: {rel!r}")
    path = os.path.join(project, *rel.split("/"))
    return _require_contained(project, path)


def _load_manifest(project: str) -> dict[str, Any]:
    path = _require_contained(project, _manifest_path(project))
    if not os.path.exists(path):
        return {"version": MANIFEST_VERSION, "files": {}, "created_dirs": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        raise RuntimeError(f"cannot read installation manifest {path}: {e}") from e
    if (manifest.get("version") != MANIFEST_VERSION
            or not isinstance(manifest.get("files"), dict)):
        raise RuntimeError(f"unsupported installation manifest: {path}")
    manifest.setdefault("created_dirs", [])
    if not isinstance(manifest["created_dirs"], list):
        raise RuntimeError(f"invalid installation manifest: {path}")
    for rel, record in manifest["files"].items():
        _from_manifest_rel(project, rel)
        if (not isinstance(record, dict)
                or not isinstance(record.get("installed_sha256"), str)):
            raise RuntimeError(f"invalid file record in installation manifest: {rel!r}")
        previous = record.get("previous")
        if previous is not None:
            if (not isinstance(previous, dict)
                    or not isinstance(previous.get("backup"), str)
                    or not isinstance(previous.get("sha256"), str)):
                raise RuntimeError(f"invalid backup record in installation manifest: {rel!r}")
            _from_manifest_rel(project, previous["backup"])
    for rel in manifest["created_dirs"]:
        _from_manifest_rel(project, rel)
    return manifest


def _save_manifest(project: str, manifest: dict[str, Any]) -> None:
    data = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")
    _atomic_write(_require_contained(project, _manifest_path(project)), data, 0o600)


def _relative(project: str, path: str) -> str:
    _require_contained(project, path)
    return os.path.relpath(path, project).replace(os.sep, "/")


def _backup_rel(target_rel: str) -> str:
    key = hashlib.sha256(target_rel.encode("utf-8")).hexdigest()[:16]
    return (os.path.join(BACKUP_DIR_REL, f"{key}-{os.path.basename(target_rel)}.bak")
            .replace(os.sep, "/"))


def _ensure_dir(project: str, path: str, manifest: dict[str, Any]) -> None:
    missing = []
    cursor = path
    while cursor != project and not os.path.exists(cursor):
        missing.append(cursor)
        cursor = os.path.dirname(cursor)
    if cursor != project and not os.path.isdir(cursor):
        raise RuntimeError(f"cannot create installer directory beneath: {cursor}")
    os.makedirs(path, exist_ok=True)
    _require_contained(project, path)
    for created in reversed(missing):
        rel = _relative(project, created)
        if rel not in manifest["created_dirs"]:
            manifest["created_dirs"].append(rel)


def write_out(project: str, dest: str, content: str,
              manifest: dict[str, Any]) -> None:
    """Atomically install one file and record enough state to undo it safely.

    A reinstall may replace only the exact bytes from the prior install. This
    prevents a user's post-install edits from being silently overwritten.
    """
    data = content.encode("utf-8")
    new_hash = _sha256(data)
    rel = _relative(project, dest)
    record = cast(dict[str, Any] | None, manifest["files"].get(rel))
    exists = os.path.lexists(dest)
    if exists and (os.path.islink(dest) or not os.path.isfile(dest)):
        raise RuntimeError(f"refusing to replace non-regular file: {dest}")
    old = None
    if exists:
        with open(dest, "rb") as f:
            old = f.read()

    if record is not None and old is not None and _sha256(old) != record["installed_sha256"]:
        raise RuntimeError(f"refusing to overwrite locally modified installed file: {dest}")

    if record is None:
        previous = None
        if old is not None:
            backup_rel = _backup_rel(rel)
            backup_path = _from_manifest_rel(project, backup_rel)
            _ensure_dir(project, os.path.dirname(backup_path), manifest)
            _atomic_write(backup_path, old, 0o600)
            previous = {
                "backup": backup_rel,
                "sha256": _sha256(old),
                "mode": stat.S_IMODE(os.stat(dest).st_mode),
            }
        record = cast(dict[str, Any], {"previous": previous})

    record["installed_sha256"] = new_hash
    manifest["files"][rel] = record
    if old == data:
        print(f"  unchanged  {dest}")
    else:
        _atomic_write(dest, data)
        action = "updated" if old is not None else "installed"
        detail = " (original backed up)" if record.get("previous") else ""
        print(f"  {action:9}  {dest}{detail}")
    _save_manifest(project, manifest)


def _gateway_downstream_config(project: str, names: list[str], uv_path: str) -> str:
    """Render the project-owned gateway catalog with launch-ready commands."""
    workspace = _slashes(project)
    servers: dict[str, dict[str, Any]] = {}
    shell_env: dict[str, str] = {}
    if "shell" in names:
        found = detect_interpreters()
        shell_env = {
            f"SHELL_MCP_{name.upper()}": path for name, path in found.items()
        }
        if found:
            shell_env["SHELL_MCP_DEFAULT_SHELL"] = _default_shell_for(found)
    for name in names:
        env = {"MCP_WORKSPACE": workspace}
        if name == "shell":
            env.update(shell_env)
        servers[name] = {
            "command": uv_path,
            "args": [
                "run", "--no-sync", "--project",
                _slashes(KIT_DIR),
                f"{name}-mcp",
            ],
            "env": env,
        }
    return json.dumps({"servers": servers}, indent=2, ensure_ascii=False) + "\n"


def _reject_gateway_duplicates(project: str, names: list[str]) -> None:
    duplicates = [
        name for name in names
        if os.path.lexists(os.path.join(project, ".continue", "mcpServers", f"{name}.yaml"))
    ]
    if duplicates:
        joined = ", ".join(f"{name}.yaml" for name in duplicates)
        raise RuntimeError(
            f"gateway downstreams are already registered directly ({joined}); "
            "uninstall those direct configs before installing the gateway"
        )


def _reject_direct_gateway_overlap(project: str, names: list[str]) -> None:
    config_path = os.path.join(project, GATEWAY_CONFIG_REL)
    gateway_yaml = os.path.join(project, ".continue", "mcpServers", "gateway.yaml")
    if not os.path.lexists(gateway_yaml) or not os.path.exists(config_path):
        return
    try:
        downstreams = _load_gateway_downstreams(config_path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"cannot verify existing gateway config before direct install: {e}"
        ) from e
    overlap = sorted(set(names) & set(downstreams))
    if overlap:
        raise RuntimeError(
            "server(s) already configured behind the gateway: " + ", ".join(overlap)
        )


def install(project: str, names: list[str], uv_path: str, gateway: bool = False) -> None:
    mcp_dir = os.path.join(project, ".continue", "mcpServers")
    rules_dir = os.path.join(project, ".continue", "rules")
    if gateway:
        _reject_gateway_duplicates(project, names)
    else:
        _reject_direct_gateway_overlap(project, names)
    manifest = _load_manifest(project)
    _ensure_dir(project, mcp_dir, manifest)
    _ensure_dir(project, rules_dir, manifest)

    installed_names = [GATEWAY] if gateway else names
    for name in installed_names:
        src = os.path.join(KIT_DIR, f"{name}-mcp", ".continue", "mcpServers", f"{name}.yaml")
        with open(src, "r", encoding="utf-8") as f:
            content = stamp(f.read(), name, project, uv_path)
        if name == "shell":
            content = stamp_shell_interpreters(content)
        write_out(project, os.path.join(mcp_dir, f"{name}.yaml"), content, manifest)

    if gateway:
        write_out(
            project,
            os.path.join(project, GATEWAY_CONFIG_REL),
            _gateway_downstream_config(project, names, uv_path),
            manifest,
        )

    for rule in RULES:
        src = os.path.join(KIT_DIR, "rules", rule)
        with open(src, "r", encoding="utf-8") as f:
            content = f.read()
        write_out(project, os.path.join(rules_dir, rule), content, manifest)


def _summary_line(out: str, err: str) -> str:
    """Pull uv's one-line tally (e.g. 'Installed 67 packages in 87ms') out of the
    captured output, so a successful sync gets a compact summary instead of its
    full package dump. Empty string if no such line is found."""
    markers = ("Installed", "Audited", "Prepared", "Resolved")
    for line in reversed((out + err).splitlines()):
        s = line.strip()
        if "package" in s and any(s.startswith(m) for m in markers):
            return s
    return ""


def sync_deps(names: list[str]) -> int:
    """Sync the unified toolkit environment once for all selected servers."""
    uv = shutil.which("uv")
    if not uv:
        print("\nWARNING: `uv` not found on PATH — skipping dependency setup.\n"
              "Install uv (https://docs.astral.sh/uv/) then re-run, or run\n"
              "  uv sync --project <toolkit>/continue-mcp\n"
              "by hand. Until then the MCP servers can't start.",
              file=sys.stderr)
        return 1

    print(f"\nSyncing the shared environment for {len(names)} selected server(s).")
    print(CORP_NOTE)
    start = time.monotonic()
    proc = subprocess.run(
        [uv, "sync", "--project", KIT_DIR], capture_output=True, text=True
    )
    duration = time.monotonic() - start
    if proc.returncode:
        print(f"Shared environment sync failed (exit {proc.returncode}):",
              file=sys.stderr)
        for line in (proc.stderr or proc.stdout).strip().splitlines()[-12:]:
            print(f"    | {line}", file=sys.stderr)
        return 1
    summary = _summary_line(proc.stdout, proc.stderr)
    extra = f" — {summary}" if summary else ""
    print(f"Shared environment synced ({duration:.1f}s){extra}")
    return 0


# --- doctor (--check): verify an install end-to-end -------------------------
def _mcp_handshake(cmd: list[str], *, env: dict[str, str] | None = None,
                   cwd: str | None = None,
                   timeout: float = 30.0,
                   gateway_servers: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Spawn a stdio MCP server and drive a real initialize -> tools/list
    handshake with plain newline-delimited JSON-RPC (stdlib only). This is the
    same flow Continue performs at connect, so a pass here means the yaml's
    command actually produces a working server."""
    import json
    import queue

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env, cwd=cwd,
        )
    except OSError as e:
        return False, f"spawn failed: {e}"

    q: queue.Queue = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line)

    threading.Thread(target=_reader, daemon=True).start()

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for _ in proc.stderr:
            pass

    threading.Thread(target=_drain_stderr, daemon=True).start()

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv(want_id: int) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                line = q.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError from None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                return msg

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "install-workspace-doctor", "version": "1.0"}}})
        recv(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = recv(2).get("result", {}).get("tools", [])
        tool_names = [t.get("name", "") for t in tools if isinstance(t, dict)]
        if gateway_servers:
            expected_meta = {"search", "describe", "call"}
            if set(tool_names) != expected_meta:
                return False, f"unexpected gateway tools: {', '.join(tool_names)}"
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "search", "arguments": {"query": ""}}})
            called = recv(3)
            if "error" in called:
                return False, f"gateway search failed: {called['error']}"
            result = called.get("result", {})
            data = result.get("structuredContent") or {}
            unavailable = data.get("unavailable_servers") or {}
            if unavailable:
                detail = "; ".join(f"{name}: {error}" for name, error in unavailable.items())
                return False, f"downstream unavailable: {detail}"
            discovered = {
                str(tool.get("name", "")).split(".", 1)[0]
                for tool in data.get("tools", []) if isinstance(tool, dict)
            }
            missing = sorted(set(gateway_servers) - discovered)
            if missing:
                return False, f"downstream catalog missing: {', '.join(missing)}"
            return True, (f"3 gateway tools; downstream catalog reached: "
                          f"{', '.join(sorted(discovered))}")
        return True, f"{len(tools)} tool(s): {', '.join(tool_names)}"
    except TimeoutError:
        return False, f"no reply within {timeout:.0f}s"
    except Exception as e:  # any protocol surprise is a failed check, not a crash
        return False, f"handshake error: {e}"
    finally:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass


def _strip_yaml_comment(value: str) -> str:
    """Strip a YAML comment without treating # inside quotes as a comment."""
    quote = None
    escaped = False
    for i, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
        elif char in "\"'":
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif char == "#" and quote is None and (i == 0 or value[i - 1].isspace()):
            return value[:i].rstrip()
    return value.strip()


def _yaml_scalar(value: str) -> str:
    value = _strip_yaml_comment(value).strip()
    if not value:
        return ""
    if value.startswith('"'):
        parsed = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError("expected a string")
        return parsed
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def _parse_installed_config(path: str) -> dict:
    """Parse the small Continue MCP YAML surface emitted by this installer.

    Keeping the installer stdlib-only avoids making installation itself depend
    on a server venv. Inline args use JSON, which is also valid YAML.
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    config: dict = {"env": {}}
    env_indent = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if env_indent is not None:
            if indent > env_indent and ":" in stripped:
                key, value = stripped.split(":", 1)
                if not key.startswith("#"):
                    config["env"][key.strip()] = _yaml_scalar(value)
                continue
            env_indent = None
        normalized = stripped[2:].lstrip() if stripped.startswith("- ") else stripped
        if ":" not in normalized:
            continue
        key, value = normalized.split(":", 1)
        key = key.strip()
        if key == "env":
            env_indent = indent
        elif key in {"command", "cwd"}:
            config[key] = _yaml_scalar(value)
        elif key == "args":
            parsed = json.loads(_strip_yaml_comment(value))
            if not isinstance(parsed, list) or not all(isinstance(v, str) for v in parsed):
                raise ValueError("args must be a JSON/YAML list of strings")
            config[key] = parsed
        elif key == "connectionTimeout":
            config[key] = int(_strip_yaml_comment(value))
    if not config.get("command"):
        raise ValueError("missing mcpServers command")
    if "args" not in config:
        raise ValueError("missing mcpServers args")
    return config


def _rg_status(names: list[str]) -> tuple[bool, str]:
    """Resolve ripgrep the way search-mcp's rg_bin() does — RIPGREP_BIN, then the
    search venv's bundled `rg` (the optional `rg` extra), then a system rg on PATH.
    Returns (ok, human detail). Only meaningful when search is being installed."""
    override = os.environ.get("RIPGREP_BIN")
    if override:
        return os.path.isfile(override), f"RIPGREP_BIN={override}"
    scripts = "Scripts" if os.name == "nt" else "bin"
    exe = "rg.exe" if os.name == "nt" else "rg"
    bundled = os.path.join(KIT_DIR, ".venv", scripts, exe)
    if os.path.isfile(bundled):
        return True, f"bundled ripgrep-bin ({bundled})"
    system = shutil.which("rg")
    if system:
        return True, f"system rg ({system})"
    return False, (
        "not found — search will fail. Fix with ONE of:\n"
        "         brew install ripgrep   (or apt/choco/your package manager)\n"
        "         uv tool install ripgrep-bin   (global prebuilt rg)\n"
        f"         uv sync --project {KIT_DIR} --extra rg\n"
        "         set RIPGREP_BIN=/abs/path/to/rg\n"
        "       ripgrep-bin is a third-party repackage of ripgrep's official "
        "binaries; prefer a system rg or RIPGREP_BIN to avoid it."
    )


def _load_gateway_downstreams(path: str) -> dict[str, dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        parsed = json.load(f)
    servers = parsed.get("servers") if isinstance(parsed, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("gateway config must contain a servers object")
    for name, spec in servers.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            raise ValueError("gateway server entries must be objects")
        if not isinstance(spec.get("command"), str):
            raise ValueError(f"gateway server {name!r} is missing command")
        args = spec.get("args")
        if not isinstance(args, list) or not all(isinstance(v, str) for v in args):
            raise ValueError(f"gateway server {name!r} args must be strings")
        env = spec.get("env", {})
        if not isinstance(env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError(f"gateway server {name!r} env must contain strings")
    return cast(dict[str, dict[str, Any]], servers)


def doctor(project: str, names: list[str], gateway: bool = False) -> int:
    """Verify the install: uv, shared project + venv, stamps, interpreters,
    interpreters, and a LIVE MCP handshake per server. Returns failure count."""
    failures = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        mark = "ok " if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures += 1

    uv = shutil.which("uv")
    print("doctor: toolkit checkout")
    check(bool(uv), "uv on PATH", uv or "not found — install https://docs.astral.sh/uv/")
    venv = os.path.join(KIT_DIR, ".venv")
    check(os.path.isfile(os.path.join(KIT_DIR, "pyproject.toml")),
          "unified toolkit project", KIT_DIR)
    check(os.path.isdir(venv), "shared toolkit venv",
          venv if os.path.isdir(venv) else "missing — run the installer (or uv sync)")

    if "shell" in names:
        print("doctor: shells for shell-mcp")
        found = detect_interpreters()
        check(bool(found), "interpreters detected",
              ", ".join(f"{k}={v}" for k, v in found.items()) or "none found")

    if "search" in names:
        print("doctor: ripgrep for search-mcp")
        rg_ok, rg_detail = _rg_status(names)
        check(rg_ok, "ripgrep resolves", rg_detail)

    print(f"doctor: project config ({project})")
    mcp_dir = os.path.join(project, ".continue", "mcpServers")
    configs: dict[str, dict] = {}
    config_names = [GATEWAY] if gateway else names
    for name in config_names:
        dest = os.path.join(mcp_dir, f"{name}.yaml")
        if not os.path.isfile(dest):
            check(False, f"{name}.yaml", "not installed")
            continue
        try:
            with open(dest, "r", encoding="utf-8") as f:
                content = f.read()
            config = _parse_installed_config(dest)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as e:
            check(False, f"{name}.yaml parses", str(e))
            continue
        stamped = "/absolute/path/to" not in content
        check(stamped, f"{name}.yaml stamped",
              dest if stamped else "unstamped placeholders — re-run the installer")
        workspace = config["env"].get("MCP_WORKSPACE")
        check(workspace == _slashes(project), f"{name}.yaml workspace",
              workspace or "MCP_WORKSPACE missing")
        configs[name] = config

    downstreams: dict[str, dict[str, Any]] = {}
    if gateway and GATEWAY in configs:
        expected_config = _slashes(os.path.join(project, GATEWAY_CONFIG_REL))
        configured_path = configs[GATEWAY]["env"].get("GATEWAY_CONFIG")
        check(configured_path == expected_config, "gateway.yaml downstream config",
              configured_path or "GATEWAY_CONFIG missing")
        if configured_path == expected_config:
            try:
                downstreams = _load_gateway_downstreams(configured_path)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as e:
                check(False, "gateway.config.json parses", str(e))
            else:
                check(set(downstreams) == set(names), "gateway downstream selection",
                      ", ".join(sorted(downstreams)))
                for name, spec in downstreams.items():
                    expected_project = _slashes(KIT_DIR)
                    expected_args = ["run", "--no-sync", "--project",
                                     expected_project, f"{name}-mcp"]
                    check(os.path.isabs(spec["command"]), f"gateway {name} command absolute",
                          spec["command"])
                    check(spec["args"] == expected_args, f"gateway {name} args stamped",
                          " ".join(spec["args"]))
                    workspace = spec.get("env", {}).get("MCP_WORKSPACE")
                    check(workspace == _slashes(project), f"gateway {name} workspace",
                          workspace or "MCP_WORKSPACE missing")

    print("doctor: live MCP handshake (initialize + tools/list, like Continue does)")
    for name in config_names:
        config = configs.get(name)
        if config is None:
            check(False, f"{name}-mcp responds", "skipped — invalid installed config")
            continue
        child_env = os.environ.copy()
        child_env.update(config["env"])
        timeout_ms = config.get("connectionTimeout", 30_000)
        handshake_kwargs: dict[str, Any] = {
            "env": child_env,
            "cwd": config.get("cwd"),
            "timeout": max(1.0, timeout_ms / 1000),
        }
        if gateway:
            handshake_kwargs["gateway_servers"] = tuple(names)
        ok, detail = _mcp_handshake(
            [config["command"], *config["args"]], **handshake_kwargs)
        check(ok, f"{name}-mcp responds", detail)
    return failures


def uninstall(project: str, names: list[str], all_selected: bool,
              gateway: bool = False) -> None:
    """Undo only files whose bytes still match the installation manifest."""
    manifest = _load_manifest(project)
    changed = 0
    retained = 0
    target_names = [GATEWAY] if gateway else names
    targets = [os.path.join(".continue", "mcpServers", f"{n}.yaml")
               for n in target_names]
    if gateway:
        targets.append(GATEWAY_CONFIG_REL)
    installed_servers = {
        rel.removeprefix(".continue/mcpServers/").removesuffix(".yaml")
        for rel in manifest["files"]
        if rel.startswith(".continue/mcpServers/") and rel.endswith(".yaml")
    }
    # Rules are shared. Remove them for a full uninstall, or when this operation
    # removes the last installed server (including a matching --only install).
    if installed_servers.issubset(target_names):
        targets += [os.path.join(".continue", "rules", r) for r in RULES]
    for target_rel_os in targets:
        rel = target_rel_os.replace(os.sep, "/")
        record = manifest["files"].get(rel)
        if not record:
            print(f"  retained   {os.path.join(project, target_rel_os)} (not in manifest)")
            retained += 1
            continue
        target = os.path.join(project, target_rel_os)
        if os.path.lexists(target):
            if os.path.islink(target) or not os.path.isfile(target):
                print(f"  retained   {target} (not a regular installer-owned file)")
                retained += 1
                continue
            with open(target, "rb") as f:
                current = f.read()
            if _sha256(current) != record["installed_sha256"]:
                print(f"  retained   {target} (modified since installation)")
                retained += 1
                continue
        previous = record.get("previous")
        if previous:
            backup = _from_manifest_rel(project, previous["backup"])
            try:
                with open(backup, "rb") as f:
                    original = f.read()
            except OSError as e:
                print(f"  retained   {target} (cannot read backup: {e})")
                retained += 1
                continue
            if _sha256(original) != previous["sha256"]:
                print(f"  retained   {target} (backup hash mismatch)")
                retained += 1
                continue
            _atomic_write(target, original, previous.get("mode"))
            os.unlink(backup)
            print(f"  restored   {target}")
        elif os.path.lexists(target):
            os.unlink(target)
            _fsync_dir(os.path.dirname(target))
            print(f"  removed    {target}")
        else:
            print(f"  absent     {target}")
        del manifest["files"][rel]
        changed += 1
        _save_manifest(project, manifest)
    if not manifest["files"]:
        try:
            os.unlink(_manifest_path(project))
            _fsync_dir(os.path.dirname(_manifest_path(project)))
        except FileNotFoundError:
            pass
        for rel in reversed(manifest.get("created_dirs", [])):
            try:
                os.rmdir(_from_manifest_rel(project, rel))
            except OSError:
                pass
    print(f"Uninstalled {changed} file(s); retained {retained}. The toolkit "
          "checkout and its shared venv are untouched.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install the MCP toolkit into a project.")
    ap.add_argument("project", help="path to the project (workspace root)")
    ap.add_argument("--only", default="",
                    help=f"comma-separated subset of: {','.join(SERVERS)}")
    ap.add_argument("--gateway", action="store_true",
                    help="register one progressive-disclosure gateway; --only "
                         "selects its downstream servers")
    ap.add_argument("--no-sync", action="store_true",
                    help="skip `uv sync` of the shared toolkit project")
    ap.add_argument("--check", action="store_true",
                    help="doctor mode: verify venvs, stamps, interpreters, and a "
                         "live MCP handshake per server; installs nothing")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove the installed yamls/rules from the project")
    args = ap.parse_args(argv)

    project = os.path.abspath(args.project)
    if not os.path.isdir(project):
        print(f"error: {project} is not a directory", file=sys.stderr)
        return 1
    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(SERVERS)
    unknown = sorted(set(names) - set(SERVERS))
    if unknown:
        print(f"error: unknown server(s) {unknown}; choose from {SERVERS}",
              file=sys.stderr)
        return 1

    if args.check:
        failures = doctor(project, names, args.gateway)
        if failures:
            print(f"\ndoctor: {failures} check(s) failed.", file=sys.stderr)
        else:
            print("\ndoctor: all checks passed.")
        return 1 if failures else 0

    if args.uninstall:
        try:
            uninstall(project, names, set(names) == set(SERVERS), args.gateway)
        except (OSError, UnicodeError, RuntimeError) as e:
            print(f"error: uninstall failed: {e}", file=sys.stderr)
            return 1
        return 0

    # Stamp uv's absolute path into `command:` so Continue doesn't rely on the
    # PATH it inherits. Fall back to bare "uv" (PATH lookup) only if uv isn't
    # found now — the sync step below will warn about that separately.
    uv = shutil.which("uv")
    if args.gateway and not uv:
        print("error: gateway installation requires uv on PATH so every stamped "
              "launcher can use its absolute path", file=sys.stderr)
        return 1
    uv_path = _slashes(uv) if uv else "uv"
    if not uv:
        print("WARNING: `uv` not found on PATH — leaving command: uv in the yaml "
              "(Continue must find uv on its own PATH).", file=sys.stderr)

    mode = f"gateway + {len(names)} downstream server(s)" if args.gateway else f"{len(names)} server(s)"
    print(f"Installing {mode} + {len(RULES)} rule(s) into {project}")
    try:
        install(project, names, uv_path, args.gateway)
    except (OSError, UnicodeError, RuntimeError) as e:
        print(f"error: install failed: {e}", file=sys.stderr)
        return 1

    failures = 0
    if args.no_sync:
        print("\nSkipping `uv sync` (--no-sync). Run it at the continue-mcp root "
              "before the servers will start.")
    else:
        sync_names = [GATEWAY, *names] if args.gateway else names
        failures = sync_deps(sync_names)

    if args.gateway:
        print("\nGateway installed. Continue should register only gateway.yaml for "
              "the selected downstream tools. Set gateway.search/describe to "
              "Automatic and gateway.call to Ask First.")
    else:
        print(policy_checklist(names))
    if failures:
        print("WARNING: the shared environment did not sync — see errors above.",
              file=sys.stderr)
        print(CORP_NOTE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
