"""genesis.util.proc_io — the shared /proc/<pid>/io sampler (watchdog + Guardian)."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from genesis.util import proc_io


def _proc(root: Path, pid: int, *, read: int, write: int, comm: str = "worker",
          cgroup: str | None = "0::/user.slice/user@1000.service/app.slice/genesis-backup.service\n") -> None:
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "io").write_text(
        f"rchar: 1\nwchar: 2\nsyscr: 3\nsyscw: 4\nread_bytes: {read}\nwrite_bytes: {write}\n"
        "cancelled_write_bytes: 0\n"
    )
    (d / "comm").write_text(comm + "\n")
    if cgroup is not None:
        (d / "cgroup").write_text(cgroup)


@pytest.fixture
def fake_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(proc_io, "_PROC", str(tmp_path))
    return tmp_path


def test_read_proc_io_parses_the_bytes_lines(fake_proc: Path) -> None:
    _proc(fake_proc, 42, read=4096, write=8192, comm="python3")
    got = proc_io.read_proc_io(42)
    assert got == {"pid": 42, "read_bytes": 4096, "write_bytes": 8192,
                   "total_bytes": 12288, "comm": "python3"}


def test_read_proc_io_is_none_for_a_missing_or_unreadable_pid(fake_proc: Path) -> None:
    import os

    assert proc_io.read_proc_io(7) is None
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes, so an unreadable file cannot be built")
    _proc(fake_proc, 8, read=1, write=1)
    (fake_proc / "8" / "io").chmod(0)
    try:
        assert proc_io.read_proc_io(8) is None
    finally:
        (fake_proc / "8" / "io").chmod(0o644)


def test_rank_uses_the_delta_not_the_lifetime_total(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long-lived process with a huge cumulative total but no current I/O ranks last."""
    _proc(fake_proc, 1, read=10**12, write=10**12, comm="old-giant")
    _proc(fake_proc, 2, read=0, write=0, comm="writer")
    _proc(fake_proc, 3, read=0, write=0, comm="exits")

    def _sleep(_s: float) -> None:
        # Between the two samples: pid 2 writes 5 MB, pid 3 exits.
        _proc(fake_proc, 2, read=0, write=5_000_000, comm="writer")
        for f in (fake_proc / "3").iterdir():
            f.unlink()
        (fake_proc / "3").rmdir()

    monkeypatch.setattr(proc_io.time, "sleep", _sleep)
    rates, readable, total, churn = proc_io.rank_by_io_rate(
        [1, 2, 3, 99], top_n=5, sample_interval_s=1.0
    )

    assert readable == 3  # pid 99 never existed
    assert total == 5_000_000.0
    assert [r["pid"] for r in rates] == [2, 1]  # pid 3 not ranked, 2 first
    assert churn == {"exited": 1, "started": 0}  # ... and counted, never "no I/O"
    assert rates[0]["write_rate"] == 5_000_000.0
    assert rates[1]["total_rate"] == 0.0


def test_rank_with_nothing_readable_does_not_sleep(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(proc_io.time, "sleep", slept.append)
    assert proc_io.rank_by_io_rate([5, 6]) == ([], 0, 0.0, {"exited": 0, "started": 0})
    assert slept == []


def test_systemd_unit_is_the_last_cgroup_component(fake_proc: Path) -> None:
    _proc(fake_proc, 10, read=0, write=0)
    assert proc_io.systemd_unit(10) == "genesis-backup.service"
    _proc(fake_proc, 11, read=0, write=0, cgroup="0::/\n")
    assert proc_io.systemd_unit(11) is None
    _proc(fake_proc, 12, read=0, write=0, cgroup=None)
    assert proc_io.systemd_unit(12) is None


def test_proc_io_imports_only_the_standard_library() -> None:
    """The host Guardian imports this; its venv has pyyaml and nothing else."""
    import ast

    tree = ast.parse(Path(proc_io.__file__).read_text())
    roots = {
        (n.module or "").split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    import sys

    assert roots - {"__future__"} <= set(sys.stdlib_module_names), roots


def test_the_guardian_ranker_delegates_to_the_shared_sampler(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from genesis.guardian import cgroup_ops

    _proc(fake_proc, 20, read=0, write=0, comm="a")
    monkeypatch.setattr(cgroup_ops, "list_container_pids", lambda _c: [20])
    monkeypatch.setattr(proc_io.time, "sleep", lambda _s: None)
    assert [r["pid"] for r in cgroup_ops.find_top_io_pids_rate("c")] == [20]
    assert cgroup_ops.find_top_io_pids("c")[0]["comm"] == "a"


def test_total_counts_processes_beyond_the_top_n(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The total is the denominator over ALL readable pids, not just those shown."""
    for pid in (1, 2, 3):
        _proc(fake_proc, pid, read=0, write=0)

    def _sleep(_s: float) -> None:
        for pid, w in ((1, 3_000), (2, 2_000), (3, 1_000)):
            _proc(fake_proc, pid, read=0, write=w)

    monkeypatch.setattr(proc_io.time, "sleep", _sleep)
    rates, readable, total, _churn = proc_io.rank_by_io_rate(
        [1, 2, 3], top_n=1, sample_interval_s=1.0
    )
    assert [r["pid"] for r in rates] == [1]
    assert (readable, total) == (3, 6_000.0)


def _stat(root: Path, pid: int, ppid: int, comm: str) -> None:
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1 4194560\n")
    (d / "comm").write_text(comm + "\n")


def test_claude_ancestor_walks_up_to_the_session(fake_proc: Path) -> None:
    _stat(fake_proc, 100, 1, "systemd")
    _stat(fake_proc, 200, 100, "claude")
    _stat(fake_proc, 300, 200, "bash")
    _stat(fake_proc, 400, 300, "python3")
    assert proc_io.claude_ancestor(400) == 200
    assert proc_io.claude_ancestor(200) is None  # stops at pid 1 without a claude
    _stat(fake_proc, 500, 1, "cron")
    assert proc_io.claude_ancestor(500) is None
    assert proc_io.claude_ancestor(999) is None  # gone


def test_claude_ancestor_handles_a_comm_with_spaces_and_parens(fake_proc: Path) -> None:
    _stat(fake_proc, 200, 1, "claude")
    _stat(fake_proc, 300, 200, "odd (name) x")
    assert proc_io.claude_ancestor(300) == 200


def test_claude_ancestor_gives_up_past_its_depth(fake_proc: Path) -> None:
    _stat(fake_proc, 10, 1, "claude")
    for pid in range(11, 21):
        _stat(fake_proc, pid, pid - 1, "sh")
    assert proc_io.claude_ancestor(20, max_depth=3) is None
    assert proc_io.claude_ancestor(20, max_depth=10) == 10


def test_guardian_rankers_honour_top_n_and_sort_order(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the two Guardian parameters a mutation sweep found unpinned."""
    from genesis.guardian import cgroup_ops

    for pid, total in ((1, 10), (2, 30), (3, 20)):
        _proc(fake_proc, pid, read=total, write=0)
    monkeypatch.setattr(cgroup_ops, "list_container_pids", lambda _c: [1, 2, 3])
    assert [r["pid"] for r in cgroup_ops.find_top_io_pids("c", top_n=2)] == [2, 3]

    def _sleep(_s: float) -> None:
        for pid, total in ((1, 110), (2, 30), (3, 70)):
            _proc(fake_proc, pid, read=total, write=0)

    monkeypatch.setattr(proc_io.time, "sleep", _sleep)
    assert [r["pid"] for r in cgroup_ops.find_top_io_pids_rate("c", top_n=2)] == [1, 3]



# ── round-1 review: churn, the measured interval, and comm bytes ────────────


def test_a_process_started_mid_sample_is_measured_from_zero(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-1 review: the second pass walked only the first snapshot's pids, so a
    short-lived culprit that started during the interval was never seen."""
    _proc(fake_proc, 1, read=0, write=0, comm="steady")

    def _sleep(_s: float) -> None:
        _proc(fake_proc, 7, read=0, write=3_000_000, comm="burst")  # started mid-sample

    monkeypatch.setattr(proc_io.time, "sleep", _sleep)
    rates, readable, total, churn = proc_io.rank_by_io_rate(
        [1], top_n=5, sample_interval_s=1.0, pid_source=lambda: [1, 7]
    )
    assert [r["pid"] for r in rates] == [7, 1]
    assert rates[0]["write_rate"] == 3_000_000.0
    assert (readable, total) == (1, 3_000_000.0)
    assert churn == {"exited": 0, "started": 1}


def test_rates_divide_by_the_measured_interval(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-1 review: under I/O pressure the sleep overruns; a 1 s request that
    took 5 s must not report five times the real rate."""
    import types

    clock = {"t": 100.0}
    _proc(fake_proc, 2, read=0, write=0, comm="writer")

    def _sleep(_s: float) -> None:
        clock["t"] += 5.0  # overran a 1 s request
        _proc(fake_proc, 2, read=0, write=5_000_000, comm="writer")

    fake_time = types.SimpleNamespace(monotonic=lambda: clock["t"], sleep=_sleep)
    monkeypatch.setattr(proc_io, "time", fake_time)
    rates, _r, total, _c = proc_io.rank_by_io_rate([2], sample_interval_s=1.0)
    assert rates[0]["write_rate"] == 1_000_000.0
    assert total == 1_000_000.0


def test_a_non_utf8_comm_keeps_the_pid(fake_proc: Path) -> None:
    """Round-1 review: comm may hold any non-NUL bytes; a decode error dropped a
    pid whose I/O counters were readable."""
    _proc(fake_proc, 9, read=10, write=20)
    (fake_proc / "9" / "comm").write_bytes(b"odd\xff\xfename\n")
    got = proc_io.read_proc_io(9)
    assert got is not None and got["total_bytes"] == 30
    assert got["comm"].startswith("odd") and "\ufffd" in got["comm"]


# ── round-2 review: a pid reused within the interval ────────────────────────


def _starttime(root: Path, pid: int, starttime: int, comm: str = "worker") -> None:
    """A /proc/<pid>/stat whose field 22 is ``starttime`` (fields 3..21 filler)."""
    filler = " ".join(["0"] * 18)  # fields 4..21
    (root / str(pid) / "stat").write_text(f"{pid} ({comm}) S {filler} {starttime} 0 0\n")


def test_a_pid_reused_mid_sample_is_two_processes_not_one_delta(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pid whose process exited and was reused within the interval must not be
    rated as the new process's counters minus the old one's."""
    _proc(fake_proc, 5, read=0, write=2_000_000, comm="old")
    _starttime(fake_proc, 5, 1000)

    def _sleep(_s: float) -> None:
        # pid 5 exits; a new process takes pid 5 and writes 3 MB.
        _proc(fake_proc, 5, read=0, write=3_000_000, comm="new")
        _starttime(fake_proc, 5, 2000)

    monkeypatch.setattr(proc_io.time, "sleep", _sleep)
    rates, readable, total, churn = proc_io.rank_by_io_rate(
        [5], top_n=5, sample_interval_s=1.0
    )
    assert churn == {"exited": 1, "started": 1}
    assert readable == 1
    # Measured from zero: 3 MB, never the cross-generation 3 MB - 2 MB = 1 MB.
    assert rates[0]["comm"] == "new"
    assert rates[0]["write_rate"] == 3_000_000.0
    assert total == 3_000_000.0


def test_the_same_starttime_is_an_ordinary_delta(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proc(fake_proc, 5, read=0, write=2_000_000)
    _starttime(fake_proc, 5, 1000)

    def _sleep(_s: float) -> None:
        _proc(fake_proc, 5, read=0, write=3_000_000)
        _starttime(fake_proc, 5, 1000)

    monkeypatch.setattr(proc_io.time, "sleep", _sleep)
    rates, _r, total, churn = proc_io.rank_by_io_rate([5], sample_interval_s=1.0)
    assert churn == {"exited": 0, "started": 0}
    assert rates[0]["write_rate"] == 1_000_000.0
    assert total == 1_000_000.0


def test_a_starttime_readable_at_one_sample_only_is_not_ranked(
    fake_proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity unknown: neither a delta nor a from-zero measure is trustworthy."""
    _proc(fake_proc, 5, read=0, write=2_000_000)
    _starttime(fake_proc, 5, 1000)

    def _sleep(_s: float) -> None:
        _proc(fake_proc, 5, read=0, write=3_000_000)
        (fake_proc / "5" / "stat").unlink()

    monkeypatch.setattr(proc_io.time, "sleep", _sleep)
    rates, readable, total, churn = proc_io.rank_by_io_rate([5], sample_interval_s=1.0)
    assert (rates, readable, total) == ([], 1, 0.0)
    assert churn == {"exited": 1, "started": 0}


def test_read_starttime_handles_a_comm_with_spaces_and_parens(fake_proc: Path) -> None:
    _proc(fake_proc, 30, read=0, write=0)
    _starttime(fake_proc, 30, 123456, comm="odd ) (name) x 99")
    assert proc_io.read_starttime(30) == 123456
    (fake_proc / "30" / "stat").write_text("30 (short) S 1 2\n")
    assert proc_io.read_starttime(30) is None  # truncated stat
    assert proc_io.read_starttime(31) is None  # gone


# --- tri-state reads, against the real kernel --------------------------------

_ZOMBIE_LEADER = r"""
import ctypes, sys, threading, time
buf = bytearray(24 * 1024 * 1024)
for i in range(0, len(buf), 4096):
    buf[i] = 1
threading.Thread(target=lambda: time.sleep(60)).start()
ctypes.CDLL(None).pthread_exit(None)
"""


@pytest.fixture
def zombie_leader():
    """A real process whose main thread exited while a worker thread still
    holds its memory: the kernel shows its leader as Z with two threads."""
    import signal
    import subprocess
    import sys
    import time

    if not sys.platform.startswith("linux"):
        pytest.skip("needs the Linux /proc")
    import ctypes

    if not hasattr(ctypes.CDLL(None), "pthread_exit"):
        pytest.skip("no pthread_exit in this libc")
    child = subprocess.Popen([sys.executable, "-c", _ZOMBIE_LEADER], start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            st = proc_io.probe_stat(child.pid)
            if isinstance(st, proc_io.Stat) and st.state == "Z" and st.threads >= 2:
                break
            if child.poll() is not None:
                pytest.fail(f"the child exited early (rc {child.returncode})")
            time.sleep(0.05)
        else:
            pytest.fail("the child never showed a zombie leader with a live thread")
        yield child.pid
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=20)


def test_a_real_zombie_leader_reads_as_alive_and_holding_memory(zombie_leader):
    from genesis.sentinel import codex_mcp_stop, process_stop

    st = proc_io.probe_stat(zombie_leader)
    assert isinstance(st, proc_io.Stat)
    assert st.state == "Z" and not st.exited and not st.kernel_thread
    # Its own status has no Vm lines; the figure comes from the live task.
    assert proc_io.held_kb(zombie_leader) >= 20 * 1024
    assert process_stop._alive(zombie_leader, st.starttime) is True
    assert codex_mcp_stop._group_alive(zombie_leader) is True


def test_a_real_group_reads_gone_once_killed_and_reaped(zombie_leader):
    """Control for the test above: the same group, killed and reaped, is gone,
    so 'alive' there is the reader's answer, not an always-True."""
    import signal
    import time

    from genesis.sentinel import codex_mcp_stop

    os.killpg(zombie_leader, signal.SIGKILL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with contextlib.suppress(ChildProcessError):
            os.waitpid(zombie_leader, os.WNOHANG)
        if proc_io.probe_stat(zombie_leader) == proc_io.GONE:
            break
        time.sleep(0.05)
    assert proc_io.probe_stat(zombie_leader) == proc_io.GONE
    assert codex_mcp_stop._group_alive(zombie_leader) is False


def test_a_reaped_process_reads_as_gone(tmp_path):
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=20)
    assert proc_io.probe_stat(child.pid) == proc_io.GONE
    assert proc_io.held_kb(child.pid) == proc_io.GONE


# --- tri-state reads, fake tree ------------------------------------------------


def test_probe_stat_tells_gone_from_unreadable(fake_proc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert proc_io.probe_stat(4242) == proc_io.GONE
    (fake_proc / "4243").mkdir()
    (fake_proc / "4243" / "stat").mkdir()  # reading it raises IsADirectoryError
    assert proc_io.probe_stat(4243) == proc_io.UNREADABLE
    (fake_proc / "4244").mkdir()
    (fake_proc / "4244" / "stat").write_text("4244 (x) S 1\n")  # truncated
    assert proc_io.probe_stat(4244) == proc_io.UNREADABLE


def test_held_kb_reads_an_unreadable_status_as_unreadable(fake_proc: Path) -> None:
    (fake_proc / "4245").mkdir()
    (fake_proc / "4245" / "status").mkdir()
    assert proc_io.held_kb(4245) == proc_io.UNREADABLE


def test_held_kb_with_no_live_task_is_zero(fake_proc: Path) -> None:
    d = fake_proc / "4246"
    (d / "task" / "4246").mkdir(parents=True)
    (d / "status").write_text("Name:\tx\n")
    (d / "task" / "4246" / "status").write_text("Name:\tx\n")
    assert proc_io.held_kb(4246) == 0


def test_probe_stat_reads_the_kernel_thread_flag_and_exit_state(fake_proc: Path) -> None:
    def write(pid, state, flags, threads):
        fields = [state, "2", str(pid)] + ["0"] * 16 + [str(pid * 10)]
        fields[6] = str(flags)
        fields[17] = str(threads)
        (fake_proc / str(pid)).mkdir()
        (fake_proc / str(pid) / "stat").write_text(f"{pid} (k w) " + " ".join(fields) + "\n")

    write(4250, "S", 0x00200000, 1)
    write(4251, "S", 0, 1)
    write(4252, "Z", 0, 2)
    write(4253, "Z", 0, 1)
    k, u, leader, dead = (proc_io.probe_stat(p) for p in (4250, 4251, 4252, 4253))
    assert k.kernel_thread and not u.kernel_thread
    assert k.comm == "k w" and k.pgid == 4250 and k.starttime == 42500
    assert not leader.exited and dead.exited

