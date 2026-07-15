"""
audit.py — measure what each server actually costs: cold-start latency and
resting token weight.

The toolkit's two performance currencies are (1) time from spawn to a usable
tool list — what Continue's connectionTimeout is waiting on — and (2) the
resting context cost of every advertised tool schema, which is paid on every
request. The design docs cite ~550–1,400 tokens/tool from external sources;
this script prints OUR numbers, so a dependency bump or a fattened docstring
shows up as a measured regression instead of a vibe.

Each server is spawned exactly the way the stamped yaml does it
(`uv run --no-sync --project <pkg> <name>-mcp`, a real subprocess over stdio),
so cold-start here includes interpreter start + fastmcp import — the real
thing, not an in-process shortcut.

Token counts are estimated as ceil(chars / 4) over the serialized
{name, description, inputSchema}: deterministic and offline (a real tokenizer
would need a network fetch). Treat them as comparable-over-time, not exact.

Run from the continue-mcp dir (any server's venv provides fastmcp):
  uv run --no-sync --project hello-mcp python bench/audit.py           # all
  uv run --no-sync --project hello-mcp python bench/audit.py shell fs  # subset
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import sys
import time

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

KIT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVERS = ["hello", "shell", "search", "edit", "fs", "sql", "notes"]


def est_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


async def audit_one(uv: str, name: str) -> dict:
    pkg = os.path.join(KIT_DIR, f"{name}-mcp")
    transport = StdioTransport(
        command=uv, args=["run", "--no-sync", "--project", pkg, f"{name}-mcp"],
    )
    t0 = time.monotonic()
    async with Client(transport) as client:
        tools = await client.list_tools()
        cold_ms = (time.monotonic() - t0) * 1000
    rows = []
    for t in tools:
        blob = json.dumps({
            "name": t.name,
            "description": t.description or "",
            "inputSchema": t.inputSchema or {},
        }, separators=(",", ":"))
        rows.append({"tool": t.name, "chars": len(blob),
                     "est_tokens": est_tokens(blob)})
    return {"server": name, "cold_ms": cold_ms, "tools": rows,
            "est_tokens": sum(r["est_tokens"] for r in rows)}


async def main(names: list[str]) -> int:
    uv = shutil.which("uv")
    if not uv:
        print("uv not found on PATH", file=sys.stderr)
        return 1
    print(f"{'server':<10} {'cold-start':>10}   {'tools':>5}   {'est tokens at rest':>18}")
    grand = 0
    slowest = 0.0
    for name in names:
        r = await audit_one(uv, name)
        grand += r["est_tokens"]
        slowest = max(slowest, r["cold_ms"])
        print(f"{r['server']:<10} {r['cold_ms']:>8.0f}ms   {len(r['tools']):>5}   "
              f"{r['est_tokens']:>18}")
        for row in r["tools"]:
            print(f"  · {row['tool']:<20} {row['est_tokens']:>6} tok "
                  f"({row['chars']} chars)")
    print(f"\ntotal resting tool-definition cost (all {len(names)} servers): "
          f"~{grand} tokens/request")
    print(f"slowest cold start: {slowest:.0f}ms "
          f"(Continue's connectionTimeout is 120000ms)")
    return 0


if __name__ == "__main__":
    picked = sys.argv[1:] or SERVERS
    unknown = sorted(set(picked) - set(SERVERS))
    if unknown:
        print(f"unknown server(s) {unknown}; choose from {SERVERS}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(picked)))
