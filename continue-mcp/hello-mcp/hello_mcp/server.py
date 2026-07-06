"""
hello-mcp — the enablement check.

The single most important 5 minutes in this whole project: prove that MCP is
actually turned on in your reskinned corporate Continue build BEFORE you invest
in the shell/search/gateway MCPs. If `ping` returns "pong" from inside Continue's
agent, MCP works and the plan is viable. If it doesn't, stop and find out whether
IT disabled MCP or locked the .continue config — that's the one thing that can
invalidate everything downstream.

Run:  uv run hello-mcp
"""
from __future__ import annotations

import os
import platform

from fastmcp import FastMCP

mcp = FastMCP("hello")


@mcp.tool
async def ping() -> str:
    """Health check. Returns 'pong'. Use this to confirm MCP is enabled and this
    server is reachable from the agent."""
    return "pong"


@mcp.tool
async def echo(text: str) -> str:
    """Echo the given text back verbatim. Confirms arguments round-trip across the
    MCP boundary (schema in, result out)."""
    return text


@mcp.tool
async def whoami() -> dict:
    """Report host OS/arch plus the server's cwd, MCP_WORKSPACE, and the base
    that relative paths resolve against. Run this first in any new environment."""
    workspace = os.environ.get("MCP_WORKSPACE")
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cwd": os.getcwd(),
        "mcp_workspace": workspace,
        "resolved_base": os.path.abspath(workspace or os.getcwd()),
    }


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
