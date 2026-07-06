# continue-mcp — starter kit

The code that accompanies [`../continue-mcp-toolkit.md`](../continue-mcp-toolkit.md):
the background-job shell MCP, the Continue wiring, and the tool-factory skill.
All servers are pure Python (hatchling + `uv`). Read the design doc first.

```
continue-mcp/
  hello-mcp/                    # START HERE: proves MCP is enabled in your build
    hello_mcp/server.py        # ping -> "pong", echo, whoami
    tests/test_hello.py
    .continue/mcpServers/hello.yaml
  shell-mcp/                    # the flagship: a terminal-runner MCP
    pyproject.toml             # hatchling + fastmcp; console-script entry
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
  fs-mcp/                       # line-ranged read + list dir, replaces built-in Read/List dir
    fs_mcp/server.py           # numbered line-ranged reads, depth-limited listings, hard caps
    tests/test_fs.py           # golden tests (paging, caps, BOM/CRLF, .git skip)
    .continue/mcpServers/fs.yaml
  sql-mcp/                      # SQL format/lint via sqruff (Rust binary from PyPI wheels)
    sql_mcp/server.py          # format / lint tools; stdin -> sqruff -> structured results
    sql_mcp/default.sqruff     # house style: snowflake, lowercase, leading commas
    tests/test_sql.py          # golden tests against the real sqruff binary
    .continue/mcpServers/sql.yaml
  notes-mcp/                    # repo-local agent memory: index + one .md file per note
    notes_mcp/server.py        # list/read/write/delete over <repo-root>/.continue-notes/
    tests/test_notes.py        # golden tests (repo-root discovery, name safety, hooks)
    .continue/mcpServers/notes.yaml
  rules/                        # Continue rules that make the toolkit work
    notes.md                   # discovery rule: consult notes at task start/end
    rule-rule.md               # the meta-rule: token discipline for authoring rules
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
cd hello-mcp && uv run --extra test pytest -q && uv run hello-mcp

# 1. The flagship terminal MCP. Run the golden suite (incl. the tree-kill test).
cd ../shell-mcp && uv run --extra test pytest -q && uv run shell-mcp

# 2. ripgrep search MCP. Install the rg binary (PyPI), then run its tests.
uv tool install ripgrep
cd ../search-mcp && uv run --extra test pytest -q && uv run search-mcp

# 3. Unicode-robust edit MCP (fixes non-ASCII match failures).
cd ../edit-mcp && uv run --extra test pytest -q && uv run edit-mcp

# 4. Line-ranged read / list dir (replaces built-in Read file & List dir).
cd ../fs-mcp && uv run --extra test pytest -q && uv run fs-mcp

# 5. SQL format/lint via sqruff (installs as a dependency — no extra step).
cd ../sql-mcp && uv run --extra test pytest -q && uv run sql-mcp

# 6. Repo-local agent memory (pair with rules/notes.md — see below).
cd ../notes-mcp && uv run --extra test pytest -q && uv run notes-mcp

# 7. (Optional) Gateway: collapse many tools to 3 schemas at rest.
cd ../gateway-mcp && uv run --extra test pytest -q && uv run gateway-mcp
```

Every package has two test layers, both deterministic (no LLM, no network):
golden tests of the implementation, and `tests/test_mcp_surface.py`, which
drives the server over the real MCP boundary with fastmcp's in-process client —
the same list-tools/call-tool flow Continue performs. CI runs all of it on
Linux, macOS, and Windows.

Then in Continue: add each `.continue/mcpServers/*.yaml` and set tool policies:
- built-in **`run_terminal_command`** → Excluded; `shell.*` → Ask First
- built-in **Grep search** and **Glob search** → Excluded; `search.*` → Automatic
- built-in **Edit file** and **Create file** → Excluded; `edit.*` → Ask First
- built-in **Read file** and **List dir** → Excluded; `fs.*` → Automatic
- `sql.*` → Automatic (replaces nothing; string-in/string-out only)
- `notes.*` → Automatic; also copy `rules/notes.md` into `.continue/rules/`
  (the rule is the discovery mechanism — without it, notes go unread)

## Install into a project (one command)

```bash
python3 install-workspace.py /path/to/your/project              # macOS/Linux
python install-workspace.py C:/path/to/your/project             # Windows
python3 install-workspace.py /path/to/proj --only shell,fs      # a subset
```

Copies every server's yaml into the project's `.continue/mcpServers/` with the
two absolute paths stamped in (`--project` → this checkout, `MCP_WORKSPACE` →
the project), copies the two rules into `.continue/rules/`, and prints the
tool-policy checklist. Re-running updates in place. Then ask the agent to call
`hello.ping` (MCP is on) and `hello.whoami` (shows the cwd and workspace the
servers actually see).

## Wiring: paths resolve against YOUR workspace, not the server's cwd

Every yaml uses the same pattern: `uv run --project <abs path to the package>`
pins the server's environment (no `cwd` needed for the server itself), and
`MCP_WORKSPACE` names your workspace root. All relative paths the agent passes
— and shell-mcp's default working directory — resolve against `MCP_WORKSPACE`,
falling back to the server's cwd if unset. Without this, relative paths would
resolve into this toolkit's checkout, not your project. The installer above
stamps all of this for you; `hello.whoami` reports what the servers actually
resolved, so you can verify any environment in one call.

**Using the gateway?** Register ONLY `gateway.yaml` with Continue (not the
downstream `shell/search/edit` yamls — the gateway connects to those itself). It's
best for the long tail of tools; keep the 2–3 you use every message registered
directly. See `gateway-mcp/README.md` for the head/tail tradeoff.
</content>
