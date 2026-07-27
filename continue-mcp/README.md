# continue-mcp

One Python distribution containing five focused stdio MCP servers:

- `shell-mcp`: foreground/background commands (Ask First)
- `fs-mcp`: bounded reads/listings (may be Automatic)
- `search-mcp`: ripgrep content/file search (may be Automatic)
- `edit-mcp`: exactly `edit` and `create_file` (Ask First)
- `sql-mcp`: optional sqruff formatting/linting

Filesystem path controls are defense-in-depth workspace path scoping, not a
sandbox. `MCP_JAIL_EXTRA` adds explicit roots and `MCP_JAIL=0` disables scoping.

## Install

```bash
python install-workspace.py /path/to/project              # shell,fs,search,edit
python install-workspace.py /path/to/project --with-sql   # defaults plus SQL
python install-workspace.py /path/to/project --only fs,search
python install-workspace.py /path/to/project --no-sync
uv run --project . --no-sync python install-workspace.py /path/to/project --check
```

`--only` conflicts with `--with-sql`. Installation runs one locked root sync,
then writes only missing YAML files; identical files are unchanged and differing
files are refused. YAML stamps absolute uv, toolkit, and workspace paths and
`--no-sync`. Check mode compares exact current rendering and performs a real
FastMCP stdio handshake. Invoking check through the synced toolkit environment
as shown above ensures its FastMCP import is available; import failures are also
reported as a clean installer failure. The optional `rules/rule-rule.md` guidance is not
installed automatically.

Shell interpreters resolve at runtime. Override with `SHELL_MCP_BASH`,
`SHELL_MCP_PWSH`, `SHELL_MCP_POWERSHELL`, `SHELL_MCP_CMD`, and
`SHELL_MCP_DEFAULT_SHELL` where GUI PATH differs.

## Development

```bash
uv run --extra test python scripts/run_server_tests.py
python scripts/sync_metadata.py --check
python -m compileall .
```

Search needs `rg` on PATH or `RIPGREP_BIN`. SQL robustness changes are outside
this contraction. Historical gateway/factory design discussion is retained only
in repository history documentation.
