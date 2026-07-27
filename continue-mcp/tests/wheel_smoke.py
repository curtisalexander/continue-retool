"""Handshake with every packaged console script in an isolated environment."""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import shutil
import sys
import tempfile
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from continue_mcp_common.metadata import server_names

def console(venv: Path, name: str) -> str:
    path = shutil.which(f"{name}-mcp", path=str(venv / ("Scripts" if os.name == "nt" else "bin")))
    if not path:
        raise RuntimeError(f"missing {name}-mcp console script")
    return path

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
    with tempfile.TemporaryDirectory() as workspace:
        for name in server_names():
            asyncio.run(handshake(console(Path(sys.argv[1]).resolve(), name), workspace))
            print(f"ok {name}-mcp")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
