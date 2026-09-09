import json
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


@pytest.fixture
def native_command(monkeypatch):
    """Exercise pipe ownership independently of a shell's pipeline lifetime.

    Keep the real process-group/Windows Job Object spawn and reaper. Only the
    command builder changes: PowerShell can wait for descendant pipeline EOF,
    so a native parent's exit does not imply the outer shell has exited.
    Call with shell='bash' to use the ordinary argv branch, including on Windows.
    """
    from shell_mcp import server
    monkeypatch.setattr(
        server, "build_argv", lambda cmd, *_args: [sys.executable, *json.loads(cmd)]
    )
    return lambda *args: json.dumps([str(arg) for arg in args])


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
