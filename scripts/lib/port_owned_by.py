"""Is every socket LISTENING on a TCP port held by one given process?

Usage: port_owned_by.py <port> <pid>

Exit 0 only when at least one socket listens on <port> (IPv4 or IPv6) and every
such socket is an open file descriptor of <pid>. Exit 1 in every other case:
nothing listening yet, a listener belonging to any other process, or anything
that could not be read. There is no "could not tell, so yes" answer.

Why this, and not a health response or a manifest: a deploy restarts
genesis-server and must know the RESTARTED unit is what serves. A 200 from the
port can come from an old server started outside systemd; the bootstrap
manifest is written before the web server binds (its Flask thread runs as a
daemon, so a failed bind leaves the process up). The socket itself says who
serves: /proc/net/tcp{,6} lists every listening socket with its inode, and
/proc/<pid>/fd links each descriptor the process holds to a `socket:[inode]`.
Same-uid reads only; no external tool (`ss` is not installed everywhere).
"""

from __future__ import annotations

import os
import sys

_LISTEN = "0A"


def _listening_inodes(port: int) -> set[str]:
    """Inodes of every LISTEN socket on `port`, from /proc/net/tcp and tcp6.

    Raises OSError if the IPv4 table cannot be read, or if the IPv6 table exists
    but cannot be read; only an absent IPv6 table (IPv6 disabled) is skipped.
    """
    inodes: set[str] = set()
    for table, required in (("/proc/net/tcp", True), ("/proc/net/tcp6", False)):
        try:
            with open(table) as fh:
                lines = fh.read().splitlines()[1:]
        except FileNotFoundError:
            # Only an ABSENT IPv6 table (IPv6 disabled) may be skipped: one that
            # exists but cannot be read may hold a foreign listener.
            if required:
                raise
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != _LISTEN:
                continue
            local_port = int(fields[1].rsplit(":", 1)[1], 16)
            if local_port == port:
                inodes.add(fields[9])
    return inodes


def _socket_inodes_of(pid: int) -> set[str]:
    """Inodes of every socket descriptor `pid` holds. Raises OSError if unreadable."""
    held: set[str] = set()
    fd_dir = f"/proc/{pid}/fd"
    for name in os.listdir(fd_dir):
        try:
            target = os.readlink(os.path.join(fd_dir, name))
        except OSError:
            continue  # the descriptor closed between listdir and readlink
        if target.startswith("socket:[") and target.endswith("]"):
            held.add(target[len("socket:[") : -1])
    return held


def main(argv: list[str]) -> int:
    try:
        port, pid = int(argv[1]), int(argv[2])
    except (IndexError, ValueError):
        print("usage: port_owned_by.py <port> <pid>", file=sys.stderr)
        return 1
    if pid <= 1:
        return 1
    try:
        listening = _listening_inodes(port)
        if not listening:
            return 1
        return 0 if listening <= _socket_inodes_of(pid) else 1
    except (OSError, ValueError, IndexError):
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
