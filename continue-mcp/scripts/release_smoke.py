"""Exercise source installation and both distribution formats from awkward paths."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def run(*command: str | Path) -> None:
    printable = [str(part) for part in command]
    print("+", subprocess.list2cmdline(printable), flush=True)
    subprocess.run(printable, check=True)


def copy_toolkit(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__", "*.pyc"),
    )


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def one_artifact(directory: Path, pattern: str) -> Path:
    matches = list(directory.glob(pattern))
    if len(matches) != 1:
        raise RuntimeError(f"expected one {pattern} artifact, found: {matches}")
    return matches[0]


def main() -> int:
    if reconfigure := getattr(sys.stdout, "reconfigure", None):
        reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser()
    parser.add_argument("scratch", type=Path)
    args = parser.parse_args()

    source = Path(__file__).resolve().parents[1]
    scratch = args.scratch.resolve()
    toolkit = scratch / "toolkit path 工具包"
    workspace = scratch / "workspace path 工作区"
    dist = scratch / "distribution files"
    wheel_venv = scratch / "wheel environment"
    sdist_venv = scratch / "sdist environment"
    scratch.mkdir(parents=True, exist_ok=True)
    copy_toolkit(source, toolkit)
    workspace.mkdir()
    dist.mkdir()

    # Running the copied wrapper proves generated source-checkout commands retain
    # the real (not synthetic) toolkit and workspace paths, including Unicode.
    installer = toolkit / "install-workspace.py"
    run(sys.executable, installer, workspace)
    run(sys.executable, installer, workspace, "--check")
    if not (workspace / ".continue/mcpServers/shell.yaml").is_file():
        raise RuntimeError("installer did not create shell.yaml")
    for config in (workspace / ".continue").rglob("*"):
        if config.is_file() and "/absolute/path/to" in config.read_text(encoding="utf-8"):
            raise RuntimeError(f"unstamped placeholder remains in {config}")

    run("uv", "build", "--project", toolkit, "--out-dir", dist)
    wheel = one_artifact(dist, "*.whl")
    sdist = one_artifact(dist, "*.tar.gz")
    smoke = toolkit / "tests/wheel_smoke.py"
    for artifact, venv in ((wheel, wheel_venv), (sdist, sdist_venv)):
        run("uv", "venv", venv)
        run("uv", "pip", "install", "--python", venv, artifact)
        run(venv_python(venv), smoke, venv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
