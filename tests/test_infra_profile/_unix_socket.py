"""Create a real unix socket inode for collector tests.

The collectors check the file TYPE, so a touched regular file no longer stands
in for a socket. Bound by relative name from the parent directory because a
pytest tmp_path can exceed sun_path's ~108-byte limit.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path


def bind_unix_socket(path: Path) -> Path:
    """Leave a socket inode at ``path`` (the listener is closed; the inode stays)."""
    cwd = os.getcwd()
    os.chdir(path.parent)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(path.name)
    finally:
        os.chdir(cwd)
    return path
