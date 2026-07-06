#!/usr/bin/env python3
"""
install-workspace.py — wire this toolkit into a project in one command.

Copies each server's .continue/mcpServers/*.yaml into the target project,
stamping the two absolute paths as it goes:
  * --project  -> this toolkit checkout (where the script lives)
  * MCP_WORKSPACE -> the target project root
and copies the two rules (notes discovery + the rule rule) into
.continue/rules/. Stdlib only; works on macOS/Linux/Windows.

Usage:
  python3 install-workspace.py /path/to/your/project
  python3 install-workspace.py /path/to/your/project --only shell,search,edit
"""
from __future__ import annotations

import argparse
import os
import sys

KIT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVERS = ["hello", "shell", "search", "edit", "fs", "sql", "notes"]
RULES = ["notes.md", "rule-rule.md"]

POLICY_CHECKLIST = """
Next, in Continue's Agent tool settings:
  * built-in run_terminal_command      -> Excluded;  shell.*  -> Ask First
  * built-in Grep search / Glob search -> Excluded;  search.* -> Automatic
  * built-in Edit file / Create file   -> Excluded;  edit.*   -> Ask First
  * built-in Read file / List dir      -> Excluded;  fs.*     -> Automatic
  * sql.* and notes.*                  -> Automatic (replace no built-ins)

Then ask the agent to call hello.ping (proves MCP is on) and hello.whoami
(shows the cwd and MCP_WORKSPACE the servers actually see).

Using the gateway instead? Register gateway.yaml by hand and do NOT install
the downstream servers here too — see gateway-mcp/README.md.
"""


def _slashes(p: str) -> str:
    """Forward slashes everywhere: valid on Windows, and avoids backslash
    escapes inside double-quoted YAML strings."""
    return os.path.abspath(p).replace("\\", "/")


def stamp(text: str, server: str, workspace: str) -> str:
    text = text.replace(
        f"/absolute/path/to/continue-mcp/{server}-mcp",
        _slashes(os.path.join(KIT_DIR, f"{server}-mcp")),
    )
    text = text.replace("/absolute/path/to/your/workspace", _slashes(workspace))
    if "/absolute/path/to" in text:
        raise RuntimeError(f"{server}.yaml still has an unstamped placeholder")
    return text


def install(project: str, names: list[str]) -> None:
    mcp_dir = os.path.join(project, ".continue", "mcpServers")
    rules_dir = os.path.join(project, ".continue", "rules")
    os.makedirs(mcp_dir, exist_ok=True)
    os.makedirs(rules_dir, exist_ok=True)

    for name in names:
        src = os.path.join(KIT_DIR, f"{name}-mcp", ".continue", "mcpServers", f"{name}.yaml")
        with open(src, "r", encoding="utf-8") as f:
            content = stamp(f.read(), name, project)
        dest = os.path.join(mcp_dir, f"{name}.yaml")
        existed = os.path.exists(dest)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  {'updated' if existed else 'installed'}  {dest}")

    for rule in RULES:
        src = os.path.join(KIT_DIR, "rules", rule)
        dest = os.path.join(rules_dir, rule)
        with open(src, "r", encoding="utf-8") as f:
            content = f.read()
        existed = os.path.exists(dest)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  {'updated' if existed else 'installed'}  {dest}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install the MCP toolkit into a project.")
    ap.add_argument("project", help="path to the project (workspace root)")
    ap.add_argument("--only", default="",
                    help=f"comma-separated subset of: {','.join(SERVERS)}")
    args = ap.parse_args(argv)

    project = os.path.abspath(args.project)
    if not os.path.isdir(project):
        print(f"error: {project} is not a directory", file=sys.stderr)
        return 1
    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(SERVERS)
    unknown = sorted(set(names) - set(SERVERS))
    if unknown:
        print(f"error: unknown server(s) {unknown}; choose from {SERVERS} "
              "(the gateway is deliberately not installable here — see "
              "gateway-mcp/README.md)", file=sys.stderr)
        return 1

    print(f"Installing {len(names)} server(s) + {len(RULES)} rule(s) into {project}")
    install(project, names)
    print(POLICY_CHECKLIST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
