import os
import sys

# Make the search_mcp package importable when running pytest from search-mcp/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import pytest


@pytest.fixture(autouse=True)
def _workspace_is_tmp(tmp_path, monkeypatch):
    """Point default-on workspace path scoping at each test's temporary root."""
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
