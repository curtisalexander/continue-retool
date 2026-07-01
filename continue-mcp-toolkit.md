# Replacing Continue's Tools with Your Own MCP Toolkit

*Working design doc, 2026-07-01. Covers: how tool use works in a Continue.dev
(VS Code) coding agent, why a local MCP server is the "native" way to own your
tools, how to build a terminal-runner MCP (bash **and** PowerShell), whether the
Python-wraps-Rust (maturin) "Trojan horse" pattern buys you real performance, and
a framework + skill for spawning new tools rapidly — the "ouroboros."*

---

## 0. TL;DR — what I'd actually build

| Decision | Recommendation | Why |
|---|---|---|
| **How to add/replace tools** | Local **MCP server(s)** wired in via `.continue` config; set the built-in `run_terminal_command` policy to **Excluded**. | MCP is the only *config-level* extensibility surface in a reskinned extension you can't fork. Its results feed the model exactly like built-in tools. |
| **Terminal MCP execution model** | **Background-job model** (`start` → `poll` → `output` → `kill`), not run-to-completion. | This is the only model that gives you real *cancel*, *timeout*, and *incremental output injection* — and it dodges Continue's ~7–10 s tool-call timeout. |
| **First implementation language** | **Python + FastMCP.** Spawn via `asyncio.create_subprocess_exec` with a **new process group / job object** so you can kill the whole child tree. | Fastest path to working. The bottleneck is the child process, not the harness — Python is not the constraint here. |
| **When to add Rust** | Selectively, via **maturin/PyO3**, for genuinely hot or OS-gnarly paths (huge-output ring buffers, PTYs, robust cross-platform process-group kill, fast search/parse tools). | Keep the "install like a Python tool via `uv`/wheel" ergonomics; get native speed only where it matters. |
| **Token minimization** | A **gateway/dispatcher MCP** (progressive disclosure) + terse schemas, instead of exposing 40 fat tools at once. | Continue loads *every* MCP tool schema up front; that's the token tax you're feeling. Hide tools behind a search/dispatch tool. |
| **Rapid tool creation** | A **cookiecutter template + a `new-mcp-tool` skill** that hands the LLM the pattern, an example, and tests. | Each FastMCP tool is one decorated function → trivial to codegen. This is your ouroboros. |

### Decisions locked (2026-07-01)

Three foundational choices are settled; the rest of this doc assumes them:

- **Cross-platform, equally (Windows + macOS/Linux).** The terminal MCP must work
  on both from day one. Consequence: day-one kill uses `killpg`+`setsid` on Unix
  and a `taskkill /T /F` fallback on Windows; a *unified* Rust Job-Object kill is
  the documented upgrade (§3b/§4), not a launch blocker.
- **Pure-Python first, Rust added selectively.** Prove the shell MCP shape in
  async Python (option B), then move a hot/gnarly path into a maturin core only
  when a profiler — not a hunch — says so. The Windows tree-kill is the natural
  first Rust candidate *once the Python fallback is proven*.
- **Toolkit scale TBD → build gateway-ready, not gateway-now.** Skip the gateway
  MCP initially, but keep tool descriptions terse and the factory skill
  gateway-aware so §5b can be added later with zero rework.

The single most important idea in this whole doc: **in Continue, every tool
result — built-in or MCP — is already injected back into the model's context as a
tool-result message. You are not inventing a new injection channel; you are taking
ownership of the tool whose output gets injected.** MCP is simply the sanctioned
place to put code whose return value the agent already knows how to consume.

---

## 1. How tool use works in the Continue extension

### 1a. The mental model

Continue's Agent mode is a standard **tool-calling loop**. Three moving parts:

```text
   ┌────────────────────────────────────────────────────────────────────────┐
   │                       CONTINUE EXTENSION (VS Code)                     │
   │                                                                        │
   │    your message ──┐                                                    │
   │                   v                                                    │
   │         ┌───────────────────┐                                          │
   │         │ request builder   │───────────────┐   tools[] (JSON schemas) │
   │         └───────────────────┘               v                          │
   │                                      ┌────────────────┐                │
   │                                      │   THE LLM      │  (your         │
   │                                      │ (API endpoint) │  corp          │
   │                                      └───────┬────────┘  endpoint)     │
   │                                              │ tool_call               │
   │                   ┌──────────────────────────┘                         │
   │                   v                                                    │
   │         ┌───────────────────┐                                          │
   │         │ tool dispatcher   │  permission gate (Ask/Auto/Excl)         │
   │         └─────────┬─────────┘                                          │
   │         built-in? │  MCP?                                              │
   │            ┌──────┴───────┐                                            │
   │            v              v                                            │
   │    read_file, edit,   ┌────────────────┐                               │
   │    run_terminal_cmd   │ MCP client     │ ── stdio/http ──> YOUR        │
   │    (compiled in)      └────────────────┘                 MCP           │
   │            │              │                            SERVER          │
   │            └──────┬───────┘                                            │
   │                   v  tool result (string/content)                      │
   │         ┌───────────────────┐                                          │
   │         │ appended to msgs  │  ──> loop back to the LLM                │
   │         └───────────────────┘                                          │
   └────────────────────────────────────────────────────────────────────────┘
```

Step by step, the way Continue documents it:

1. **Tool advertisement.** On each agent request, Continue sends the model a list
   of tools as JSON objects — each with a `name`, a `description`, and an
   `arguments` JSON schema. (`read_file` with a `filepath` arg, etc.)
2. **The model decides.** It either answers in prose or emits a **tool call**
   (name + arguments).
3. **Permission gate.** Each tool has a **policy**: *Ask First* (you click
   Continue/Cancel), *Automatic* (runs with no prompt), or **Excluded** (never
   offered to the model). This is the lever you'll pull to retire the built-in
   terminal.
4. **Dispatch.** Continue routes the call to either **built-in functionality** or
   **the MCP server that offers that tool**.
5. **Result injection.** The tool's return value is appended to the message
   history as a tool-result and the loop repeats. *This is the "inject output
   back into the agent's input" you were looking for — it's the default behavior
   of the loop, not something you have to engineer.*

Two Continue-specific details worth knowing:

- **"System message tools."** Some models don't have native function-calling, or
  do it badly. Continue can fall back to describing the tools *in the system
  prompt* and parsing tool calls out of the text. Net effect: your MCP tools work
  across a wide range of models/providers — but this fallback is **token-hungry**,
  which is part of why you're seeing prompt bloat.
- **Built-in tool inventory (Agent mode):** read-only tools (Read file, Read
  current file, List dir, Glob, Grep, Fetch URL, Search web, View diff, Repo map,
  Subdirectory, Codebase) **plus** the write/act tools: **Create file, Edit file,
  `run_terminal_command`, Create Rule Block.** Plan mode exposes only the
  read-only subset.

### 1b. Why a local MCP server is the right — and "native" — move

Your instinct is correct, but for a sharper reason than "it's the only way to
inject output." Let's be precise about *why* MCP wins in your situation:

| If you wanted to… | Non-MCP option | Why it fails in a corporate reskin |
|---|---|---|
| Add a brand-new tool | Fork the extension, add a built-in tool, rebuild | You (probably) can't ship a custom fork through corporate; you don't control the reskin's build/release. |
| Change the terminal's behavior | Patch `run_terminal_command` in source | Same problem — it's compiled in. |
| Feed richer output back | "Inject" via some side channel | There is no side channel. The *only* thing the model reads is messages + tool results. |
| Add a tool via config, no rebuild | **MCP server** | ✅ `.continue` config picks it up; results flow through the same loop as built-ins. |

So the "native-ness" of MCP is three things at once:

1. **It's the config-level extension point** — no fork, no rebuild, survives
   extension updates. In a locked-down corp environment this is often the *only*
   sanctioned way to add capability.
2. **Its results are first-class.** An MCP tool's return value is injected into
   context identically to a built-in tool's. The model can't tell the difference.
   That's the "native injection" you want.
3. **It's a place to run arbitrary local code.** The MCP server is *your* process
   on the user's machine. It can shell out, capture stdout/stderr, transform
   output, keep state between calls, talk to a Rust binary — anything — and hand
   the agent back exactly the bytes you choose.

One caveat to confirm early (see Open Questions): **your reskinned extension must
have MCP enabled.** Most Continue builds do (it's config-driven via
`.continue/mcpServers/*.yaml` or the `mcpServers` block in `config.yaml`), but a
hardened corporate build *could* have disabled it. That's the one thing that would
sink this plan, so verify it on day one with a trivial "hello" MCP.

### 1c. Wiring it up (the config you'll actually write)

Continue reads MCP servers from a `mcpServers` list (global `config.yaml`, or a
per-workspace `.continue/mcpServers/<name>.yaml`). stdio is the default transport;
HTTP/SSE exists for remote servers.

```yaml
# .continue/mcpServers/shell.yaml
name: shell
command: uv
args: ["run", "shell-mcp"]      # or a wheel-installed console script
cwd: /Users/you/tools/shell-mcp # optional working dir
env:
  SHELL_MCP_DEFAULT_TIMEOUT: "120"
```

Then, in Agent-mode tool settings, set **`run_terminal_command` → Excluded** and
your new `shell.run`/`shell.start` tools to **Ask First** (or Automatic once you
trust them). Now the model reaches for *your* terminal, not Continue's.

---

## 2. Replacing `run_terminal_command` — the design space

Your requirements: run bash **or** PowerShell, capture stdout **and** stderr,
**cancel/kill**, and set a **timeout**. The naive read is "just `subprocess.run`."
That works for `ls`, but it fails exactly where terminals get interesting. Here's
the honest design space.

### 2a. The fork that decides everything: sync vs. background job

**Synchronous / run-to-completion** — one tool call blocks until the command
exits, then returns `{stdout, stderr, exit_code}`.

- ✅ Dead simple. ~30 lines.
- ❌ **Cancel is basically impossible.** The model made one blocking call; there's
  no second call it can make to say "kill it" while the first is still running.
- ❌ **Timeouts fight the transport.** MCP clients (Continue included) drop tool
  calls that don't answer in roughly **7–10 seconds** — you can get a `424 Failed
  Dependency` or a hang. A 3-minute `pytest` run will just die.
- ❌ No incremental output. The agent sees nothing until the end.

**Background-job model** — `start` returns a `job_id` immediately; the model then
calls `poll`/`output`/`kill` on that id.

```text
  model: shell.start({cmd:"pytest -q", shell:"bash", timeout:300})
     └─▶ returns {job_id:"j1", state:"running"}          (instant — no transport timeout)
  model: shell.output({job_id:"j1", since:0})
     └─▶ {stdout:"…12 passed…", stderr:"", cursor:812, state:"running"}
  model: shell.output({job_id:"j1", since:812})           (incremental "injection")
     └─▶ {stdout:"…\n38 passed\n", state:"exited", exit_code:0}
  # or, mid-run:
  model: shell.kill({job_id:"j1"})  ─▶ {state:"killed"}
```

- ✅ **Cancel/kill is a first-class second call.** This is *the* reason to go
  background.
- ✅ **Timeout is yours to enforce**, independent of the client — the server kills
  the job at the deadline and reports it.
- ✅ **Incremental output injection**: `output(since=cursor)` streams new bytes
  back into the agent's context as the job runs. This is the deepest version of
  "inject output back into the agent's input" — the agent can watch a long build
  and react.
- ✅ Each call returns instantly, so you never hit the 7–10 s wall.
- ❌ More surface area (a small job registry, a few tools instead of one). Worth it.

**Recommendation: build the background-job model.** Optionally keep a thin
`shell.run` convenience wrapper (start + wait-with-short-timeout) for quick
one-liners, implemented on top of the job registry. FastMCP even has a native
background-task protocol (`task=True`, SEP-1686) you can lean on, but a hand-rolled
in-process registry is simpler to reason about and fully under your control.

### 2b. The parts that are easy to get wrong

**Shell selection (bash vs PowerShell).** Don't guess — take a `shell` argument
and map it explicitly, with a sane per-OS default:

| `shell` value | Unix/macOS | Windows |
|---|---|---|
| `bash` | `/bin/bash -lc "<cmd>"` | `bash -lc` (Git Bash / WSL, if present) |
| `pwsh` | `pwsh -NoProfile -Command "<cmd>"` | `pwsh …` (PowerShell 7+) |
| `powershell` | — | `powershell.exe -NoProfile -Command` (Windows PS 5.1) |
| `cmd` | — | `cmd.exe /c "<cmd>"` |
| default | `bash` | `pwsh` if present else `powershell` |

Pass the command as a single string to the shell (`-c` / `-Command`) rather than
trying to tokenize it yourself — the user's mental model is "type a shell line."

**Killing the whole tree — the #1 bug.** `process.kill()` on the top process
leaves orphaned children (your `npm test` dies but the `node` it spawned keeps
running). You must kill the **process group / job object**:

- **Unix/macOS:** launch with `start_new_session=True` (i.e. `setsid`), then
  `os.killpg(os.getpgid(pid), SIGTERM)` → escalate to `SIGKILL` after a grace
  period.
- **Windows:** launch with `CREATE_NEW_PROCESS_GROUP`, and — for reliable tree
  kill — assign the child to a **Job Object** and terminate the job (or shell out
  to `taskkill /T /F /PID`). This is fiddly in pure Python and is one of the
  better arguments for a small Rust core (§3–4).

**Timeout = kill + report, not exception-and-lose-output.** On deadline, kill the
group but still return whatever stdout/stderr you captured, plus
`{state:"timeout", exit_code:null}`. Partial output is often the most useful thing
the agent gets.

**Output capture without deadlocks.** The classic trap: filling an OS pipe buffer
while the child blocks on write. Read stdout/stderr **concurrently** (async tasks
or threads), append into per-job buffers. Decide up front:

- *Interleave* stdout+stderr into one stream (what a human sees) **or** keep them
  separate (cleaner for the model). Recommendation: keep separate, but tag lines
  so order is recoverable.
- **Cap the buffer** (e.g. keep first N KB + last N KB with a "…truncated…"
  marker). A runaway command can emit gigabytes; you do not want that in the
  context window. This ring-buffer/truncation logic is another place a Rust core
  earns its keep.

**Environment & cwd.** Take an optional `cwd` and `env` overlay. Default `cwd` to
the workspace root Continue is operating in. Never inherit secrets you don't mean
to; consider an allowlist for env passthrough in a corporate setting.

### 2c. What the tool surface looks like

```text
shell.start(cmd, shell?, cwd?, env?, timeout?)  -> {job_id, state}
shell.output(job_id, since?)                     -> {stdout, stderr, cursor, state, exit_code?}
shell.poll(job_id)                               -> {state, exit_code?, runtime_ms}
shell.kill(job_id, signal?)                      -> {state}
shell.list()                                     -> [{job_id, cmd, state, runtime_ms}]
shell.run(cmd, shell?, timeout?)                 -> {stdout, stderr, exit_code}   # convenience
```

Keep the **descriptions terse** (see §5 on token cost). Six tools, each one or two
sentences.

---

## 3. Implementation approaches compared

Four realistic ways to build the terminal MCP. Scored on **fast** (runtime speed),
**ergonomic** (nice to build/maintain), **simple** (least machinery).

| Approach | Fast? | Ergonomic? | Simple? | When it's the right call |
|---|---|---|---|---|
| **A. Pure Python, `subprocess` + threads** | Fine* | Good | ★ Simplest | Prototype; you want it working this afternoon. |
| **B. Pure Python, `asyncio` subprocess (FastMCP-native)** | Fine* | ★ Best in Python | Simple-ish | **The recommendation.** Async matches MCP; cancellation/timeout are natural. |
| **C. Python MCP shell + Rust core (maturin/PyO3)** | ★ Fastest on hot paths | Good (two languages) | Medium | Huge output, PTYs, bulletproof cross-platform kill, or you want Rust in the codebase. |
| **D. Pure Rust MCP (`rmcp` crate)** | ★ Fastest overall | Good if you love Rust | Medium | You don't need the Python/`uv`/wheel ergonomics and want one static binary. |

\* **"Fine" is the honest answer for a terminal runner.** The wall-clock is
dominated by the *child process* (pytest, npm, cargo). Your harness spends its life
`await`-ing a pipe. Python's overhead here is microseconds against seconds of child
runtime. **Do not pick a language for the harness on "speed" grounds for a process
runner** — pick it on ergonomics and on the two places native code genuinely helps:
process-group kill on Windows, and high-throughput output buffering.

### 3a. A/B — the Python options

- **A (threads):** `subprocess.Popen` + two reader threads draining stdout/stderr
  into buffers + a watchdog thread for the timeout. Works everywhere, no async
  brain-bending. The downside is thread bookkeeping and that FastMCP is async, so
  you're bridging thread↔async.
- **B (asyncio):** `asyncio.create_subprocess_exec`, drain pipes with
  `StreamReader`, timeout via `asyncio.wait_for`, cancel via task cancellation +
  `killpg`. This is the cleanest fit: FastMCP tools are coroutines, so everything
  composes. **Start here.**

Watch the known FastMCP/stdio gotcha: naive blocking subprocess calls inside a
stdio server can **hang the whole server**. The background-job model plus async
draining sidesteps it, because no tool call ever blocks for long.

### 3b. C — Python shell over a Rust core (the maturin pattern)

This is your `rip` pattern applied to MCP: **Rust does the gnarly systems work;
Python is the thin MCP-facing shell; the whole thing ships as a wheel installed by
`uv` — no Rust toolchain on the target machine.**

```text
   ┌──────────────────────────────────────────────┐
   │  wheel (built by maturin, installed by uv)   │
   │                                              │
   │   shell_mcp/  (Python)                       │
   │     server.py   ── FastMCP tools ────┐       │
   │                                      │ calls │
   │   _core  (Rust, compiled via PyO3) <─┘       │
   │     spawn_group(cmd) -> handle               │
   │     read(handle, since) -> bytes             │
   │     kill_tree(handle)                        │
   │     (Job Objects on Windows, setsid on Unix) │
   └──────────────────────────────────────────────┘
```

What Rust actually buys you *here specifically*:

- **Rock-solid tree kill**, especially Windows Job Objects — a genuine pain in
  Python, clean in Rust (`std::process` + `windows`/`nix` crates, or the
  `command-group`/`shared_child` crates).
- **Cheap, correct output ring-buffering** at high throughput without the GIL in
  the read path.
- **PTY support** (via `portable-pty`) if you ever need programs that behave
  differently when they think they're on a terminal (colors, progress bars,
  interactive prompts).

What it costs: a two-language build, a `Cargo.toml` + `pyproject.toml`, and CI that
builds wheels per platform. For a *terminal* MCP this is arguably over-engineering
on day one — but it's a **great second step** once the Python version proves the
shape, and it's the exact seam where native code pays off.

### 3c. D — pure Rust via `rmcp`

The official Rust MCP SDK (`rmcp`) is mature in 2026. One static binary, fastest
startup, no Python at all. You lose the `uv`/wheel install story (which you like)
and the "codegen a tool as a decorated Python function" ergonomics (your ouroboros
in §6 is much easier in Python). **Pick D only if** you're standardizing on a
single distributed binary and don't want Python in the loop.

---

## 4. Does the maturin "Trojan horse" make higher-performing MCPs?

Short answer: **yes, but be honest about *which* MCPs.** The pattern is excellent;
the performance win is real *only for CPU-bound or syscall-gnarly tools*, not for
I/O-bound ones.

The pattern (from `rip`): write the hot logic in Rust, expose it to Python with
**PyO3**, build a wheel with **maturin**, install with **uv**. The user sees a
normal Python tool; you get native speed. It's a Trojan horse because Rust rides in
inside a Python-shaped package — no Rust toolchain required on the target.

**Where it genuinely makes a faster MCP:**

| Tool type | Native speedup? | Why |
|---|---|---|
| Code search / grep-like (walk + regex over a big tree) | **Large** | CPU + syscall bound; this is exactly what ripgrep proves. |
| Parse/format/lint (SQL formatter, AST tools) | **Large** | CPU-bound tree work; no GIL, real parsers (rowan, chumsky). |
| Hashing / dedup / large-file diff | **Large** | CPU-bound byte crunching. |
| Structured-output transforms on MBs of text | **Medium** | Avoids Python allocation/GIL overhead. |
| **Terminal runner** | **Small** | I/O-bound; win is *correctness* (tree kill, PTY), not throughput. |
| Web fetch / API call wrapper | **~None** | Network-bound; Rust changes nothing. |

So the framing to carry forward: **maturin doesn't make MCPs faster; it makes
CPU-bound MCPs faster, while keeping Python's packaging and codegen ergonomics.**
Use it as a *selective* accelerator — Python shell everywhere, Rust core where a
profiler (not a hunch) says the time goes. The `rip` repo is the proof the
distribution story works; reuse its `pyproject.toml`/`Cargo.toml`/CI skeleton
verbatim.

A single, uniform build recipe you can standardize on:

```toml
# pyproject.toml (excerpt)
[build-system]
requires = ["maturin>=1.5"]
build-backend = "maturin"

[project]
name = "shell-mcp"
requires-python = ">=3.11"
dependencies = ["fastmcp>=2"]

[project.scripts]
shell-mcp = "shell_mcp.server:main"   # the console script Continue launches

[tool.maturin]
features = ["pyo3/extension-module"]
module-name = "shell_mcp._core"       # the Rust extension, imported by Python
```

```toml
# Cargo.toml (excerpt)
[lib]
name = "_core"
crate-type = ["cdylib"]

[dependencies]
pyo3 = { version = "0.22", features = ["extension-module"] }
# command-group / shared_child / portable-pty as needed
```

Same skeleton works for *every* tool in your toolkit — pure-Python tools just skip
the `[tool.maturin]`/Rust half.

---

## 5. The broader toolkit — and killing the token tax

You want to replace *many* built-in tools with MCPs, make them **discoverable**,
and **stop the prompt bloat**. Two things to understand first.

> **For the full "which tools go direct vs. behind the gateway" strategy — the
> head/tail tradeoff, the token math, the ladder of levers, and a worked example —
> see the companion doc [`continue-mcp-token-strategy.md`](continue-mcp-token-strategy.md).**
> The short version: decide each tool by `schema_size × (1 − usage)` — big and rare
> → gateway; small or constant → direct.

### 5a. Why your tokens are disappearing

Every MCP tool Continue loads costs **~550–1,400 tokens** for its name +
description + JSON schema + field docs, and they're loaded **up front on every
request** (worse if "system message tools" is active, since the whole schema goes
in the system prompt as prose). Independent 2026 measurements put a handful of MCP
servers at **30k–67k tokens before you type anything** — a third of a 200k window
gone to tool *definitions*. That's the bloat you're feeling. Fat built-in tool
prompts + several MCPs = your context is half-eaten at rest.

### 5b. The fix: progressive disclosure via a gateway MCP

The 2026 consensus fix (Anthropic's **Tool Search Tool**, Cloudflare's **Code
Mode**, "code execution with MCP") is the same idea: **don't put every tool schema
in context — put a *way to find and call* tools in context.** Reported savings are
dramatic (85% to ~99% fewer tokens) because the model loads a schema only when it
actually needs that tool.

You can build this yourself as **one gateway MCP** that exposes ~3 tools instead of
40:

```text
tools.search(query)          -> [{name, one_line}]      # cheap catalog lookup
tools.describe(name)         -> {full schema}           # load one schema on demand
tools.call(name, args)       -> {result}                # dispatch to the real impl
```

Now Continue only pays for **three** schemas at rest. The other 40 live behind
`search`/`describe`, disclosed one at a time. This directly serves all three of
your goals: **fewer tokens, infinite expansion** (add tools without adding resting
cost), and you still get **native injection** (the gateway's result is a normal
tool result).

> Check whether your Continue build natively supports the MCP/Anthropic Tool
> Search flow — if it does, you may get this for free. If not, the gateway MCP
> above reproduces it in ~100 lines and works with any model.

Trade-off to name honestly: a gateway adds **one extra round-trip** (search →
call) and slightly more reasoning burden on the model. For a toolkit of 5 tools,
skip it and just keep descriptions terse. For 20+, the gateway wins decisively.

**Built and ready in `continue-mcp/gateway-mcp/`.** A FastMCP server exposing
`search`/`describe`/`call` that is itself an MCP *client* to your downstream servers
(configured in `gateway.config.json`), aggregating their catalogs at startup. The
ranking/catalog core (`registry.py`) is pure stdlib and unit-tested. Its README
documents the purpose, the aggregator design, the search→describe→call flow, and the
important **head/tail** guidance: put the *long tail* of tools behind the gateway,
but keep the 2–3 tools you use every message (edit, shell.run, search.grep)
registered directly — searching→describing→calling for the thing you do constantly
isn't worth the hop. When using the gateway, register **only** the gateway with
Continue, not the downstream servers.

### 5c. Tools worth building (beyond the terminal)

Prioritized by leverage — replace the token-heavy built-ins first, then expand:

1. **`shell` (this doc)** — the flagship; unlocks the background-job + injection pattern.
2. **`search`** — a ripgrep-backed grep/glob tool. `ripgrep` ships on **PyPI**, so
   `uv tool install ripgrep` puts the real `rg` binary on PATH with no raw-binary
   install needed; the MCP just shells out to it. (If you'd rather search
   in-process, embed BurntSushi's `grep`/`ignore` crates as a PyO3 lib via maturin —
   the same wheel pattern, no subprocess. Either way it's far cheaper in tokens than
   built-in codebase context.)
3. **`edit` — ready in `continue-mcp/edit-mcp/`.** Replaces the built-in Edit/Create
   file tools with a matcher ported from Pi's edit tool: exact match first, then a
   Unicode-normalized fuzzy fallback (NFKC, smart quotes, dashes, exotic spaces,
   trailing whitespace, CRLF/BOM) that maps back to real line ranges so untouched
   lines keep their bytes. Fixes the non-ASCII match failures the stock tool hits.
   30+ pure-stdlib tests, all green.
4. **`fs`** — read/list with *your* conventions (line-ranged reads, terse output,
   workspace-relative paths) to cut the verbosity of the built-in read tools.
5. **`repo`** — repo-map / symbol index / "where is X defined" backed by tree-sitter
   (Rust). Replaces expensive codebase context with targeted lookups.
6. **`sql`** — your Snowflake formatter (see `snowflake-formatter-brainstorm.md`)
   exposed as a `format`/`lint` tool; the maturin pattern is already the plan there.
7. **`notes`/`memory`** — a scratchpad the agent can write to and read back
   (stateful MCP; trivially injects prior output into later turns).
8. **`http`/`api`** — thin, allow-listed corporate API callers (network-bound, so
   pure Python — no Rust needed).

Every one of these is the **same shape**: a FastMCP server, a handful of terse
tools, optional Rust core for the CPU-bound ones. Which brings us to the factory.

---

## 6. The ouroboros — a framework + skill that builds new tools

You want the "software creating software for itself" effect. Concretely, that's two
artifacts: a **template** (the invariant scaffold) and a **skill** (the instructions
that let an LLM fill the template correctly). Because every FastMCP tool is *one
decorated function*, codegen is genuinely easy here.

### 6a. The template (cookiecutter)

One repo layout, reused for every tool. Pure-Python tools use only the top half;
CPU-bound tools add the Rust core.

```text
mcp-<name>/
  pyproject.toml            # maturin backend, console-script entry, fastmcp dep
  Cargo.toml                # only if a Rust core is needed
  src/lib.rs                # Rust core (optional)
  <name>_mcp/
    server.py               # FastMCP() + @mcp.tool functions (terse descriptions)
    _core.pyi               # type stubs for the Rust extension (optional)
  tests/
    test_tools.py           # golden tests per tool
  .continue/
    mcpServers/<name>.yaml   # ready-to-drop-in Continue wiring
  README.md                 # one paragraph + the tool list
```

### 6b. The skill (`new-mcp-tool`) — the ouroboros loop

A Claude Code / agent **skill** that, given a one-line spec ("a tool that runs
Snowflake queries and returns rows as markdown"), does this:

```text
1. Ask 2–3 clarifying questions (inputs? outputs? side effects? CPU-bound → Rust?)
2. Copy the cookiecutter template.
3. Write the @mcp.tool function(s) with TERSE descriptions (enforce a token budget:
   name + ≤2 sentence description + minimal schema).
4. If CPU-bound, stub the Rust core and the PyO3 binding.
5. Generate golden tests and run them (this is where your shell-MCP eats its own
   dog food — the generator uses the terminal tool to run the tests).
6. Emit the .continue/mcpServers/<name>.yaml wiring and print the exact tool-policy
   steps ("set X to Automatic, Exclude built-in Y").
7. Register the tool in the gateway MCP's catalog (§5b) so it's discoverable with
   zero resting token cost.
```

The self-referential ("ouroboros") part: step 5 runs the tests **through your own
`shell` MCP**, and step 7 registers the new tool into the **gateway** so the *next*
generation can discover it. The toolkit literally grows itself, and each new tool
becomes immediately available to the agent that's building the *next* one.

A sketch of the skill's core instruction (what you'd put in `SKILL.md`):

> **You are a tool factory.** Given a spec, produce a FastMCP tool from the
> template. Rules: (1) descriptions ≤ 2 sentences and ≤ ~80 tokens — the agent
> pays for these on every request; (2) prefer one flexible tool over three narrow
> ones; (3) mark CPU-bound work for a Rust core, I/O-bound work stays Python;
> (4) every tool ships with a golden test and you must run it via `shell.run`
> before declaring done; (5) register the tool in the gateway catalog. Output the
> file tree, the code, the test result, and the Continue wiring.

### 6c. Why this is the right foundation

- **Uniformity** — one template means every tool has the same build, the same
  install (`uv`/wheel), the same wiring, the same test story. New tools are cheap.
- **Token discipline is enforced by the factory**, not left to willpower — the
  skill *bakes in* the terse-description budget and gateway registration.
- **Native speed is opt-in** — the Rust seam is in the template, dormant until a
  tool needs it.
- **Infinite expansion** — because resting token cost is decoupled from tool count
  (gateway), you can grow the toolkit without growing the prompt.

---

## 7. Recommended foundations (the order I'd build in)

1. **Prove MCP works in your reskin.** Ship `continue-mcp/hello-mcp/` (a `ping`
   that returns `"pong"`, plus `echo`/`whoami`), wire it via its `hello.yaml`, and
   ask the agent to call `ping`. Confirms MCP is enabled and you can wire tools.
   *Do this before anything else — it's the one thing that can invalidate the plan.*
2. **Build `shell` in pure-async Python (option B)** with the background-job model,
   process-group kill, timeout, and incremental `output`. Exclude the built-in
   `run_terminal_command`. This is 80% of the value.
3. **Prove the pure-Python cross-platform kill on both OSes** — `killpg` on
   Unix, `taskkill /T /F` on Windows — with a test that spawns a child-that-spawns-
   a-child and asserts the whole tree dies. Only *after* this is green do you add
   the maturin skeleton (copy from `rip`) and move Windows tree-kill into a unified
   Rust Job-Object implementation. This ordering matches your "pure-Python first,
   both OSes" decision: correctness in Python first, native unification second.
4. **`search` — ready in `continue-mcp/search-mcp/`.** Replaces Continue's built-in
   Grep/Glob search by shelling out to `rg` (`uv tool install ripgrep` — it's on
   PyPI). Native speed, gitignore-aware, compact structured output, hard result cap.
   Exclude the built-in Grep/Glob tools; set `search.*` to Automatic. (Want
   in-process, no binary? Embed the `grep`/`ignore` crates via PyO3 — same shape.)
5. **Stand up the gateway MCP** once you have ~5 tools and the token math favors it.
6. **Write the cookiecutter template + `new-mcp-tool` skill.** Now the toolkit
   builds itself.

---

## 8. Open questions (I need your read on these)

1. **Is MCP enabled in your reskinned/corporate Continue build?** (Deal-breaker if
   not — verify with the `hello` MCP first.) Do you control `.continue/config.yaml`
   and the per-tool policies, or are those locked by IT?
2. **Primary OS for the developers using this** — Windows, macOS, or both? This
   decides how hard the process-kill story is (Windows Job Objects push you toward
   a Rust core sooner) and whether PowerShell is the default shell.
3. **Sync convenience vs. pure background model** — do you want the simple
   `shell.run` one-liner alongside the job model, or keep the surface minimal?
4. **How "live" does output injection need to be?** Run-to-completion-then-inject
   (simplest) vs. true incremental streaming while the agent watches (the
   background model enables it, but the agent has to poll)?
5. **Toolkit scale you're targeting** — a handful of tools (skip the gateway) or
   dozens (build the gateway early)?
6. **Rust appetite now vs. later** — start pure-Python and add Rust selectively
   (my recommendation), or set up the maturin hybrid from commit one so the seam's
   always there?

---

## Sources

- [How Agent Mode Works — Continue Docs](https://docs.continue.dev/ide-extensions/agent/how-it-works) · [Agent quick start](https://docs.continue.dev/ide-extensions/agent/quick-start) · [How to Customize Agent Mode](https://docs.continue.dev/ide-extensions/agent/how-to-customize)
- [Tool Calling — Continue (DeepWiki)](https://deepwiki.com/continuedev/continue/4.5-tool-calling) · [Tool Permissions — Continue CLI docs](https://docs.continue.dev/cli/tool-permissions) · [config.yaml Reference](https://docs.continue.dev/reference)
- [Set up MCP in Continue](https://docs.continue.dev/customize/deep-dives/mcp) · [MCP servers — Continue Docs](https://docs.continue.dev/customize/mcp-tools)
- [FastMCP](https://github.com/PrefectHQ/fastmcp) · [FastMCP Background Tasks (SEP-1686)](https://gofastmcp.com/servers/tasks) · [mcp-background-job (dylan-gluck)](https://github.com/dylan-gluck/mcp-background-job) · [Fix MCP Timeouts: async HandleId pattern](https://dev.to/aws/fix-mcp-timeouts-async-handleid-pattern-8ek)
- [MCP context-bloat fix 2026: Tool Search / Code Mode / progressive disclosure](https://mcp.directory/blog/mcp-context-bloat-fix-2026-tool-search-code-mode-progressive-disclosure) · [MCP token optimization compared (StackOne)](https://www.stackone.com/blog/mcp-token-optimization/) · [Your MCP server is eating your context window (Apideck)](https://www.apideck.com/blog/mcp-server-eating-context-window-cli-alternative)
- [maturin](https://www.maturin.rs/) · [PyO3](https://pyo3.rs/) · [rmcp — Rust MCP SDK](https://github.com/modelcontextprotocol/rust-sdk) · [`rip` — Rust binary in a Python wheel (curtisalexander)](https://github.com/curtisalexander/rip) · [command-group crate](https://crates.io/crates/command-group) · [portable-pty](https://crates.io/crates/portable-pty)
</content>
</invoke>
