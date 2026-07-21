"""Handshake with every console script from an isolated wheel environment."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


SCRIPT = Path(__file__).parents[1] / "install-workspace.py"
SPEC = importlib.util.spec_from_file_location("install_workspace_wheel_smoke", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)

SERVERS = installer.SERVERS


def _console(venv: Path, name: str) -> str:
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    resolved = shutil.which(f"{name}-mcp", path=str(scripts))
    if not resolved:
        raise RuntimeError(f"{name}-mcp console script is missing from {scripts}")
    return resolved


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: wheel_smoke.py /path/to/isolated-venv")
    venv = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="continue-mcp-wheel-smoke-") as workspace:
        base_env = os.environ.copy()
        base_env["MCP_WORKSPACE"] = workspace
        for name in SERVERS:
            ok, detail = installer._mcp_handshake(
                [_console(venv, name)], env=base_env, cwd=workspace, timeout=60,
            )
            if not ok:
                print(f"FAIL {name}-mcp: {detail}", file=sys.stderr)
                return 1
            print(f"ok {name}-mcp: {detail}")

        gateway_config = Path(workspace) / "gateway.config.json"
        gateway_config.write_text(json.dumps({
            "servers": {
                "hello": {
                    "command": _console(venv, "hello"),
                    "env": {"MCP_WORKSPACE": workspace},
                }
            }
        }), encoding="utf-8")
        gateway_env = dict(base_env, GATEWAY_CONFIG=str(gateway_config))
        ok, detail = installer._mcp_handshake(
            [_console(venv, "gateway")], env=gateway_env, cwd=workspace,
            timeout=60, gateway_servers=("hello",),
        )
        if not ok:
            print(f"FAIL gateway-mcp: {detail}", file=sys.stderr)
            return 1
        print(f"ok gateway-mcp: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
