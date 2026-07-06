# fs-mcp — line-ranged reads and directory listings

Replaces the built-in **Read file** and **List dir** tools. Built because the
stock read tool's behavior pushes the agent into writing throwaway
PowerShell/Python scripts to inspect files instead of just reading them — these
tools make the direct path the easy path.

## Tools

| Tool | What it does |
|---|---|
| `fs.read(path, start_line?, limit?)` | Numbered lines (`42<TAB>text`), 1-based ranges, capped at 2000 lines/call |
| `fs.list(path, depth?, include_hidden?)` | `{path, type, size}` entries, dirs first, capped at 500; `.git` always skipped |

Design points:

- **Paging built in.** Every read returns `total_lines` and `truncated`, so the
  agent knows exactly what follow-up call fetches the rest — no scripting needed.
- **Hard caps everywhere.** A 500 MB log or a 10,000-entry directory can't
  flood the context window: lines are clipped at 2,000 chars, reads at
  `FS_MCP_DEFAULT_LIMIT` lines, listings at `FS_MCP_MAX_ENTRIES` entries.
- **Windows-friendly.** UTF-8 BOM stripped, CRLF handled, undecodable bytes
  replaced rather than erroring.

## Setup

```bash
uv run --extra test pytest -q
uv run fs-mcp                   # run the server (stdio)
```

Then register `.continue/mcpServers/fs.yaml` with Continue, set the built-in
**Read file** and **List dir** to **Excluded**, and `fs.*` to **Automatic**
(both tools are read-only).

Relative paths resolve against the server's working directory — set `cwd` in
the yaml to your workspace root, or have the agent use absolute paths.
