"""Windows process ownership, including descendants whose parent has exited.

The server holds the only long-lived handle to a kill-on-close Job Object. A
small launcher joins it *before* creating the shell, avoiding the race in
assigning an already-running shell. No breakaway permission is granted.
"""
from __future__ import annotations

import ctypes as ct
import subprocess
import sys
import uuid
from functools import cache


class _BasicLimits(ct.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ct.c_int64),
        ("PerJobUserTimeLimit", ct.c_int64),
        ("LimitFlags", ct.c_uint32),
        ("MinimumWorkingSetSize", ct.c_size_t),
        ("MaximumWorkingSetSize", ct.c_size_t),
        ("ActiveProcessLimit", ct.c_uint32),
        ("Affinity", ct.c_size_t),
        ("PriorityClass", ct.c_uint32),
        ("SchedulingClass", ct.c_uint32),
    ]


class _ExtendedLimits(ct.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", ct.c_uint64 * 6),
        ("ProcessMemoryLimit", ct.c_size_t),
        ("JobMemoryLimit", ct.c_size_t),
        ("PeakProcessMemoryUsed", ct.c_size_t),
        ("PeakJobMemoryUsed", ct.c_size_t),
    ]


@cache
def _api():
    api = getattr(ct, "WinDLL")("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ct.c_void_p, ct.c_wchar_p], ct.c_void_p),
        "OpenJobObjectW": ([ct.c_uint32, ct.c_int, ct.c_wchar_p], ct.c_void_p),
        "SetInformationJobObject": ([ct.c_void_p, ct.c_int, ct.c_void_p, ct.c_uint32], ct.c_int),
        "AssignProcessToJobObject": ([ct.c_void_p, ct.c_void_p], ct.c_int),
        "TerminateJobObject": ([ct.c_void_p, ct.c_uint32], ct.c_int),
        "GetCurrentProcess": ([], ct.c_void_p),
        "CloseHandle": ([ct.c_void_p], ct.c_int),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes = args
        function.restype = result
    return api


def _checked(value):
    if not value:
        raise getattr(ct, "WinError")(getattr(ct, "get_last_error")())
    return value


class WindowsJob:
    def __init__(self):
        self.name = "Local\\continue-mcp-" + uuid.uuid4().hex
        self.handle = _checked(_api().CreateJobObjectW(None, self.name))
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        try:
            _checked(_api().SetInformationJobObject(
                self.handle, 9, ct.byref(limits), ct.sizeof(limits),
            ))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Release ownership; Windows terminates every remaining member."""
        if self.handle is not None:
            # Explicit termination also covers the launcher's brief assignment
            # handle: cleanup must not wait for that other handle to close.
            _checked(_api().TerminateJobObject(self.handle, 1))
            _checked(_api().CloseHandle(self.handle))
            self.handle = None


def main() -> int:
    name, shell, *argv = sys.argv[1:]
    api = _api()
    # Only assignment rights; the launcher never inherits the owner's handle.
    handle = _checked(api.OpenJobObjectW(0x0001, False, name))
    try:
        _checked(api.AssignProcessToJobObject(handle, api.GetCurrentProcess()))
    finally:
        _checked(api.CloseHandle(handle))
    # If the server died before assignment, opening the named job failed above.
    # If it dies after assignment, kill-on-close kills this launcher as well.
    streams = dict(stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    if shell == "cmd":
        child = subprocess.Popen(
            argv[-1], shell=True, executable=argv[0], creationflags=0x08000000,
            **streams,
        )
    else:
        child = subprocess.Popen(argv, creationflags=0x08000000, **streams)  # NO_WINDOW
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
