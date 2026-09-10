# continue-retool

Focused local MCP servers for Continue: shell execution, bounded filesystem reads,
ripgrep search, and Unicode-aware editing. SQL formatting/linting remains packaged
as an optional fifth server.

Path-aware servers use realpath-based **workspace path scoping as defense in
depth**. This reduces accidental or injected access outside configured roots; it
is not a sandbox and does not eliminate filesystem races. Keep edit and shell
tools **Ask First** by default; terminal-only users can explicitly opt into
**Automatic** execution as described below. Under your threat model, read-only
fs/search tools may be Automatic.

## Replace Continue's terminal without repeated approval

From this checkout, with Python 3.11+ and `uv` installed, choose one:

```bash
# Only this project (use an existing absolute project path):
python continue-mcp/install-workspace.py "/path/to/project" --only shell
# OR globally, in your personal Continue configuration (Bash / PowerShell):
python continue-mcp/install-workspace.py "$HOME" --only shell
```

In Continue **Agent mode → tools icon**, set the seven tools in the `shell` MCP
group to **Automatic**, and the built-in `run_terminal_command` to **Excluded**.
Continue saves tool policies locally per user; the installer registers the server
but does not grant approval. Automatic shell commands run with your account's
authority, without per-command Continue prompts.

Global registration defaults commands to **your home directory**, not the open
project; pass the project's absolute path as `cwd` on every run/start call.
Project registration defaults to the specified project. Neither is a shell sandbox.
See the [terminal-only setup guide](continue-mcp/shell-mcp/README.md#setup)
for clone/install instructions, Windows paths, verification, and rollback.

## Current inventory

<!-- BEGIN GENERATED SERVER INVENTORY -->
  - `shell-mcp/` — Terminal runner with background jobs, tree-kill, and timeouts
  - `fs-mcp/` — Bounded line reads and directory listings
  - `search-mcp/` — ripgrep-backed content and file search
  - `edit-mcp/` — Atomic Unicode-tolerant file editing
  - `sql-mcp/` — Optional SQL formatting and linting through sqruff
<!-- END GENERATED SERVER INVENTORY -->

All servers share one distribution and environment while running as separate
stdio processes. From a source checkout, install the three defaults with the
preserved wrapper (it performs one locked `uv sync`):

```bash
uv run continue-mcp/install-workspace.py /path/to/project
# explicitly opt into disk mutation (and optionally combine with SQL):
uv run continue-mcp/install-workspace.py /path/to/project --with-edit --with-sql
# add packaged, optional SQL:
uv run continue-mcp/install-workspace.py /path/to/project --with-sql
```

From PowerShell, install Python 3.11+, `uv`, and ripgrep (`rg`), then run from
this checkout:

```powershell
$workspace = 'C:\Users\Me\workspace path 工作区'  # existing project directory
uv run continue-mcp/install-workspace.py "$workspace"
if ($LASTEXITCODE -ne 0) { throw 'Installation failed' }
uv run --project continue-mcp --no-sync python continue-mcp/install-workspace.py "$workspace" --check
if ($LASTEXITCODE -ne 0) { throw 'Doctor failed' }
```

An installed wheel instead provides `continue-mcp-install`. Run it from the
durable virtual/tool environment where the wheel is installed; generated YAML
uses that environment's Python with `-m`, so package installs need neither a
source checkout nor `uv`/`uvx`. Do not use an ephemeral `uvx` environment because
Continue must be able to launch the same installation later.

The installer does not change model configuration, built-in permissions, or remove
existing server YAML. See the [Continue compatibility and migration guide](continue-mcp/CONTINUE_COMPATIBILITY.md),
[the toolkit guide](continue-mcp/README.md), [current architecture](ARCHITECTURE.md),
and [ADRs](docs/adr/README.md). Superseded explorations remain clearly retained
under [docs/history](docs/history/).

Search requires a system `rg`, `uv tool install ripgrep-bin`, or `RIPGREP_BIN`.
GUI-launched editors may not inherit terminal environment changes; use an
absolute `RIPGREP_BIN` in the generated YAML when needed.

## Site and license

The [project site](https://curtisalexander.github.io/continue-retool/) is served
from `docs/`. Rebuild generated Pandoc pages with `./build/build-docs.sh`.
That contributor-only script requires Bash (Git Bash or WSL on Windows); the MCP
runtime itself does not require Git Bash.
Licensed under the [MIT License](LICENSE).
