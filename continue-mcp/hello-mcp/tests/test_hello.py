"""Tests for the enablement-check server. Run: uv run pytest (from hello-mcp/)."""
import asyncio

from hello_mcp import server


def test_ping_returns_pong():
    assert asyncio.run(server.ping()) == "pong"


def test_echo_round_trips():
    assert asyncio.run(server.echo("continue-mcp")) == "continue-mcp"


def test_whoami_reports_host():
    info = asyncio.run(server.whoami())
    assert set(info) >= {"system", "machine", "python"}
    assert info["python"]
