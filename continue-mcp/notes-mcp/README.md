# notes-mcp — repo-local agent memory

An index plus one markdown file per note, stored **in the current repo** at
`<repo-root>/.continue-notes/` — never the home directory. The repo root is
found by walking up from `MCP_WORKSPACE` to the nearest `.git` (with no repo,
the workspace itself is used).

Notes are the agent's working memory — task state, discoveries, corrections.
They are *facts*, not policy: policy belongs in Continue rules. The companion
rule (`../rules/notes.md`) is the discovery mechanism — it tells the agent to
consult the index at task start and record state at task end. Server without
rule = memory nobody reads.

## Tools

| Tool | What it does |
|---|---|
| `notes.list()` | Bounded index: `{name, hook, age_days}` per note, with an explicit partial-result flag |
| `notes.read(name)` | Content of one note (capped at 50KB; an oversized note is truncated with a pointer to `fs.read` for the rest) |
| `notes.search(query)` | Case-insensitive substring search across all notes — for when the hooks aren't enough (capped at 200 matches, long lines clipped) |
| `notes.write(name, content, append?)` | Atomically create/update a bounded note; first line becomes the hook |
| `notes.delete(name)` | Remove a wrong or stale note |

Progressive disclosure, applied to memory: the index costs a few hundred
tokens no matter how much is stored; contents load only on demand.

## Design points

- **Plain markdown files.** Greppable, hand-editable, diffable. Commit them or
  add `.continue-notes/` to `.gitignore` — your choice per repo. When a note
  graduates to shared truth, move its text into a rule or ARCHITECTURE.md.
- **Path safety.** `NOTES_MCP_DIRNAME` must be relative and resolve inside the
  real repository root. Absolute paths, traversal, escaping directory symlinks,
  note symlinks, and note names outside `[A-Za-z0-9._-]` are rejected with a
  structured error.
- **Atomic writes.** Content is UTF-8 encoded and size-checked before mutation,
  written and synced through a unique sibling, and installed with `os.replace`.
  Existing permission bits are preserved and failed writes leave the old bytes
  untouched. Individual notes are capped by `NOTES_MCP_MAX_NOTE_BYTES` (4MB).
- **Bounded output and work.** `read` counts encoded bytes and is capped by
  `NOTES_MCP_MAX_READ_BYTES` (50KB). `list` is capped by entries, index bytes,
  and directory entries examined. `search` is capped by matches and total bytes
  scanned (`NOTES_MCP_MAX_SEARCH_BYTES`, 2MB), with long lines clipped. Partial
  results set `truncated`; inaccessible or unsafe entries are reported as
  `skipped` rather than raising protocol exceptions.
- **The promotion pipeline.** Durable preferences start as notes; when one
  keeps proving true, the agent proposes a rule (see `../rules/rule-rule.md`)
  and you promote it deliberately.

## Setup

```bash
uv run --extra test pytest -q
uv run notes-mcp                # run the server (stdio)
```

Register `.continue/mcpServers/notes.yaml` with Continue **and** copy
`../rules/notes.md` into your workspace's `.continue/rules/`. Replaces no
built-in tool.
