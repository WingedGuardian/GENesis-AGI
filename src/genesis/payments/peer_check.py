"""Who may take a card from the vault: the genesis-health MCP server, checked from /proc.

The socket's uid check lets any process of the owner's user in. The release
ops (``release_*``) also require the peer to be the health MCP server a Claude
Code session launched:

- its ``/proc/<pid>/stat`` start time is no later than the accept, so a pid
  reused after the accept is refused (one reused between connect() and the
  accept would need a full pid wrap in that wait), and it is read again at the end;
- ``/proc/<pid>/exe`` resolves to the venv python's real path;
- its argv is exactly ``<venv>/bin/python <repo>/scripts/genesis_mcp_server.py
  --server health``;
- its environment at exec held no ``PYTHON*`` variable (``PYTHONPATH``,
  ``PYTHONSTARTUP`` and the rest change what the interpreter runs);
- a ``claude`` process is among its first eight ancestors, each started no
  later than its child.

Every read fails closed with a fixed reason code. A refusal's detail records
which check failed, the exe path, the comm and the NAMES of any ``PYTHON*``
variables, never their values (API keys live in that environment) and never
the argv.

Against a process of the same user this is friction, not a boundary: such a
process can exec this exact argv itself, plant a ``.pth`` file in the venv, or
connect and then exec the real server. The owner's Telegram tap, which shows
the requester, is the control.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from genesis.env import repo_root, venv_path

# Fixed reason codes: the only text a refusal carries about the peer.
PEER_UNKNOWN = "peer unknown"
PEER_GONE = "peer gone"
PID_REUSED = "pid reused"
EXE_MISMATCH = "exe mismatch"
ARGV_MISMATCH = "argv mismatch"
ENVIRON_UNREADABLE = "environ unreadable"
PYTHON_ENV = "python env"
NO_CLAUDE = "no claude ancestor"
ANCESTOR_GONE = "ancestor gone"
ANCESTOR_REUSED = "ancestor reused"

_MAX_HOPS = 8
_CMDLINE_CAP = 4096
_ENVIRON_CAP = 256 * 1024
_MAX_ENV_NAMES = 8
_MAX_NAME_CHARS = 64

FOREGROUND = "foreground"
LAUNCHED = "genesis-launched"


class PeerRefused(Exception):
    """The peer may not use the release ops. ``reason`` is a fixed code."""

    def __init__(self, reason: str, detail: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


@dataclass(frozen=True)
class Peer:
    """A verified release peer. Value-free: safe to show and to record."""

    pid: int
    start: int
    comm: str
    chain: tuple[str, ...]
    claude_pid: int
    kind: str  # FOREGROUND or LAUNCHED

    def detail(self) -> dict:
        return {
            "pid": self.pid,
            "start": self.start,
            "comm": self.comm,
            "chain": list(self.chain),
            "claude_pid": self.claude_pid,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class PeerPolicy:
    python: str
    script: str
    args: tuple[str, ...] = ("--server", "health")
    ancestor_comm: str = "claude"
    proc_root: Path = field(default=Path("/proc"))

    @classmethod
    def default(cls) -> PeerPolicy:
        return cls(
            python=str(venv_path() / "bin" / "python"),
            script=str(repo_root() / "scripts" / "genesis_mcp_server.py"),
        )


def accept_ticks() -> int:
    """Now, in the clock ticks ``/proc/<pid>/stat`` start times use (since boot)."""
    return int(time.clock_gettime(time.CLOCK_BOOTTIME) * os.sysconf("SC_CLK_TCK"))


def _stat(proc: Path, pid: int) -> tuple[str, int, int]:
    """(state, ppid, start) from ``/proc/<pid>/stat``; OSError/ValueError/IndexError on failure."""
    raw = (proc / str(pid) / "stat").read_bytes().decode("ascii", "replace")
    fields = raw[raw.rindex(")") + 1 :].split()  # comm may hold spaces and ')'
    return fields[0], int(fields[1]), int(fields[19])


def start_of(pid: int, proc_root: Path = Path("/proc")) -> int | None:
    """``pid``'s start time, or None if it cannot be read (gone, zombie, unreadable)."""
    try:
        state, _ppid, start = _stat(proc_root, pid)
    except (OSError, ValueError, IndexError):
        return None
    return None if state in ("Z", "X") else start


def _read_capped(file: Path, cap: int) -> bytes | None:
    """The file's bytes, or None when it is longer than ``cap`` (never cut short)."""
    with open(file, "rb") as f:
        data = f.read(cap + 1)
    return None if len(data) > cap else data


def _comm(proc: Path, pid: int) -> str:
    return (proc / str(pid) / "comm").read_bytes().decode("utf-8", "replace").rstrip("\n")


def _python_names(environ: bytes) -> list[str]:
    names = []
    for entry in environ.split(b"\0"):
        name = entry.split(b"=", 1)[0]
        if name.startswith(b"PYTHON"):
            names.append(name.decode("utf-8", "replace"))
    return names


def check_release_peer(pid: int, *, policy: PeerPolicy, server_pid: int, accept_ticks: int) -> Peer:
    """Verify the peer ``pid`` (from SO_PEERCRED) as the health MCP server; raise PeerRefused."""
    proc = policy.proc_root
    seen: dict = {}

    def refuse(reason: str) -> PeerRefused:
        return PeerRefused(reason, {"check": reason, **seen})

    if pid <= 0:  # 0: the peer is in another pid namespace
        raise refuse(PEER_UNKNOWN)
    # 2. stat: alive, and started no later than the accept.
    try:
        state, ppid, start = _stat(proc, pid)
        seen["comm"] = _comm(proc, pid)
    except (OSError, ValueError, IndexError):
        raise refuse(PEER_GONE) from None
    if state in ("Z", "X"):
        raise refuse(PEER_GONE)
    if start > accept_ticks + 1:
        raise refuse(PID_REUSED)
    # 3. exe: the venv python's real file, not one replaced under a live server.
    try:
        exe = os.readlink(proc / str(pid) / "exe")
    except OSError:
        raise refuse(EXE_MISMATCH) from None
    seen["exe"] = exe
    # A python replaced under a live server reads "<path> (deleted)", which
    # never resolves to the venv python's real path.
    if os.path.realpath(exe) != os.path.realpath(policy.python):
        raise refuse(EXE_MISMATCH)
    # 4. argv, exactly.
    try:
        cmdline = _read_capped(proc / str(pid) / "cmdline", _CMDLINE_CAP)
    except OSError:
        raise refuse(ARGV_MISMATCH) from None
    argv = [os.fsdecode(a) for a in cmdline.split(b"\0")] if cmdline else []
    if argv and argv[-1] == "":
        argv.pop()
    if argv != [policy.python, policy.script, *policy.args]:
        raise refuse(ARGV_MISMATCH)
    # 5. environ at exec: no PYTHON* variable. Names only, never values.
    try:
        environ = _read_capped(proc / str(pid) / "environ", _ENVIRON_CAP)
    except FileNotFoundError:
        raise refuse(PEER_GONE) from None
    except OSError:
        raise refuse(ENVIRON_UNREADABLE) from None
    if environ is None:
        raise refuse(ENVIRON_UNREADABLE)
    names = _python_names(environ)
    del environ
    if names:
        seen["python_env"] = [n[:_MAX_NAME_CHARS] for n in names[:_MAX_ENV_NAMES]]
        raise refuse(PYTHON_ENV)
    # 6. ancestors: a claude process within eight hops, each no younger than its child.
    chain: list[str] = []
    claude_pid, child_start, at = None, start, ppid
    for _ in range(_MAX_HOPS):
        if at <= 0:
            break
        try:
            _state, parent, a_start = _stat(proc, at)
            comm = _comm(proc, at)
        except (OSError, ValueError, IndexError):
            raise refuse(ANCESTOR_GONE) from None
        if a_start > child_start:
            raise refuse(ANCESTOR_REUSED)
        chain.append(comm)
        if comm == policy.ancestor_comm:
            claude_pid = at
            break
        child_start, at = a_start, parent
    seen["chain"] = chain
    if claude_pid is None:
        raise refuse(NO_CLAUDE)
    kind = FOREGROUND
    at = parent
    for _ in range(_MAX_HOPS):  # launched by genesis-server: it is above claude
        if at <= 0:
            break
        if at == server_pid:
            kind = LAUNCHED
            break
        try:
            _state, at, _start = _stat(proc, at)
        except (OSError, ValueError, IndexError):
            break  # display only: the kind falls back to the environ marker
    if kind == FOREGROUND:
        try:
            claude_env = _read_capped(proc / str(claude_pid) / "environ", _ENVIRON_CAP) or b""
        except OSError:
            claude_env = b""
        if b"GENESIS_CC_SESSION=1" in claude_env.split(b"\0"):
            kind = LAUNCHED
        del claude_env
    # 8. the same process all along.
    if start_of(pid, proc) != start:
        raise refuse(PEER_GONE)
    return Peer(pid, start, seen["comm"], tuple(chain), claude_pid, kind)
