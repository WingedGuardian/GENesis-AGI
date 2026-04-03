"""PTY session manager for the built-in web terminal.

Pure Python stdlib — no transport or framework dependencies.
Used by the WebSocket handler in routes/terminal.py.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import pty
import select
import signal
import struct
import termios
import time

logger = logging.getLogger(__name__)


class TerminalSession:
    """Manages a single PTY shell session.

    Synchronous API — designed for use in a threaded WebSocket handler
    (flask-sock runs each connection in its own thread).
    """

    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd or os.path.expanduser("~/genesis")
        self.master_fd: int | None = None
        self._pid: int | None = None
        self._closed = False

    def start(self) -> None:
        """Spawn a bash shell attached to a new PTY."""
        master, slave = pty.openpty()

        # Copy env BEFORE fork — os.environ.copy() acquires a lock that
        # could deadlock in the child if another thread holds it.
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"

        pid = os.fork()
        if pid == 0:
            # ── Child process ──
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            if slave > 2:
                os.close(slave)
            os.chdir(self.cwd)
            os.execvpe("bash", ["bash", "--login"], env)
        else:
            # ── Parent process ──
            os.close(slave)
            self.master_fd = master
            self._pid = pid
            logger.info("Terminal session started (pid %d)", pid)

    def write(self, data: str) -> None:
        """Send input to the PTY."""
        if self.master_fd is not None:
            os.write(self.master_fd, data.encode("utf-8"))

    def read(self, timeout: float = 0.05) -> bytes | None:
        """Non-blocking read from the PTY.

        Returns raw bytes or None if no data available within timeout.
        """
        if self.master_fd is None:
            return None
        try:
            r, _, _ = select.select([self.master_fd], [], [], timeout)
            if r:
                return os.read(self.master_fd, 65536)
        except OSError as e:
            if e.errno == errno.EIO:
                # EIO = child process exited
                return None
            raise
        return None

    def resize(self, rows: int, cols: int) -> None:
        """Set the PTY window size."""
        if self.master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    def is_alive(self) -> bool:
        """Check if the child process is still running."""
        if self._pid is None:
            return False
        try:
            pid, _status = os.waitpid(self._pid, os.WNOHANG)
            return pid == 0  # 0 means still running
        except ChildProcessError:
            return False

    def close(self) -> None:
        """Kill the shell and clean up the PTY."""
        if self._closed:
            return
        self._closed = True

        if self._pid is not None:
            try:
                pgid = os.getpgid(self._pid)
                if pgid > 1:  # CRITICAL: never kill pgid 1 (all user processes)
                    os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass

            # Wait for child to exit, force kill if needed
            for _ in range(10):  # up to 0.5s
                try:
                    pid, _ = os.waitpid(self._pid, os.WNOHANG)
                    if pid != 0:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.05)
            else:
                # Still alive after 0.5s — force kill
                try:
                    pgid = os.getpgid(self._pid)
                    if pgid > 1:
                        os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(self._pid, 0)

            logger.info("Terminal session stopped (pid %d)", self._pid)

        if self.master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = None
