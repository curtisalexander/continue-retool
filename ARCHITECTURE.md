# continue-mcp architecture

This is the maintained current-state description. Historical designs are under
[`docs/history/`](docs/history/).

Continue directly launches selected stdio servers from one packaged Python
distribution. The installer enables `shell`, `fs`, and `search` by default;
disk-mutating `edit` and packaged `sql` are explicit opt-ins. There is no gateway, notes service, hello
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

Edit/create encode before writing and use sibling temporary files. New-file
publication is no-overwrite: Windows uses atomic `os.rename` and needs no
hardlinks on NTFS/exFAT; POSIX requires hard-link support and reports an error
without it. Replacement is atomic where supported; Windows sharing locks fail
safely rather than replacing an incompatibly open target. Edit's bounded
digest/stat conflict check remains optimistic and best-effort.

## Installation and packaging

`servers.json` is the compact inventory and default-selection source. The source
wrapper performs one locked uv sync and emits absolute uv/toolkit/workspace paths
plus `--no-sync`. An installed wheel exposes `continue-mcp-install` and emits its
installed Python with `-m`; package mode needs no checkout or uv, but its
environment must be retained. Both modes render all selected YAML first, stamp
detected interpreters and `SHELL_MCP_PREFERRED_SHELL`, upgrade only marked or
exactly recognized legacy generated files, and refuse differing user-authored
files. The installer warns about, but never
silently removes, unselected installer-owned configurations. `--check` compares exact
rendered content and uses FastMCP `Client`/`StdioTransport` to execute meaningful
temporary-fixture checks for each selected server; fixtures are created inside and
removed from the workspace even on failure.

`SHELL_MCP_DEFAULT_SHELL` is a strict explicit override; installer preference is
separate so Windows can fall back `pwsh` → `powershell` → `cmd`. Old DEFAULT-stamped
YAML requires an installer rerun. Shell ownership uses a saved POSIX process group
or a kill-on-close Windows Job Object joined by a launcher before shell creation.
Timeout remains active through post-parent pipe draining (one second absolute,
0.5 second idle), then completion kills remaining owned descendants. This is not
sandboxing: externally brokered processes and deliberate POSIX `setsid` escapes
are outside the guarantee; persistent daemons belong under a service manager.

The supported automatic topology is a saved, local, single-root workspace. Remote
use requires an explicitly validated installation on the same host and filesystem
as the workspace. Multi-root workspaces require manual configuration and are not
automatically supported. Tool annotations describe intent; they are not Continue
permission policy, and the installer never changes models or built-in permissions.

`scripts/sync_metadata.py --check` verifies generated packaging, inventory, and
landing-page cards. Golden and FastMCP surface tests cover each server.

## Decisions and history

ADR-0001 remains current for unified packaging; ADR-0002 applies with the scoped
defense-in-depth language above; ADR-0003 is superseded by direct-only
registration; ADR-0004 remains current with optimistic conflict qualifications.
