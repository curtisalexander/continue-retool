# fs-mcp — line-ranged reads and directory listings

Provides saved-disk reads and directory listings. It does not read VS Code's
unsaved document buffers; retain Continue's editor-aware read tools. See the
[migration guide](../CONTINUE_COMPATIBILITY.md).

## Tools

| Tool | What it does |
|---|---|
| `fs.read(path, start_line?, limit?, encoding?, start_column?, content_only?)` | Paged text, capped at 2000 lines and 50KiB of content/call; optional explicit codec and unnumbered content |
| `fs.list(path, depth?, include_hidden?)` | `{path, type, size}` entries, dirs first, capped at 500; `.git` always skipped |

Design points:

- **Paging built in.** Every read returns `truncated` and — when truncated —
  paired `next_start_line`/`next_start_column` (echoed into the output block too), so the agent knows the
  exact follow-up call that fetches the rest, including the remainder of a very
  wide Unicode line, with no missing or duplicated characters. `content_only`
  returns just file content while retaining paging/status/encoding metadata. Line delivery
  stops after one look-ahead line proves another page exists instead of counting
  the rest. Encoding detection may scan a legacy file, but it uses bounded
  memory during detection. Reading retains one physical line at a time.
  `total_lines` is exact only when
  `total_lines_exact` is true; otherwise it is null and
  `total_lines_at_least` reports the observed lower bound.
- **Hard caps that actually bind.** The line cap and the per-line cap *multiply*
  (2000 lines × 2000 chars is ~4MB), so on their own they don't bound the result
  — a merely *wide* file still floods the context. The 50KiB numbered-content cap
  (`FS_MCP_MAX_BYTES`) bounds the rows, not the serialized MCP response: summary,
  fencing, continuation hints, and the structured duplicate are additional.
  `truncated_by` distinguishes bytes, lines, and character segments. Listings
  are capped at `FS_MCP_MAX_ENTRIES` output entries, 5,000 examined entries
  (including hidden names), and 20 levels of recursion. Scan-budget exhaustion
  is explicitly reported; narrow the directory path to continue. Unreadable entries produce
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

Use the workspace installer to register the server. After validating it in
Continue, you may disable built-in `ls`; retain `read_file` and
`read_currently_open_file` for editor buffers. Continue exposes `fs_read` and
`fs_list`; set them to Automatic only under your threat model. MCP read-only
annotations do not configure Continue's permissions.

Relative paths resolve against `MCP_WORKSPACE` (stamped into the yaml by the
installer), falling back to the server's cwd — so they mean your project, not
wherever Continue happened to launch the process.
