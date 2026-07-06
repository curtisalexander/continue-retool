"""A tiny downstream MCP server the gateway tests spawn over stdio.
Not a test file — a fixture. Two tools with distinct, searchable names."""
from fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool
async def upper(text: str) -> str:
    """Uppercase the given text and return it."""
    return text.upper()


@mcp.tool
async def add(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


if __name__ == "__main__":
    mcp.run()  # stdio transport
