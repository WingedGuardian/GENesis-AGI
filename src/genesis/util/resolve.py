"""Path resolution that refuses a symlink loop on every supported Python."""

from __future__ import annotations

import errno
import os
from pathlib import Path


def resolve_refusing_loops(path: Path) -> Path:
    """``path.resolve()``, raising when the path is a symlink loop.

    Python 3.12 raises RuntimeError from a non-strict ``resolve()`` on a loop,
    while 3.13 and later return the looping path as if it were fine (measured on
    3.12.3, 3.13.16 and 3.14.8). Code that treats "cannot be resolved" as a
    refusal calls this, and catches ``(OSError, RuntimeError)``. A missing path,
    or any other failure to stat it, resolves as it always has.
    """
    resolved = path.resolve()
    try:
        os.stat(resolved)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise
    return resolved
