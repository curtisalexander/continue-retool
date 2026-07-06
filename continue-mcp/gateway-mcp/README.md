# gateway-mcp — progressive disclosure for your MCP toolkit

One MCP server that hides **many** tools behind **three** meta-tools, so Continue
pays for ~3 tool schemas at rest instead of N. This is Anthropic's Tool Search /
progressive-disclosure pattern, reproduced locally so it works with any model.

---

## 1. Purpose — why this exists

Every MCP tool Continue loads costs **~550–1,400 tokens** for its name, description,
and JSON schema, and they're loaded **up front on every request**. A handful of MCP
servers can eat **30k–67k tokens before you type anything** — a third of a 200k
window gone to tool *definitions*. As your toolkit grows (the whole point of the
"infinite expansion" plan), this resting cost grows with it.

The gateway breaks that link. Instead of advertising all N tools, it advertises
three:

| Meta-tool | Step | Returns |
|---|---|---|
| `gateway.search(query)` | 1 — discover | a shortlist of `{name, summary}` — **not** schemas |
| `gateway.describe(name)` | 2 — load schema | the full JSON argument schema for **one** tool |
| `gateway.call(name, arguments)` | 3 — run | the tool's result, injected like a native tool |

Now the model **discovers** a tool by intent, **loads one schema** on demand, and
**runs it** — and Continue's resting context holds only the three gateway schemas.
Add 50 more tools and the resting cost doesn't move.

### The honest tradeoff: head vs. tail

The gateway trades **one round-trip** (search → describe → call) for **near-zero
resting cost**. That trade is:

- **Great for the long tail** — the many tools you use occasionally. Paying a
  discovery hop the rare time you need `sql.format` or `repo.symbols` is cheap;
  paying their schema cost on *every* message is not.
- **Not great for the hot head** — the 2–3 tools you use every single message
  (`edit.edit`, `shell.run`, `search.grep`). Making the model search→describe→call
  to do the thing it does constantly adds latency and reasoning overhead.

**Recommended architecture: hybrid.**
- Keep the **hot head** as direct MCP servers registered with Continue (their
  schema cost is worth paying because you use them constantly).
- Put the **long tail** behind the gateway (near-zero resting cost, small per-use
  hop).

The gateway can aggregate *any* servers — including shell/search/edit if you want
maximum token savings and accept the hop. Start by putting only the tail behind it;
promote a tool to direct registration the moment its per-hop latency annoys you.

> Rule of thumb from the toolkit doc: **< 5 tools, skip the gateway. 20+ tools,
> the gateway wins.** In between, put the tail behind it and keep the head direct.

---

## 2. Design — how it works

```text
        ┌──────────────┐
        │ Continue     │─────────────┐  connects to ONLY the gateway
        └──────────────┘             v  (3 schemas at rest)
                            ┌───────────────────────┐
                            │  gateway-mcp (this)   │
                            │  ┌─────────────────┐  │
                            │  │ in-mem catalog  │  │ built at startup from
                            │  │ name->summary,  │  │ each downstream's
                            │  │ schema, server  │  │ tools/list
                            │  └─────────────────┘  │
                            │  search / describe /  │
                            │  call  (MCP *server*) │
                            │                       │
                            │  MCP *client* to v v v│
                            └───┬───────┬───────┬───┘
                                │ stdio │ stdio │ stdio
                                v       v       v
                           shell-mcp   search-mcp  edit-mcp   … (any number)
```

**The gateway is both an MCP server and an MCP client.** To Continue it's a server
with three tools. Internally it's a client that spawns and connects to your
downstream stdio servers.

**Lifecycle.** On startup (`lifespan`), the gateway reads `gateway.config.json`,
connects to every downstream server, calls each one's `tools/list`, and builds an
in-memory **catalog**: `display_name → {server, raw tool name, one-line summary,
full schema, keywords}`. The connections stay open for the gateway's lifetime and
are closed on shutdown. The gateway owns the downstream processes.

**Naming.** Downstream tools are exposed as `"<server>.<tool>"` (e.g.
`shell.start`, `search.grep`). Server names must not contain `_` so routing is
unambiguous. `call("shell.start", …)` maps back to server `shell`, tool `start`.

**The three tools (`registry.py` holds the pure logic):**
- **search** ranks the catalog against the query — substring hits on the name score
  highest, then token overlap with name/keywords, then with the summary — and
  returns only `{name, summary}`. An empty query lists everything. This is the only
  step that must be cheap in tokens, and it is.
- **describe** returns the full description + JSON schema for one resolved name
  (case-insensitive; suggests close names on a typo).
- **call** routes to the owning downstream client, invokes the real tool, and
  returns its payload **faithfully** (structured data if present, else text) so
  Continue injects it exactly like a native tool result. Downstream errors are
  returned as `{error: …}` rather than crashing the gateway.

**Why `registry.py` is separate and dependency-free:** the valuable, testable logic
(catalog, ranking, summarizing) has no MCP or network in it, so it's unit-tested in
isolation.

---

## 3. Use

### Configure the downstream servers

Edit `gateway.config.json` (or point `GATEWAY_CONFIG` at another file):

```json
{
  "servers": {
    "shell":  { "command": "uv", "args": ["run", "shell-mcp"],  "cwd": "../shell-mcp" },
    "search": { "command": "uv", "args": ["run", "search-mcp"], "cwd": "../search-mcp" },
    "edit":   { "command": "uv", "args": ["run", "edit-mcp"],   "cwd": "../edit-mcp" }
  }
}
```

Use **absolute** `cwd` paths if you'll launch the gateway from outside this folder.

### Install and run

```bash
cd gateway-mcp
uv run pytest        # ranking/catalog tests (pure stdlib, no downstream servers needed)
uv run gateway-mcp   # starts the gateway; it spawns + connects the downstream servers
```

### Wire it into Continue — and register ONLY the gateway

1. Copy `.continue/mcpServers/gateway.yaml` into your workspace's `.continue/mcpServers/`.
2. **Do NOT also add `shell.yaml` / `search.yaml` / `edit.yaml` to Continue.** The
   gateway connects to those itself; registering them with Continue too would reload
   all their schemas and defeat the entire purpose.
3. Exclude the built-in Continue tools you're replacing (`run_terminal_command`,
   Grep/Glob search, Edit/Create file) as before — their replacements now live
   behind the gateway.
4. Set `gateway.search` / `gateway.describe` → **Automatic** (read-only, cheap) and
   `gateway.call` → **Ask First** until you trust it (it can reach write tools).

### The flow the model follows

```jsonc
// 1. discover
gateway.search({ "query": "replace text in a file" })
//   -> { tools: [ { name: "edit.edit", summary: "Replace old_string with new_string…" }, … ] }

// 2. load one schema
gateway.describe({ "name": "edit.edit" })
//   -> { input_schema: { properties: { path, old_string, new_string, replace_all } … } }

// 3. run it
gateway.call({ "name": "edit.edit",
               "arguments": { "path": "a.py", "old_string": "foo", "new_string": "bar" } })
//   -> the edit tool's result, injected natively
```

The gateway's own tool descriptions tell the model to follow this 1-2-3 order, and
the server ships an `instructions` string reinforcing it.

---

## 4. Token math (a concrete example)

Say shell (6 tools) + search (2) + edit (3) = **11 tools**, averaging ~900 tokens of
schema each ≈ **~9,900 tokens at rest**, every request.

Behind the gateway: **3 meta-tool schemas ≈ ~1,200 tokens at rest.** The model pays
the per-tool schema (~900 tokens) only via `describe`, only for the tool it's about
to use, only when it uses it. Net: **~85% less resting tool-definition context** —
matching the published Tool Search numbers — freeing that window for actual code.

---

## 5. Extending & alternatives

- **Add a server:** drop another entry in `gateway.config.json`. Its tools appear as
  `newserver.tool` and are searchable immediately — zero added resting cost. This is
  the "infinite expansion" property: the toolkit grows without the prompt growing.
- **Native Tool Search:** if your Continue build supports the MCP/Anthropic Tool
  Search flow directly, you may get this for free — check first. The gateway
  reproduces it for builds that don't.
- **In-process variant:** instead of spawning downstream servers, the gateway could
  `import` tool modules directly (one process, no child MCP servers). Lighter, but
  loses the "each tool is its own installable MCP" modularity. The aggregator design
  here matches your existing separate servers.
- **Promotion (hybrid):** to skip the hop for a hot tool, register that tool's MCP
  server directly with Continue *in addition to* the gateway, and drop it from the
  gateway config so it isn't offered twice.

## 6. Caveats

- **Round-trip latency:** search→describe→call is up to three model turns before the
  work happens. That's the cost of near-zero resting tokens. Keep the head direct.
- **FastMCP version:** the client wiring targets FastMCP 2.x (`Client` +
  `StdioTransport`). If your installed version constructs downstream clients
  differently, that's the only spot in `server.py` to adjust (`_connect`).
- **Startup cost:** the gateway spawns all downstream servers at startup to build the
  catalog. For a very large tail, consider lazy connection or a persisted catalog
  snapshot (noted in the code as a future option).
