# continue-mcp

One Python distribution containing five focused stdio MCP servers:

- `shell-mcp`: foreground/background commands (Ask First)
- `fs-mcp`: bounded reads/listings (may be Automatic)
- `search-mcp`: ripgrep content/file search (may be Automatic)
- `edit-mcp`: exactly `edit` and `create_file` (Ask First)
- `sql-mcp`: optional sqruff formatting/linting

Filesystem path controls are defense-in-depth workspace path scoping, not a
sandbox. `MCP_JAIL_EXTRA` adds explicit roots and `MCP_JAIL=0` disables scoping.

## Install from source

```bash
python install-workspace.py /path/to/project              # shell,fs,search
python install-workspace.py /path/to/project --with-edit  # add disk mutation
python install-workspace.py /path/to/project --with-sql   # defaults plus SQL
python install-workspace.py /path/to/project --with-edit --with-sql
python install-workspace.py /path/to/project --only fs,search
python install-workspace.py /path/to/project --no-sync
uv run --project . --no-sync python install-workspace.py /path/to/project --check
```

`install-workspace.py` remains the source-checkout wrapper. `--only` conflicts
with either opt-in flag; the two opt-in flags can be combined. Source installation
runs one locked root `uv sync`,
then creates or upgrades installer-owned YAML files; identical files are unchanged
and differing user-authored files are refused. Unselected existing configurations
are never silently removed: installer-owned ones produce a warning. Users upgrading
from the former edit-by-default behavior should use `--with-edit` to retain that
selection, or manually remove its YAML after review. A versioned ownership marker makes
that distinction explicit, with exact recognition for legacy unmarked generated
files. Source YAML stamps absolute uv, toolkit, workspace, and detected shell-interpreter
paths plus `--no-sync`. Check mode compares exact current rendering and performs real
fs read, search grep, shell echo, optional edit create/edit, and optional SQL format
operations against temporary workspace fixtures, which are cleaned on failure.
This catches runtime failures such as missing `rg`. Invoking check through the synced toolkit environment
as shown above ensures its FastMCP import is available; import failures are also
reported as a clean installer failure. The optional `rules/rule-rule.md` guidance is not
installed automatically.

## Install from a wheel

Install the wheel into a durable virtual or tool environment, then run
`continue-mcp-install /path/to/project` and repeat with `--check`. Package YAML
binds each server to that installed Python (for example `python -m shell_mcp.server`); it
needs neither a source checkout nor `uv`. Keep the environment installed—do not
register from an ephemeral `uvx` invocation.

The installer does not modify Continue model configuration or built-in permissions.
See [CONTINUE_COMPATIBILITY.md](CONTINUE_COMPATIBILITY.md) before disabling built-ins.

The installer detects and stamps available shell interpreters so a GUI's stale
PATH is not authoritative. Runtime resolution validates those paths and falls
back to PATH/standard locations. Override detection with `SHELL_MCP_BASH`,
`SHELL_MCP_PWSH`, `SHELL_MCP_POWERSHELL`, or `SHELL_MCP_CMD`. The stamped
`SHELL_MCP_PREFERRED_SHELL` allows runtime fallback (Windows: `pwsh`, then
`powershell`, then `cmd`). `SHELL_MCP_DEFAULT_SHELL` is only a strict explicit
override and does not fall back. Rerun the installer to migrate old generated
YAML stamped with `SHELL_MCP_DEFAULT_SHELL`.

For a selected PowerShell, `--check` also reports the resolved executable,
version, edition, Unicode round-trip result, and explicit/preferred/fallback
selection. The capability command has a 15-second execution timeout; normal
tool calls do not run a capability probe. PowerShell 5.1 remains supported.

Windows PowerShell setup and doctor example (install Python 3.11+ and ripgrep
first; `py` can replace `python` if that is your Python launcher):

```powershell
Get-Command python -ErrorAction Stop
Get-Command rg -ErrorAction Stop
$PSVersionTable.PSVersion
$venv = "$HOME\continue-mcp-env"
python -m venv "$venv"
if ($LASTEXITCODE -ne 0) { throw 'Environment creation failed' }
& "$venv\Scripts\python.exe" -m pip install "C:\Downloads\continue_mcp-<version>-py3-none-any.whl"
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed' }
& "$venv\Scripts\continue-mcp-install.exe" "C:\Users\Me\workspace path 工作区"
if ($LASTEXITCODE -ne 0) { throw 'Workspace installation failed' }
& "$venv\Scripts\continue-mcp-install.exe" "C:\Users\Me\workspace path 工作区" --check
if ($LASTEXITCODE -ne 0) { throw 'Doctor failed' }
```

PowerShell 7 is recommended; Windows PowerShell 5.1 lacks `&&`/`||`. Quote paths
with spaces or Unicode, invoke a quoted executable with `& "C:\Program
Files\...\tool.exe"`, and use `$env:VAR = 'value'`, not Bash's `VAR=value
command`. Separate `MCP_JAIL_EXTRA` roots with `;` on Windows and `:` on POSIX.
GUI editors may need restarting and explicit YAML environment entries; set an
absolute `RIPGREP_BIN` there if the GUI cannot see terminal PATH changes.

## Development

```bash
uv run --extra test python scripts/run_server_tests.py
python scripts/sync_metadata.py --check
python -m compileall .
```

Search needs `rg` on PATH or `RIPGREP_BIN`. SQL robustness changes are outside
this contraction. Historical gateway/factory design discussion is retained only
in repository history documentation.
