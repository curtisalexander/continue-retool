"""Load and validate the toolkit's authoritative server inventory."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

KIT_DIR = Path(__file__).resolve().parents[1]
source = KIT_DIR / "servers.json"
METADATA_PATH = source if source.is_file() else Path(__file__).with_name("servers.json")

def load_servers() -> list[dict[str, Any]]:
    data = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("servers"), list):
        raise ValueError("unsupported servers.json structure")
    required = {"name", "module", "default", "summary", "site_description", "responsibility", "authority", "policy"}
    names: list[str] = []
    for server in data["servers"]:
        if missing := required - server.keys():
            raise ValueError(f"{server.get('name', '<unknown>')}: missing {sorted(missing)}")
        if not isinstance(server["default"], bool):
            raise ValueError(f"{server['name']}: default must be boolean")
        names.append(server["name"])
    if len(names) != len(set(names)) or any(not name or "_" in name for name in names):
        raise ValueError("server names must be unique non-empty strings without underscores")
    return data["servers"]

def server_names() -> list[str]:
    return [server["name"] for server in load_servers()]
