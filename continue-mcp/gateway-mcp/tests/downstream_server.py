"""A tiny downstream MCP server the gateway tests spawn over stdio.
Not a test file — a fixture with searchable tools and authority variants."""
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent

mcp = FastMCP("demo")


@mcp.tool
async def upper(text: str) -> str:
    """Uppercase the given text and return it."""
    return text.upper()


@mcp.tool
async def add(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


@mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True})
async def erase(target: str) -> dict:
    """Erase a target (test fixture; no actual side effect)."""
    return {"erased": target}


@mcp.tool(annotations={"openWorldHint": True})
async def execute(command: str) -> dict:
    """Represent open-world shell execution without executing anything."""
    return {"executed": command}


@mcp.tool
async def fail(reason: str) -> ToolResult:
    """Return a native downstream MCP error result for gateway passthrough tests."""
    return ToolResult(
        content=[TextContent(type="text", text=f"downstream failed: {reason}")],
        structured_content={"ok": False, "error": reason},
        meta={"origin": "fixture"},
        is_error=True,
    )


if __name__ == "__main__":
    mcp.run()  # stdio transport
