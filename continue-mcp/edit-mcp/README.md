# edit-mcp — narrow Unicode-aware file editing

The server exposes exactly two Ask First tools:

| Tool | Purpose |
|---|---|
| `edit(path, old_string, new_string, replace_all?, dry_run?)` | Exact-first replacement with Unicode-normalized fuzzy fallback |
| `create_file(path, content, overwrite?)` | Atomic create/replace with parent creation |

Fuzzy matching normalizes NFKC, quote/dash/space variants, line endings, and
trailing whitespace only to find matches. Normalized boundaries are mapped back
to original offsets and only original matched spans are spliced, preserving
unrelated same-line Unicode and trailing whitespace byte-for-byte. Exact matches
remain preferred. BOM/EOL and cp1252/latin-1 encoding round trips are preserved;
`replace_all` finds all matches from one snapshot, and `dry_run` does not write.

Writes encode first and use a synced sibling temporary file. Bounded digest/stat
checks provide best-effort optimistic conflict detection, not an absolute race or
TOCTOU guarantee.

Realpath-based workspace path scoping is defense in depth, not a sandbox.
`MCP_JAIL_EXTRA` adds roots and `MCP_JAIL=0` disables it. Keep both tools Ask
First.

```bash
uv run --extra test pytest -q edit-mcp/tests
uv run edit-mcp
```
