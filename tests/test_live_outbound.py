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


def _subrun(tmp_path: Path, files: dict) -> tuple[int, str]:
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    for name, body in files.items():
        (tmp_path / name).write_text(body)
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
    return out.returncode, out.stdout + out.stderr


def test_an_interrupted_live_service_tests_teardown_may_still_connect(tmp_path):
    """Ctrl-C skips the item's teardown; pytest runs it at session finish, outside
    any test. A live test's cleanup (deleting what it created) must not be refused
    there."""
    _rc, text = _subrun(tmp_path, {"test_interrupted.py": _SUBRUN_INTERRUPTED})
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
