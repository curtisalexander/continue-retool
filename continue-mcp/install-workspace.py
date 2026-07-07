#!/usr/bin/env python3
"""
install-workspace.py — wire this toolkit into a project in one command.

Copies each server's .continue/mcpServers/*.yaml into the target project,
stamping the two absolute paths as it goes:
  * --project  -> this toolkit checkout (where the script lives)
  * MCP_WORKSPACE -> the target project root
and copies the two rules (notes discovery + the rule rule) into
.continue/rules/. Stdlib only; works on macOS/Linux/Windows.

Each *-mcp server is its own self-contained uv project (its own pyproject.toml
+ uv.lock), so after copying the config this script also runs `uv sync` in each
installed server's package dir. That builds the venvs now, at a terminal where
failures are visible — instead of lazily on first launch, when Continue spawns
the server headless and a slow first sync looks like a hang. Pass --no-sync to
skip (e.g. offline, or you'll sync by hand).

Re-running is safe: an existing .yaml/rule file is only rewritten when its
content actually changed, and the previous version is saved alongside as
<file>.bak before it's replaced.

Usage:
  python3 install-workspace.py /path/to/your/project
  python3 install-workspace.py /path/to/your/project --only shell,search,edit
  python3 install-workspace.py /path/to/your/project --no-sync
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
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


def write_out(dest: str, content: str) -> None:
    """Write content to dest, but only if it changed. When an existing file is
    about to be replaced, its old contents are saved to <dest>.bak first so a
    local edit is never silently lost."""
    if os.path.exists(dest):
        with open(dest, "r", encoding="utf-8") as f:
            old = f.read()
        if old == content:
            print(f"  unchanged  {dest}")
            return
        bak = dest + ".bak"
        with open(bak, "w", encoding="utf-8") as f:
            f.write(old)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  updated    {dest}  (backup -> {os.path.basename(bak)})")
        return
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  installed  {dest}")


def install(project: str, names: list[str]) -> None:
    mcp_dir = os.path.join(project, ".continue", "mcpServers")
    rules_dir = os.path.join(project, ".continue", "rules")
    os.makedirs(mcp_dir, exist_ok=True)
    os.makedirs(rules_dir, exist_ok=True)

    for name in names:
        src = os.path.join(KIT_DIR, f"{name}-mcp", ".continue", "mcpServers", f"{name}.yaml")
        with open(src, "r", encoding="utf-8") as f:
            content = stamp(f.read(), name, project)
        write_out(os.path.join(mcp_dir, f"{name}.yaml"), content)

    for rule in RULES:
        src = os.path.join(KIT_DIR, "rules", rule)
        with open(src, "r", encoding="utf-8") as f:
            content = f.read()
        write_out(os.path.join(rules_dir, rule), content)


def sync_deps(names: list[str]) -> int:
    """Run `uv sync` in each installed server's package dir so its venv is built
    now rather than lazily on first launch. Returns the number of failures."""
    uv = shutil.which("uv")
    if not uv:
        print("\nWARNING: `uv` not found on PATH — skipping dependency setup.\n"
              "Install uv (https://docs.astral.sh/uv/) then re-run, or run\n"
              "  uv sync --project <toolkit>/<name>-mcp\n"
              "in each server dir by hand. Until then the MCP servers can't start.",
              file=sys.stderr)
        return len(names)

    print(f"\nSyncing dependencies for {len(names)} server(s) (uv sync):")
    failures = 0
    for name in names:
        pkg = os.path.join(KIT_DIR, f"{name}-mcp")
        proc = subprocess.run(
            [uv, "sync", "--project", pkg],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            print(f"  synced     {name}-mcp")
        else:
            failures += 1
            print(f"  FAILED     {name}-mcp (exit {proc.returncode})", file=sys.stderr)
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
            for line in tail:
                print(f"    | {line}", file=sys.stderr)
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install the MCP toolkit into a project.")
    ap.add_argument("project", help="path to the project (workspace root)")
    ap.add_argument("--only", default="",
                    help=f"comma-separated subset of: {','.join(SERVERS)}")
    ap.add_argument("--no-sync", action="store_true",
                    help="skip `uv sync` of the server packages (config files only)")
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

    failures = 0
    if args.no_sync:
        print("\nSkipping `uv sync` (--no-sync). Run it in each *-mcp dir before "
              "the servers will start.")
    else:
        failures = sync_deps(names)

    print(POLICY_CHECKLIST)
    if failures:
        print(f"WARNING: {failures} server(s) did not sync — see errors above.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
