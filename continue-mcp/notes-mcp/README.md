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
| `notes.list()` | Cheap index: `{name, hook, age_days}` per note |
| `notes.read(name)` | Content of one note (capped at 50KB; an oversized note is truncated with a pointer to `fs.read` for the rest) |
| `notes.search(query)` | Case-insensitive substring search across all notes — for when the hooks aren't enough (capped at 200 matches, long lines clipped) |
| `notes.write(name, content, append?)` | Create/update; first line becomes the hook |
| `notes.delete(name)` | Remove a wrong or stale note |

Progressive disclosure, applied to memory: the index costs a few hundred
tokens no matter how much is stored; contents load only on demand.

## Design points

- **Plain markdown files.** Greppable, hand-editable, diffable. Commit them or
  add `.continue-notes/` to `.gitignore` — your choice per repo. When a note
  graduates to shared truth, move its text into a rule or ARCHITECTURE.md.
- **Name safety.** Note names are `[A-Za-z0-9._-]` only — no path separators,
  no traversal out of the notes directory.
- **Bounded output.** A note grows unbounded via repeated `append`, and a broad
  `search` spans every note, so both are capped the way `search-mcp` and `fs.read`
  are: `read` at `NOTES_MCP_MAX_READ_BYTES` (50KB, truncated on a line boundary
  with a pointer to `fs.read` for the tail), `search` at `NOTES_MCP_MAX_MATCHES`
  (200) with lines clipped to `NOTES_MCP_MAX_LINE_CHARS` (500). Each sets a
  `truncated` / `line_clipped` flag so the model knows it didn't see everything.
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
