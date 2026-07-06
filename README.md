# continue-retool

Retooling [Continue](https://continue.dev) with our own tools — a set of local
MCP servers that replace built-in agent tools (terminal, search, edit) with
sharper, more controllable versions, plus a progressive-disclosure gateway and a
tool-factory skill for growing the kit.

The premise: the default agent tools are fine, but *your own* tools can be more
Unicode-robust, cheaper on tokens, and safer to grant — and once you can build
one MCP, you can build the rest.

**Status:** daily-driver software, pure Python throughout, tested
deterministically (fastmcp in-process client — no LLM in the test loop) on
Linux/macOS/Windows via CI. Direction decisions live in the
[decision log](continue-mcp-toolkit.md#decision-log) inside the design doc.

## What's here

- **[`continue-mcp/`](continue-mcp/)** — the code: a starter kit of MCP servers
  you enable in Continue.
  - `hello-mcp/` — start here; proves MCP is enabled in your build
  - `shell-mcp/` — the flagship terminal runner (async, background jobs, tree-kill, timeouts)
  - `search-mcp/` — ripgrep-backed search, replaces built-in Grep/Glob
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
command: `python3 continue-mcp/install-workspace.py /path/to/your/project`.

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
