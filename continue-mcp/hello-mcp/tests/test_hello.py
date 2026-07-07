"""Tests for the enablement-check server. Run: uv run pytest (from hello-mcp/)."""
import asyncio

from hello_mcp import server


def test_ping_returns_pong():
    assert asyncio.run(server.ping()) == "pong"


def test_echo_round_trips():
    assert asyncio.run(server.echo("continue-mcp")) == "continue-mcp"


def test_whoami_reports_host():
    info = asyncio.run(server.whoami()).structured_content
    assert set(info) >= {"system", "machine", "python"}
    assert info["python"]


def test_whoami_reports_path_resolution_base(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    info = asyncio.run(server.whoami()).structured_content
    assert info["mcp_workspace"] == str(tmp_path)
    assert info["resolved_base"] == str(tmp_path)
    assert info["cwd"]  # always present


def test_whoami_falls_back_to_cwd(monkeypatch):
    monkeypatch.delenv("MCP_WORKSPACE", raising=False)
    import os
    info = asyncio.run(server.whoami()).structured_content
    assert info["mcp_workspace"] is None
    assert info["resolved_base"] == os.path.abspath(os.getcwd())
