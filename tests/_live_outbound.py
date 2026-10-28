"""The outbound fence: a test may not connect to a live service on this machine.

The suite runs on the same machine as a live install: a database server, a
vector store, the Genesis server, other sessions' tools. A test that connects
to one of them by accident (a default URL, a client built before a fixture
redirected it) reads or writes live state. On CI nothing listens, so the same
connection is refused there. This plugin, armed by ``tests/conftest.py`` after
the live fence, makes it refused here too.

A PEP 578 audit hook on ``socket.connect`` judges every in-process connect:

- **TCP to this machine** (loopback, ``0.0.0.0``/``::``, or one of its own
  addresses): refused when a socket is LISTENING NOW on that port at that
  destination (the same address, or a wildcard on either side) and at least one
  such socket does not belong to this pytest process or one of its children.
  Decided at connect time from ``/proc/net/tcp*`` and the process tree's open
  sockets, so a test's own server (in a thread or a child) is reachable, a
  service that started after the run began is still fenced, and a test's own
  listener on one address does not vouch for someone else's on another address
  with the same port. A port nobody listens on fails on its own, as on CI.
- **TCP to a private address that is not this machine** (RFC 1918, 100.64/10,
  link-local, ULA; the ranges of ``genesis.util.netclass``): refused on any
  port, since CI has no route there either.
- Port 53 (DNS) is exempt everywhere.
- **AF_UNIX**: a socket that is listening, lives under the real home (from the
  live fence, ``tests._live_fence._S.real_home``), ``/run/user/<uid>/`` or
  ``/run/dbus/``, and is not owned by this process tree (``/proc/net/unix``,
  read as bytes so any legal path matches) is refused with
  ``FileNotFoundError``, which is what CI gives for a socket that does not
  exist. Abstract sockets are not judged. With no resolvable real home, only
  the two ``/run`` directories are judged.
- Public addresses are untouched.

A refusal raises ``ConnectionRefusedError`` (or ``FileNotFoundError`` for a
unix socket) to the caller, so the code under test takes the same path it takes
on CI. The test is NOT failed: the refused connection is the protection, and it
is listed in the run summary, per test.

A fault inside the judgement (an unreadable ``/proc``, an unexpected format)
never breaks the connect: it is allowed, counted by exception type, and the
summary says ``outbound fence: N judgments failed and were allowed (...)``, so
a fence that stopped judging is visible rather than silent.

A test marked ``live_service`` may connect to live services; it is listed in
the summary and is expected to be opt-in (``GENESIS_LIVE_E2E=1``).

Two behaviours to know. ``connect_ex`` raises the refusal instead of returning
``ECONNREFUSED`` (callers that catch ``OSError`` see the same outcome). And a
test's own server that daemonizes (double fork / ``setsid``) is no longer in the
pytest process tree, so it reads as someone else's and is refused: mark such a
test ``live_service``.

Residuals, by design (an in-process hook sees only this interpreter's
connects; the OS-level sandbox covers them): connections made by child
processes, connects done in C (curl_cffi), UDP, and unix datagram sockets
(only listening stream and seqpacket sockets are judged). Unix sockets are
judged only under the real home and the user and dbus run directories, so a
system service elsewhere (``/run/systemd``, a VPN daemon) is not refused. A
thread an earlier unmarked test left running is let through while a
``live_service`` test runs. A unix socket path
containing a newline cannot be represented in ``/proc/net/unix`` and is not
matched. ``/proc/net/tcp6`` records no interface, so an IPv6 link-local
destination's scope id (``fe80::1%eth0``) is ignored: two listeners with the
same link-local address on different interfaces are judged as one.
"""

from __future__ import annotations

import contextlib
import errno
import ipaddress
import os
import socket
import sys
import threading
from collections import Counter
from pathlib import Path

import pytest

from tests import _live_fence

_EXEMPT_PORTS = frozenset({53})
_LISTEN = "0A"
_UNIX_ACCEPTING = 0x10000  # __SO_ACCEPTCON in /proc/net/unix Flags


class _State:
    installed = False
    pid = 0
    real_home: str | None = None
    uid = 0
    local_addrs: frozenset[str] = frozenset()
    lan_nets: tuple = ()
    gai = None
    live_service = False
    interrupted_live = False
    refusals: list[str] = []
    live_service_tests: list[str] = []
    faults: Counter = Counter()  # exception type name -> judgments allowed by a fault


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


def _unmapped(ip):
    """``::ffff:a.b.c.d`` as the IPv4 address it is."""
    if ip.version == 6 and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _proc_tcp_address(field: str):
    """The (address, port) of a ``/proc/net/tcp*`` ``HEXADDR:HEXPORT`` field.

    The address is in host byte order per 32-bit word: four bytes for tcp, four
    words for tcp6 (little-endian on the machines this runs on)."""
    hex_addr, hex_port = field.split(":")
    raw = bytes.fromhex(hex_addr)
    order = "little" if sys.byteorder == "little" else "big"
    words = [
        int.from_bytes(raw[i : i + 4], order).to_bytes(4, "big") for i in range(0, len(raw), 4)
    ]
    packed = b"".join(words)
    ip = ipaddress.IPv4Address(packed) if len(packed) == 4 else ipaddress.IPv6Address(packed)
    return _unmapped(ip), int(hex_port, 16)


def _tcp_listen_inodes(port: int, dest) -> set[str]:
    """Inodes of sockets listening on ``port`` that a connect to ``dest`` reaches:
    the same address, or a wildcard (``0.0.0.0``/``::``) on either side."""
    dest = _unmapped(dest)
    inodes = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in _proc_lines(path):
            parts = line.split()
            if len(parts) <= 9 or parts[3] != _LISTEN:
                continue
            local, local_port = _proc_tcp_address(parts[1])
            if local_port != port:
                continue
            # Family-blind on purpose: a 0.0.0.0 listener also counts for a ::1
            # destination, and a 0.0.0.0 destination for every listener. Both can
            # only over-refuse, never let a live service through.
            if local == dest or local.is_unspecified or dest.is_unspecified:
                inodes.add(parts[9])
    return inodes


def _unix_listen_inodes(path: bytes) -> set[str]:
    """Inodes of sockets listening at ``path``. Read as bytes and split at most 7
    times: the eighth field is the whole bound path, spaces and non-UTF-8 bytes
    included."""
    try:
        with open("/proc/net/unix", "rb") as f:
            lines = f.read().split(b"\n")[1:]
    except OSError:
        return set()
    inodes = set()
    for line in lines:
        parts = line.split(None, 7)
        if len(parts) == 8 and parts[7] == path and int(parts[3], 16) & _UNIX_ACCEPTING:
            inodes.add(os.fsdecode(parts[6]))
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
    """Listening, and at least one listener is not this process tree's."""
    # The process-tree walk is skipped when nothing listens: most connects in the
    # suite go to a port with no listener.
    return bool(inodes) and bool(inodes - _our_socket_inodes())


def _classify(ip) -> str:
    ip = _unmapped(ip)
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
    if where == "local" and _live(_tcp_listen_inodes(port, ip)):
        return f"{host}:{port} (a live service on this machine)"
    return None


def _live_dirs() -> list[bytes]:
    dirs = [f"/run/user/{_S.uid}", "/run/dbus"]
    if _S.real_home:
        dirs.insert(0, _S.real_home)
    return [os.fsencode(d).rstrip(b"/") for d in dirs]


def _unix_reason(address) -> str | None:
    if isinstance(address, str):
        address = os.fsencode(address)
    elif isinstance(address, (bytearray, memoryview)):
        address = bytes(address)  # connect() takes any bytes-like path
    if not isinstance(address, bytes):
        # Never a silent allow: an address shape this does not know is a fault,
        # counted and named in the summary.
        raise TypeError(f"unix address of type {type(address).__name__}")
    if not address or address.startswith(b"\0"):
        return None
    # /proc/net/unix holds the path the server BOUND, which may differ from the
    # resolved one (a symlinked directory), so judge both spellings.
    paths = {os.path.abspath(address), os.path.realpath(address)}
    dirs = _live_dirs()
    for path in sorted(paths):
        if not any(path == d or path.startswith(d + b"/") for d in dirs):
            continue
        if _live(_unix_listen_inodes(path)):
            return f"unix socket {os.fsdecode(path)!r} (a live service on this machine)"
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
    except Exception as fault:  # noqa: BLE001 - a fence fault must never break a connect
        # Allowed, but counted: the summary names it, so a fence that stopped
        # judging is visible rather than silent.
        _S.faults[type(fault).__name__] += 1
        return
    finally:
        _busy.on = False
    if reason is None:
        return
    node = os.environ.get("PYTEST_CURRENT_TEST", "<no test>").rsplit(" (", 1)[0]
    _S.refusals.append(f"{node}\t{reason}")
    raise exc(code, f"live fence: refused a connection to {reason}")


def install(config) -> None:
    """Arm the outbound fence. Idempotent: an audit hook cannot be removed.

    Armed after the live fence, whose ``_S.real_home`` (the password-database
    home, with its HOME fallback) it reads."""
    if _S.installed:
        return
    # A private name, read on purpose: the fence must classify addresses exactly
    # as the runtime's egress classifier does, not by a copy that drifts.
    from genesis.util.netclass import _LOCAL_NETS

    _S.pid = os.getpid()
    _S.uid = os.getuid()
    _S.real_home = _live_fence._S.real_home
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
        # -x and --maxfail need nothing here: pytest tears everything down inside
        # the failing item's own protocol once the session is stopping
        # (_pytest/runner.py, runtestprotocol).
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
    if _S.faults:
        total = sum(_S.faults.values())
        kinds = ", ".join(f"{name} x{n}" for name, n in sorted(_S.faults.items()))
        tr.section("live fence: outbound judgments that failed", red=True)
        tr.write_line(f"outbound fence: {total} judgments failed and were allowed ({kinds})")
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
        # this run's result. Faults go in too: a reader of the file alone must
        # see when the fence stopped judging.
        lines = [f"{line}\n" for line in _S.refusals]
        lines += [f"FAULT\t{kind}\t{n}\n" for kind, n in sorted(_S.faults.items())]
        try:
            Path(report).write_text("".join(lines))
        except OSError as exc:
            tr.write_line(f"outbound fence: report not written to {report} ({exc})")
    if _S.live_service_tests:
        tr.section("live fence: live_service tests (live connections allowed)")
        for node in _S.live_service_tests:
            tr.write_line(node)
