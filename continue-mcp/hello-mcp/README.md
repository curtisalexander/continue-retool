# hello-mcp — the enablement check

The single most important 5 minutes in the whole project: prove MCP is actually
turned on in your (possibly reskinned/corporate) Continue build **before**
investing in the real servers. If `ping` returns `"pong"` from inside Continue's
agent, MCP works and the plan is viable. If it doesn't, stop and find out
whether IT disabled MCP or locked the `.continue` config — that one fact can
invalidate everything downstream.

## Tools

| Tool | What it does |
|---|---|
| `hello.ping()` | Returns `"pong"` — MCP is on and this server is reachable |
| `hello.echo(text)` | Round-trips an argument — schemas work in both directions |
| `hello.whoami()` | Host OS/arch, the server's cwd, `MCP_WORKSPACE`, and the base relative paths resolve against |

All three are read-only (annotated `readOnlyHint`); set `hello.*` to
**Automatic**.

## Setup

```bash
uv run --extra test pytest -q   # golden + MCP-surface tests
uv run hello-mcp                # run the server (stdio)
```

Then register `.continue/mcpServers/hello.yaml` (the installer stamps it for
you) and paste this into Continue's Agent chat:

> Call the hello.ping tool and show me the raw result, then call hello.whoami
> and tell me the cwd and MCP_WORKSPACE it reports.

A `pong` means MCP is on; `whoami`'s paths should point at YOUR project — if
they point at this toolkit checkout instead, the yaml's `MCP_WORKSPACE` stamp
is wrong (re-run the installer, or run it with `--check`).
