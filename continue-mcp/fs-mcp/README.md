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
- **Workspace-jailed (default ON).** Both tools run on Automatic, so paths are
  confined to `MCP_WORKSPACE` (realpath'd — symlinks can't tunnel out). A
  prompt-injected read of `~/.ssh/…` fails closed with a structured refusal.
  `MCP_JAIL_EXTRA` adds roots; `MCP_JAIL=0` disables. See the kit README.

## Setup

```bash
uv run --extra test pytest -q
uv run fs-mcp                   # run the server (stdio)
```

Then register `.continue/mcpServers/fs.yaml` with Continue, set the built-in
**Read file** and **List dir** to **Excluded**, and `fs.*` to **Automatic**
(both tools are read-only).

Relative paths resolve against `MCP_WORKSPACE` (stamped into the yaml by the
installer), falling back to the server's cwd — so they mean your project, not
wherever Continue happened to launch the process.
