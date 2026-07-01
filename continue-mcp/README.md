# continue-mcp — starter kit

Code sketches that accompany [`../continue-mcp-toolkit.md`](../continue-mcp-toolkit.md).
These are **starting points**, not finished software — they show the shape of the
background-job shell MCP, the maturin packaging seam, the Continue wiring, and the
tool-factory skill. Read the design doc first.

```
continue-mcp/
  hello-mcp/                    # START HERE: proves MCP is enabled in your build
    hello_mcp/server.py        # ping -> "pong", echo, whoami
    tests/test_hello.py
    .continue/mcpServers/hello.yaml
  shell-mcp/                    # the flagship: a terminal-runner MCP
    pyproject.toml             # maturin backend + fastmcp; console-script entry
    shell_mcp/server.py        # async, background-job model, tree-kill, timeout
    tests/test_tools.py        # golden tests incl. cross-platform tree-kill
    .continue/mcpServers/shell.yaml
  search-mcp/                   # ripgrep search, replaces built-in Grep/Glob search
    search_mcp/server.py       # shells out to `rg`; compact structured results
    tests/test_search.py       # arg-builder unit tests + rg integration tests
    .continue/mcpServers/search.yaml
  edit-mcp/                     # Unicode-robust edit, replaces built-in Edit/Create file
    edit_mcp/matcher.py        # exact->fuzzy match (NFKC, quotes, dashes, spaces, CRLF/BOM)
    edit_mcp/server.py         # edit / multi_edit / create_file tools
    tests/test_matcher.py      # 30+ non-ASCII golden tests (pure stdlib)
    .continue/mcpServers/edit.yaml
  gateway-mcp/                  # progressive disclosure: many tools behind 3 meta-tools
    gateway_mcp/registry.py    # pure catalog + ranking (search which tool you need)
    gateway_mcp/server.py      # search / describe / call; MCP client to downstream servers
    gateway.config.json        # lists the downstream servers to aggregate
    tests/test_registry.py     # ranking/catalog tests (pure stdlib)
    .continue/mcpServers/gateway.yaml
  skills/
    new-mcp-tool/SKILL.md      # the "ouroboros" tool factory
```

## Order of operations

```bash
# 0. Prove MCP is on (5 minutes). Wire hello.yaml, ask the agent to call `ping`.
cd hello-mcp && uv run pytest && uv run hello-mcp

# 1. The flagship terminal MCP. Run the golden suite (incl. the tree-kill test).
cd ../shell-mcp && uv run pytest && uv run shell-mcp

# 2. ripgrep search MCP. Install the rg binary (PyPI), then run its tests.
uv tool install ripgrep
cd ../search-mcp && uv run pytest && uv run search-mcp

# 3. Unicode-robust edit MCP (fixes non-ASCII match failures).
cd ../edit-mcp && uv run pytest && uv run edit-mcp

# 4. (Optional) Gateway: collapse many tools to 3 schemas at rest.
cd ../gateway-mcp && uv run pytest && uv run gateway-mcp
```

Then in Continue: add each `.continue/mcpServers/*.yaml` and set tool policies:
- built-in **`run_terminal_command`** → Excluded; `shell.*` → Ask First
- built-in **Grep search** and **Glob search** → Excluded; `search.*` → Automatic
- built-in **Edit file** and **Create file** → Excluded; `edit.*` → Ask First

**Using the gateway?** Register ONLY `gateway.yaml` with Continue (not the
downstream `shell/search/edit` yamls — the gateway connects to those itself). It's
best for the long tail of tools; keep the 2–3 you use every message registered
directly. See `gateway-mcp/README.md` for the head/tail tradeoff.
</content>
