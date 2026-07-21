"""Update dependencies while excluding packages uploaded in the last seven days."""
from __future__ import annotations

import argparse
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


KIT_DIR = Path(__file__).resolve().parents[1]
MINIMUM_AGE_DAYS = 7


def cutoff(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    eligible_before = current.astimezone(timezone.utc) - timedelta(days=MINIMUM_AGE_DAYS)
    return eligible_before.isoformat(timespec="seconds").replace("+00:00", "Z")


def command(packages: list[str], cutoff_date: str) -> list[str]:
    args = ["uv", "lock", "--project", str(KIT_DIR), "--exclude-newer", cutoff_date]
    if packages:
        for package in packages:
            args.extend(["--upgrade-package", package])
    else:
        args.append("--upgrade")
    return args


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update only packages at least seven days old according to upload time."
    )
    parser.add_argument("packages", nargs="*")
    parser.add_argument("--print-command", action="store_true")
    args = parser.parse_args()
    cutoff_date = cutoff()
    cmd = command(args.packages, cutoff_date)
    print(f"eligibility cutoff: {cutoff_date} ({MINIMUM_AGE_DAYS} days)")
    if args.print_command:
        print(" ".join(cmd))
        return 0
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
