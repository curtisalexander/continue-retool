"""Small cross-platform atomic publication primitives."""
from __future__ import annotations

import os


def _is_windows() -> bool:
    return os.name == "nt"


def create_from_temp(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """Publish a closed sibling temporary without replacing an existing path."""
    if _is_windows():
        # Windows rename is atomic and fails when destination already exists.
        os.rename(source, destination)
        return
    try:
        os.link(source, destination)
    except OSError as exc:
        exc.add_note("atomic create-if-absent requires hard-link support on POSIX")
        raise
