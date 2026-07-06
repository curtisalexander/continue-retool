"""
gateway-mcp — one MCP server that hides many tools behind three meta-tools.

Continue connects to ONLY this gateway. The gateway is itself an MCP *client* to
your downstream servers (shell, search, edit, …), aggregates their tool catalogs,
and exposes just three tools:

    gateway.search(query)          -> lightweight {name, summary} shortlist   (step 1)
    gateway.describe(name)         -> full JSON schema for one tool           (step 2)
    gateway.call(name, arguments)  -> run it; result is injected natively      (step 3)

Net effect: Continue pays for 3 tool schemas at rest instead of N, and the model
loads a real tool's schema only when it needs it — Anthropic's Tool Search /
progressive-disclosure pattern, reproduced locally so it works with any model.

Config: gateway.config.json (or $GATEWAY_CONFIG) lists the downstream servers.
See README.md for the purpose/design/use writeup and the head/tail tradeoff.

NOTE: exact FastMCP client symbols move between versions. This targets FastMCP 2.x
(Client + StdioTransport). If your installed version differs, the only thing to
adjust is how a downstream client is constructed in `_connect`.
"""
from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Optional

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport

from .registry import build_catalog, rank_tools

INSTRUCTIONS = (
    "This server exposes many tools behind three meta-tools. To use ANY capability: "
    "1) call search(query) to find the tool, 2) call describe(name) to get its "
    "argument schema, 3) call call(name, arguments) to run it. Do not guess tool "
    "names or arguments — discover them via search/describe first."
)


class _State:
    clients: dict = {}   # server name -> connected FastMCP Client
    catalog = None       # registry.Catalog


STATE = _State()


def _load_config() -> tuple[dict, str]:
    """Returns (config, base_dir). Relative `cwd` entries in the config resolve
    against the config file's own directory, as the file documents."""
    path = os.environ.get("GATEWAY_CONFIG") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway.config.json"
    )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f), os.path.dirname(os.path.abspath(path))


def _connect(spec: dict, base_dir: str) -> Client:
    """Build a client to one downstream stdio MCP server. Using a direct transport
    (not the multi-server mcpServers wrapper) keeps tool names unprefixed."""
    cwd = spec.get("cwd")
    if cwd and not os.path.isabs(cwd):
        cwd = os.path.join(base_dir, cwd)
    transport = StdioTransport(
        command=spec["command"],
        args=spec.get("args", []),
        env=spec.get("env"),
        cwd=cwd,
    )
    return Client(transport)


@asynccontextmanager
async def lifespan(_app):
    """On startup: connect to every downstream server, build the catalog, keep the
    connections open for the gateway's lifetime. On shutdown: close them all."""
    config, base_dir = _load_config()
    async with AsyncExitStack() as stack:
        clients: dict = {}
        raw: list[dict] = []
        for server, spec in config.get("servers", {}).items():
            if "_" in server:
                raise ValueError(f"server name {server!r} must not contain '_'")
            client = await stack.enter_async_context(_connect(spec, base_dir))
            clients[server] = client
            for t in await client.list_tools():
                raw.append({
                    "server": server,
                    "tool": t.name,
                    "description": getattr(t, "description", "") or "",
                    "input_schema": getattr(t, "inputSchema", None) or {},
                })
        STATE.clients = clients
        STATE.catalog = build_catalog(raw)
        yield
        STATE.clients = {}
        STATE.catalog = None


mcp = FastMCP("gateway", instructions=INSTRUCTIONS, lifespan=lifespan)


def _unwrap(result):
    """Return the downstream tool's payload faithfully so Continue injects it like a
    native tool result."""
    data = getattr(result, "data", None)
    if data is not None:
        return data
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None)
    if content:
        texts = [getattr(c, "text", None) for c in content]
        texts = [t for t in texts if t is not None]
        if texts:
            return "\n".join(texts)
    return result


# --- the three meta-tools --------------------------------------------------
@mcp.tool
async def search(query: str = "", limit: int = 15) -> dict:
    """STEP 1 of 3. Find tools by keyword/intent (e.g. 'run a command', 'search
    code', 'replace text in a file'). Returns a shortlist of {name, summary} — NOT
    full schemas. Then call describe(name) for the arguments. Empty query lists
    everything."""
    if STATE.catalog is None:
        return {"error": "catalog not ready"}
    hits = rank_tools(STATE.catalog, query, limit)
    return {
        "query": query,
        "count": len(hits),
        "tools": [{"name": e.name, "summary": e.summary} for e in hits],
        "next": "call describe(name) to get a tool's argument schema",
    }


@mcp.tool
async def describe(name: str) -> dict:
    """STEP 2 of 3. Get the full description + JSON argument schema for ONE tool
    (a name from search(), e.g. 'shell.start'). Use it to build the arguments for
    call()."""
    if STATE.catalog is None:
        return {"error": "catalog not ready"}
    e = STATE.catalog.resolve(name)
    if not e:
        return {"error": f"unknown tool {name!r}", "did_you_mean": STATE.catalog.suggest(name)}
    return {"name": e.name, "description": e.description, "input_schema": e.schema}


@mcp.tool
async def call(name: str, arguments: Optional[dict] = None) -> object:
    """STEP 3 of 3. Run a tool discovered via search()/describe(). `name` is like
    'shell.start'; `arguments` must match that tool's schema (see describe()). The
    tool's result is returned and injected into context just like a native tool."""
    if STATE.catalog is None:
        return {"error": "catalog not ready"}
    e = STATE.catalog.resolve(name)
    if not e:
        return {"error": f"unknown tool {name!r}; call search() first",
                "did_you_mean": STATE.catalog.suggest(name)}
    client = STATE.clients.get(e.server)
    if client is None:
        return {"error": f"downstream server {e.server!r} is not connected"}
    try:
        result = await client.call_tool(e.tool, arguments or {})
    except Exception as exc:  # surface downstream errors to the model, don't crash
        return {"error": f"call to {name} failed: {exc}"}
    return _unwrap(result)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
