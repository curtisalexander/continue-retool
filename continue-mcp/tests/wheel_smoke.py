"""Handshake with every packaged console script in an isolated environment."""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from continue_mcp_common.metadata import server_names

def executable(venv: Path, name: str) -> str:
    path = shutil.which(name, path=str(venv / ("Scripts" if os.name == "nt" else "bin")))
    if not path:
        raise RuntimeError(f"missing {name} console script")
    return path


def console(venv: Path, name: str) -> str:
    return executable(venv, f"{name}-mcp")

async def handshake(command: str, workspace: str) -> None:
    transport = StdioTransport(
        command=command,
        args=[],
        env={**os.environ, "MCP_WORKSPACE": workspace},
        keep_alive=False,
    )
    async with Client(transport, init_timeout=120, timeout=120) as client:
        await client.list_tools()

def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: wheel_smoke.py /path/to/venv")
    venv = Path(sys.argv[1]).resolve()
    install = executable(venv, "continue-mcp-install")
    # Package mode must work without uv or the original checkout on PATH.
    isolated_env = {**os.environ, "PATH": str(venv / ("Scripts" if os.name == "nt" else "bin"))}
    with tempfile.TemporaryDirectory() as workspace:
        subprocess.run([install, workspace, "--only", "fs"], env=isolated_env, check=True)
        subprocess.run([install, workspace, "--only", "fs", "--check"], env=isolated_env, check=True)
    with tempfile.TemporaryDirectory() as workspace:
        subprocess.run([install, workspace, "--with-edit", "--with-sql"], check=True)
        subprocess.run([install, workspace, "--with-edit", "--with-sql", "--check"], check=True)
        for name in server_names():
            asyncio.run(handshake(console(venv, name), workspace))
            print(f"ok {name}-mcp")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
