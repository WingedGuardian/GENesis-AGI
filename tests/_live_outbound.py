"""The outbound fence: a test may not connect to a live service on this machine.

The suite runs on the same machine as a live install: a database server, a
vector store, the Genesis server, other sessions' tools. A test that connects
to one of them by accident (a default URL, a client built before a fixture
redirected it) reads or writes live state. On CI nothing listens, so the same
connection is refused there. This plugin, armed by ``tests/conftest.py`` after
the live fence, makes it refused here too.

A PEP 578 audit hook on ``socket.connect`` judges every in-process connect:

- **TCP to this machine** (loopback, ``0.0.0.0``/``::``, or one of its own
  addresses): refused when the port is LISTENING NOW and no listening socket on
  it belongs to this pytest process or one of its children. Decided at connect
  time from ``/proc/net/tcp*`` and the process tree's open sockets, so a test's
  own server (in a thread or a child) is always reachable, and a service that
  started after the run began is still fenced. A port nobody listens on is left
  to fail on its own, as on CI.
- **TCP to a private address that is not this machine** (RFC 1918, 100.64/10,
  link-local, ULA; the ranges of ``genesis.util.netclass``): refused on any
  port, since CI has no route there either.
- Port 53 (DNS) is exempt everywhere.
- **AF_UNIX**: a socket that is listening, lives under the real home,
  ``/run/user/<uid>/`` or ``/run/dbus/``, and is not owned by this process tree
  (``/proc/net/unix``) is refused with ``FileNotFoundError``, which is what CI
  gives for a socket that does not exist. Abstract sockets are not judged.
- Public addresses are untouched.

A refusal raises ``ConnectionRefusedError`` (or ``FileNotFoundError`` for a
unix socket) to the caller, so the code under test takes the same path it takes
on CI. The test is NOT failed: the refused connection is the protection, and it
is listed in the run summary, per test.

A test marked ``live_service`` may connect to live services; it is listed in
the summary and is expected to be opt-in (``GENESIS_LIVE_E2E=1``).

Two behaviours to know. ``connect_ex`` raises the refusal instead of returning
``ECONNREFUSED`` (callers that catch ``OSError`` see the same outcome). And a
test's own server that daemonizes (double fork / ``setsid``) is no longer in the
pytest process tree, so it reads as someone else's and is refused: mark such a
test ``live_service``.

Residuals, by design (an in-process hook sees only this interpreter's
connects; the OS-level sandbox covers them): connections made by child
processes, connects done in C (curl_cffi), and UDP.
"""

from __future__ import annotations

import contextlib
import errno
import ipaddress
import os
import pwd
import socket
import sys
import threading
from pathlib import Path

import pytest

_EXEMPT_PORTS = frozenset({53})
_LISTEN = "0A"
_UNIX_ACCEPTING = 0x10000  # __SO_ACCEPTCON in /proc/net/unix Flags


class _State:
    installed = False
    pid = 0
    real_home = ""
    uid = 0
    local_addrs: frozenset[str] = frozenset()
    lan_nets: tuple = ()
    gai = None
    live_service = False
    interrupted_live = False
    refusals: list[str] = []
    live_service_tests: list[str] = []


_S = _State()
_busy = threading.local()


def _proc_lines(path: str) -> list[str]:
    try:
        with open(path) as f:
            return f.read().splitlines()[1:]
    except OSError:
        return []


def _local_addresses() -> frozenset[str]:
    out: set[str] = set()
    for line in _proc_lines("/proc/net/if_inet6"):
        with contextlib.suppress(ValueError, IndexError):
            out.add(str(ipaddress.ip_address(int(line.split()[0], 16))))
    try:
        with open("/proc/net/fib_trie") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    for i, line in enumerate(lines):
        if "/32 host LOCAL" in line and i:
            out.add(lines[i - 1].split()[-1])
    return frozenset(out)


def _tcp_listen_inodes(port: int) -> set[str]:
    inodes = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in _proc_lines(path):
            parts = line.split()
            if (
                len(parts) > 9
                and parts[3] == _LISTEN
                and int(parts[1].rsplit(":", 1)[1], 16) == port
            ):
                inodes.add(parts[9])
    return inodes


def _unix_listen_inodes(path: str) -> set[str]:
    inodes = set()
    for line in _proc_lines("/proc/net/unix"):
        parts = line.split()
        if len(parts) >= 8 and parts[7] == path and int(parts[3], 16) & _UNIX_ACCEPTING:
            inodes.add(parts[6])
    return inodes


def _our_socket_inodes() -> set[str]:
    """Socket inodes held by this process and every descendant."""
    pending, seen, owned = [_S.pid], set(), set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            fds = []
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                owned.add(target[8:-1])
        try:
            tasks = os.listdir(f"/proc/{pid}/task")
        except OSError:
            tasks = []
        for task in tasks:
            try:
                with open(f"/proc/{pid}/task/{task}/children") as f:
                    pending.extend(int(c) for c in f.read().split())
            except (OSError, ValueError):
                pass
    return owned


def _live(inodes: set[str]) -> bool:
    """Listening, and not by this process tree."""
    return bool(inodes) and not (inodes & _our_socket_inodes())


def _classify(ip) -> str:
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_unspecified or ip.is_loopback or str(ip) in _S.local_addrs:
        return "local"
    if any(ip in net for net in _S.lan_nets):
        return "lan"
    return "public"


def _inet_reason(address, family) -> str | None:
    host, port = address[0], address[1]
    if host in ("", b""):
        # The kernel connects an empty host to INADDR_ANY: this machine.
        host = "::" if family == socket.AF_INET6 else "0.0.0.0"  # noqa: S104 - a connect target
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0] if isinstance(host, str) else host)
    except ValueError:
        try:
            ip = ipaddress.ip_address(_S.gai(host, port, family, socket.SOCK_STREAM)[0][4][0])
        except (OSError, ValueError, IndexError, UnicodeError):
            return None  # cannot resolve: the connect fails on its own, as on CI
    if port in _EXEMPT_PORTS:
        return None
    where = _classify(ip)
    if where == "lan":
        return f"{host}:{port} (a private address that is not this machine)"
    if where == "local" and _live(_tcp_listen_inodes(port)):
        return f"{host}:{port} (a live service on this machine)"
    return None


def _unix_reason(address) -> str | None:
    if isinstance(address, bytes):
        if address.startswith(b"\0"):
            return None
        address = os.fsdecode(address)
    if not isinstance(address, str) or not address or address.startswith("\0"):
        return None
    # /proc/net/unix holds the path the server BOUND, which may differ from the
    # resolved one (a symlinked directory), so judge both spellings.
    paths = {os.path.abspath(address), os.path.realpath(address)}
    live_dirs = (_S.real_home, f"/run/user/{_S.uid}", "/run/dbus")
    for path in sorted(paths):
        if not any(path == d or path.startswith(d.rstrip("/") + "/") for d in live_dirs):
            continue
        if _live(_unix_listen_inodes(path)):
            return f"unix socket {path} (a live service on this machine)"
    return None


def _hook(event: str, args: tuple) -> None:
    if event != "socket.connect" or getattr(_busy, "on", False) or _S.live_service:
        return
    _busy.on = True
    try:
        sock, address = args
        family = getattr(sock, "family", None)
        if family == socket.AF_UNIX:
            reason = _unix_reason(address)
            exc = FileNotFoundError
            code = errno.ENOENT
        elif (
            family in (socket.AF_INET, socket.AF_INET6)
            and getattr(sock, "type", None) == socket.SOCK_STREAM
            and isinstance(address, tuple)
        ):
            reason = _inet_reason(address, family)
            exc = ConnectionRefusedError
            code = errno.ECONNREFUSED
        else:
            return
    except Exception:  # noqa: BLE001 - a fence fault must never break a connect
        return
    finally:
        _busy.on = False
    if reason is None:
        return
    node = os.environ.get("PYTEST_CURRENT_TEST", "<no test>").rsplit(" (", 1)[0]
    _S.refusals.append(f"{node}\t{reason}")
    raise exc(code, f"live fence: refused a connection to {reason}")


def install(config) -> None:
    """Arm the outbound fence. Idempotent: an audit hook cannot be removed."""
    if _S.installed:
        return
    from genesis.util.netclass import _LOCAL_NETS

    _S.pid = os.getpid()
    _S.uid = os.getuid()
    _S.real_home = pwd.getpwuid(_S.uid).pw_dir
    _S.local_addrs = _local_addresses()
    _S.lan_nets = tuple(
        n for n in _LOCAL_NETS if not (n.is_loopback or n.network_address.is_loopback)
    )
    _S.gai = socket.getaddrinfo  # captured: tests may monkeypatch it
    config.addinivalue_line(
        "markers",
        "live_service: the test deliberately connects to a live service on this "
        "machine (opt-in with GENESIS_LIVE_E2E=1); the outbound fence lets it",
    )
    sys.addaudithook(_hook)
    _S.installed = True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """A ``live_service`` test may connect for its WHOLE protocol: setup and
    teardown included, so a module-scope fixture that opens the connection (set
    up during the first item, torn down during the last) is let through too. A
    function-scope fixture would run after those and miss them. Mark the whole
    module when its fixtures connect: a fixture set up or torn down during an
    unmarked item stays fenced."""
    marked = _S.installed and item.get_closest_marker("live_service") is not None
    if marked:
        _S.live_service = True
        _S.live_service_tests.append(item.nodeid)
    try:
        outcome = yield
        # An interrupt skips this item's teardown; pytest runs it at session
        # finish, and it still belongs to this test (see pytest_sessionfinish).
        _S.interrupted_live = marked and outcome.excinfo is not None
    finally:
        _S.live_service = False


@pytest.hookimpl(hookwrapper=True)
def pytest_sessionfinish(session, exitstatus):
    """Teardown left over by an interrupted ``live_service`` test runs here, so
    it may still connect (a live test cleaning up must not be refused)."""
    _S.live_service = _S.interrupted_live
    try:
        yield
    finally:
        _S.live_service = False


def pytest_terminal_summary(terminalreporter):
    if not _S.installed:
        return
    tr = terminalreporter
    if _S.refusals:
        by_reason: dict[str, set[str]] = {}
        for line in _S.refusals:
            node, reason = line.split("\t", 1)
            by_reason.setdefault(reason, set()).add(node)
        tr.section("live fence: live connections refused (reported, not failed)")
        for reason, nodes in sorted(by_reason.items()):
            tr.write_line(f"{reason}: {len(nodes)} tests")
    report = os.environ.get("GENESIS_LIVE_OUTBOUND_REPORT")
    if report:
        # Written on every run, empty when clean, so an old file never reads as
        # this run's result.
        Path(report).write_text("".join(f"{line}\n" for line in _S.refusals))
    if _S.live_service_tests:
        tr.section("live fence: live_service tests (live connections allowed)")
        for node in _S.live_service_tests:
            tr.write_line(node)
