#!/usr/bin/env python3
"""Install the selected continue-mcp servers into one Continue workspace."""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(KIT_DIR))
from continue_mcp_common.metadata import load_servers  # noqa: E402

SERVERS = {item["name"]: item for item in load_servers()}
DEFAULT_SERVERS = tuple(item["name"] for item in load_servers() if item["default"])


def _quote(value: str) -> str:
    """Render a JSON-style scalar, valid YAML and safe for arbitrary paths."""
    import json
    return json.dumps(value, ensure_ascii=False)


def render_config(name: str, uv: str, workspace: Path) -> str:
    args = ["run", "--no-sync", "--project", str(KIT_DIR), f"{name}-mcp"]
    rendered_args = ", ".join(_quote(value) for value in args)
    return (
        f"name: {name}\nversion: 0.0.1\nschema: v1\nmcpServers:\n"
        f"  - name: {name}\n    command: {_quote(uv)}\n"
        f"    args: [{rendered_args}]\n    connectionTimeout: 120000\n"
        f"    env:\n      MCP_WORKSPACE: {_quote(str(workspace))}\n"
    )


def expected_outputs(project: str | os.PathLike[str], selected: list[str], uv: str) -> dict[Path, str]:
    project_path = Path(project)
    if not project_path.is_dir():
        raise RuntimeError(f"project does not exist or is not a directory: {project_path}")
    workspace = project_path.resolve()
    return {
        workspace / ".continue" / "mcpServers" / f"{name}.yaml":
            render_config(name, os.path.abspath(uv), workspace)
        for name in selected
    }


def sync_deps(uv: str) -> None:
    subprocess.run([uv, "sync", "--locked", "--project", str(KIT_DIR)], check=True)


def _preflight(outputs: dict[Path, str], workspace: Path) -> set[Path]:
    """Reject unsafe or conflicting targets before sync or file creation."""
    differing: list[Path] = []
    existing: set[Path] = set()
    for path, text in outputs.items():
        try:
            target = path.lstat()
        except FileNotFoundError:
            target = None
        if target is not None:
            existing.add(path)
            if not stat.S_ISREG(target.st_mode):
                raise RuntimeError(f"refusing non-regular output file: {path}")
            if path.read_bytes() != text.encode("utf-8"):
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
    created: list[Path] = []
    try:
        for path, text in outputs.items():
            if path in existing:
                print(f"unchanged {path}")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8", newline="\n") as output:
                created.append(path)
                output.write(text)
            print(f"created {path}")
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise


async def _handshake(name: str, uv: str, workspace: Path) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=uv,
        args=["run", "--no-sync", "--project", str(KIT_DIR), f"{name}-mcp"],
        env={**os.environ, "MCP_WORKSPACE": str(workspace)},
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
        asyncio.run(_handshake(name, os.path.abspath(uv), workspace))
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
