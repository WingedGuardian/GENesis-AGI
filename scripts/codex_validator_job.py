"""Bind the fixed probe governor to its request's Linux process lifetime."""

from __future__ import annotations

import ctypes
import os
import runpy
import signal
import sys
from pathlib import Path


def arm_parent_death(expected_parent: int) -> None:
    if sys.platform != "linux" or type(expected_parent) is not int or expected_parent <= 1:
        raise ValueError("Unsupported validator parent")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(1, int(signal.SIGTERM), 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "Validator parent lifetime unavailable")
    # Death before arming sends no signal. Refuse before admitting any job.
    if os.getppid() != expected_parent:
        raise ValueError("Validator parent changed")


def main() -> None:
    try:
        arm_parent_death(int(sys.argv[1]))
    except (Exception, KeyboardInterrupt):
        raise SystemExit("Validator job refused") from None
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    sys.argv = ["genesis.hostmetrics", *sys.argv[2:]]
    # Stay in the armed child. Forking another governor loses the lifetime edge.
    runpy.run_module("genesis.hostmetrics", run_name="__main__")


if __name__ == "__main__":
    main()
