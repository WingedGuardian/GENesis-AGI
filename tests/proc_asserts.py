"""Shared assertion helpers for process-kill tests.

``os.kill(pid, 0)`` SUCCEEDS on a zombie — on hosts whose pid 1 does not
promptly reap orphans (minimal-init containers), a SIGKILLed grandchild can
sit in state Z long enough to flake a bare ``ProcessLookupError`` assertion
even though the group kill worked (Codex P2, PR #1415). "Terminated" for
these tests means: gone, OR present only as a zombie.
"""

from __future__ import annotations

import os


def process_terminated(pid: int) -> bool:
    """True iff *pid* is gone or is a zombie (dead, awaiting reap)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            # field 3 (after the parenthesised comm, which may contain spaces)
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return True
    return state == "Z"
