"""Atomic file writes — temp file + os.replace to prevent torn reads.

A direct ``Path.write_text`` truncates the destination before writing, so a
concurrent reader (or a crash mid-write) can observe a partial/empty file.
``atomic_write_text`` writes to a sibling temp file and ``os.replace``s it into
place, which is atomic on POSIX within the same filesystem: a reader sees either
the old contents or the complete new contents, never a partial write.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path | str, content: str) -> None:
    """Atomically write ``content`` to ``path`` (temp file in the same dir + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
