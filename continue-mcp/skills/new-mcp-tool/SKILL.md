---
name: new-mcp-tool
description: >
  Generate a new pure-Python FastMCP tool from a one-line spec, run its tests,
  and emit the Continue wiring. Use when the user wants to add a capability to
  their MCP toolkit. This is the "tool factory" — software that builds more
  tools for itself.
---

# new-mcp-tool — the tool factory

You are a **tool factory**. Given a short spec ("a tool that runs a Snowflake
query and returns rows as markdown"), you produce a working FastMCP server
shaped exactly like the existing ones in `continue-mcp/` (search-mcp is the
reference implementation), prove it with a test, and hand back everything
needed to wire it into Continue. The toolkit grows itself: you run tests
through the `shell` MCP, and you register each new tool where the next
generation can discover it.

## Non-negotiable rules

1. **Token discipline.** Every tool description is **≤ 2 sentences and ≤ ~80
   tokens**. The agent pays for these on *every* request. Terse names, minimal
   JSON schema, no prose padding. This is the whole point — do not regress it.
2. **One flexible tool beats three narrow ones.** Prefer a single tool with a
   couple of optional args over a family of near-duplicates.
3. **Pure Python only.** Every server is pure Python: hatchling build backend,
   `fastmcp` dependency, run with `uv`. No compiled extensions, no
   maturin/Rust. If a hot path needs native speed, shell out to a proven
   binary the way search-mcp shells out to `rg`.
4. **Ships with a test, and you run it.** Every tool gets a golden test; you
   run it via `shell.run` before declaring done. If it fails, fix and re-run.
5. **Register it in the right place — never both.** Decide by
   `schema_size × (1 − usage)` (see `continue-mcp-token-strategy.md`):
   - **Hot tool** (used most messages) → register its
     `.continue/mcpServers/<name>.yaml` directly with Continue.
   - **Tail tool** (occasional) → add a server block to
     `gateway-mcp/gateway.config.json` so the gateway discloses it on demand.
     A tail server must **not** also be registered directly with Continue —
     that double-loads its schemas.

## Procedure

1. **Clarify (2–3 questions max).** Inputs? Outputs (the shape the agent
   sees)? Side effects / safety? Hot or tail (rule 5)?
2. **Scaffold** this layout (mirror search-mcp; note the `<name>-mcp` /
   `<name>_mcp` naming convention):
   ```
   <name>-mcp/
     pyproject.toml               # hatchling; template below
     <name>_mcp/__init__.py
     <name>_mcp/server.py         # FastMCP() + @mcp.tool functions
     tests/conftest.py            # sys.path shim (copy from any sibling)
     tests/test_tools.py          # golden tests
     .continue/mcpServers/<name>.yaml
     README.md                    # one paragraph + the tool list
   ```
   Keep pure logic (parsers, matchers, rankers) in its own stdlib-only module
   next to `server.py` so it's unit-testable without MCP — like
   `edit_mcp/matcher.py` and `gateway_mcp/registry.py`.
3. **Write the tool(s).** Each `@mcp.tool` is one decorated async function.
   The docstring becomes the description the model sees — enforce rule 1 on it.
4. **Test through our own shell MCP** (the ouroboros step):
   `shell.run("cd <name>-mcp && uv run --extra test pytest -q")`.
   Iterate until green.
5. **Emit wiring + policy steps.** Print the `.continue/mcpServers/<name>.yaml`
   and the exact tool-policy instructions ("set `<name>.*` to Ask First /
   Automatic; if it replaces a built-in, set that built-in to Excluded").
6. **Register for discovery** per rule 5: hot → tell the user to drop the yaml
   into their workspace's `.continue/mcpServers/`; tail → append to the
   `servers` object in `gateway-mcp/gateway.config.json`:
   ```json
   "<name>": { "command": "uv", "args": ["run", "<name>-mcp"], "cwd": "../<name>-mcp" }
   ```
   (Server names must not contain `_`. The gateway picks it up on next start.)

## Template: pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "<name>-mcp"
version = "0.1.0"
description = "<one line>"
requires-python = ">=3.11"
dependencies = ["fastmcp>=2"]

[project.optional-dependencies]
test = ["pytest>=8"]

[project.scripts]
<name>-mcp = "<name>_mcp.server:main"

[tool.hatch.build.targets.wheel]
packages = ["<name>_mcp"]
```

## Template: a minimal tool

```python
from fastmcp import FastMCP

mcp = FastMCP("<name>")

@mcp.tool
async def do_thing(target: str, mode: str = "default") -> dict:
    """<= 2 sentences. What it does + what it returns. That's the whole budget."""
    ...
    return {"ok": True, "result": ...}

def main() -> None:
    mcp.run()  # stdio transport

if __name__ == "__main__":
    main()
```

## Output format

Return: the file tree, each file's contents, the test result (pass/fail with
the last lines of output), the Continue wiring block, and where you registered
it (direct yaml or the `gateway.config.json` entry). Nothing else — no
narration.
