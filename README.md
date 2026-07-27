# continue-retool

Focused local MCP servers for Continue: shell execution, bounded filesystem reads,
ripgrep search, and Unicode-aware editing. SQL formatting/linting remains packaged
as an optional fifth server.

Path-aware servers use realpath-based **workspace path scoping as defense in
depth**. This reduces accidental or injected access outside configured roots; it
is not a sandbox and does not eliminate filesystem races. Keep edit and shell
tools **Ask First**. Under your threat model, read-only fs/search tools may be
Automatic.

## Current inventory

<!-- BEGIN GENERATED SERVER INVENTORY -->
  - `shell-mcp/` — Terminal runner with background jobs, tree-kill, and timeouts
  - `fs-mcp/` — Bounded line reads and directory listings
  - `search-mcp/` — ripgrep-backed content and file search
  - `edit-mcp/` — Atomic Unicode-tolerant file editing
  - `sql-mcp/` — Optional SQL formatting and linting through sqruff
<!-- END GENERATED SERVER INVENTORY -->

All servers share one distribution, lockfile, and environment while running as
separate stdio processes. Install the four defaults with:

```bash
uv run continue-mcp/install-workspace.py /path/to/project
# add packaged, optional SQL:
uv run continue-mcp/install-workspace.py /path/to/project --with-sql
```

See [the toolkit guide](continue-mcp/README.md), [current architecture](ARCHITECTURE.md),
and [ADRs](docs/adr/README.md). Superseded explorations remain clearly retained
under [docs/history](docs/history/).

Search requires a system `rg`, `uv tool install ripgrep-bin`, or `RIPGREP_BIN`.

## Site and license

The [project site](https://curtisalexander.github.io/continue-retool/) is served
from `docs/`. Rebuild generated Pandoc pages with `./build/build-docs.sh`.
Licensed under the [MIT License](LICENSE).
