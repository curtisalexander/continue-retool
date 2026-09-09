from pathlib import Path

import pytest

from continue_mcp_common import atomic


def test_create_from_temp_never_clobbers(tmp_path: Path):
    source = tmp_path / "temp"
    destination = tmp_path / "target"
    source.write_text("new")
    destination.write_text("mine")
    with pytest.raises(FileExistsError):
        atomic.create_from_temp(source, destination)
    assert destination.read_text() == "mine"


def test_windows_create_uses_rename_not_hardlink(tmp_path: Path, monkeypatch):
    source = tmp_path / "temp"
    destination = tmp_path / "target"
    source.write_text("new")
    monkeypatch.setattr(atomic, "_is_windows", lambda: True)
    monkeypatch.setattr(atomic.os, "link", lambda *args: pytest.fail("hard link used"))
    atomic.create_from_temp(source, destination)
    assert destination.read_text() == "new"
    assert not source.exists()
