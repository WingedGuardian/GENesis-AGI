"""Tests for the outbound fence (tests/_live_outbound.py).

Every probe is harmless even if the fence were absent: connections go only to
listeners these tests open themselves, to a closed port, or to an address no
route reaches, and nothing is ever sent. A "live service" is simulated by
telling the fence that the test's own listener is not ours.
"""

from __future__ import annotations

import asyncio
import errno
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import _live_outbound as outbound


@pytest.fixture()
def listener():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    yield srv.getsockname()[1]
    srv.close()


@pytest.fixture()
def not_ours(monkeypatch):
    """Make every listener on this machine look like someone else's service."""
    monkeypatch.setattr(outbound, "_our_socket_inodes", lambda: set())


def _connect(addr, family=socket.AF_INET):
    s = socket.socket(family, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(addr)
    finally:
        s.close()


def test_the_fence_is_armed():
    assert outbound._S.installed


def test_a_tests_own_server_is_reachable(listener):
    _connect(("127.0.0.1", listener))


def test_a_server_in_a_child_process_is_reachable(tmp_path):
    ready = tmp_path / "port"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import socket, sys, time\n"
            "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
            "open(sys.argv[1], 'w').write(str(s.getsockname()[1]))\n"
            "time.sleep(60)\n",
            str(ready),
        ]
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() or not ready.read_text():
            assert time.monotonic() < deadline, "child never listened"
            time.sleep(0.05)
        _connect(("127.0.0.1", int(ready.read_text())))
    finally:
        child.kill()
        child.wait()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "0.0.0.0", "", "::ffff:127.0.0.1"])  # noqa: S104 - connect targets
def test_a_live_service_is_refused_like_ci(listener, not_ours, host):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    before = len(outbound._S.refusals)
    with pytest.raises(ConnectionRefusedError) as info:
        _connect((host, listener), family)
    assert info.value.errno == errno.ECONNREFUSED
    assert len(outbound._S.refusals) == before + 1


def test_connect_ex_and_asyncio_are_refused_too(listener, not_ours):
    s = socket.socket()
    try:
        with pytest.raises(ConnectionRefusedError):
            s.connect_ex(("127.0.0.1", listener))
    finally:
        s.close()

    async def via_asyncio():
        await asyncio.open_connection("127.0.0.1", listener)

    with pytest.raises(ConnectionRefusedError):
        asyncio.run(via_asyncio())


def test_httpx_reaches_the_caller_as_a_connect_error(listener, not_ours):
    httpx = pytest.importorskip("httpx")
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{listener}/", timeout=5)


def test_a_port_nobody_listens_on_fails_on_its_own(not_ours):
    before = len(outbound._S.refusals)
    with pytest.raises(ConnectionRefusedError):
        _connect(("127.0.0.1", 9))
    assert len(outbound._S.refusals) == before  # the kernel refused it, not the fence


def test_a_private_address_off_this_machine_is_refused_on_any_port():
    # The last host of the first private range the fence judges: not this
    # machine. Unfenced, the probe sends a SYN at most and times out after 5s.
    lan = next(n for n in outbound._S.lan_nets if n.version == 4)
    host = str(lan[-2])
    assert host not in outbound._S.local_addrs
    with pytest.raises(ConnectionRefusedError, match="private address"):
        _connect((host, 4242))


def test_a_live_unix_socket_is_refused_as_absent(tmp_path, not_ours, monkeypatch):
    monkeypatch.setattr(outbound._S, "real_home", str(tmp_path))
    path = tmp_path / "svc.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen()
    try:
        with pytest.raises(FileNotFoundError):
            _connect(str(path), socket.AF_UNIX)
    finally:
        srv.close()


def test_a_tests_own_unix_socket_is_reachable(tmp_path, monkeypatch):
    monkeypatch.setattr(outbound._S, "real_home", str(tmp_path))
    path = tmp_path / "own.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen()
    try:
        _connect(str(path), socket.AF_UNIX)
    finally:
        srv.close()


def _unix_listener(path: bytes) -> socket.socket:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen()
    return srv


@pytest.mark.parametrize("name", [b"svc with space.sock", b"svc-\xff-bytes.sock"])
def test_a_live_unix_socket_with_any_legal_path_is_refused(tmp_path, not_ours, monkeypatch, name):
    """Acceptance 2: /proc/net/unix holds the bound path verbatim, spaces and
    non-UTF-8 bytes included, so both are matched and refused."""
    monkeypatch.setattr(outbound._S, "real_home", str(tmp_path))
    path = os.fsencode(tmp_path) + b"/" + name
    srv = _unix_listener(path)
    try:
        with pytest.raises(FileNotFoundError):
            _connect(path, socket.AF_UNIX)
    finally:
        srv.close()


def test_a_bytearray_unix_path_is_judged_like_bytes(tmp_path, not_ours, monkeypatch):
    """connect() takes any bytes-like path; a bytearray must not slip past as an
    address the fence does not recognise."""
    monkeypatch.setattr(outbound._S, "real_home", str(tmp_path))
    path = os.fsencode(tmp_path) + b"/svc.sock"
    srv = _unix_listener(path)
    try:
        with pytest.raises(FileNotFoundError):
            _connect(bytearray(path), socket.AF_UNIX)
    finally:
        srv.close()


def test_a_non_utf8_socket_elsewhere_does_not_disable_the_fence(tmp_path, not_ours, monkeypatch):
    """Acceptance 2: an odd socket path anywhere on the machine (here, outside
    every judged directory) must not turn the judgement of an ordinary one into a
    fault that allows it."""
    home = tmp_path / "home"
    other = tmp_path / "other"
    home.mkdir()
    other.mkdir()
    monkeypatch.setattr(outbound._S, "real_home", str(home))
    odd = _unix_listener(os.fsencode(other) + b"/odd-\xfe\xff.sock")
    ordinary = _unix_listener(os.fsencode(home / "ordinary.sock"))
    faults = sum(outbound._S.faults.values())
    try:
        with pytest.raises(FileNotFoundError):
            _connect(str(home / "ordinary.sock"), socket.AF_UNIX)
    finally:
        odd.close()
        ordinary.close()
    assert sum(outbound._S.faults.values()) == faults


def test_without_a_real_home_only_the_run_directories_are_judged(tmp_path, not_ours, monkeypatch):
    monkeypatch.setattr(outbound._S, "real_home", None)
    path = tmp_path / "anywhere.sock"
    srv = _unix_listener(os.fsencode(path))
    try:
        _connect(str(path), socket.AF_UNIX)  # not under /run/user/<uid> or /run/dbus
    finally:
        srv.close()
    assert outbound._live_dirs() == [
        os.fsencode(f"/run/user/{os.getuid()}"),
        b"/run/dbus",
    ]


def test_a_tests_own_listener_does_not_vouch_for_anothers_on_the_same_port(monkeypatch):
    """Acceptance 3: a test-owned listener on 127.0.0.1:P and a foreign one on
    another local address with the same port P. Only the foreign one is refused."""
    own = socket.socket()
    own.bind(("127.0.0.1", 0))
    own.listen()
    port = own.getsockname()[1]
    foreign = socket.socket()
    foreign.bind(("127.0.0.2", port))
    foreign.listen()
    own_inode = str(os.fstat(own.fileno()).st_ino)
    monkeypatch.setattr(outbound, "_our_socket_inodes", lambda: {own_inode})
    try:
        _connect(("127.0.0.1", port))
        with pytest.raises(ConnectionRefusedError, match="live service"):
            _connect(("127.0.0.2", port))
    finally:
        own.close()
        foreign.close()


def test_a_wildcard_listener_is_reached_from_any_local_address(listener, monkeypatch):
    """A listener on 0.0.0.0 answers a connect to 127.0.0.1: it counts."""
    srv = socket.socket()
    srv.bind(("0.0.0.0", 0))  # noqa: S104 - a test listener
    srv.listen()
    port = srv.getsockname()[1]
    monkeypatch.setattr(outbound, "_our_socket_inodes", lambda: set())
    try:
        with pytest.raises(ConnectionRefusedError):
            _connect(("127.0.0.1", port))
    finally:
        srv.close()


def test_a_fault_in_the_judgement_allows_the_connect_and_is_counted(listener, monkeypatch):
    """Acceptance 4: a fence fault never breaks a connect, and it is counted."""

    def broken(*_args):
        raise RuntimeError("forced fault")

    monkeypatch.setattr(outbound, "_tcp_listen_inodes", broken)
    before = outbound._S.faults["RuntimeError"]
    _connect(("127.0.0.1", listener))
    assert outbound._S.faults["RuntimeError"] == before + 1


@pytest.mark.live_service
def test_a_live_service_test_may_connect(listener, not_ours):
    _connect(("127.0.0.1", listener))


_SUBRUN_CONFTEST = """
pytest_plugins = ["tests._live_outbound"]


def pytest_configure(config):
    from tests import _live_outbound

    _live_outbound.install(config)
"""

_SUBRUN_TESTS = """
import socket

import pytest

from tests import _live_outbound


def test_refused_but_not_failed(monkeypatch):
    monkeypatch.setattr(_live_outbound, "_our_socket_inodes", lambda: set())
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen()
    c = socket.socket()
    try:
        with pytest.raises(ConnectionRefusedError):
            c.connect(srv.getsockname())
    finally:
        c.close(); srv.close()
"""


_SUBRUN_MODULE_FIXTURE = """
import socket

import pytest

from tests import _live_outbound

_live_outbound._our_socket_inodes = lambda: set()  # every listener reads as live


@pytest.fixture(scope="module")
def reached():
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen()
    c = socket.socket()
    try:
        c.connect(srv.getsockname())
        yield "connected"
    except ConnectionRefusedError:
        yield "refused"
    finally:
        c.close(); srv.close()


@pytest.mark.live_service
def test_marked(reached):
    print("MARKED=" + reached)
"""

_SUBRUN_MODULE_FIXTURE_CONTROL = _SUBRUN_MODULE_FIXTURE.replace(
    "@pytest.mark.live_service\ndef test_marked", "def test_unmarked"
).replace("MARKED=", "UNMARKED=")


def test_a_live_service_tests_module_fixture_may_connect_too(tmp_path):
    """Module-scope fixtures are set up before any function fixture, so the
    opt-in must cover the whole test protocol, not start at a function fixture."""
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    (tmp_path / "test_marked.py").write_text(_SUBRUN_MODULE_FIXTURE)
    (tmp_path / "test_unmarked.py").write_text(_SUBRUN_MODULE_FIXTURE_CONTROL)
    repo = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": f"{repo}{os.pathsep}{repo / 'src'}"}
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("GENESIS_LIVE_OUTBOUND_REPORT", None)  # never the outer run's report
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = out.stdout + out.stderr
    assert out.returncode == 0, text
    assert "MARKED=connected" in text, text
    assert "UNMARKED=refused" in text, text  # control: the fence is armed in the sub-run


_SUBRUN_INTERRUPTED = """
import socket

import pytest

from tests import _live_outbound

_live_outbound._our_socket_inodes = lambda: set()  # every listener reads as live

_SRV = socket.socket()
_SRV.bind(("127.0.0.1", 0))
_SRV.listen()


@pytest.fixture(scope="module")
def cleanup():
    yield
    c = socket.socket()
    try:
        c.connect(_SRV.getsockname())
        print("TEARDOWN=connected")
    except ConnectionRefusedError:
        print("TEARDOWN=refused")
    finally:
        c.close()


@pytest.mark.live_service
def test_interrupted(cleanup):
    raise KeyboardInterrupt
"""


def _subrun(tmp_path: Path, files: dict, *extra: str) -> tuple[int, str]:
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    repo = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": f"{repo}{os.pathsep}{repo / 'src'}"}
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("GENESIS_LIVE_OUTBOUND_REPORT", None)  # never the outer run's report
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-s",
            "-p",
            "no:cacheprovider",
            *extra,
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return out.returncode, out.stdout + out.stderr


def test_an_interrupted_live_service_tests_teardown_may_still_connect(tmp_path):
    """Ctrl-C skips the item's teardown; pytest runs it at session finish, outside
    any test. A live test's cleanup (deleting what it created) must not be refused
    there."""
    _rc, text = _subrun(tmp_path, {"test_interrupted.py": _SUBRUN_INTERRUPTED})
    assert "TEARDOWN=connected" in text, text


def test_a_live_service_tests_teardown_may_still_connect_after_x_stops_the_run(tmp_path):
    """-x stops the run after a live test fails while the module is still open
    (the second test keeps it open). pytest 9 then tears everything down inside
    the failing item's protocol (_pytest/runner.py, runtestprotocol), where the
    fence lets the live test through. This pins that: a pytest that deferred the
    teardown to session finish would have the cleanup refused, leaking what the
    live test created."""
    body = _SUBRUN_INTERRUPTED.replace(
        "    raise KeyboardInterrupt\n",
        "    assert False\n\n\n@pytest.mark.live_service\ndef test_after(cleanup):\n    pass\n",
    )
    assert body != _SUBRUN_INTERRUPTED
    _rc, text = _subrun(tmp_path, {"test_x.py": body}, "-x")
    assert "1 failed" in text, text
    assert "TEARDOWN=connected" in text, text


def test_a_run_reports_refusals_without_failing_the_test(tmp_path):
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    (tmp_path / "test_sub.py").write_text(_SUBRUN_TESTS)
    repo = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": f"{repo}{os.pathsep}{repo / 'src'}"}
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("GENESIS_LIVE_OUTBOUND_REPORT", None)  # never the outer run's report
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = out.stdout + out.stderr
    assert out.returncode == 0, text
    assert "live connections refused (reported, not failed)" in text, text
    assert "a live service on this machine" in text, text


_SUBRUN_FAULT = """
import socket

from tests import _live_outbound


def test_a_fault(monkeypatch):
    def broken(*_args):
        raise RuntimeError("forced fault")

    monkeypatch.setattr(_live_outbound, "_tcp_listen_inodes", broken)
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen()
    c = socket.socket()
    try:
        c.connect(srv.getsockname())  # allowed: a fault never breaks a connect
    finally:
        c.close(); srv.close()
"""


def test_the_summary_names_the_faults(tmp_path):
    """Acceptance 4: the run summary says the fence stopped judging, by type."""
    rc, text = _subrun(tmp_path, {"test_fault.py": _SUBRUN_FAULT})
    assert rc == 0, text
    assert "outbound fence: 1 judgments failed and were allowed (RuntimeError x1)" in text, text


def test_the_report_file_names_the_faults(tmp_path, monkeypatch):
    """A reader of the report alone must see the fence stopped judging."""
    report = tmp_path / "report.tsv"
    monkeypatch.setenv("GENESIS_LIVE_OUTBOUND_REPORT", str(report))
    run = tmp_path / "run"
    run.mkdir()
    repo = Path(__file__).resolve().parent.parent
    (run / "conftest.py").write_text(_SUBRUN_CONFTEST)
    (run / "test_fault.py").write_text(_SUBRUN_FAULT)
    env = {**os.environ, "PYTHONPATH": f"{repo}{os.pathsep}{repo / 'src'}"}
    env.pop("PYTEST_CURRENT_TEST", None)
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(run)],
        cwd=run,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAULT\tRuntimeError\t1" in report.read_text()


def test_every_live_service_test_file_is_gated_on_the_opt_in():
    """The marker alone opens the fence, and the opt-in is each test's own
    ``skipif``. A file that marks a test ``live_service`` must also read
    ``GENESIS_LIVE_E2E``, or the test reaches live services on every default run.
    This file is exempt: its marked tests connect only to their own listeners."""
    tests_dir = Path(__file__).resolve().parent
    ungated = [
        str(path.relative_to(tests_dir))
        for path in sorted(tests_dir.rglob("test_*.py"))
        if path.name != Path(__file__).name
        and "mark.live_service" in (text := path.read_text(errors="replace"))
        and "GENESIS_LIVE_E2E" not in text
    ]
    assert ungated == []
