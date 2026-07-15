import os
import sys

# Make the search_mcp package importable when running pytest from search-mcp/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import pytest


@pytest.fixture(autouse=True)
def _workspace_is_tmp(tmp_path, monkeypatch):
    """The workspace jail (default ON) confines paths to MCP_WORKSPACE. Tests
    operate on absolute tmp_path files, so point the workspace there — exactly
    what the installer's stamp does in production. Individual tests still
    override MCP_WORKSPACE/MCP_JAIL for their own scenarios."""
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
