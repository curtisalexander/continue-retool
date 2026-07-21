"""Register an already-scaffolded MCP server and refresh derived surfaces."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


KIT_DIR = Path(__file__).resolve().parents[1]
METADATA = KIT_DIR / "servers.json"


def write_metadata(text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=METADATA.parent, delete=False,
        prefix=".servers.", suffix=".json",
    ) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    temp_path.replace(METADATA)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--registration", choices=("direct", "gateway"), required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--site-description", required=True)
    parser.add_argument("--responsibility", required=True)
    parser.add_argument("--authority", required=True)
    parser.add_argument("--policy", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]*", args.name):
        parser.error("name must contain lowercase letters, digits, or hyphens")
    module = args.name.replace("-", "_") + "_mcp"
    base = KIT_DIR / f"{args.name}-mcp"
    required = [
        base / module / "__init__.py", base / module / "server.py", base / "README.md",
        base / "tests" / "test_mcp_surface.py",
        base / ".continue" / "mcpServers" / f"{args.name}.yaml",
    ]
    missing = [str(path.relative_to(KIT_DIR)) for path in required if not path.is_file()]
    if missing:
        parser.error(f"scaffold is incomplete; missing: {', '.join(missing)}")

    original_metadata = METADATA.read_text(encoding="utf-8")
    data = json.loads(original_metadata)
    if any(server["name"] == args.name for server in data["servers"]):
        parser.error(f"{args.name!r} is already registered")
    data["servers"].append({
        "name": args.name,
        "module": module,
        "registration": args.registration,
        "summary": args.summary,
        "site_description": args.site_description,
        "responsibility": args.responsibility,
        "authority": args.authority,
        "policy": args.policy,
    })
    updated_metadata = json.dumps(data, indent=2) + "\n"
    write_metadata(updated_metadata)

    packaging = subprocess.run([
        sys.executable, str(KIT_DIR / "scripts" / "sync_metadata.py"),
        "--packaging-only",
    ], check=False)
    if packaging.returncode:
        write_metadata(original_metadata)
        subprocess.run([
            sys.executable, str(KIT_DIR / "scripts" / "sync_metadata.py"),
            "--packaging-only",
        ], check=False)
        print("registration added, but packaging generation failed", file=sys.stderr)
        return packaging.returncode
    audit = subprocess.run([
        "uv", "run", "--project", str(KIT_DIR), "python",
        str(KIT_DIR / "bench" / "audit.py"), "--write-schema-metrics",
        str(KIT_DIR / "bench" / "schema-metrics.json"),
    ], check=False)
    if audit.returncode:
        write_metadata(original_metadata)
        subprocess.run([
            sys.executable, str(KIT_DIR / "scripts" / "sync_metadata.py"),
            "--packaging-only",
        ], check=False)
        print("registration rolled back because the schema audit failed", file=sys.stderr)
        return audit.returncode
    return subprocess.run([
        sys.executable, str(KIT_DIR / "scripts" / "sync_metadata.py"),
    ], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
