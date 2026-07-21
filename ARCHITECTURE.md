# continue-mcp architecture

This is the maintained description of the system as it exists today. Setup and
operator commands live in [`continue-mcp/README.md`](continue-mcp/README.md);
why the major choices were made lives in [`docs/adr/`](docs/adr/); superseded
design exploration is preserved in
[`docs/history/continue-mcp-toolkit-design.md`](docs/history/continue-mcp-toolkit-design.md).

## System at a glance

`continue-mcp` is one Python distribution that exposes eight console commands.
Each command starts a separate stdio MCP process; sharing a package and virtual
environment does not create a shared daemon or shared authority.

```text
Continue
  ├─ direct registration ──> hello | shell | search | edit | fs | sql | notes
  │                           (one process for each selected server)
  └─ gateway registration ─> gateway ──stdio──> selected downstream servers

installer ──writes──> <workspace>/.continue/{mcpServers,rules,...}
          └─syncs───> continue-mcp/.venv (once for the unified distribution)
```

There are two supported registration topologies:

- **Direct:** Continue starts every server selected by `--only`. This is the
  shortest path for frequently used tools.
- **Gateway:** Continue starts only `gateway-mcp`; the gateway owns the selected
  downstream processes and exposes them through `search`, `describe`, and
  `call`. A downstream must not also be registered directly.

In both topologies, `--only` selects active registrations. All server code is
installed in the shared environment, but unregistered entry points never start
and receive no tool authority.

## Components and authority

| Component | Responsibility | External authority |
|---|---|---|
<!-- BEGIN GENERATED COMPONENT INVENTORY -->
| `hello-mcp` | Connectivity and workspace diagnostics | Environment and current-directory metadata only |
| `shell-mcp` | Foreground and background commands, polling, input, cancellation | Arbitrary subprocesses; intended to remain human-approved |
| `search-mcp` | Content and file search through ripgrep | Read access within configured jail roots; spawns rg |
| `edit-mcp` | Atomic create/edit/move/delete with Unicode-tolerant matching | File mutation within configured jail roots |
| `fs-mcp` | Bounded line reads and directory listings | Read access within configured jail roots |
| `sql-mcp` | SQL formatting and linting through sqruff | SQL strings and a subprocess; no file-path tool input |
| `notes-mcp` | Bounded repository-local Markdown memory | The configured note directory under the repository root |
| `gateway-mcp` | Discovery and routing for downstream MCP tools | The union of the configured downstream servers' authority |
<!-- END GENERATED COMPONENT INVENTORY -->
| `install-workspace.py` | Install, verify, and safely uninstall workspace configuration | Toolkit environment plus the target workspace's `.continue/` tree |

The shared `continue_mcp_common` package is deliberately policy-neutral:

- `config.py` parses and clamps numeric environment settings.
- `paths.py` resolves workspace-relative paths, Unicode path variants, and
  realpath-based containment.
- `results.py` creates consistent human-readable and structured MCP results.

Server modules retain policy: which roots are allowed, which limits apply, and
whether an operation is read-only or destructive.

## Trust boundaries

The primary boundary is the workspace named by `MCP_WORKSPACE`.
`fs-mcp`, `search-mcp`, and `edit-mcp` resolve symlinks and reject a real target
outside that root. `MCP_JAIL_EXTRA` adds explicit roots; `MCP_JAIL=0` disables
the boundary. Notes use a separate repository-local containment rule and reject
absolute names, traversal, and escaping storage configurations.

`shell-mcp` is intentionally not path-jailed: an arbitrary command cannot be
made safe by checking one path argument. Its tools should be human-approved,
and it is the explicit escape hatch for legitimate work outside the workspace.
The gateway does not reduce downstream authority, so `gateway.call` should be
treated as broadly as the most powerful server behind it.

Expected filesystem, configuration, and subprocess failures cross the MCP
boundary as `{ok: false, error: ...}` results. Exceptions are reserved for
unexpected defects. Tool annotations describe read-only and destructive intent
so clients can apply policy mechanically.

## Data safety and workload bounds

File mutations encode the complete replacement before touching the destination,
write and `fsync` a uniquely named sibling, preserve ordinary mode bits, and use
`os.replace`. Optimistic conflict checks prevent a changed source from being
silently overwritten. Pre-replacement failures leave the original bytes intact.

Model-facing and internal work is bounded by server-specific limits: bytes,
lines, entries, recursion depth, matches, subprocess time, concurrent jobs,
retained job state, and spill logs. Limits supplied through the environment are
validated and clamped. Partial results identify truncation and, where useful, a
continuation cursor.

Subprocess servers drain stdout and stderr concurrently. Shell commands run in
their own process group (or Windows process tree), are killed on timeout and
server shutdown, and retain incremental decoder state across polls. Search and
SQL translate spawn, timeout, decode, and non-success exit conditions into
structured results.

## Installation and runtime flow

`install-workspace.py` resolves absolute `uv`, toolkit, and workspace paths;
runs one root `uv sync`; and installs only the chosen Continue registrations.
Generated commands use `uv run --no-sync --project <toolkit>` so GUI startup is
PATH-independent and never performs a network sync. Gateway mode also writes a
workspace-owned downstream configuration.

The installer records owned-file hashes and backups in
`.continue/.continue-mcp-install.json` and
`.continue/.continue-mcp-backups/`. Reinstall refuses to overwrite a local
change. Uninstall removes only unchanged installer-created files and restores
unchanged files that existed before installation.

Doctor mode (`--check`) parses the installed YAML and launches its exact command,
arguments, environment, working directory, and timeout. It performs the real
MCP initialize/tools-list handshake; gateway checks also prove that every
selected downstream appears in the live catalog.

## Packaging and extension points

[`continue-mcp/servers.json`](continue-mcp/servers.json) is the server registry.
Packaging entry points, installer choices and policies, audit and wheel-smoke
coverage, CI suites, documentation inventories, token examples, and the sample
gateway configuration are loaded or generated from it. `scripts/sync_metadata.py
--check` is the drift gate used by CI.

The [`new-mcp-tool` skill](continue-mcp/skills/new-mcp-tool/SKILL.md) creates the
files, then `scripts/register_server.py` adds one metadata record and regenerates
every derived surface. Tests have two layers: implementation-focused
golden tests and MCP-surface tests using FastMCP's in-process client. CI runs
those suites on Linux, macOS, and Windows, then performs installer, doctor,
clean-wheel, lint, type, audit, and generated-document checks.

## Decision and history map

- [`ADR-0001`](docs/adr/0001-unified-distribution.md): one distribution with
  selective activation.
- [`ADR-0002`](docs/adr/0002-tool-authority-and-workspace-boundaries.md):
  workspace confinement and approval policy.
- [`ADR-0003`](docs/adr/0003-direct-and-gateway-registration.md): direct and
  progressive-disclosure topologies.
- [`ADR-0004`](docs/adr/0004-safe-mutation-and-bounded-execution.md): atomic
  mutation and bounded process/output behavior.
- [`Historical toolkit design`](docs/history/continue-mcp-toolkit-design.md):
  original alternatives, experiments, open questions, and dated decision log.
- [`Token-cost strategy`](continue-mcp-token-strategy.md): the detailed
  head-versus-tail cost model.
