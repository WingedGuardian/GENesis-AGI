"""scripts/lib/port_owned_by.py against REAL sockets.

The code-only deploy's health check trusts this to answer "is every socket
listening on the health port held by the restarted unit's pid?". It is driven
here with listening sockets this test (and a child it starts) actually open, so
/proc/net/tcp{,6} and /proc/<pid>/fd are read for real; the deploy tests
substitute a stand-in so they never probe the live server's port.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "scripts" / "lib" / "port_owned_by.py"

pytestmark = pytest.mark.skipif(
    not Path("/proc/net/tcp").exists(), reason="needs Linux /proc/net/tcp"
)


def _probe(port: int, pid: int | str) -> int:
    return subprocess.run(
        [sys.executable, str(PROBE), str(port), str(pid)],
        capture_output=True,
        text=True,
        timeout=30,
    ).returncode


@pytest.fixture
def listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        yield s.getsockname()[1]
    finally:
        s.close()


def test_a_socket_this_process_listens_on_is_owned_by_it(listener):
    import os

    assert _probe(listener, os.getpid()) == 0


def test_the_same_socket_is_not_owned_by_another_process(listener):
    other = subprocess.Popen(["sleep", "30"])
    try:
        assert _probe(listener, other.pid) == 1
    finally:
        other.kill()
        other.wait()


def test_nothing_listening_is_never_proof():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # bound once, now free: nothing listens on it
    import os

    assert _probe(port, os.getpid()) == 1


def test_a_second_listener_held_by_another_process_is_not_owned(listener):
    """The unit's own socket is not enough when ANOTHER process also listens on
    the port (here an IPv6-only socket from a child): every listener counts."""
    if not Path("/proc/net/tcp6").exists() or not socket.has_ipv6:
        pytest.skip("no IPv6 here")
    import os

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import socket, sys, time\n"
            "s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)\n"
            "s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)\n"
            f"s.bind(('::1', {listener}))\n"
            "s.listen(1)\n"
            "print('up', flush=True)\n"
            "time.sleep(30)\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = child.stdout.readline().strip()
        if line != "up":
            pytest.skip(f"could not open an IPv6 listener here: {child.stderr.read()[:200]}")
        time.sleep(0.2)
        # Control: this process alone did own the port before the child bound.
        assert _probe(listener, os.getpid()) == 1, "a foreign listener must refuse"
        assert _probe(listener, child.pid) == 1, "and the child does not own ours"
    finally:
        child.kill()
        child.wait()
    assert _probe(listener, os.getpid()) == 0, "control: once the child is gone, ours again"


def test_a_second_ipv4_listener_held_by_another_process_is_not_owned(listener):
    """The same rule within /proc/net/tcp alone, so a runner without IPv6 still
    checks it: Linux lets a child bind 127.0.0.2 on the port our socket holds on
    127.0.0.1, and that second listener must refuse."""
    import os

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import socket, sys, time\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            f"s.bind(('127.0.0.2', {listener}))\n"
            "s.listen(1)\n"
            "print('up', flush=True)\n"
            "time.sleep(30)\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = child.stdout.readline().strip()
        if line != "up":
            pytest.skip(f"could not bind 127.0.0.2 here: {child.stderr.read()[:200]}")
        time.sleep(0.2)
        assert _probe(listener, os.getpid()) == 1, "a foreign listener must refuse"
        assert _probe(listener, child.pid) == 1, "and the child does not own ours"
    finally:
        child.kill()
        child.wait()
    assert _probe(listener, os.getpid()) == 0, "control: once the child is gone, ours again"


@pytest.mark.parametrize(
    "args", [["5000"], ["x", "1234"], ["5000", "x"], ["5000", "1"], ["5000", "0"]]
)
def test_bad_arguments_are_never_proof(args):
    r = subprocess.run([sys.executable, str(PROBE), *args], capture_output=True, timeout=30)
    assert r.returncode == 1


def _load_probe():
    import importlib.util

    spec = importlib.util.spec_from_file_location("port_owned_by", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    ("error", "expected"),
    [(PermissionError, 1), (FileNotFoundError, 0)],
    ids=["unreadable-tcp6-refuses", "absent-tcp6-is-skipped"],
)
def test_only_an_absent_ipv6_table_may_be_skipped(listener, monkeypatch, error, expected):
    """A tcp6 table that exists but cannot be read may hide a foreign IPv6
    listener, so it refuses; an absent one (IPv6 disabled) is skipped."""
    import builtins
    import os

    mod = _load_probe()
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if str(path) == "/proc/net/tcp6":
            raise error(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert mod.main(["port_owned_by.py", str(listener), str(os.getpid())]) == expected
