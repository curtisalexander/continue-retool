"""Consistent FastMCP result rendering."""

from __future__ import annotations

from fastmcp.tools import ToolResult
from mcp.types import TextContent


def result(summary: str, data: dict, block: str = "", lang: str = "") -> ToolResult:
    """Build a rendered summary and matching structured payload."""
    markdown = summary
    if block.strip():
        markdown += f"\n\n```{lang}\n{block}\n```"
    return ToolResult(
        content=[TextContent(type="text", text=markdown)],
        structured_content=data,
    )
