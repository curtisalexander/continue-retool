# fs-mcp — line-ranged reads and directory listings

Replaces the built-in **Read file** and **List dir** tools. Built because the
stock read tool's behavior pushes the agent into writing throwaway
PowerShell/Python scripts to inspect files instead of just reading them — these
tools make the direct path the easy path.

## Tools

| Tool | What it does |
|---|---|
| `fs.read(path, start_line?, limit?, encoding?)` | Numbered lines (`42<TAB>text`), 1-based ranges, capped at 2000 lines **or 50KB**/call; optional explicit codec |
| `fs.list(path, depth?, include_hidden?)` | `{path, type, size}` entries, dirs first, capped at 500; `.git` always skipped |

Design points:

- **Paging built in.** Every read returns `truncated` and — when truncated —
  `next_start_line` (echoed into the output block too), so the agent knows the
  exact follow-up call that fetches the rest, no scripting needed. Line delivery
  stops after one look-ahead line proves another page exists instead of counting
  the rest. Encoding detection may scan a legacy file, but it uses bounded
  memory and never retains a multi-GB file. `total_lines` is exact only when
  `total_lines_exact` is true; otherwise it is null and
  `total_lines_at_least` reports the observed lower bound.
- **Hard caps that actually bind.** The line cap and the per-line cap *multiply*
  (2000 lines × 2000 chars is ~4MB), so on their own they don't bound the result
  — a merely *wide* file still floods the context. The 50KB total-byte cap
  (`FS_MCP_MAX_BYTES`) is the one that binds; `truncated_by` reports which limit
  hit first. Listings are capped at `FS_MCP_MAX_ENTRIES` output entries, 5,000
  scanned entries, and 20 levels of recursion. Unreadable entries produce
  bounded `errors` and an explicit `partial` result instead of aborting the
  tool call.
- **Binary files are refused, not mangled.** A NUL byte or a high-byte-dense,
  non-UTF-8 head means the file is returned as a structured error naming its
  size, instead of replacement-character mojibake the model can't identify.
  Legacy-encoded *text* is decoded with the same UTF-8/BOM, UTF-16 BOM,
  cp1252, then latin-1 policy used by editing and search. Results report the
  selected encoding and any decode loss.
  `encoding` bypasses detection for known external bytes (for example a shell
  spill) and uses replacement only when that explicitly selected codec sees
  malformed input; the result reports that loss.
- **Unicode-robust paths.** The same "looks identical but differs in bytes"
  problem `edit-mcp` fixes for file *content*, fixed for the *filename*: an
  NFC request finds an NFD file on disk (macOS stores decomposed), and macOS
  screenshot names (curly apostrophe, narrow NBSP before AM/PM) resolve even
  when the model types the plain-ASCII spelling.
- **Windows-friendly.** UTF-8 and UTF-16 BOMs are recognized, CRLF is displayed
  consistently, and cp1252 corporate files retain smart punctuation and accents.
- **Defense-in-depth workspace path scoping (default ON).** Paths are checked
  against realpath'd `MCP_WORKSPACE`, reducing accidental or prompt-injected
  access outside the project, including symlink escapes. This is not a sandbox
  or security boundary. `MCP_JAIL_EXTRA` adds roots and `MCP_JAIL=0` disables
  scoping; Automatic remains appropriate only for this read-only capability.

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
