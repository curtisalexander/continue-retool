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
from fastmcp.tools import ToolResult

from continue_mcp_common.results import result as _result

mcp = FastMCP("hello")


@mcp.tool(annotations={"readOnlyHint": True})
async def ping() -> str:
    """Health check. Returns 'pong'. Use this to confirm MCP is enabled and this
    server is reachable from the agent."""
    return "pong"


@mcp.tool(annotations={"readOnlyHint": True})
async def echo(text: str) -> str:
    """Echo the given text back verbatim. Confirms arguments round-trip across the
    MCP boundary (schema in, result out)."""
    return text


@mcp.tool(annotations={"readOnlyHint": True})
async def whoami() -> ToolResult:
    """Report host OS/arch plus the server's cwd, MCP_WORKSPACE, and the base
    that relative paths resolve against. Run this first in any new environment."""
    workspace = os.environ.get("MCP_WORKSPACE")
    d = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cwd": os.getcwd(),
        "mcp_workspace": workspace,
        "resolved_base": os.path.abspath(workspace or os.getcwd()),
    }
    return _result(
        f"{d['system']} {d['release']} ({d['machine']}) · Python {d['python']}",
        d,
        block=(f"cwd            {d['cwd']}\n"
               f"MCP_WORKSPACE  {d['mcp_workspace']}\n"
               f"resolved base  {d['resolved_base']}"),
    )


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
