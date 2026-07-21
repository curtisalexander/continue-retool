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

Both tools are **workspace-jailed by default**: the search path must live under
`MCP_WORKSPACE` (realpath'd), because `search.*` runs on Automatic — no human
approves each call. `MCP_JAIL_EXTRA` adds roots; `MCP_JAIL=0` disables. See
the kit README for the full policy story.

## 1. Provide the `rg` binary (the thing we call out to)

This MCP does **not** install ripgrep by default — it calls the `rg` binary that
the server resolves at runtime, in this order: **`RIPGREP_BIN`** (an explicit
path), then **`rg` on your PATH**. Pick whichever fix suits you:

```bash
# A. A system rg — the simplest, nothing third-party:
brew install ripgrep            # or: apt install ripgrep / choco install ripgrep

# B. A global prebuilt rg via uv (works on Windows too; survives project re-syncs):
uv tool install ripgrep-bin     # puts `rg` on your PATH (e.g. ~/.local/bin/rg)

# C. Into the toolkit's shared venv, via the optional extra:
uv sync --project <toolkit>/continue-mcp --extra rg

rg --version                    # confirm whichever you chose is found
```

**Heads-up on B and C:** the `rg` extra pulls
[`ripgrep-bin`](https://pypi.org/project/ripgrep-bin/), a **third-party**
repackage (`Bing-su/pip-binary-factory`) of ripgrep's *official* release binaries
— convenient and pinned (`==15.2.0`), but not published by ripgrep's author. If
you'd rather not trust a repackager, use **A** or point `RIPGREP_BIN` at any rg
you already have. (The similarly named PyPI `ripgrep` package is **not** an
option here — it ships no Windows, Intel-Mac, or linux-arm64 wheel.)

If `rg` lives somewhere non-standard, pin it in `search.yaml`:
`RIPGREP_BIN: /path/to/rg`. When it can't find rg at all, the server raises an
error that lists every one of these fixes — and `install-workspace.py --check`
(the doctor) reports the same thing at install time, before your first search.

## 2. Install and run the MCP

```bash
cd <toolkit>/continue-mcp
uv run --extra test pytest -q search-mcp/tests  # integration tests need rg installed
uv run search-mcp                            # Continue launches this for you
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
- **Long lines can't crash or flood.** `rg --json` emits the *whole* matching
  line (and `--max-columns` is ignored in JSON mode), so a match in minified JS or
  a one-line lockfile once overran asyncio's stream buffer and crashed the tool.
  Now the buffer is raised to `SEARCH_MCP_MAX_RECORD` (default 8MB) and each line
  is clipped to `SEARCH_MCP_MAX_LINE_CHARS` (default 500, `line_clipped` flags it);
  a record past even 8MB degrades to a partial result, never a traceback.
- **`context` and `multiline`** map to `rg -C` and `rg --multiline --multiline-dotall`.
- **Timeout** (`SEARCH_MCP_TIMEOUT`, default 30s) kills a runaway search and returns
  whatever was collected with `timed_out: true`.
- **Shelling out to `rg` is the design**, not a stopgap: the toolkit is pure Python
  by decision (see the records in `../../docs/adr/`), and
  CPU-heavy work belongs in proven native binaries invoked as subprocesses.
