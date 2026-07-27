# shell-mcp — the flagship terminal runner

Replaces Continue's built-in `run_terminal_command` with a background-job model
that never blocks the MCP transport: **start → poll/output → kill**, plus a
synchronous `run` convenience for quick one-liners. Design rationale lives in
[`../../docs/history/continue-mcp-toolkit-design.md`](../../docs/history/continue-mcp-toolkit-design.md)
§2–3.

## Tools

| Tool | What it does |
|---|---|
| `shell.run(cmd, shell?, cwd?, timeout?, env?)` | Start + wait (default 30s); for quick one-liners |
| `shell.start(cmd, shell?, cwd?, timeout?, env?, interactive?)` | Launch in the background, returns a `job_id` instantly |
| `shell.output(job_id, since_stdout?, since_stderr?, tail?)` | Incremental output via stable byte cursors; `tail=N` returns just the last N lines |
| `shell.poll(job_id)` | Lightweight state/exit-code/runtime check (read-only) |
| `shell.send(job_id, text, eof?)` | Write to an `interactive=true` job's stdin (prompts, REPLs) |
| `shell.kill(job_id)` | Kill the job and its **whole process tree** |
| `shell.list_jobs()` | All known jobs and their states (read-only) |

## The load-bearing engineering

- **Tree-kill, both OSes.** `setsid` + `killpg` on Unix/macOS, `taskkill /T /F`
  on Windows — killing a job takes down grandchildren too (the golden test
  proves it with a sentinel file).
- **No flashing Windows consoles.** Windows children run in a new process group
  with `CREATE_NO_WINDOW`; stdout and stderr remain connected to the MCP's pipes
  while `taskkill` can still terminate the whole tree.
- **Server-enforced timeout.** A command that outlives its `timeout` is killed
  and reported as `state: "timeout"` with partial output — never a hung tool
  call.
- **Capped buffers, stable cursors, recoverable overflow.** Each stream keeps
  head + most-recent tail under `SHELL_MCP_MAX_BUFFER` (default 256 KiB). Output
  cursors are *logical byte offsets into the stream*, so they stay valid across
  truncation — a chatty job streams incrementally without duplicated or silently
  dropped chunks. When a stream overflows, the **full** output is spilled to
  `.continue-mcp/logs/` inside the workspace (so workspace-scoped `fs.read`/`search`
  tools can open it), and the `...[N bytes truncated — full output: …]...` marker
  names the file. A job that fits in the buffer never touches disk.
  `SHELL_MCP_SPILL=0` disables spilling; `SHELL_MCP_SPILL_DIR` relocates it.
- **Right encoding per platform.** PowerShell starts with UTF-8 console and
  pipeline encodings so characters such as emoji are preserved before capture.
  Output is decoded as UTF-8 when valid, otherwise with the Windows OEM code
  page for legacy commands. ANSI styling is disabled because MCP responses are
  text rather than terminal emulators. Force decoding with `SHELL_MCP_ENCODING`.
- **Interpreter resolution.** `shell = bash | pwsh | powershell | cmd` is
  resolved by the server (installer-stamped `SHELL_MCP_<SHELL>` env → PATH →
  known install locations) so a stale GUI PATH can't break it. On Windows the
  resolved interpreter's directory is also exposed to commands inside the shell,
  making a redundant nested `pwsh` work without `where pwsh`. The Windows default
  is pwsh-if-installed, else powershell.
- **stdin is never the transport.** Children get `DEVNULL` (or a pipe with
  `interactive=true`) — a child that reads stdin can't eat MCP protocol bytes.
- **Workspace-relative.** `cwd` defaults to `MCP_WORKSPACE`; relative `cwd`
  resolves against it. `env` overlays the server's environment per call.
- **Bounded registry.** Finished jobs beyond `SHELL_MCP_MAX_FINISHED`
  (default 20) are pruned, oldest first — a week-long session can't leak
  buffers or spill logs. Spill logs remain available while their jobs are
  retained and are removed when those jobs are pruned. At most
  `SHELL_MCP_MAX_RUNNING` commands run concurrently (default 8), and server
  shutdown kills and reaps every remaining process group.

## Setup

```bash
uv run --extra test pytest -q   # golden suite incl. the tree-kill test
uv run shell-mcp                # run the server (stdio)
```

Register `.continue/mcpServers/shell.yaml` (installer-stamped), set the
built-in `run_terminal_command` to **Excluded**, and `shell.*` to **Ask First**
because commands have open-world authority.
