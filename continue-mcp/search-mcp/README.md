# search-mcp — ripgrep search, replacing Continue's built-in search

Replaces Continue's built-in **Grep search** and **Glob search** tools with an MCP
that shells out to [ripgrep](https://github.com/BurntSushi/ripgrep). You get native
speed, gitignore-awareness for free, terse structured results (fewer tokens than the
built-in tool prompts), and a hard result cap so a broad query can't flood the
context window.

| Tool | Replaces | Returns |
|---|---|---|
| `search.grep(pattern, …)` | Grep search | matching lines as `{file, line, column, text}` |
| `search.files(glob, …)` | Glob search | file paths matching a glob |

## 1. Install the `rg` binary (the thing we call out to)

This MCP does **not** bundle ripgrep — it calls the `rg` binary on your PATH.
ripgrep is published on PyPI, so install it as a `uv` tool (no raw-binary install,
no Rust toolchain needed):

```bash
uv tool install ripgrep      # puts `rg` on your PATH (e.g. ~/.local/bin/rg)
rg --version                 # confirm it's found
```

If `rg` lives somewhere non-standard, pin it explicitly in `search.yaml`:
`RIPGREP_BIN: /path/to/rg`. The server raises a clear "install it with
`uv tool install ripgrep`" error if it can't find the binary.

## 2. Install and run the MCP

```bash
cd search-mcp
uv run pytest        # unit tests always run; integration tests need rg installed
uv run search-mcp    # starts the stdio server (Continue launches this for you)
```

## 3. Wire it into Continue and retire the built-ins

1. Copy `.continue/mcpServers/search.yaml` into your workspace's `.continue/mcpServers/`.
2. In Agent-mode tool settings:
   - set built-in **Grep search** → **Excluded**
   - set built-in **Glob search** → **Excluded**
   - set **`search.grep`** and **`search.files`** → **Automatic** (they're
     read-only, so auto-running them is safe and keeps the agent fast)

Now the agent reaches for `rg` instead of Continue's built-in search.

## Usage examples (what the agent calls)

```jsonc
search.grep({ "pattern": "TODO|FIXME", "glob": ["*.py"] })
search.grep({ "pattern": "def \\w+", "path": "src", "max_results": 50 })
search.grep({ "pattern": "start.*end", "multiline": true, "context": 2 })
search.files({ "glob": ["*.ts", "!**/dist/**"] })
```

## Notes

- **Result shape is deliberately compact.** Each hit is one small object; broad
  searches are capped (`SEARCH_MCP_MAX_RESULTS`, default 1000) and flagged
  `truncated: true` so the model knows to narrow the query rather than assume it
  saw everything.
- **`context` and `multiline`** map to `rg -C` and `rg --multiline --multiline-dotall`.
- **Timeout** (`SEARCH_MCP_TIMEOUT`, default 30s) kills a runaway search and returns
  whatever was collected with `timed_out: true`.
- **Want in-process search with no separate binary?** Swap the subprocess calls for
  a PyO3 library built on BurntSushi's `grep`/`ignore` crates (maturin
  `bindings = "pyo3"`) — same result shape, no PATH dependency. Shelling out to the
  PyPI `rg` is simpler and is the default here.
