# edit-mcp — a robust, Unicode-aware file-edit tool

Replaces Continue's built-in **Edit file** and **Create file** tools with an MCP
whose matching engine is ported from [Pi's edit tool](https://github.com/badlogic/pi-mono)
(`packages/coding-agent/src/core/tools/edit-diff.ts`). It fixes the failure you hit
constantly: an `old_string` that *looks* identical to the file but differs in bytes,
so exact `str.replace` misses it — especially anywhere non-ASCII is involved.

| Tool | Replaces | Notes |
|---|---|---|
| `edit.edit(path, old_string, new_string, replace_all?, dry_run?)` | Edit file | exact → fuzzy fallback; unique unless `replace_all`; `dry_run` previews the diff without writing |
| `edit.multi_edit(path, edits, dry_run?)` | — | several edits, one atomic write |
| `edit.create_file(path, content, overwrite?)` | Create file | makes parent dirs |
| `edit.delete_file(path)` | — | file deletion gets its own policy lane (not `rm` via the shell tool) |
| `edit.move_file(path, new_path, overwrite?)` | — | move/rename; refuses to clobber unless `overwrite` |

Files that aren't UTF-8 are handled too: a cp1252/latin-1 file is detected,
edited, and **written back in its own encoding** — never transcoded. Writes are
atomic replacements: content is encoded first, written and synced to a sibling
temporary file, then moved over the destination, so an encoding or write failure
leaves the prior file intact. Before replacement, a bounded content-digest check
(up to 1 MiB by default; larger files use a stat fingerprint) refuses to overwrite
a file changed since the edit read it. Ordinary permission bits are preserved. Every
failure (no match, ambiguous match, missing
file, or an `old_string` **identical** to `new_string` — a no-op that would
otherwise report a phantom success) comes back as structured `{ok: false, error}`
the model can react to. The same Unicode-robust matching applies to the
**filename**: an NFC path finds its NFD twin on disk, so a macOS-pasted name
edits the file it names instead of a spurious "file not found". And because some
models double-encode array arguments, `multi_edit` accepts `edits` as a JSON
**string** as well as a list.

All five tools are **workspace-jailed by default**: every path (including
`move_file`'s destination) must live under `MCP_WORKSPACE` after realpath
resolution, so an injected instruction can't write or delete outside the
project even if you promote `edit.*` to Automatic. `MCP_JAIL_EXTRA` adds
roots; `MCP_JAIL=0` disables. See the kit README.

## Why your current tool fails on non-ASCII (and how this fixes it)

Models routinely emit an `old_string` that differs from disk by invisible or
look-alike characters:

- **Smart quotes** `“ ” ‘ ’` vs ASCII `" '`
- **Dashes** en/em/figure/minus `– — ‒ −` vs hyphen `-`
- **Spaces** non-breaking / thin / ideographic vs a normal space
- **Accents**: `é` as one code point (NFC) vs `e` + combining accent (NFD) — the
  classic macOS-paste bug
- **Full-width** forms `ｖａｌ` vs `val` (NFKC)
- **Trailing whitespace**, **CRLF vs LF**, and a leading **BOM**

`edit-mcp` matches in two tiers, exactly like Pi:

1. **Exact** — tried first on the raw text. If it hits, nothing is normalized and
   every byte is preserved.
2. **Fuzzy fallback** — normalize both sides (NFKC → per-line trailing-trim → fold
   smart quotes → fold dashes → fold exotic spaces), find the match in normalized
   space, then map it back to real **line ranges**. Only the touched lines are
   rewritten; every untouched line is copied verbatim, so exotic characters
   *elsewhere* in the file are never disturbed. CRLF and BOM are detected and
   restored on write.

On no match, you get a `difflib` "closest match near line N" hint so the model can
self-correct. On an ambiguous match, you get a "not unique — add context or use
replace_all" error instead of a silent wrong edit.

## Install and run

```bash
cd edit-mcp
uv run --extra test pytest -q  # tests are deterministic; no rg/network needed
uv run edit-mcp      # starts the stdio server (Continue launches this for you)
```

## Wire it into Continue and retire the built-ins

1. Copy `.continue/mcpServers/edit.yaml` into your workspace's `.continue/mcpServers/`.
2. In Agent-mode tool settings:
   - set built-in **Edit file** → **Excluded**
   - set built-in **Create file** → **Excluded**
   - set **`edit.edit` / `edit.multi_edit` / `edit.create_file`** → **Ask First**
     (they write to disk — keep a human in the loop until you trust them)

## Design notes

- **`matcher.py` is dependency-free stdlib** on purpose: it's the valuable, testable
  core. For edits, Python is not the bottleneck — correctness is.
- **Exact-first is a feature, not an optimization.** When the bytes already match we
  refuse to normalize, guaranteeing byte-perfect edits in the common case.
- **Sequential multi-edit** (each edit sees the previous result) sidesteps the
  overlapping-edit bugs that batch-offset approaches hit.
- **Atomic replacement** uses a sibling temporary file so the final
  `os.replace` stays on one filesystem. It preserves ordinary mode bits and
  follows an existing safe symlink to its target; replacing an inode does not
  preserve hard-link identity or every platform-specific extended attribute.
- **Optimistic conflict detection** hashes ordinary source files and verifies the
  digest immediately before replacement. Set `EDIT_MCP_CONFLICT_HASH_MAX_BYTES`
  to tune the bounded second-read threshold; larger files use inode/size/high-
  resolution timestamp metadata instead.
- Strategies mirror Pi's; other agents (OpenCode) add more fallbacks
  (indentation-flexible, block-anchor). Easy to add here later if a real case needs
  it — but NFKC + quote/dash/space folding covers the non-ASCII failures you're
  seeing today.
