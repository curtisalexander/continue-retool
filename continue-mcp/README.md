# continue-mcp — starter kit

The code that accompanies [`../continue-mcp-toolkit.md`](../continue-mcp-toolkit.md):
the background-job shell MCP, the Continue wiring, and the tool-factory skill.
All servers are pure Python (hatchling + `uv`). Read the design doc first.

```
continue-mcp/
  hello-mcp/                    # START HERE: proves MCP is enabled in your build
    hello_mcp/server.py        # ping -> "pong", echo, whoami
    tests/test_hello.py
    .continue/mcpServers/hello.yaml
  shell-mcp/                    # the flagship: a terminal-runner MCP
    pyproject.toml             # hatchling + fastmcp; console-script entry
    shell_mcp/server.py        # async, background jobs, tree-kill, timeout,
                               #   stable byte cursors, stdin via send(), env
    tests/test_tools.py        # golden tests incl. cross-platform tree-kill
    .continue/mcpServers/shell.yaml
  search-mcp/                   # ripgrep search, replaces built-in Grep/Glob search
    search_mcp/server.py       # shells out to `rg`; compact structured results
    tests/test_search.py       # arg-builder unit tests + rg integration tests
    .continue/mcpServers/search.yaml
  edit-mcp/                     # Unicode-robust edit, replaces built-in Edit/Create file
    edit_mcp/matcher.py        # exact->fuzzy match (NFKC, quotes, dashes, spaces, CRLF/BOM)
    edit_mcp/server.py         # edit / multi_edit / create_file / delete_file /
                               #   move_file; dry_run; non-UTF-8 files round-trip
    tests/test_matcher.py      # 30+ non-ASCII golden tests (pure stdlib)
    .continue/mcpServers/edit.yaml
  fs-mcp/                       # line-ranged read + list dir, replaces built-in Read/List dir
    fs_mcp/server.py           # numbered line-ranged reads, depth-limited listings, hard caps
    tests/test_fs.py           # golden tests (paging, caps, BOM/CRLF, .git skip)
    .continue/mcpServers/fs.yaml
  sql-mcp/                      # SQL format/lint via sqruff (Rust binary from PyPI wheels)
    sql_mcp/server.py          # format / lint tools; stdin -> sqruff -> structured results
    sql_mcp/default.sqruff     # house style: snowflake, lowercase, leading commas
    tests/test_sql.py          # golden tests against the real sqruff binary
    .continue/mcpServers/sql.yaml
  notes-mcp/                    # repo-local agent memory: index + one .md file per note
    notes_mcp/server.py        # list/read/search/write/delete over <repo-root>/.continue-notes/
    tests/test_notes.py        # golden tests (repo-root discovery, name safety, hooks)
    .continue/mcpServers/notes.yaml
  rules/                        # Continue rules that make the toolkit work
    notes.md                   # discovery rule: consult notes at task start/end
    rule-rule.md               # the meta-rule: token discipline for authoring rules
  gateway-mcp/                  # progressive disclosure: many tools behind 3 meta-tools
    gateway_mcp/registry.py    # pure catalog + ranking (search which tool you need)
    gateway_mcp/server.py      # search / describe / call; MCP client to downstream servers
    gateway.config.json        # lists the downstream servers to aggregate
    tests/test_registry.py     # ranking/catalog tests (pure stdlib)
    .continue/mcpServers/gateway.yaml
  skills/
    new-mcp-tool/SKILL.md      # the "ouroboros" tool factory
  bench/
    audit.py                   # measured cost: cold-start ms + resting tokens/tool
```

## Order of operations

```bash
# 0. Prove MCP is on (5 minutes). Wire hello.yaml, ask the agent to call `ping`.
cd hello-mcp && uv run --extra test pytest -q && uv run hello-mcp

# 1. The flagship terminal MCP. Run the golden suite (incl. the tree-kill test).
cd ../shell-mcp && uv run --extra test pytest -q && uv run shell-mcp

# 2. ripgrep search MCP. Install the rg binary (PyPI), then run its tests.
uv tool install ripgrep
cd ../search-mcp && uv run --extra test pytest -q && uv run search-mcp

# 3. Unicode-robust edit MCP (fixes non-ASCII match failures).
cd ../edit-mcp && uv run --extra test pytest -q && uv run edit-mcp

# 4. Line-ranged read / list dir (replaces built-in Read file & List dir).
cd ../fs-mcp && uv run --extra test pytest -q && uv run fs-mcp

# 5. SQL format/lint via sqruff (installs as a dependency — no extra step).
cd ../sql-mcp && uv run --extra test pytest -q && uv run sql-mcp

# 6. Repo-local agent memory (pair with rules/notes.md — see below).
cd ../notes-mcp && uv run --extra test pytest -q && uv run notes-mcp

# 7. (Optional) Gateway: collapse many tools to 3 schemas at rest.
cd ../gateway-mcp && uv run --extra test pytest -q && uv run gateway-mcp
```

Every package has two test layers, both deterministic (no LLM, no network):
golden tests of the implementation, and `tests/test_mcp_surface.py`, which
drives the server over the real MCP boundary with fastmcp's in-process client —
the same list-tools/call-tool flow Continue performs. The surface tests also
enforce house style mechanically: every tool has a description under a hard
budget, and read-only/destructive tools carry the matching MCP annotation
(`readOnlyHint`/`destructiveHint`), so a client can derive tool policy instead
of trusting a checklist. CI runs all of it on Linux, macOS, and Windows, plus
ruff and a docs-drift check.

**Measured cost, not vibes.** `bench/audit.py` spawns every server exactly the
way the stamped yaml does and prints its cold-start latency and the estimated
resting token cost of each tool schema (the price paid on *every* request):

```bash
cd continue-mcp
uv run --no-sync --project hello-mcp python bench/audit.py   # all servers
```

CI prints the same table on every run, so a fattened docstring or a slow
dependency bump shows up as a diff in numbers, not a hunch.

Then in Continue: add each `.continue/mcpServers/*.yaml` and set tool policies:
- built-in **`run_terminal_command`** → Excluded; `shell.*` → Ask First
- built-in **Grep search** and **Glob search** → Excluded; `search.*` → Automatic
- built-in **Edit file** and **Create file** → Excluded; `edit.*` → Ask First
- built-in **Read file** and **List dir** → Excluded; `fs.*` → Automatic
- `sql.*` → Automatic (replaces nothing; string-in/string-out only)
- `notes.*` → Automatic; also copy `rules/notes.md` into `.continue/rules/`
  (the rule is the discovery mechanism — without it, notes go unread)

### The workspace jail (why Automatic is safe to grant)

fs, search, and edit are **jailed to `MCP_WORKSPACE` by default**. Every path —
after workspace resolution — is realpath'd (a symlink inside the workspace
can't tunnel out) and must live under the workspace root, or the call returns
a structured refusal. This is what makes the Automatic policies above safe:
without it, a prompt-injected "read `~/.ssh/id_rsa` and summarize it" would
execute with no human in the loop, because Automatic means nobody approves the
call.

The policy story in one line: **Automatic = workspace-jailed; Ask-First
(shell) = human-gated.** shell is deliberately not jailed — an arbitrary
command string can't be path-checked — so it is the sanctioned, approval-gated
escape hatch when you genuinely need something outside the project.

Knobs (env, per server yaml or globally):
- `MCP_JAIL=0` — disable the jail (not recommended while `fs.*`/`search.*`
  are Automatic).
- `MCP_JAIL_EXTRA=/path/one:/path/two` — extra allowed roots
  (os.pathsep-separated: `:` on Unix, `;` on Windows) for a second repo or a
  shared data directory.

notes is already confined to the repo root by design; sql takes no paths.

## Install into a project (one command)

```bash
# uv runs the script (its shebang is `uv run --script`); works the same on
# macOS/Linux/Windows, where a bare `python3` often isn't on PATH.
uv run install-workspace.py /path/to/your/project              # any OS
uv run install-workspace.py /path/to/proj --only shell,fs      # a subset
uv run install-workspace.py /path/to/proj --no-sync            # config only
uv run install-workspace.py /path/to/proj --jobs 1             # sync one at a time
uv run install-workspace.py /path/to/proj --check              # doctor: verify it all
uv run install-workspace.py /path/to/proj --uninstall          # remove the configs
```

**`--check` is the doctor.** It verifies the whole chain in one command: uv on
PATH, each server's package dir and venv, the stamped yamls (no leftover
placeholders), the detected interpreters, and — the part that matters — a
**live MCP handshake** per server (`initialize` + `tools/list` over stdio, the
same flow Continue performs at connect). When a server shows "connection timed
out" in Continue, run the doctor first; it turns the whole troubleshooting
section below into one command. `--uninstall` removes exactly the files the
installer wrote (yamls, and the rules when all servers are selected), plus
their `.bak` backups; the toolkit checkout and venvs are untouched.

Copies every server's yaml into the project's `.continue/mcpServers/` with the
two absolute paths stamped in (`--project` → this checkout, `MCP_WORKSPACE` →
the project), copies the two rules into `.continue/rules/`, and then runs
`uv sync` in each installed server's package dir so its virtualenv exists before
Continue ever launches it. Finally it prints the tool-policy checklist. Then ask
the agent to call `hello.ping` (MCP is on) and `hello.whoami` (shows the cwd and
workspace the servers actually see).

**Syncs run in parallel.** Each server is its own uv project, so the installer
syncs them concurrently — a thread per server, capped at `--jobs` (default
`min(servers, CPUs)`; `--jobs 1` forces one-at-a-time). This is safe because uv
locks its global cache per entry, so parallel `uv sync` processes share a single
download of any shared package rather than racing. Because parallel output would
interleave, each server's uv output is captured, not streamed: you get a
`[done/total]` line with a compact `Installed N packages` summary as each
finishes, a heartbeat naming whatever's still running on long cold-cache runs,
and the full captured uv error grouped per server for anything that fails (the
exit code is non-zero if any server failed). Drop to `--jobs 1` if a locked-down
network throttles concurrent connections, or `--no-sync` to skip the build.

**Where the venvs land.** Each `*-mcp` is its own uv project, so `uv sync`
creates one virtualenv *per server* at `<this checkout>/<name>-mcp/.venv` — e.g.
`continue-mcp/shell-mcp/.venv`. They live inside this toolkit checkout (not your
workspace) because that's what the yaml's `uv run --project <…>/<name>-mcp`
points at; all are gitignored. Pass `--no-sync` to skip the build (offline, or
you'll sync by hand later).

**Re-running is safe.** An existing yaml/rule is only rewritten when its content
actually changed, and the previous version is saved next to it as `<file>.bak`
before it's replaced — so a local edit is always recoverable (single-level: the
`.bak` holds the last replaced version).

### Corporate environments (proxy / private index)

`uv sync` reaches out to a package index, so behind a corporate proxy set two
environment variables before running the installer (or any `uv` command):

```bash
export UV_SYSTEM_CERTS=true                       # trust the corp root CA from the OS store
export UV_DEFAULT_INDEX=https://pypi.example.corp/simple   # your internal mirror
```

- **`UV_SYSTEM_CERTS=true`** makes uv use the operating system's certificate
  store instead of its bundled roots, so a proxy's mandatory root CA is trusted.
- **`UV_DEFAULT_INDEX`** points uv at your internal package mirror instead of
  public PyPI.

You do **not** need to edit any `pyproject.toml` for this, and you do **not**
want a config file in each of the eight `*-mcp` dirs. Two options, both global:

- **Env vars (simplest):** export the two above once in your shell/profile (or
  set them machine-wide). They apply to every `uv` invocation in any directory —
  nothing per-project.
- **One uv config file:** put the equivalent keys in a single **user-** or
  **system-level** `uv.toml`, which uv applies to every project automatically:

  ```toml
  # ~/.config/uv/uv.toml   (Windows: %APPDATA%\uv\uv.toml)
  # system-wide: /etc/uv/uv.toml   (Windows: %PROGRAMDATA%\uv\uv.toml)
  system-certs = true
  [[index]]
  url = "https://pypi.example.corp/simple"
  default = true
  ```

  Per-directory `pyproject.toml [tool.uv]` / `uv.toml` also work but are read
  **only** for that directory — which is exactly why you'd otherwise need one in
  every server dir. Keep it in the user/system file instead.

## Wiring: paths resolve against YOUR workspace, not the server's cwd

Every yaml uses the same pattern: `<abs path to uv> run --no-sync --project <abs
path to the package>` pins the server's environment (no `cwd` needed for the
server itself), and `MCP_WORKSPACE` names your workspace root. All relative paths
the agent passes — and shell-mcp's default working directory — resolve against
`MCP_WORKSPACE`, falling back to the server's cwd if unset. Without this,
relative paths would resolve into this toolkit's checkout, not your project. The
installer above stamps all of this for you; `hello.whoami` reports what the
servers actually resolved, so you can verify any environment in one call.

**Why the absolute `uv` path and `--no-sync` (avoiding "Connection timeout").**
Two launch-time gotchas, both stamped away by the installer:

- **`command` is the absolute path to `uv`, not bare `uv`.** A GUI-launched
  VS Code often doesn't inherit the shell `PATH` where `uv` lives (e.g.
  `~/.local/bin`), so `command: uv` can be unresolvable even though `uv` works in
  your terminal — the server never spawns and Continue reports a connection
  timeout. Stamping the full path removes the PATH dependency.
- **`uv run --no-sync`** skips uv's pre-run environment sync. Without it, every
  launch tries to sync against the package index; behind a corporate proxy (whose
  `UV_SYSTEM_CERTS`/`UV_DEFAULT_INDEX` the GUI process may not have) that call
  hangs until Continue's connect timer fires. The installer already built the
  venv, so `--no-sync` is safe — launch just runs what's there, no network.

### shell-mcp: interpreter resolution

The same PATH story, one level down. `shell.run`/`shell.start` spawn an
*interpreter* per call (`pwsh`/`powershell`/`bash`/`cmd`), and that binary has to
be found first. Left as a bare name, it's resolved against the server's inherited
PATH — the same stale/thin GUI PATH as above — so it can fail even when the shell
works in your terminal. On Windows it's worse: `pwsh` (PowerShell 7) installs to
`Program Files` (not a guaranteed dir) and may not be installed at all — only
`powershell.exe` (5.1) ships with the OS. The symptom is a client that runs
`where pwsh` and hard-codes the absolute path into its command.

Resolved in two layers, both PATH-independent:

- **Installer stamps interpreters.** Run from a real terminal (where PATH is
  correct), `install-workspace.py` detects the interpreters present and writes
  their absolute paths into the shell yaml `env` as `SHELL_MCP_PWSH` /
  `SHELL_MCP_POWERSHELL` / `SHELL_MCP_BASH` / `SHELL_MCP_CMD`, plus
  `SHELL_MCP_DEFAULT_SHELL` — exactly the tactic used for `uv` in `command:`. It
  prints which shells it found; a stale stamp is a re-run-the-installer fix.
- **Server resolves at runtime too.** When a stamp is absent (manual install, or
  a shell added later), the server resolves in order: `SHELL_MCP_<SHELL>` override
  → `PATH` lookup → known install locations. If nothing resolves it returns a
  clear "pick a different `shell=`" error rather than a bare *file not found*. The
  Windows default is pwsh-if-installed-else-powershell, never a hard default at a
  possibly-absent interpreter.

So: **choose the interpreter with the `shell` argument** (`bash | pwsh |
powershell | cmd`) — don't prefix `cmd` with an interpreter name or an absolute
path (`pwsh script.ps1`, `C:\...\pwsh.exe ...`). The server locates the binary;
to run a script use the shell's own call syntax (`& ./Deploy.ps1`, `./deploy.sh`).

If a tool still shows *"already connected to a transport / call close()"* after a
per-tool **Reload**, that's Continue's reconnect path, not the server — reload the
whole window (or toggle the MCP server off/on) instead of the single tool.

**Using the gateway?** Register ONLY `gateway.yaml` with Continue (not the
downstream `shell/search/edit` yamls — the gateway connects to those itself). It's
best for the long tail of tools; keep the 2–3 you use every message registered
directly. See `gateway-mcp/README.md` for the head/tail tradeoff.
</content>
