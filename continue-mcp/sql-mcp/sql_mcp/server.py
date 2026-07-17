"""
sql-mcp — SQL formatting and linting for Continue.dev, backed by sqruff.

sqruff (https://github.com/quarylabs/sqruff) is a fast SQL linter/formatter
written in Rust and shipped as prebuilt wheels on PyPI, so it installs as a
plain dependency of this package — the same "proven native binary behind a
Python MCP" pattern search-mcp uses with ripgrep.

House style lives in sql_mcp/default.sqruff (Snowflake dialect, lowercase
everything, leading commas). Point SQL_MCP_CONFIG at your own .sqruff to
override it without touching this package.

Tools:
  sql.format(sql, dialect?) -> the SQL rewritten to house style
  sql.lint(sql, dialect?)   -> structured violations (code, line, column, message)

Run:  uv run sql-mcp
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from typing import Optional

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

mcp = FastMCP("sql")


def _result(summary: str, data: dict, block: str = "", lang: str = "") -> ToolResult:
    """content is what Continue's UI shows (summary + optional fenced block);
    structured_content is what the model/tests read via res.data."""
    md = summary
    if block.strip():
        md += f"\n\n```{lang}\n{block}\n```"
    return ToolResult(content=[TextContent(type="text", text=md)], structured_content=data)

DEFAULT_TIMEOUT = float(os.environ.get("SQL_MCP_TIMEOUT", "30"))


def sqruff_bin() -> str:
    """Find the sqruff binary: SQRUFF_BIN, PATH, then the running venv's bin."""
    b = os.environ.get("SQRUFF_BIN") or shutil.which("sqruff")
    if not b:
        candidate = os.path.join(
            os.path.dirname(sys.executable),
            "sqruff.exe" if sys.platform.startswith("win") else "sqruff",
        )
        if os.path.exists(candidate):
            b = candidate
    if not b:
        raise RuntimeError(
            "sqruff not found. It ships with this package (pip/uv install), or "
            "point SQRUFF_BIN at the binary."
        )
    return b


def config_path() -> str:
    """The .sqruff config: SQL_MCP_CONFIG overrides the packaged house style."""
    override = os.environ.get("SQL_MCP_CONFIG")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "default.sqruff")


async def _run_sqruff(subcmd: list[str], sql: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sqruff_bin(), *subcmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(sql.encode("utf-8")), DEFAULT_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"sqruff timed out after {DEFAULT_TIMEOUT}s")
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _base_args(cmd: str, dialect: Optional[str]) -> list[str]:
    args = [cmd, "--config", config_path()]
    if dialect:
        args += ["--dialect", dialect]
    args.append("-")  # read from stdin
    return args


# --- tools -----------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def format(sql: str, dialect: Optional[str] = None) -> ToolResult:
    """Format SQL to house style (lowercase keywords/identifiers, leading commas;
    Snowflake dialect by default). Returns the rewritten SQL and whether it changed."""
    if not sql.strip():
        # Empty / whitespace-only input is a no-op, not a failure: sqruff exits 0
        # with no stdout for it, which would otherwise trip the no-output branch
        # below and misreport a false "unparsable SQL" error.
        data = {"ok": True, "sql": sql, "changed": False}
        return _result("formatted (no change) — empty input", data, block=sql, lang="sql")
    rc, out, err = await _run_sqruff(_base_args("fix", dialect), sql)
    # fix -: fixed SQL on stdout, lint report on stderr. Unparsable input yields
    # no usable rewrite — report the error instead of returning garbage.
    if not out.strip():
        detail = err.strip().splitlines()
        data = {"ok": False, "error": "sqruff produced no output (unparsable SQL?)",
                "detail": detail[-5:] if detail else []}
        return _result(f"❌ {data['error']}", data, block="\n".join(data.get('detail') or []))
    out = out.rstrip("\n") + "\n"  # sqruff pads stdout with an extra newline
    data = {"ok": True, "sql": out, "changed": out.rstrip("\n") != sql.rstrip("\n")}
    summary = "formatted (changed)" if data["changed"] else "formatted (no change)"
    return _result(summary, data, block=data["sql"], lang="sql")


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def lint(sql: str, dialect: Optional[str] = None) -> ToolResult:
    """Lint SQL against house style (Snowflake dialect by default). Returns
    violations as {code, line, column, message}; empty list means clean."""
    rc, out, err = await _run_sqruff(
        _base_args("lint", dialect) + ["-f", "json"], sql
    )
    if rc != 0 and not out.strip():
        # sqruff died without producing a report (bad config, crashed binary).
        # This must NOT read as "clean" — a false clean is the worst failure
        # mode a linter can have.
        detail = err.strip().splitlines()
        data = {"ok": False, "error": f"sqruff failed (exit {rc}) with no report",
                "detail": detail[-5:] if detail else []}
        return _result(f"❌ {data['error']}", data, block="\n".join(data["detail"]))
    try:
        parsed = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        data = {"ok": False, "error": "could not parse sqruff output",
                "detail": (err or out).strip().splitlines()[-5:]}
        return _result(f"❌ {data['error']}", data, block="\n".join(data.get('detail') or []))
    violations = []
    for _fname, diags in parsed.items():
        for d in diags:
            start = d.get("range", {}).get("start", {})
            violations.append({
                "code": d.get("code"),
                "line": start.get("line"),
                "column": start.get("character"),
                "message": d.get("message"),
            })
    data = {"ok": True, "violations": violations, "count": len(violations)}
    if data["count"] == 0:
        return _result("clean — 0 violations", data)
    block = "\n".join(f"{v['line']}:{v['column']} {v['code']} {v['message']}" for v in data["violations"])
    return _result(f"{data['count']} violation(s)", data, block)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
