"""genesis.sentinel.process_stop: target selection, stop sequence, lever.

Runs against a synthetic /proc (proc_io._PROC is the seam) and an injected
kill, so no real process is ever signalled.
"""

from __future__ import annotations

import signal

import pytest

from genesis.sentinel import process_stop as mod
from genesis.util import proc_io

SELF = 50  # the tool's own process in the fake tree
GB = 1024 * 1024  # kB


def _w(
    root,
    pid,
    *,
    ppid=1,
    comm="python3",
    argv=("python3", "x.py"),
    rss_kb=1024,
    swap_kb=0,
    state="S",
    cgroup="/user.slice/user-1000.slice/session-1.scope",
    starttime=None,
    status=True,
):
    d = root / str(pid)
    d.mkdir()
    after = (
        ["S" if state is None else state, str(ppid), str(pid)]
        + ["0"] * 16
        + [str(starttime or pid * 10)]
    )
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(after) + "\n")
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + (b"\0" if argv else b""))
    (d / "cgroup").write_text(f"0::{cgroup}\n")
    if status:
        (d / "status").write_text(f"Name:\t{comm}\nVmRSS:\t{rss_kb} kB\nVmSwap:\t{swap_kb} kB\n")


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(proc_io, "_PROC", str(root))
    _w(root, 1, ppid=0, comm="systemd", argv=("/sbin/init",), rss_kb=8 * GB)  # pid 1: never
    _w(root, 40, ppid=1, comm="bash", argv=("bash",), rss_kb=7 * GB)  # ancestor of the tool
    _w(root, SELF, ppid=40, comm="python3", argv=("python3", "-m", "x"), rss_kb=7 * GB)
    # The target: a runaway CLI, 5 GB resident + 2 GB swapped.
    _w(
        root,
        200,
        comm="codex",
        argv=("/opt/bin/codex", "--token", "s3cr3t"),
        rss_kb=5 * GB,
        swap_kb=2 * GB,
    )
    # Exclusions, one per rule.
    _w(
        root,
        201,
        comm="python",
        argv=("python", "-m", "genesis", "serve"),
        rss_kb=9 * GB,
        cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/genesis-server.service",
    )
    _w(root, 202, comm="small", argv=("small",), rss_kb=1 * GB)
    _w(root, 203, comm="zomb", argv=("zomb",), rss_kb=8 * GB, state="Z")
    _w(root, 204, comm="kworker", argv=(), rss_kb=8 * GB)
    return root


def test_scan_targets_only_the_runaway(fake_proc):
    found = mod.scan(6144, self_pid=SELF)
    assert [t.pid for t in found.targets] == [200]
    t = found.targets[0]
    assert t.held_kb == 7 * GB  # RSS + swap
    assert t.label == "codex"  # argv after the program is never printed
    assert found.uncertain == []


def test_swap_counts_toward_the_threshold(fake_proc):
    assert [t.pid for t in mod.scan(6144, self_pid=SELF).targets] == [
        200
    ]  # 5 GB RSS alone is under
    assert mod.scan(7169, self_pid=SELF).targets == []


def test_another_users_process_is_never_a_target(fake_proc, monkeypatch):
    monkeypatch.setattr(mod, "_same_uid", lambda pid: pid != 200)
    assert mod.scan(6144, self_pid=SELF).targets == []


def test_unreadable_candidates_are_uncertain(fake_proc, monkeypatch):
    monkeypatch.setattr(mod, "_same_uid", lambda pid: None if pid == 200 else True)
    found = mod.scan(6144, self_pid=SELF)
    assert found.targets == [] and any(u.startswith("200 ") for u in found.uncertain)
    (fake_proc / "200" / "cgroup").unlink()
    monkeypatch.setattr(mod, "_same_uid", lambda pid: True)
    found = mod.scan(6144, self_pid=SELF)
    assert found.targets == [] and any("cgroup unreadable" in u for u in found.uncertain)


def test_largest_first(fake_proc):
    _w(fake_proc, 210, comm="big", argv=("big",), rss_kb=9 * GB)
    assert [t.pid for t in mod.scan(6144, self_pid=SELF).targets] == [210, 200]


def test_unlistable_proc_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(proc_io, "_PROC", str(tmp_path / "missing"))
    with pytest.raises(OSError):
        mod.scan(6144, self_pid=SELF)


# --- stop -----------------------------------------------------------------


class _Fake:
    def __init__(self, survive_term=(), refuse=()):
        self.alive = set()
        self.sent = []
        self.survive_term = set(survive_term)
        self.refuse = set(refuse)

    def kill(self, pid, starttime, sig):
        if pid in self.refuse:
            raise PermissionError("nope")
        self.sent.append((pid, sig))
        if sig == signal.SIGKILL or pid not in self.survive_term:
            self.alive.discard(pid)

    def is_alive(self, pid, starttime):
        return pid in self.alive


def _targets(*pids):
    return [
        mod.Target(pid=p, starttime=p * 10, label="x", held_kb=GB, unit="s.scope") for p in pids
    ]


def test_stop_term_then_gone():
    f = _Fake()
    f.alive = {200}
    stopped, failures = mod.stop(
        _targets(200), kill=f.kill, alive=f.is_alive, sleep=lambda s: None, grace_s=0
    )
    assert [t.pid for t in stopped] == [200] and failures == []
    assert f.sent == [(200, signal.SIGTERM)]


def test_stop_escalates_to_kill():
    f = _Fake(survive_term={200})
    f.alive = {200}
    stopped, failures = mod.stop(
        _targets(200), kill=f.kill, alive=f.is_alive, sleep=lambda s: None, grace_s=0
    )
    assert f.sent == [(200, signal.SIGTERM), (200, signal.SIGKILL)]
    assert [t.pid for t in stopped] == [200] and failures == []


def test_stop_never_signals_a_reused_pid():
    """alive() checks the start time: a pid now held by another process reads
    as gone, so nothing is sent to it."""
    f = _Fake()
    stopped, failures = mod.stop(
        _targets(200), kill=f.kill, alive=f.is_alive, sleep=lambda s: None, grace_s=0
    )
    assert f.sent == [] and [t.pid for t in stopped] == [200]


def test_stop_reports_a_refused_signal():
    f = _Fake(refuse={200})
    f.alive = {200}
    stopped, failures = mod.stop(
        _targets(200), kill=f.kill, alive=f.is_alive, sleep=lambda s: None, grace_s=0
    )
    assert stopped == [] and "refused" in failures[0]


def test_stop_never_signals_pid_one_or_less():
    f = _Fake()
    f.alive = {1, 0}
    stopped, failures = mod.stop(
        _targets(1, 0), kill=f.kill, alive=f.is_alive, sleep=lambda s: None, grace_s=0
    )
    assert f.sent == [] and len(failures) == 2


def test_alive_checks_identity_and_zombies(fake_proc):
    assert mod._alive(200, 2000) is True
    assert mod._alive(200, 1) is False  # different start time: a reused pid
    assert mod._alive(203, 2030) is False  # zombie
    assert mod._alive(999, 1) is False


# --- lever and threshold ---------------------------------------------------


@pytest.mark.parametrize(
    "body,mode,mb",
    [
        ("", mod.MODE_PROPOSE_ONLY, mod.DEFAULT_PROPOSE_AT_MB),
        (
            "process_stop:\n  mode: propose_only\n  propose_at_mb: 2048\n",
            mod.MODE_PROPOSE_ONLY,
            2048,
        ),
        ("process_stop:\n  mode: off\n", mod.MODE_OFF, mod.DEFAULT_PROPOSE_AT_MB),
        ("process_stop:\n  mode: live\n", mod.MODE_OFF, mod.DEFAULT_PROPOSE_AT_MB),
        ("process_stop:\n", mod.MODE_OFF, mod.DEFAULT_PROPOSE_AT_MB),
        ("process_stop:\n  propose_at_mb: 0\n", mod.MODE_PROPOSE_ONLY, None),
        ("process_stop:\n  propose_at_mb: true\n", mod.MODE_PROPOSE_ONLY, None),
        ("process_stop: [\n", mod.MODE_OFF, None),
    ],
)
def test_lever_and_threshold(tmp_path, monkeypatch, body, mode, mb):
    monkeypatch.setattr(mod, "_WARNED_ONCE", set())
    cfg = tmp_path / "sentinel.yaml"
    cfg.write_text(body)
    assert mod.load_mode(cfg) == mode
    assert mod.load_propose_at_mb(cfg) == mb


def test_env_kill_switch(tmp_path, monkeypatch):
    cfg = tmp_path / "sentinel.yaml"
    cfg.write_text("process_stop:\n  mode: propose_only\n")
    monkeypatch.setenv("GENESIS_PROCESS_STOP_DISABLED", "1")
    assert mod.load_mode(cfg) == mod.MODE_OFF


def test_shipped_config_default(monkeypatch):
    monkeypatch.delenv("GENESIS_PROCESS_STOP_DISABLED", raising=False)
    assert mod.load_mode() == mod.MODE_PROPOSE_ONLY
    assert mod.load_propose_at_mb() == 6144


# --- main --------------------------------------------------------------------


def test_main_dry_run_signals_nothing_and_never_prints_argv(fake_proc, monkeypatch, capsys):
    monkeypatch.setattr(mod, "load_mode", lambda path=None: mod.MODE_PROPOSE_ONLY)
    monkeypatch.setattr(mod, "load_propose_at_mb", lambda path=None: 6144)
    monkeypatch.setattr(mod.os, "getpid", lambda: SELF)
    monkeypatch.setattr(mod.os, "kill", lambda *a: pytest.fail("dry-run signalled"))
    assert mod.main([]) == mod.EXIT_OK
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith(
        "process_stop: would stop 1 process(es) at or over 6144 MB"
    )
    assert "pid 200" in out and "s3cr3t" not in out and "--token" not in out


def test_main_off_refuses(monkeypatch, capsys):
    monkeypatch.setattr(mod, "load_mode", lambda path=None: mod.MODE_OFF)
    assert mod.main(["--execute"]) == mod.EXIT_DISABLED


def _main_env(monkeypatch):
    monkeypatch.setattr(mod, "load_mode", lambda path=None: mod.MODE_PROPOSE_ONLY)
    monkeypatch.setattr(mod, "load_propose_at_mb", lambda path=None: 6144)
    monkeypatch.setattr(mod.os, "getpid", lambda: SELF)
    calls = []

    def fake_stop(targets):
        calls.append([t.pid for t in targets])
        return targets, []

    monkeypatch.setattr(mod, "stop", fake_stop)
    return calls


def test_dry_run_prints_the_exact_approval_command(fake_proc, monkeypatch, capsys):
    _main_env(monkeypatch)
    assert mod.main([]) == mod.EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert (
        lines[1]
        == "  approve with: python -m genesis.sentinel.process_stop --execute --only 200:2000"
    )


def test_execute_without_only_is_refused(fake_proc, monkeypatch, capsys):
    calls = _main_env(monkeypatch)
    assert mod.main(["--execute"]) == mod.EXIT_USAGE
    assert calls == []


@pytest.mark.parametrize("raw", ["", "200", "200:", "x:1", "1:5", "200:2000,junk", "200:-1", "²00:2000"])
def test_unparseable_only_is_refused(fake_proc, monkeypatch, raw):
    calls = _main_env(monkeypatch)
    assert mod.main(["--execute", "--only", raw]) == mod.EXIT_USAGE
    assert calls == []


def test_execute_stops_only_the_approved_identity(fake_proc, monkeypatch, capsys):
    """A second process crossed the line after the dry-run: the approval for
    200 must never stop it (architect BLOCKER, 2026-11-15)."""
    calls = _main_env(monkeypatch)
    _w(fake_proc, 210, comm="claude", argv=("claude",), rss_kb=9 * GB)
    # Devin review: 210 is still over the line, so the run is not done (the
    # Sentinel must not record the alarm resolved), but it is never signalled.
    assert mod.main(["--execute", "--only", "200:2000"]) == mod.EXIT_PARTIAL
    assert calls == [[200]]
    out = capsys.readouterr().out
    assert "stopped 1 of 1 approved" in out
    assert "NOT APPROVED pid 210:2100" in out


def test_execute_skips_an_approved_pid_now_held_by_another_process(fake_proc, monkeypatch, capsys):
    calls = _main_env(monkeypatch)
    # The start time differs: the approval names another process. The one now
    # at pid 200 is still over the line and unapproved, so the run is partial.
    assert mod.main(["--execute", "--only", "200:1999"]) == mod.EXIT_PARTIAL
    assert calls == [[]]
    out = capsys.readouterr().out
    assert "not stopped 200:1999: no longer an eligible target" in out
    assert "NOT APPROVED pid 200:2000" in out


def test_execute_skips_an_approved_process_now_under_the_line(fake_proc, monkeypatch, capsys):
    calls = _main_env(monkeypatch)
    (fake_proc / "200" / "status").write_text("Name:\tcodex\nVmRSS:\t1024 kB\nVmSwap:\t0 kB\n")
    assert mod.main(["--execute", "--only", "200:2000"]) == mod.EXIT_OK
    assert calls == [[]]


@pytest.mark.parametrize("unreadable", ["cgroup", "raises"])
def test_an_approved_process_that_cannot_be_read_is_a_partial_result(
    fake_proc, monkeypatch, capsys, unreadable
):
    """Codex/Devin review: still running but unjudged is not done, so the
    Sentinel must not record the remedy as resolved."""
    calls = _main_env(monkeypatch)
    if unreadable == "cgroup":
        (fake_proc / "200" / "cgroup").unlink()
    else:
        monkeypatch.setattr(
            mod, "_same_uid", lambda pid: (_ for _ in ()).throw(RuntimeError())
        )
    assert mod.main(["--execute", "--only", "200:2000"]) == mod.EXIT_PARTIAL
    assert calls == [[]]
    out = capsys.readouterr().out
    assert "NOT STOPPED 200:2000: could not be read" in out
    assert "no longer an eligible target" not in out


def test_an_unreadable_unapproved_candidate_makes_the_run_partial(fake_proc, monkeypatch, capsys):
    """Audit (round 2): the approved process stops, but another big process
    could not be read, so the run cannot say nothing over the line remains
    (the alarm, reading the same scan, says unknown). It is never signalled."""
    calls = _main_env(monkeypatch)
    _w(fake_proc, 220, comm="python", argv=("python",), rss_kb=9 * GB)
    (fake_proc / "220" / "cgroup").unlink()
    assert mod.main(["--execute", "--only", "200:2000"]) == mod.EXIT_PARTIAL
    assert calls == [[200]]
    out = capsys.readouterr().out
    assert "stopped 1 of 1 approved" in out
    assert "NOT JUDGED pid 220" in out


def test_a_status_read_error_is_uncertain_not_gone(fake_proc, monkeypatch, capsys):
    """Only a vanished process reads as gone; EACCES or EIO is unjudged."""
    calls = _main_env(monkeypatch)
    real = mod.Path.read_bytes

    def _read_bytes(self):
        if self.name == "status" and self.parent.name == "200":
            raise PermissionError(13, "denied")
        return real(self)

    monkeypatch.setattr(mod.Path, "read_bytes", _read_bytes)
    assert 200 in mod.scan(6144, self_pid=SELF).uncertain_pids
    assert mod.main(["--execute", "--only", "200:2000"]) == mod.EXIT_PARTIAL
    assert calls == [[]]
    assert "NOT STOPPED 200:2000" in capsys.readouterr().out


def _target(pid=200, st=2000):
    return mod.Target(pid=pid, starttime=st, held_kb=9 * GB, label="x", unit="session-1.scope")


def test_unreadable_liveness_before_the_signal_is_a_failure_not_a_stop():
    """Codex review: an unreadable stat is no evidence the process stopped."""
    sent = []
    stopped, failures = mod.stop(
        [_target()], kill=lambda *a: sent.append(a), alive=lambda pid, st: None, sleep=lambda s: None,
    )
    assert stopped == [] and sent == []
    assert failures and "liveness unreadable" in failures[0]


def test_unreadable_liveness_after_the_signals_is_a_failure_not_a_stop():
    states = iter([True] + [None] * 50)
    stopped, failures = mod.stop(
        [_target()], kill=lambda *a: None, alive=lambda pid, st: next(states),
        sleep=lambda s: None, grace_s=0, kill_grace_s=0,
    )
    assert stopped == []
    assert failures


def test_alive_reads_a_permission_error_as_unknown(fake_proc, monkeypatch):
    real = mod.Path.read_bytes

    def _read_bytes(self):
        if self.name == "stat" and self.parent.name == "200":
            raise PermissionError(13, "denied")
        return real(self)

    monkeypatch.setattr(mod.Path, "read_bytes", _read_bytes)
    assert mod._alive(200, 2000) is None
    assert mod._alive(999, 1) is False  # gone
    assert mod._alive(210 if (fake_proc / "210").exists() else 50, 1) is False  # another start time


def test_a_non_utf8_process_name_never_aborts_the_scan(fake_proc):
    """Security review W1: a same-uid process can name itself with any bytes."""
    _w(fake_proc, 220, comm="x", argv=("bad",), rss_kb=9 * GB)
    (fake_proc / "220" / "status").write_bytes(
        b"Name:\t\xffbad\nVmRSS:\t9437184 kB\nVmSwap:\t0 kB\n"
    )
    assert sorted(t.pid for t in mod.scan(6144, self_pid=SELF).targets) == [200, 220]


def test_one_raising_process_is_uncertain_not_fatal(fake_proc, monkeypatch):
    real = mod._service_cgroup
    monkeypatch.setattr(
        mod,
        "_service_cgroup",
        lambda pid: (_ for _ in ()).throw(RuntimeError()) if pid == 200 else real(pid),
    )
    _w(fake_proc, 210, comm="big", argv=("big",), rss_kb=9 * GB)
    found = mod.scan(6144, self_pid=SELF)
    assert [t.pid for t in found.targets] == [210]
    assert any(u.startswith("200 ") for u in found.uncertain)


def test_a_sub_cgroup_of_a_service_is_never_a_target(fake_proc):
    """Security review W2: a delegated sub-cgroup still belongs to its service."""
    _w(
        fake_proc,
        230,
        comm="worker",
        argv=("worker",),
        rss_kb=9 * GB,
        cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/genesis-server.service/payload",
    )
    assert [t.pid for t in mod.scan(6144, self_pid=SELF).targets] == [200]


def test_a_pidfd_error_is_a_reported_failure_not_a_crash():
    """Security review W3."""

    class Boom(_Fake):
        def kill(self, pid, starttime, sig):
            raise OSError(38, "Function not implemented")

    f = Boom()
    f.alive = {200}
    stopped, failures = mod.stop(
        _targets(200),
        kill=f.kill,
        alive=f.is_alive,
        sleep=lambda s: None,
        grace_s=0,
        kill_grace_s=0,
    )
    assert stopped == [] and "refused" in failures[0]


def test_init_scope_is_never_a_target(fake_proc):
    _w(
        fake_proc,
        249,
        comm="systemd",
        argv=("/lib/systemd/systemd", "--user"),
        rss_kb=9 * GB,
        cgroup="/user.slice/user-1000.slice/user@1000.service/init.scope",
    )
    assert [t.pid for t in mod.scan(6144, self_pid=SELF).targets] == [200]


def test_stop_waits_for_a_slow_teardown_after_sigkill():
    """A big process can read as alive for a while after SIGKILL; that is not a failure."""
    f = _Fake(survive_term={200})
    f.alive = {200}
    reads = {"n": 0}

    def slow_alive(pid, starttime):
        if (pid, signal.SIGKILL) in f.sent:
            reads["n"] += 1
            return reads["n"] < 4  # still tearing down for three more reads
        return pid in f.alive

    stopped, failures = mod.stop(
        _targets(200),
        kill=f.kill,
        alive=slow_alive,
        sleep=lambda s: None,
        grace_s=0,
        kill_grace_s=5,
    )
    assert failures == [] and [t.pid for t in stopped] == [200]


def test_pidfd_kill_refuses_a_different_start_time_and_signals_the_right_one():
    """Real process, real pidfd: a wrong start time signals nothing."""
    import subprocess

    child = subprocess.Popen(["sleep", "30"])
    try:
        real = proc_io.read_starttime(child.pid)
        assert real is not None
        with pytest.raises(ProcessLookupError):
            mod._pidfd_kill(child.pid, real + 1, signal.SIGTERM)
        assert child.poll() is None  # still running
        mod._pidfd_kill(child.pid, real, signal.SIGTERM)
        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
