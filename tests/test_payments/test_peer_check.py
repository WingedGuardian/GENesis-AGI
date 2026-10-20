"""Tests for the release peer check, on a fake /proc tree."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from genesis.payments import peer_check as pc

ACCEPT = 10_000
PEER = 500
SERVER = 777  # genesis-server: not in the default chain


def _stat_line(pid: int, comm: str, state: str, ppid: int, start: int) -> str:
    # After ")": state, ppid, 17 fields, then starttime (index 19), then more.
    rest = [state, str(ppid), *["0"] * 17, str(start), *["0"] * 5]
    return f"{pid} ({comm}) " + " ".join(rest) + "\n"


class FakeProc:
    """A /proc tree: peer 500 python <- 400 terse <- 300 claude <- 200 bash <- 100 tmux <- 1."""

    def __init__(self, root: Path) -> None:
        self.root = root / "proc"
        real = root / "usr" / "bin" / "python3.12"
        real.parent.mkdir(parents=True)
        real.write_text("")
        self.python = root / "venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.python.symlink_to(real)
        self.real = real
        self.script = str(root / "repo" / "scripts" / "genesis_mcp_server.py")
        for pid, comm, ppid, start in (
            (1, "systemd", 0, 1),
            (100, "tmux: server", 1, 10),
            (200, "bash", 100, 20),
            (300, "claude", 200, 30),
            (400, "terse", 300, 40),
        ):
            self.add(pid, comm, ppid, start)
        self.add(PEER, "python", 400, 50, argv=self.argv(), environ={"HOME": "/h"})

    def argv(self) -> list[str]:
        return [str(self.python), self.script, "--server", "health"]

    def add(self, pid, comm, ppid, start, *, state="S", argv=None, environ=None, exe=None):
        d = self.root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(_stat_line(pid, comm, state, ppid, start))
        (d / "comm").write_text(comm + "\n")
        if exe is None:
            exe = str(self.real)
        if (d / "exe").is_symlink():
            (d / "exe").unlink()
        (d / "exe").symlink_to(exe)
        (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in (argv or [comm])))
        env = environ or {}
        (d / "environ").write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items()))

    def policy(self) -> pc.PeerPolicy:
        return pc.PeerPolicy(python=str(self.python), script=self.script, proc_root=self.root)

    def check(self, pid=PEER, server_pid=SERVER, accept=ACCEPT) -> pc.Peer:
        return pc.check_release_peer(
            pid, policy=self.policy(), server_pid=server_pid, accept_ticks=accept
        )


@pytest.fixture
def proc(tmp_path):
    return FakeProc(tmp_path)


def _refused(proc, **kw) -> pc.PeerRefused:
    with pytest.raises(pc.PeerRefused) as info:
        proc.check(**kw)
    return info.value


# -- the genuine peer --------------------------------------------------------


def test_the_health_server_under_a_foreground_claude_passes(proc):
    peer = proc.check()
    assert (peer.pid, peer.start, peer.comm) == (PEER, 50, "python")
    assert peer.chain == ("terse", "claude")
    assert (peer.claude_pid, peer.kind) == (300, pc.FOREGROUND)


def test_a_session_genesis_server_launched_is_told_apart_by_ancestry(proc):
    assert proc.check(server_pid=100).kind == pc.LAUNCHED


def test_a_session_genesis_server_launched_is_told_apart_by_its_environ(proc):
    (proc.root / "300" / "environ").write_bytes(b"A=1\0GENESIS_CC_SESSION=1\0")
    assert proc.check().kind == pc.LAUNCHED


def test_an_unreadable_claude_environ_leaves_the_kind_to_ancestry(proc):
    (proc.root / "300" / "environ").write_bytes(b"GENESIS_CC_SESSION=1\0")
    (proc.root / "300" / "environ").chmod(0)
    try:
        assert proc.check().kind == pc.FOREGROUND
    finally:
        (proc.root / "300" / "environ").chmod(0o600)


def test_the_default_policy_is_the_venv_python_and_repo_script(monkeypatch, tmp_path):
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("VENV_PATH", raising=False)
    policy = pc.PeerPolicy.default()
    assert policy.python == str(tmp_path / ".venv" / "bin" / "python")
    assert policy.script == str(tmp_path / "scripts" / "genesis_mcp_server.py")
    assert (policy.args, policy.ancestor_comm) == (("--server", "health"), "claude")


def test_accept_ticks_is_on_the_stat_start_time_clock():
    own = pc.start_of(os.getpid())
    assert own is not None and own <= pc.accept_ticks() + 1


# -- every refusal, by its fixed reason ------------------------------------------


def _gone(p):
    for f in (p.root / str(PEER)).iterdir():
        f.unlink()
    (p.root / str(PEER)).rmdir()


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda p: None, None),  # control: the unmodified tree passes
        (_gone, pc.PEER_GONE),
        (lambda p: p.add(PEER, "python", 400, 50, state="Z", argv=p.argv()), pc.PEER_GONE),
        (lambda p: p.add(PEER, "python", 400, 50, state="X", argv=p.argv()), pc.PEER_GONE),
        (lambda p: (p.root / str(PEER) / "comm").unlink(), pc.PEER_GONE),
        (lambda p: (p.root / str(PEER) / "stat").write_text("garbage"), pc.PEER_GONE),
        (lambda p: p.add(PEER, "python", 400, ACCEPT + 2, argv=p.argv()), pc.PID_REUSED),
        (lambda p: (p.root / str(PEER) / "exe").unlink(), pc.EXE_MISMATCH),
        (
            lambda p: p.add(PEER, "python", 400, 50, argv=p.argv(), exe="/bin/sh"),
            pc.EXE_MISMATCH,
        ),
        (
            lambda p: p.add(PEER, "python", 400, 50, argv=p.argv(), exe=f"{p.real} (deleted)"),
            pc.EXE_MISMATCH,
        ),
        (lambda p: (p.root / str(PEER) / "cmdline").write_bytes(b""), pc.ARGV_MISMATCH),
        (lambda p: (p.root / str(PEER) / "cmdline").unlink(), pc.ARGV_MISMATCH),
        (
            lambda p: (p.root / str(PEER) / "cmdline").write_bytes(b"x" * 5000),
            pc.ARGV_MISMATCH,
        ),
        (lambda p: (p.root / str(PEER) / "environ").unlink(), pc.PEER_GONE),
        (
            lambda p: (p.root / str(PEER) / "environ").write_bytes(b"A=" + b"x" * 300_000),
            pc.ENVIRON_UNREADABLE,
        ),
        (lambda p: (p.root / "400" / "stat").unlink(), pc.ANCESTOR_GONE),
        (lambda p: (p.root / "400" / "comm").unlink(), pc.ANCESTOR_GONE),
        (lambda p: p.add(400, "terse", 300, 60), pc.ANCESTOR_REUSED),
        (
            lambda p: p.add(300, "claude", 200, 45),  # older than its child terse
            pc.ANCESTOR_REUSED,
        ),
        (lambda p: p.add(300, "node", 200, 30), pc.NO_CLAUDE),
    ],
)
def test_each_failed_read_refuses_with_its_reason(proc, setup, reason):
    setup(proc)
    if reason is None:
        assert proc.check().pid == PEER
        return
    exc = _refused(proc)
    assert exc.reason == reason
    assert exc.detail["check"] == reason


@pytest.mark.parametrize("state", ["Z", "X"])
def test_a_dead_peer_is_refused_at_the_first_read(proc, state):
    proc.add(PEER, "python", 400, 50, state=state, argv=proc.argv())
    exc = _refused(proc)
    # Refused at the stat read, before exe or argv are looked at (a later
    # re-read would also refuse it, so this is what tells the check apart).
    assert exc.reason == pc.PEER_GONE and "exe" not in exc.detail


def test_pid_zero_is_a_peer_in_another_namespace(proc):
    assert _refused(proc, pid=0).reason == pc.PEER_UNKNOWN


@pytest.mark.parametrize(
    "argv",
    [
        lambda p: [*p.argv(), "--extra"],
        lambda p: [str(p.python), "/wt/scripts/genesis_mcp_server.py", "--server", "health"],
        lambda p: [str(p.python), p.script, "--server=health"],
        lambda p: [str(p.python), "-I", p.script, "--server", "health"],
        lambda p: [str(p.python), p.script, "--server", "memory"],
        lambda p: ["python", p.script, "--server", "health"],
    ],
)
def test_any_other_argv_is_refused(proc, argv):
    proc.add(PEER, "python", 400, 50, argv=argv(proc))
    assert _refused(proc).reason == pc.ARGV_MISMATCH


@pytest.mark.parametrize(
    "name", ["PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONSAFEPATH", "PYTHONX"]
)
def test_a_python_variable_refuses_and_records_its_name_only(proc, name):
    proc.add(PEER, "python", 400, 50, argv=proc.argv(), environ={"HOME": "/h", name: "s3cr3t-v"})
    exc = _refused(proc)
    assert exc.reason == pc.PYTHON_ENV
    assert exc.detail["python_env"] == [name]
    assert "s3cr3t-v" not in repr(exc.detail)


def test_recorded_python_names_are_capped(proc):
    env = {f"PYTHON{i}" + "N" * 100: "v" for i in range(12)}
    proc.add(PEER, "python", 400, 50, argv=proc.argv(), environ=env)
    names = _refused(proc).detail["python_env"]
    assert len(names) == 8 and all(len(n) == 64 for n in names)


def test_an_unreadable_environ_refuses(proc):
    environ = proc.root / str(PEER) / "environ"
    environ.chmod(0)
    try:
        assert _refused(proc).reason == pc.ENVIRON_UNREADABLE
    finally:
        environ.chmod(0o600)


def test_a_refusal_records_exe_and_comm_never_argv(proc):
    proc.add(PEER, "python", 400, 50, argv=[*proc.argv(), "TOKENARG"])
    detail = _refused(proc).detail
    assert detail["comm"] == "python" and detail["exe"] == str(proc.real)
    assert "TOKENARG" not in repr(detail)


def test_claude_at_the_eighth_hop_passes_and_at_the_ninth_refuses(proc):
    # Rebuild the chain: peer <- 8 wrappers ... <- claude.
    def chain(wrappers: int) -> None:
        parent = 900
        proc.add(900, "claude", 1, 1)
        for i in range(wrappers):
            pid = 901 + i
            proc.add(pid, f"w{i}", parent, 2 + i)
            parent = pid
        proc.add(PEER, "python", parent, 50, argv=proc.argv())

    chain(7)  # claude is hop 8
    assert proc.check().claude_pid == 900
    chain(8)  # claude is hop 9
    assert _refused(proc).reason == pc.NO_CLAUDE


def test_a_stat_that_changes_between_the_first_and_last_read_refuses(proc, monkeypatch):
    real = pc._stat
    calls = {"n": 0}

    def swapping(root, pid):
        out = real(root, pid)
        if pid == PEER:
            calls["n"] += 1
            if calls["n"] == 1:  # the pid is reused after the first read
                proc.add(PEER, "python", 400, 51, argv=proc.argv())
        return out

    monkeypatch.setattr(pc, "_stat", swapping)
    assert _refused(proc).reason == pc.PEER_GONE


def test_start_of_reads_a_live_pid_and_refuses_a_zombie(proc):
    assert pc.start_of(PEER, proc.root) == 50
    proc.add(PEER, "python", 400, 50, state="Z")
    assert pc.start_of(PEER, proc.root) is None
    assert pc.start_of(4242, proc.root) is None
