"""Load and validate the toolkit's authoritative server inventory."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


KIT_DIR = Path(__file__).resolve().parents[1]
SOURCE_METADATA_PATH = KIT_DIR / "servers.json"
PACKAGED_METADATA_PATH = Path(__file__).with_name("servers.json")
METADATA_PATH = (
    SOURCE_METADATA_PATH if SOURCE_METADATA_PATH.is_file() else PACKAGED_METADATA_PATH
)


def load_servers(*, include_gateway: bool = True) -> list[dict[str, Any]]:
    data = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported servers.json schema_version")
    servers = data.get("servers")
    if not isinstance(servers, list):
        raise ValueError("servers.json must contain a servers list")
    names = [server.get("name") for server in servers]
    if any(not isinstance(name, str) or not name or "_" in name for name in names):
        raise ValueError("server names must be non-empty strings without underscores")
    if len(names) != len(set(names)):
        raise ValueError("server names must be unique")
    required = {
        "name", "module", "registration", "summary", "site_description",
        "responsibility", "authority", "policy",
    }
    for server in servers:
        missing = required - server.keys()
        if missing:
            raise ValueError(f"{server.get('name', '<unknown>')}: missing {sorted(missing)}")
        if server["registration"] not in {"direct", "gateway", "gateway-host"}:
            raise ValueError(f"{server['name']}: invalid registration")
    if include_gateway:
        return servers
    return [server for server in servers if server["registration"] != "gateway-host"]


def server_names(*, include_gateway: bool = True) -> list[str]:
    return [server["name"] for server in load_servers(include_gateway=include_gateway)]
