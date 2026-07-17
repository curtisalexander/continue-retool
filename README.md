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
Linux/macOS/Windows via CI. Direction decisions live in the
[decision log](continue-mcp-toolkit.md#decision-log) inside the design doc.

## What's here

- **[`continue-mcp/`](continue-mcp/)** — the code: a starter kit of MCP servers
  you enable in Continue.
  - `hello-mcp/` — start here; proves MCP is enabled in your build
  - `shell-mcp/` — the flagship terminal runner (async, background jobs, tree-kill, timeouts)
  - `search-mcp/` — ripgrep-backed search, replaces built-in Grep/Glob (needs an
    `rg` binary you provide — see below)
  - `edit-mcp/` — Unicode-robust edit, replaces built-in Edit/Create file
  - `fs-mcp/` — line-ranged read + list dir, replaces built-in Read file/List dir
  - `sql-mcp/` — SQL format/lint via sqruff (Snowflake, lowercase, leading commas)
  - `notes-mcp/` — repo-local agent memory (index + one markdown file per note)
  - `rules/` — Continue rules: notes discovery + the rule-authoring meta-rule
  - `gateway-mcp/` — progressive disclosure: many tools behind 3 meta-tools
  - `skills/new-mcp-tool/` — the "ouroboros" tool factory
- **[`continue-mcp-toolkit.md`](continue-mcp-toolkit.md)** — the design doc. Read this first.
- **[`continue-mcp-token-strategy.md`](continue-mcp-token-strategy.md)** — which
  built-in tools to replace, and the direct-vs-gateway token-cost tradeoff.

See [`continue-mcp/README.md`](continue-mcp/README.md) for the order of
operations and per-server setup. To wire the whole kit into a project in one
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
from `docs/` via GitHub Pages. The two design docs are rendered from their
markdown sources with [pandoc](https://pandoc.org) — after editing a `.md`,
regenerate the HTML with:

```bash
./build/build-docs.sh   # requires pandoc; run from anywhere
```

`docs/index.html` (the landing page) is hand-maintained and not regenerated.

## License

[MIT](LICENSE)
