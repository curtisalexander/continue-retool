# shell-mcp — the flagship terminal runner

Replaces Continue's built-in `run_terminal_command` with a background-job model
that never blocks the MCP transport: **start → poll/output → kill**, plus a
synchronous `run` convenience for quick one-liners. Design rationale lives in
[`../../docs/history/continue-mcp-toolkit-design.md`](../../docs/history/continue-mcp-toolkit-design.md)
§2–3.

## Tools

| Tool | What it does |
|---|---|
| `shell.run(cmd, shell?, cwd?, timeout?, env?, encoding?)` | Start + wait (default 30s); for quick one-liners |
| `shell.start(cmd, shell?, cwd?, timeout?, env?, encoding?, interactive?)` | Launch in the background, returns a `job_id` instantly |
| `shell.output(job_id, since_stdout?, since_stderr?, tail?)` | Incremental output via stable byte cursors; `tail=N` returns just the last N lines |
| `shell.poll(job_id)` | Lightweight state/exit-code/runtime check (read-only) |
| `shell.send(job_id, text, eof?)` | Write to an `interactive=true` job's stdin (prompts, REPLs) |
| `shell.kill(job_id)` | Kill the job and its **whole process tree** |
| `shell.list_jobs()` | All known jobs and their states (read-only) |

## The load-bearing engineering

- **Owned trees, both OSes.** Unix/macOS saves the process group created by
  `setsid`. Windows creates a kill-on-close Job Object and launches a helper that
  joins it *before* creating the requested shell, avoiding an assignment race.
  Completion, timeout, kill, and shutdown terminate remaining owned descendants.
  Use a service manager, not `shell.start`, for persistent daemons.
  Shutdown rejects new starts and waits for in-flight spawns before stopping
  registered jobs; cancellation during spawn also owns and reaps the child.
  Reported runtime freezes when the shell exits, rather than measuring job age.
- **No flashing Windows consoles.** Windows children use `CREATE_NO_WINDOW`;
  stdout and stderr remain connected to the MCP pipes.
- **Server-enforced timeout.** A command that outlives its `timeout` is killed
  and reported as `state: "timeout"` with partial output — never a hung tool
  call.
- **Capped buffers, stable cursors, recoverable overflow.** Each stream keeps
  head + most-recent tail under `SHELL_MCP_MAX_BUFFER` (default 256 KiB). Output
  cursors are *logical byte offsets into the stream*, so they stay valid across
  truncation — a chatty job streams incrementally without duplicated or silently
  dropped chunks. When a stream overflows, the **full** output is spilled to
  `.continue-mcp/logs/<instance>/` inside the workspace (so workspace-scoped `fs.read`/`search`
  tools can open it), and the `...[N bytes truncated — full output: …]...` marker
  names the file. A job that fits in the buffer never touches disk.
  Each stream's spill is itself bounded by `SHELL_MCP_MAX_SPILL_BYTES` (default
  16 MiB). `SHELL_MCP_SPILL=0` disables spilling;
  `SHELL_MCP_SPILL_DIR` relocates it. Disk/open/write/finalize failures and a
  reached disk cap never stop pipe draining: retained output stays capped and
  the response reports that the spill is incomplete.
  Each instance owns a unique directory and exclusively creates its files, so
  concurrent servers cannot overwrite or clean up one another's logs. New
  directories/files use POSIX modes 0700/0600; Windows privacy depends on the
  workspace's inherited ACLs. Review those ACLs before capturing sensitive output.
  Disk writes run off the event loop with one awaited write per stream (bounded
  backpressure); short writes are retried. Cancellation joins an in-flight write
  before closing its sink. An OS filesystem call that never returns can still
  delay finalization, but does not block the MCP event loop or process watchdog.
- **Content-only recovery contract.** Continue currently consumes MCP text
  content rather than `structuredContent`. Shell transcripts therefore render
  the job ID, stdout/stderr byte cursors, selected encoding, decode loss, spill
  paths, and spill-loss diagnostics directly in text as well as preserving the
  structured fields. Failed starts, timeouts, kills, non-zero exits, decode and
  stdin errors set MCP `isError`.
- **One encoding per job.** PowerShell starts with UTF-8 console and pipeline
  defaults so PowerShell text and cooperating programs preserve emoji before
  capture. Bash/PowerShell default to UTF-8; `cmd` defaults to the Windows OEM
  code page. Native programs can emit ANSI, OEM, UTF-8, or tool-specific bytes,
  so `encoding` selects the producer's actual codec per call and
  `SHELL_MCP_ENCODING` changes the server-wide default. The codec is selected
  once and reported with decode-loss metadata, so polling boundaries cannot
  change the interpretation or silently discard multibyte legacy characters.
  An initial UTF-8 BOM is hidden independently in each displayed stream; raw
  spill bytes and byte cursors retain it. Embedded U+FEFF is not stripped.
  Raw spill paths return a matching `*_full_output_encoding`; pass it to
  `fs.read(encoding=...)`. ANSI styling is disabled because MCP responses are
  text rather than terminal emulators.
- **Interpreter resolution.** `shell = bash | pwsh | powershell | cmd` is
  resolved by the server (installer-stamped `SHELL_MCP_<SHELL>` env → PATH →
  known install locations) so a stale GUI PATH can't break it. The model-facing
  schema is an enum and names the actual platform default. A redundant nested
  `pwsh`, `powershell`, `bash`, or `cmd` is rejected with an actionable error:
  the tool already invokes the interpreter. The Windows fallback order is
  `pwsh`, `powershell`, then `cmd`. Installer-detected `SHELL_MCP_PREFERRED_SHELL`
  permits fallback; an explicit `SHELL_MCP_DEFAULT_SHELL` remains strict.
- **stdin is never the transport.** Children get `DEVNULL` (or a pipe with
  `interactive=true`) — a child that reads stdin can't eat MCP protocol bytes.
  PowerShell receives `-NonInteractive` unless `interactive=true`, making
  `Read-Host` and confirmation prompts fail instead of waiting. Interactive
  jobs retain prompt/native-stdin support through `send`; this is not a PTY.
  PowerShell command transcripts use `PS>`; Bash/cmd retain `$`.
- **Workspace-relative and deterministic environment.** `cwd` defaults to
  `MCP_WORKSPACE`; relative `cwd` resolves against it. `env` overlays the copied
  server environment per call; a null value removes a variable. Windows names
  are merged case-insensitively, and no per-call value leaks into later jobs.
- **Bounded pipe completion.** Output continues draining after the parent shell
  exits, with a 0.5-second idle bound and an absolute one-second post-exit bound.
  The command timeout remains active during this window; completion then kills
  any remaining owned descendants.
- **Bounded registry.** Finished jobs beyond `SHELL_MCP_MAX_FINISHED`
  (default 20) are pruned, oldest first — a week-long session can't leak
  buffers or spill logs. Spill logs remain available while their jobs are
  retained and are removed when those jobs are pruned or the server shuts down.
  Cleanup removes only owned files and empty instance directories; it never
  recursively deletes another instance's logs. At most
  `SHELL_MCP_MAX_RUNNING` commands run concurrently (default 8), and server
  shutdown kills and reaps every remaining process group.

Tree ownership is lifecycle control, not a security sandbox. Processes brokered
by external services, and deliberate POSIX `setsid` escapes, are not guaranteed
to remain owned.

PowerShell preserves last-command status without globally setting
`ErrorActionPreference=Stop` or bypassing execution policy: a final native
nonzero status or `Write-Error` becomes exit 1, explicit `exit 7` remains 7, and
a caught error or successful final command may return 0. PowerShell 5.1 lacks
PowerShell 7's `&&`/`||`; use compatible syntax when selecting `powershell`, and
use `&` to invoke a quoted executable path.

## Setup

```bash
uv run --extra test pytest -q   # golden suite incl. the tree-kill test
uv run shell-mcp                # run the server (stdio)
```

Register `.continue/mcpServers/shell.yaml` (installer-stamped), set the
built-in `run_terminal_command` to **Excluded**, and `shell.*` to **Ask First**
because commands have open-world authority.

Continue versions that do not forward their Stop/cancel action to an in-flight
MCP call cannot trigger server-side cancellation cleanup. When cancellation
does reach `shell.run`, the server kills the whole process tree and waits for it
to be reaped before acknowledging cancellation. `shell.kill` is the explicit,
reliable fallback for background jobs.
