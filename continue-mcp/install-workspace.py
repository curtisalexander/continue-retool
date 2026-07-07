#!/usr/bin/env python3
"""
install-workspace.py — wire this toolkit into a project in one command.

Copies each server's .continue/mcpServers/*.yaml into the target project,
stamping the absolute paths as it goes:
  * command     -> the absolute path to `uv` (so Continue doesn't depend on the
                   PATH it was launched with — GUI-launched VS Code often lacks
                   the shell PATH where uv lives)
  * --project   -> this toolkit checkout (where the script lives)
  * MCP_WORKSPACE -> the target project root
and copies the two rules (notes discovery + the rule rule) into
.continue/rules/. Stdlib only; works on macOS/Linux/Windows.

The yaml launches each server with `uv run --no-sync` so startup never touches
the network (the venv is already built by this installer). Without --no-sync,
`uv run` tries to sync against the index on every launch, which hangs behind a
corporate proxy and shows up in Continue as a connection timeout.

Each *-mcp server is its own self-contained uv project (its own pyproject.toml
+ uv.lock), so after copying the config this script also runs `uv sync` in each
installed server's package dir. That builds the venvs now, at a terminal where
failures are visible — instead of lazily on first launch, when Continue spawns
the server headless and a slow first sync looks like a hang. Pass --no-sync to
skip (e.g. offline, or you'll sync by hand).

Corporate networks: `uv sync` reaches a package index, so behind a proxy set
UV_SYSTEM_CERTS=true (trust the OS cert store) and UV_DEFAULT_INDEX=<mirror>
before running this — or put system-certs/index in a user/system uv.toml. Both
apply to every uv call, so no per-directory pyproject edits. See README.

Re-running is safe: an existing .yaml/rule file is only rewritten when its
content actually changed, and the previous version is saved alongside as
<file>.bak before it's replaced.

The syncs run in parallel (a thread per server, capped at --jobs). Each
server's uv output is captured rather than interleaved; you get a live
[done/total] line as each finishes, a heartbeat naming whatever's still running
on long cold-cache runs, and the full captured error for any server that fails.

Usage:
  python3 install-workspace.py /path/to/your/project
  python3 install-workspace.py /path/to/your/project --only shell,search,edit
  python3 install-workspace.py /path/to/your/project --no-sync
  python3 install-workspace.py /path/to/your/project --jobs 1   # sequential
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import subprocess
import sys
import threading
import time

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

Suggested first prompt — paste this into Continue's Agent chat to confirm MCP
is live and the servers see the right workspace:

  Call the hello.ping tool and show me the raw result, then call hello.whoami
  and tell me the cwd and MCP_WORKSPACE it reports.

A "pong" back means MCP is on; whoami's paths should point at THIS project.

Using the gateway instead? Register gateway.yaml by hand and do NOT install
the downstream servers here too — see gateway-mcp/README.md.
"""

CORP_NOTE = (
    "  Behind a corporate proxy? `uv sync` needs UV_SYSTEM_CERTS=true and\n"
    "  UV_DEFAULT_INDEX=<your mirror> set (or a user/system uv.toml). See README."
)


def _slashes(p: str) -> str:
    """Forward slashes everywhere: valid on Windows, and avoids backslash
    escapes inside double-quoted YAML strings."""
    return os.path.abspath(p).replace("\\", "/")


def stamp(text: str, server: str, workspace: str, uv_path: str) -> str:
    text = text.replace("/absolute/path/to/uv", uv_path)
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


def install(project: str, names: list[str], uv_path: str) -> None:
    mcp_dir = os.path.join(project, ".continue", "mcpServers")
    rules_dir = os.path.join(project, ".continue", "rules")
    os.makedirs(mcp_dir, exist_ok=True)
    os.makedirs(rules_dir, exist_ok=True)

    for name in names:
        src = os.path.join(KIT_DIR, f"{name}-mcp", ".continue", "mcpServers", f"{name}.yaml")
        with open(src, "r", encoding="utf-8") as f:
            content = stamp(f.read(), name, project, uv_path)
        write_out(os.path.join(mcp_dir, f"{name}.yaml"), content)

    for rule in RULES:
        src = os.path.join(KIT_DIR, "rules", rule)
        with open(src, "r", encoding="utf-8") as f:
            content = f.read()
        write_out(os.path.join(rules_dir, rule), content)


def _sync_one(uv: str, name: str, pkg: str) -> dict:
    """Sync one server package; return its captured result. Runs in a worker
    thread — subprocess.run releases the GIL while uv works, so N of these
    overlap. Never raises: a failure is reported via the returned dict."""
    start = time.monotonic()
    proc = subprocess.run(
        [uv, "sync", "--project", pkg],
        capture_output=True, text=True,
    )
    return {
        "name": name,
        "rc": proc.returncode,
        "out": proc.stdout,
        "err": proc.stderr,
        "dur": time.monotonic() - start,
    }


def _summary_line(out: str, err: str) -> str:
    """Pull uv's one-line tally (e.g. 'Installed 67 packages in 87ms') out of the
    captured output, so a successful sync gets a compact summary instead of its
    full package dump. Empty string if no such line is found."""
    markers = ("Installed", "Audited", "Prepared", "Resolved")
    for line in reversed((out + err).splitlines()):
        s = line.strip()
        if "package" in s and any(s.startswith(m) for m in markers):
            return s
    return ""


def sync_deps(names: list[str], jobs: int = 0) -> int:
    """Run `uv sync` for each installed server's package (in parallel, up to
    `jobs` workers; 0 = auto) so venvs are built now rather than lazily on first
    launch. Returns the number of servers that failed to sync."""
    uv = shutil.which("uv")
    if not uv:
        print("\nWARNING: `uv` not found on PATH — skipping dependency setup.\n"
              "Install uv (https://docs.astral.sh/uv/) then re-run, or run\n"
              "  uv sync --project <toolkit>/<name>-mcp\n"
              "in each server dir by hand. Until then the MCP servers can't start.",
              file=sys.stderr)
        return len(names)

    total = len(names)
    workers = jobs if jobs > 0 else min(total, (os.cpu_count() or 4))
    workers = max(1, min(workers, total))
    print(f"\nSyncing dependencies for {total} server(s), up to {workers} in "
          "parallel (uv sync).")
    print(CORP_NOTE)
    print("Each server's uv output is captured (not interleaved); a [done/total]\n"
          "line prints as each finishes. On a cold cache the first run downloads +\n"
          "builds wheels, so allow a few minutes — a heartbeat names what's left.")

    # Parallelism is safe: uv locks its global cache per entry, so concurrent
    # `uv sync` processes share one download of any package rather than racing.
    lock = threading.Lock()
    running = set(names)
    stop = threading.Event()
    start_all = time.monotonic()

    def heartbeat() -> None:
        while not stop.wait(10):
            with lock:
                still = sorted(running)
                if still:
                    el = time.monotonic() - start_all
                    print(f"    ... still syncing ({len(still)}): "
                          f"{', '.join(n + '-mcp' for n in still)}  [{el:.0f}s]",
                          flush=True)
    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    results: dict[str, dict] = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_sync_one, uv, n, os.path.join(KIT_DIR, f"{n}-mcp"))
                for n in names]
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            results[r["name"]] = r
            done += 1
            ok = r["rc"] == 0
            tag = "synced" if ok else f"FAILED (exit {r['rc']})"
            summary = _summary_line(r["out"], r["err"])
            extra = f"  — {summary}" if ok and summary else ""
            with lock:
                running.discard(r["name"])
                print(f"[{done}/{total}] {tag:18} {r['name']}-mcp  "
                      f"({r['dur']:.1f}s){extra}",
                      file=sys.stdout if ok else sys.stderr, flush=True)
    stop.set()

    failed = [n for n in names if results[n]["rc"] != 0]
    if failed:
        print("\nSync failures — captured uv output:", file=sys.stderr)
        for n in failed:
            r = results[n]
            print(f"  {n}-mcp (exit {r['rc']}):", file=sys.stderr)
            for line in (r["err"] or r["out"]).strip().splitlines()[-8:]:
                print(f"    | {line}", file=sys.stderr)
    return len(failed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install the MCP toolkit into a project.")
    ap.add_argument("project", help="path to the project (workspace root)")
    ap.add_argument("--only", default="",
                    help=f"comma-separated subset of: {','.join(SERVERS)}")
    ap.add_argument("--no-sync", action="store_true",
                    help="skip `uv sync` of the server packages (config files only)")
    ap.add_argument("--jobs", type=int, default=0, metavar="N",
                    help="parallel `uv sync` workers (default: min(servers, CPUs); "
                         "1 = sequential)")
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

    # Stamp uv's absolute path into `command:` so Continue doesn't rely on the
    # PATH it inherits. Fall back to bare "uv" (PATH lookup) only if uv isn't
    # found now — the sync step below will warn about that separately.
    uv = shutil.which("uv")
    uv_path = _slashes(uv) if uv else "uv"
    if not uv:
        print("WARNING: `uv` not found on PATH — leaving command: uv in the yaml "
              "(Continue must find uv on its own PATH).", file=sys.stderr)

    print(f"Installing {len(names)} server(s) + {len(RULES)} rule(s) into {project}")
    install(project, names, uv_path)

    failures = 0
    if args.no_sync:
        print("\nSkipping `uv sync` (--no-sync). Run it in each *-mcp dir before "
              "the servers will start.")
    else:
        failures = sync_deps(names, args.jobs)

    print(POLICY_CHECKLIST)
    if failures:
        print(f"WARNING: {failures} server(s) did not sync — see errors above.",
              file=sys.stderr)
        print(CORP_NOTE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
