"""Generate packaging and current inventory surfaces from servers.json."""
from __future__ import annotations
import argparse
import html
import json
import sys
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parents[1]
ROOT = KIT_DIR.parent
sys.path.insert(0, str(KIT_DIR))
from continue_mcp_common.metadata import load_servers  # noqa: E402

def replace(path: Path, start: str, end: str, body: str, check: bool) -> bool:
    old = path.read_text(encoding="utf-8")
    if old.count(start) != 1 or old.count(end) != 1:
        raise RuntimeError(f"{path}: expected one generated marker pair")
    before, rest = old.split(start, 1)
    _, after = rest.split(end, 1)
    new = before + start + "\n" + body.rstrip() + "\n" + end + after
    if new == old:
        return False
    if check:
        print(f"stale generated content: {path.relative_to(ROOT)}", file=sys.stderr)
    else:
        path.write_text(new, encoding="utf-8", newline="\n")
    return True

def sync(check: bool) -> bool:
    servers = load_servers()
    for server in servers:
        base = KIT_DIR / f"{server['name']}-mcp"
        if not (base / server["module"] / "server.py").is_file():
            raise RuntimeError(f"missing package for {server['name']}")
    changed = False
    changed |= replace(KIT_DIR / "pyproject.toml", "# BEGIN GENERATED SERVER ENTRY POINTS", "# END GENERATED SERVER ENTRY POINTS", "\n".join(f'{s["name"]}-mcp = "{s["module"]}.server:main"' for s in servers), check)
    changed |= replace(KIT_DIR / "pyproject.toml", "# BEGIN GENERATED SERVER PACKAGES", "# END GENERATED SERVER PACKAGES", "\n".join(f'  "{s["name"]}-mcp/{s["module"]}",' for s in servers), check)
    changed |= replace(ROOT / "README.md", "<!-- BEGIN GENERATED SERVER INVENTORY -->", "<!-- END GENERATED SERVER INVENTORY -->", "\n".join(f'  - `{s["name"]}-mcp/` — {s["summary"]}' for s in servers), check)
    changed |= replace(ROOT / "ARCHITECTURE.md", "<!-- BEGIN GENERATED COMPONENT INVENTORY -->", "<!-- END GENERATED COMPONENT INVENTORY -->", "\n".join(f'| `{s["name"]}-mcp` | {s["responsibility"]} | {s["authority"]} |' for s in servers), check)
    cards = "\n".join('    <div class="tool">\n' f'      <p class="n">{html.escape(s["name"])}-mcp</p>\n' f'      <p class="d">{html.escape(s["site_description"])}</p>\n' '    </div>' for s in servers)
    changed |= replace(ROOT / "docs/index.html", "    <!-- BEGIN GENERATED SERVER CARDS -->", "    <!-- END GENERATED SERVER CARDS -->", cards, check)
    return changed

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        changed = sync(args.check)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"metadata sync failed: {exc}", file=sys.stderr)
        return 1
    return int(args.check and changed)

if __name__ == "__main__":
    raise SystemExit(main())
