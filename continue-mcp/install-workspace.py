#!/usr/bin/env -S uv run --script
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

Usage (uv runs it via the shebang — `python3` isn't on PATH on Windows):
  uv run install-workspace.py /path/to/your/project
  uv run install-workspace.py /path/to/your/project --only shell,search,edit
  uv run install-workspace.py /path/to/your/project --no-sync
  uv run install-workspace.py /path/to/your/project --jobs 1   # sequential
  uv run install-workspace.py /path/to/your/project --check    # doctor: verify
  uv run install-workspace.py /path/to/your/project --uninstall
Or, on a Unix shell, directly: ./install-workspace.py /path/to/your/project

Doctor mode (--check) verifies an install end-to-end: uv present, each server's
package dir + venv, the stamped yamls in the project, detected interpreters,
and a LIVE MCP handshake (initialize + tools/list over stdio — the same flow
Continue performs at connect). It compresses the troubleshooting checklist
into one command; run it whenever a server shows "connection timed out".
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

The Automatic grants are safe because fs/search/edit are workspace-JAILED by
default: paths outside MCP_WORKSPACE are refused (realpath'd, so symlinks
can't tunnel out). MCP_JAIL_EXTRA adds roots; MCP_JAIL=0 disables. shell is
the approval-gated escape hatch for legitimate out-of-workspace access.

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


# --- shell-mcp interpreter detection ---------------------------------------
# The shell server spawns an interpreter (pwsh/powershell/bash/cmd) per call and
# has to *find* it first. From GUI-launched VS Code its inherited PATH is often
# stale/thin (pwsh lives in Program Files, not a guaranteed dir; pwsh 7 may not
# be installed at all). So we resolve interpreters HERE — in a real terminal
# where PATH is correct — and stamp their absolute paths into the yaml env, the
# same tactic already used for uv in command:. The server still resolves at
# runtime when these are absent (manual install, or a shell added afterward).
_SHELL_INTERPRETERS = ["pwsh", "powershell", "cmd", "bash"]


def _interp_known_locations(shell: str) -> list[str]:
    """Fixed install paths to try when PATH lookup misses. Mirrors
    shell_mcp.server._known_locations — keep in sync by hand (this stdlib-only
    script can't import the server package before its venv exists)."""
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    return {
        "pwsh": [os.path.join(pf, "PowerShell", "7", "pwsh.exe")],
        "powershell": [os.path.join(
            sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")],
        "cmd": [os.path.join(sysroot, "System32", "cmd.exe")],
        "bash": ["/bin/bash", "/usr/bin/bash",
                 os.path.join(pf, "Git", "bin", "bash.exe")],
    }.get(shell, [])


def detect_interpreters() -> dict[str, str]:
    """Resolve which shells exist on THIS machine -> {shell: absolute path}.
    PATH first, then the known install locations — so a pwsh that lives in
    Program Files but not on PATH still gets stamped."""
    found: dict[str, str] = {}
    for shell in _SHELL_INTERPRETERS:
        exe = shutil.which(shell)
        if not exe:
            exe = next((c for c in _interp_known_locations(shell)
                        if os.path.isfile(c)), None)
        if exe:
            found[shell] = _slashes(exe)
    return found


def _default_shell_for(found: dict[str, str]) -> str:
    """Prefer a real interpreter: pwsh > powershell > cmd on Windows, bash off it."""
    order = ("pwsh", "powershell", "cmd") if os.name == "nt" else ("bash",)
    for s in order:
        if s in found:
            return s
    return next(iter(found), "bash")


def stamp_shell_interpreters(text: str) -> str:
    """Replace the __SHELL_INTERPRETERS__ marker line in shell.yaml with the
    detected interpreter paths + default shell, indented to match the marker."""
    marker = "# __SHELL_INTERPRETERS__"
    found = detect_interpreters()
    out: list[str] = []
    for line in text.splitlines():
        if marker in line:
            indent = line[: len(line) - len(line.lstrip())]
            if found:
                for shell, path in found.items():
                    out.append(f'{indent}SHELL_MCP_{shell.upper()}: "{path}"')
                out.append(f'{indent}SHELL_MCP_DEFAULT_SHELL: "{_default_shell_for(found)}"')
                shells = ", ".join(found)
                print(f"  detected shells for shell-mcp: {shells} "
                      f"(default {_default_shell_for(found)})")
            else:
                out.append(f"{indent}# no shells detected at install; "
                           f"server resolves interpreters at runtime")
                print("  WARNING: no pwsh/powershell/bash/cmd found on PATH — "
                      "shell-mcp will resolve interpreters at runtime.",
                      file=sys.stderr)
        else:
            out.append(line)
    result = "\n".join(out)
    return result + "\n" if text.endswith("\n") else result


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
        if name == "shell":
            content = stamp_shell_interpreters(content)
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


# --- doctor (--check): verify an install end-to-end -------------------------
def _mcp_handshake(cmd: list[str], timeout: float = 30.0) -> tuple[bool, str]:
    """Spawn a stdio MCP server and drive a real initialize -> tools/list
    handshake with plain newline-delimited JSON-RPC (stdlib only). This is the
    same flow Continue performs at connect, so a pass here means the yaml's
    command actually produces a working server."""
    import json
    import queue

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
    except OSError as e:
        return False, f"spawn failed: {e}"

    q: queue.Queue = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line)

    threading.Thread(target=_reader, daemon=True).start()

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv(want_id: int) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                line = q.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError from None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                return msg

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "install-workspace-doctor", "version": "1.0"}}})
        recv(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = recv(2).get("result", {}).get("tools", [])
        return True, f"{len(tools)} tool(s): {', '.join(t['name'] for t in tools)}"
    except TimeoutError:
        return False, f"no reply within {timeout:.0f}s"
    except Exception as e:  # any protocol surprise is a failed check, not a crash
        return False, f"handshake error: {e}"
    finally:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass


def _rg_status(names: list[str]) -> tuple[bool, str]:
    """Resolve ripgrep the way search-mcp's rg_bin() does — RIPGREP_BIN, then the
    search venv's bundled `rg` (the optional `rg` extra), then a system rg on PATH.
    Returns (ok, human detail). Only meaningful when search is being installed."""
    override = os.environ.get("RIPGREP_BIN")
    if override:
        return os.path.isfile(override), f"RIPGREP_BIN={override}"
    scripts = "Scripts" if os.name == "nt" else "bin"
    exe = "rg.exe" if os.name == "nt" else "rg"
    bundled = os.path.join(KIT_DIR, "search-mcp", ".venv", scripts, exe)
    if os.path.isfile(bundled):
        return True, f"bundled ripgrep-bin ({bundled})"
    system = shutil.which("rg")
    if system:
        return True, f"system rg ({system})"
    return False, (
        "not found — search will fail. Fix with ONE of:\n"
        "         brew install ripgrep   (or apt/choco/your package manager)\n"
        "         uv tool install ripgrep-bin   (global prebuilt rg)\n"
        f"         uv sync --project {os.path.join(KIT_DIR, 'search-mcp')} --extra rg\n"
        "         set RIPGREP_BIN=/abs/path/to/rg\n"
        "       ripgrep-bin is a third-party repackage of ripgrep's official "
        "binaries; prefer a system rg or RIPGREP_BIN to avoid it."
    )


def doctor(project: str, names: list[str]) -> int:
    """Verify the install: uv, package dirs + venvs, stamped yamls, detected
    interpreters, and a LIVE MCP handshake per server. Returns failure count."""
    failures = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        mark = "ok " if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures += 1

    uv = shutil.which("uv")
    print("doctor: toolkit checkout")
    check(bool(uv), "uv on PATH", uv or "not found — install https://docs.astral.sh/uv/")
    for name in names:
        pkg = os.path.join(KIT_DIR, f"{name}-mcp")
        venv = os.path.join(pkg, ".venv")
        check(os.path.isdir(pkg), f"{name}-mcp package dir", pkg)
        check(os.path.isdir(venv), f"{name}-mcp venv",
              venv if os.path.isdir(venv) else "missing — run the installer (or uv sync)")

    print("doctor: shells for shell-mcp")
    found = detect_interpreters()
    check(bool(found), "interpreters detected",
          ", ".join(f"{k}={v}" for k, v in found.items()) or "none found")

    if "search" in names:
        print("doctor: ripgrep for search-mcp")
        rg_ok, rg_detail = _rg_status(names)
        check(rg_ok, "ripgrep resolves", rg_detail)

    print(f"doctor: project config ({project})")
    mcp_dir = os.path.join(project, ".continue", "mcpServers")
    for name in names:
        dest = os.path.join(mcp_dir, f"{name}.yaml")
        if not os.path.isfile(dest):
            check(False, f"{name}.yaml", "not installed")
            continue
        with open(dest, "r", encoding="utf-8") as f:
            content = f.read()
        stamped = "/absolute/path/to" not in content
        check(stamped, f"{name}.yaml stamped",
              dest if stamped else "unstamped placeholders — re-run the installer")

    print("doctor: live MCP handshake (initialize + tools/list, like Continue does)")
    if not uv:
        check(False, "handshake", "skipped — no uv")
        return failures
    for name in names:
        pkg = os.path.join(KIT_DIR, f"{name}-mcp")
        ok, detail = _mcp_handshake(
            [uv, "run", "--no-sync", "--project", pkg, f"{name}-mcp"])
        check(ok, f"{name}-mcp responds", detail)
    return failures


def uninstall(project: str, names: list[str], all_selected: bool) -> None:
    """Remove the stamped yamls (and rules, when every server is selected) from
    the project. Only files this installer writes are touched; .bak too."""
    removed = 0
    targets = [os.path.join(project, ".continue", "mcpServers", f"{n}.yaml")
               for n in names]
    if all_selected:
        targets += [os.path.join(project, ".continue", "rules", r) for r in RULES]
    for t in targets:
        for path in (t, t + ".bak"):
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
                print(f"  removed  {path}")
    print(f"Uninstalled {removed} file(s) from {project}. The toolkit checkout "
          "and its venvs are untouched.")


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
    ap.add_argument("--check", action="store_true",
                    help="doctor mode: verify venvs, stamps, interpreters, and a "
                         "live MCP handshake per server; installs nothing")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove the installed yamls/rules from the project")
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

    if args.check:
        failures = doctor(project, names)
        if failures:
            print(f"\ndoctor: {failures} check(s) failed.", file=sys.stderr)
        else:
            print("\ndoctor: all checks passed.")
        return 1 if failures else 0

    if args.uninstall:
        uninstall(project, names, set(names) == set(SERVERS))
        return 0

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
