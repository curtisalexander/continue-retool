import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass

import pytest

# Make the shell_mcp package importable when running pytest from shell-mcp/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def fresh_admission_gate(monkeypatch):
    # Direct helper tests each represent a new server lifecycle.
    from shell_mcp import server
    monkeypatch.setattr(server, "_shutting_down", False)


@dataclass(frozen=True)
class ShellCase:
    name: str

    def quote(self, value: object) -> str:
        text = str(value)
        if self.name in ("pwsh", "powershell"):
            return "'" + text.replace("'", "''") + "'"
        if self.name == "cmd":
            return subprocess.list2cmdline([text])
        return shlex.quote(text)

    def invoke(self, executable: object, *args: object) -> str:
        prefix = "& " if self.name in ("pwsh", "powershell") else ""
        return prefix + " ".join(self.quote(value) for value in (executable, *args))


def _available_shells() -> list[tuple[str, str]]:
    if sys.platform.startswith("win"):
        names = ("pwsh", "powershell", "cmd")
    else:
        names = ("bash", "pwsh")
    found = []
    for name in names:
        executable = os.environ.get(f"SHELL_MCP_{name.upper()}") or shutil.which(name)
        if executable:
            found.append((name, executable))
    return found


@pytest.fixture(params=_available_shells(), ids=lambda item: item[0])
def shell_case(request, monkeypatch):
    name, executable = request.param
    monkeypatch.setenv(f"SHELL_MCP_{name.upper()}", executable)
    return ShellCase(name)
