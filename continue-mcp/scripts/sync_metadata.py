"""Generate and verify repository surfaces derived from servers.json."""
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


METRICS_PATH = KIT_DIR / "bench" / "schema-metrics.json"


def replace_block(path: Path, start: str, end: str, body: str, *, check: bool) -> bool:
    original = path.read_text(encoding="utf-8")
    if original.count(start) != 1 or original.count(end) != 1:
        raise RuntimeError(f"{path}: expected exactly one generated marker pair")
    before, remainder = original.split(start, 1)
    _, after = remainder.split(end, 1)
    updated = before + start + "\n" + body.rstrip() + "\n" + end + after
    if updated == original:
        return False
    if check:
        print(f"stale generated content: {path.relative_to(ROOT)}", file=sys.stderr)
    else:
        path.write_text(updated, encoding="utf-8", newline="\n")
        print(f"updated {path.relative_to(ROOT)}")
    return True


def validate_layout(servers: list[dict]) -> None:
    for server in servers:
        name = server["name"]
        package = KIT_DIR / f"{name}-mcp" / server["module"]
        required = [
            package / "__init__.py",
            package / "server.py",
            KIT_DIR / f"{name}-mcp" / "README.md",
            KIT_DIR / f"{name}-mcp" / "tests" / "test_mcp_surface.py",
            KIT_DIR / f"{name}-mcp" / ".continue" / "mcpServers" / f"{name}.yaml",
        ]
        missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
        if missing:
            raise RuntimeError(f"{name}: missing required files: {', '.join(missing)}")


def load_metrics(servers: list[dict]) -> dict:
    metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    names = {server["name"] for server in servers}
    measured = set(metrics.get("servers", {}))
    if measured != names:
        raise RuntimeError(
            f"schema metrics mismatch: missing={sorted(names - measured)}, "
            f"extra={sorted(measured - names)}; rerun bench/audit.py"
        )
    return metrics["servers"]


def sync(*, check: bool, packaging_only: bool = False) -> bool:
    servers = load_servers()
    validate_layout(servers)
    changed = False

    entry_points = "\n".join(
        f'{server["name"]}-mcp = "{server["module"]}.server:main"'
        for server in servers
    )
    packages = "\n".join(
        f'  "{server["name"]}-mcp/{server["module"]}",'
        for server in servers
    )
    changed |= replace_block(
        KIT_DIR / "pyproject.toml", "# BEGIN GENERATED SERVER ENTRY POINTS",
        "# END GENERATED SERVER ENTRY POINTS", entry_points, check=check,
    )
    changed |= replace_block(
        KIT_DIR / "pyproject.toml", "# BEGIN GENERATED SERVER PACKAGES",
        "# END GENERATED SERVER PACKAGES", packages, check=check,
    )
    if packaging_only:
        return changed

    metrics = load_metrics(servers)

    root_inventory = "\n".join(
        f'  - `{server["name"]}-mcp/` — {server["summary"]}' for server in servers
    )
    changed |= replace_block(
        ROOT / "README.md", "<!-- BEGIN GENERATED SERVER INVENTORY -->",
        "<!-- END GENERATED SERVER INVENTORY -->", root_inventory, check=check,
    )

    component_rows = "\n".join(
        f'| `{server["name"]}-mcp` | {server["responsibility"]} | {server["authority"]} |'
        for server in servers
    )
    changed |= replace_block(
        ROOT / "ARCHITECTURE.md", "<!-- BEGIN GENERATED COMPONENT INVENTORY -->",
        "<!-- END GENERATED COMPONENT INVENTORY -->", component_rows, check=check,
    )

    cards = []
    for server in servers:
        badge = ' <span class="start">start here</span>' if server["name"] == "hello" else ""
        cards.append(
            '    <div class="tool">\n'
            f'      <p class="n">{html.escape(server["name"])}-mcp{badge}</p>\n'
            f'      <p class="d">{html.escape(server["site_description"])}</p>\n'
            '    </div>'
        )
    changed |= replace_block(
        ROOT / "docs" / "index.html", "    <!-- BEGIN GENERATED SERVER CARDS -->",
        "    <!-- END GENERATED SERVER CARDS -->", "\n".join(cards), check=check,
    )

    gateway = metrics["gateway"]["estimated_tokens"]
    tail = [server for server in servers if server["registration"] == "gateway"]
    tail_tokens = sum(metrics[server["name"]]["estimated_tokens"] for server in tail)
    tail_count = sum(metrics[server["name"]]["tool_count"] for server in tail)
    saving = tail_tokens - gateway
    pct = round(100 * saving / tail_tokens) if tail_tokens else 0
    gateway_cost = (
        f"The audit currently estimates the gateway's three meta-tool schemas at "
        f"**~{gateway} tokens at rest**. The default tail ({', '.join(s['name'] for s in tail)}) "
        f"would cost ~{tail_tokens} tokens if registered directly, so the gateway saves "
        f"~{saving} resting tokens ({pct}%) before any per-use describe result. These values are "
        f"generated from `continue-mcp/bench/schema-metrics.json` using the repository's "
        f"deterministic `ceil(serialized characters / 4)` estimator."
    )
    changed |= replace_block(
        ROOT / "continue-mcp-token-strategy.md", "<!-- BEGIN GENERATED GATEWAY COST -->",
        "<!-- END GENERATED GATEWAY COST -->", gateway_cost, check=check,
    )

    table = [
        "The current measured inventory is:", "",
        "| Server | Tools | Schema ~tokens | Default registration |", "|---|---:|---:|---|",
    ]
    for server in servers:
        name = server["name"]
        table.append(
            f"| `{name}-mcp` | {metrics[name]['tool_count']} | {metrics[name]['estimated_tokens']} "
            f"| {server['registration']} |"
        )
    direct = sum(
        metrics[s["name"]]["estimated_tokens"] for s in servers
        if s["registration"] == "direct"
    )
    hybrid = direct + gateway
    direct_names = [server["name"] for server in servers if server["registration"] == "direct"]
    tail_display = ", ".join(f"`{server['name']}`" for server in tail)
    table.extend([
        "",
        f"With the metadata defaults, direct servers cost ~{direct} tokens and the gateway costs "
        f"~{gateway}, for **~{hybrid} resting tokens**. Registering the same tail directly would "
        f"cost ~{direct + tail_tokens}; the generated hybrid saves ~{saving} tokens per request.",
        "",
        "Concretely for Continue, register "
        + ", ".join(f"`{name}.yaml`" for name in direct_names)
        + f", and `gateway.yaml`; keep only {tail_display} in the gateway downstream "
        "configuration. Do not register a downstream both directly and through the gateway.",
    ])
    changed |= replace_block(
        ROOT / "continue-mcp-token-strategy.md", "<!-- BEGIN GENERATED TOOLKIT EXAMPLE -->",
        "<!-- END GENERATED TOOLKIT EXAMPLE -->", "\n".join(table), check=check,
    )

    tail_names = ", ".join(f"`{server['name']}-mcp`" for server in tail)
    token_math = (
        f"The default tail is {tail_names}: {tail_count} tools totaling **~{tail_tokens} tokens** "
        f"if registered directly. The gateway advertises 3 meta-tools totaling **~{gateway} "
        f"tokens**, saving **~{saving} tokens ({pct}%)** of resting schema context. The exact "
        f"per-tool estimates are generated by `bench/audit.py` and committed in "
        f"`bench/schema-metrics.json`; described downstream schemas are paid only when used."
    )
    changed |= replace_block(
        KIT_DIR / "gateway-mcp" / "README.md",
        "<!-- BEGIN GENERATED GATEWAY TOKEN MATH -->",
        "<!-- END GENERATED GATEWAY TOKEN MATH -->", token_math, check=check,
    )

    direct_tool_count = sum(
        metrics[server["name"]]["tool_count"] for server in servers
        if server["registration"] != "gateway-host"
    )
    all_direct = direct + tail_tokens
    gateway_overview = (
        f"The current downstream inventory contains **{direct_tool_count} tools totaling "
        f"~{all_direct} estimated schema tokens** if every server is registered directly. "
        f"The gateway's fixed advertisement is **3 tools totaling ~{gateway} tokens**. "
        "These generated estimates use `ceil(serialized schema characters / 4)` and are "
        "comparative rather than tokenizer-exact."
    )
    changed |= replace_block(
        KIT_DIR / "gateway-mcp" / "README.md",
        "<!-- BEGIN GENERATED GATEWAY OVERVIEW -->",
        "<!-- END GENERATED GATEWAY OVERVIEW -->", gateway_overview, check=check,
    )

    gateway_config = {
        "_comment": (
            "Generated from servers.json. Continue connects only to the gateway; "
            "relative cwd entries resolve against this file's directory."
        ),
        "servers": {
            server["name"]: {
                "command": "uv",
                "args": ["run", "--no-sync", "--project", ".", f"{server['name']}-mcp"],
                "cwd": "..",
            }
            for server in tail
        },
    }
    config_path = KIT_DIR / "gateway-mcp" / "gateway.config.json"
    config_text = json.dumps(gateway_config, indent=2) + "\n"
    if config_path.read_text(encoding="utf-8") != config_text:
        changed = True
        if check:
            print(f"stale generated content: {config_path.relative_to(ROOT)}", file=sys.stderr)
        else:
            config_path.write_text(config_text, encoding="utf-8", newline="\n")
            print(f"updated {config_path.relative_to(ROOT)}")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--packaging-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        changed = sync(check=args.check, packaging_only=args.packaging_only)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"metadata sync failed: {exc}", file=sys.stderr)
        return 1
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
