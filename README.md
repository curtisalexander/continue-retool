# continue-retool

Retooling [Continue](https://continue.dev) with our own tools — a set of local
MCP servers that replace built-in agent tools (terminal, search, edit) with
sharper, more controllable versions, plus a progressive-disclosure gateway and a
tool-factory skill for growing the kit.

The premise: the default agent tools are fine, but *your own* tools can be more
Unicode-robust, cheaper on tokens, and safer to grant — and once you can build
one MCP, you can build the rest. "Safer to grant" is literal: the read/edit
servers are **workspace-jailed by default**, so the tools you set to Automatic
can't touch anything outside your project.

**Status:** daily-driver software, pure Python throughout, tested
deterministically (fastmcp in-process client — no LLM in the test loop) on
Linux/macOS/Windows via CI. Start with the [current architecture](ARCHITECTURE.md);
direction decisions live in the [ADR index](docs/adr/README.md).

## What's here

- **[`continue-mcp/`](continue-mcp/)** — the code: a starter kit of MCP servers
  you enable in Continue.
<!-- BEGIN GENERATED SERVER INVENTORY -->
  - `hello-mcp/` — Start here; proves MCP is enabled in your build
  - `shell-mcp/` — Terminal runner with background jobs, tree-kill, and timeouts
  - `search-mcp/` — ripgrep-backed content and file search
  - `edit-mcp/` — Atomic Unicode-tolerant file editing
  - `fs-mcp/` — Bounded line reads and directory listings
  - `sql-mcp/` — SQL formatting and linting through sqruff
  - `notes-mcp/` — Bounded repository-local Markdown memory
  - `gateway-mcp/` — Progressive disclosure for downstream MCP tools
<!-- END GENERATED SERVER INVENTORY -->
  - `skills/new-mcp-tool/` — the "ouroboros" tool factory
- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — the concise current-state system
  guide. Read this first.
- **[`docs/adr/`](docs/adr/)** — accepted architecture decisions; the original
  design exploration is retained under [`docs/history/`](docs/history/).
- **[`continue-mcp-token-strategy.md`](continue-mcp-token-strategy.md)** — which
  built-in tools to replace, and the direct-vs-gateway token-cost tradeoff.

The servers share one distribution, lockfile, and environment while retaining
separate commands and processes. See [`continue-mcp/README.md`](continue-mcp/README.md)
for setup and selective registration. To wire the whole kit into a project in one
command: `uv run continue-mcp/install-workspace.py /path/to/your/project`
(and `--check` afterwards runs a doctor that verifies the install end-to-end,
live MCP handshake included).

**One external prerequisite: `ripgrep`.** Everything installs from PyPI via `uv`
except the `rg` binary that `search-mcp` shells out to — it's deliberately *not*
a default dependency, so you provide it however you like:

- a system ripgrep — `brew install ripgrep` (or `apt` / `choco` / your package manager)
- `uv tool install ripgrep-bin` — a global prebuilt `rg` that survives project re-syncs
- point `RIPGREP_BIN` at an `rg` you already have

`ripgrep-bin` is a third-party repackage of ripgrep's official binaries (the only
one covering Windows); prefer a system `rg` or `RIPGREP_BIN` if you'd rather not
pull it. The installer's `--check` doctor reports whether `rg` resolves, so a
missing binary surfaces at setup rather than on your first search. See
[`search-mcp/README.md`](continue-mcp/search-mcp/README.md) for the full rundown.

## The site

The [project site](https://curtisalexander.github.io/continue-retool/) is served
from `docs/` via GitHub Pages. The architecture, strategy, and historical design
pages are rendered from their markdown sources with [pandoc](https://pandoc.org)
— after editing a `.md`,
regenerate the HTML with:

```bash
./build/build-docs.sh   # requires pandoc; run from anywhere
```

The landing-page shell is hand-maintained; its server cards are regenerated
from `continue-mcp/servers.json` by the same command.

## License

[MIT](LICENSE)
