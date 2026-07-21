"""Run every metadata-registered server test suite."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_DIR))

from continue_mcp_common.metadata import load_servers  # noqa: E402


def main() -> int:
    failed: list[str] = []
    for server in load_servers():
        name = server["name"]
        print(f"::group::{name}-mcp", flush=True)
        result = subprocess.run([
            "uv", "run", "--project", str(KIT_DIR), "--extra", "test",
            "pytest", "-q", str(KIT_DIR / f"{name}-mcp" / "tests"),
        ], check=False)
        print("::endgroup::", flush=True)
        if result.returncode:
            failed.append(name)
    if failed:
        print(f"failed server suites: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
