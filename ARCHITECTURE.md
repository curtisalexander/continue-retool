# continue-mcp architecture

This is the maintained current-state description. Historical designs are under
[`docs/history/`](docs/history/).

Continue directly launches selected stdio servers from one packaged Python
distribution. The installer enables `shell`, `fs`, `search`, and `edit` by
default; packaged `sql` is opt-in. There is no gateway, notes service, hello
service, manifest manager, or tool factory.

## Components and authority

| Component | Responsibility | External authority |
|---|---|---|
<!-- BEGIN GENERATED COMPONENT INVENTORY -->
| `shell-mcp` | Foreground and background commands, polling, input, cancellation | Arbitrary subprocesses; keep human-approved |
| `fs-mcp` | Bounded line reads and directory listings | Read access with defense-in-depth workspace path scoping |
| `search-mcp` | Content and file search through ripgrep | Read access with defense-in-depth workspace path scoping; spawns rg |
| `edit-mcp` | Atomic create/edit with Unicode-tolerant matching | File mutation with defense-in-depth workspace path scoping |
| `sql-mcp` | SQL formatting and linting through sqruff | SQL strings and a subprocess; no file-path tool input |
<!-- END GENERATED COMPONENT INVENTORY -->

`continue_mcp_common` supplies bounded configuration, workspace-relative path
resolution, consistent results, and the shared byte/text contract. File reads,
edits, and ripgrep byte records use one deterministic UTF-8/BOM → UTF-16 BOM →
cp1252 → latin-1 policy; filename bytes remain governed by the operating-system
filesystem codec. Shell streams instead select one explicit codec per job so
polling boundaries cannot change their interpretation, including for multibyte
Windows OEM codecs. Shell-derived codecs are defaults, not guesses that can
identify every native producer; callers can override the codec and pass spill
provenance through `fs.read(encoding=...)`. Each server remains a separate process.

## Trust and mutation

`fs`, `search`, and `edit` apply realpath-based workspace path scoping. This is
defense in depth—not process isolation, a sandbox, or a proof against TOCTOU.
Extra roots and disabling controls remain available through `MCP_JAIL_EXTRA` and
`MCP_JAIL`. Reads may be Automatic under the user's threat model; edits and
arbitrary shell commands stay Ask First.

Edit/create encode before writing and use sibling temporary files plus atomic
replacement where the platform supports it. Edit's bounded digest/stat conflict
check is optimistic and best-effort; it narrows common lost-update windows but
cannot make an absolute concurrency guarantee.

## Installation and packaging

`servers.json` is the compact inventory and default-selection source. The
installer renders all selected YAML first, stamps absolute uv/toolkit/workspace
and detected-interpreter paths plus `--no-sync`, upgrades only marked or exactly
recognized legacy generated files, refuses differing user-authored files, and performs one
`uv sync --locked --project <toolkit>` unless skipped. `--check` compares exact
rendered content and uses FastMCP `Client`/`StdioTransport` for a live handshake.

`scripts/sync_metadata.py --check` verifies generated packaging, inventory, and
landing-page cards. Golden and FastMCP surface tests cover each server.

## Decisions and history

ADR-0001 remains current for unified packaging; ADR-0002 applies with the scoped
defense-in-depth language above; ADR-0003 is superseded by direct-only
registration; ADR-0004 remains current with optimistic conflict qualifications.
