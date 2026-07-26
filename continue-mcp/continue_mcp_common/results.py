"""Consistent FastMCP result rendering."""

from __future__ import annotations

from fastmcp.tools import ToolResult
from mcp.types import TextContent


def fenced_block(content: str, lang: str = "") -> str:
    """Render content in a Markdown fence that cannot be closed by the content."""
    longest_run = 0
    current_run = 0
    for char in content:
        if char == "`":
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}{lang}\n{content}\n{fence}"


def result(summary: str, data: dict, block: str = "", lang: str = "") -> ToolResult:
    """Build a rendered summary and matching structured payload."""
    markdown = summary
    if block.strip():
        markdown += f"\n\n{fenced_block(block, lang)}"
    return ToolResult(
        content=[TextContent(type="text", text=markdown)],
        structured_content=data,
    )
