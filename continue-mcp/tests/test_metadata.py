from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from continue_mcp_common.metadata import load_servers
from scripts.update_dependencies import command, cutoff


KIT_DIR = Path(__file__).parents[1]


def test_generated_metadata_is_current():
    result = subprocess.run(
        [sys.executable, str(KIT_DIR / "scripts" / "sync_metadata.py"), "--check"],
        cwd=KIT_DIR, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_gateway_defaults_come_from_registration_metadata():
    expected = {
        server["name"] for server in load_servers()
        if server["registration"] == "gateway"
    }
    config = json.loads(
        (KIT_DIR / "gateway-mcp" / "gateway.config.json").read_text(encoding="utf-8")
    )
    assert set(config["servers"]) == expected


def test_dependency_update_cutoff_and_command_are_enforced():
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    assert cutoff(now) == "2026-07-14T12:00:00Z"
    args = command(["fastmcp"], cutoff(now))
    assert args[-2:] == ["--upgrade-package", "fastmcp"]
    assert args[args.index("--exclude-newer") + 1] == "2026-07-14T12:00:00Z"
