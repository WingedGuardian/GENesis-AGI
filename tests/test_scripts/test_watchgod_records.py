"""Pause records and the operator CLI: scripts/lib/watchgod_records.sh,
scripts/lib/watchgod_signal.sh and `scripts/watchgod status|thaw|kill`.

What these pin:
  * a record is keyed by pid AND start time, written whole (temp + rename),
    owner-only, and refuses a path or key with a tab or newline;
  * the records lock times out instead of hanging when asked to, and leaves
    nothing open when it does;
  * thaw resumes only the recorded process; a recycled pid, or a process that
    is running again, is dropped without a signal;
  * kill ends only the recorded process: SIGCONT, SIGTERM, then SIGKILL after
    the grace period; it refuses "all"; a recycled pid is never touched.

The CLI tests run real processes this test owns (no fake /proc): the signal
library's fallback really calls kill(2), so a fake process table would point it
at whatever real process holds that pid number.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CLI = _ROOT / "scripts" / "watchgod"
_RECORDS = _ROOT / "scripts" / "lib" / "watchgod_records.sh"
_SIGNAL = _ROOT / "scripts" / "lib" / "watchgod_signal.sh"

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs a Linux /proc")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".genesis" / "watchgod").mkdir(parents=True)
    (h / ".genesis" / "logs").mkdir(parents=True)
    return h


def _state_dir(home: Path) -> Path:
    return home / ".genesis" / "watchgod"


def _starttime(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_text()
    return raw.rsplit(") ", 1)[1].split()[19]


def _live(pid: int) -> str:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("State:"):
                return line.split()[1]
    except OSError:
        return "gone"
    return "?"


_BOOT = Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _record(
    home: Path, pid: int, st: str, state: str = "paused", comm: str = "sleep", boot: str = _BOOT
) -> None:
    f = _state_dir(home) / "frozen"
    with open(f, "a") as fh:
        fh.write(
            f"{pid}\t{st}\t{comm}\t2026-01-01T00:00:00Z\t1:2\t/x/tasks/b.output\t{state}\t{boot}\n"
        )


def _records(home: Path) -> list[list[str]]:
    f = _state_dir(home) / "frozen"
    return [ln.split("\t") for ln in f.read_text().splitlines()] if f.exists() else []


def _cli(home: Path, *args: str, grace: int | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home))
    env.pop("DG_STATE_DIR", None)
    env.pop("DG_PROC", None)
    if grace is not None:
        env["WATCHGOD_KILL_GRACE_S"] = str(grace)
    return subprocess.run(
        [str(_CLI), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def _lib(home: Path, snippet: str) -> subprocess.CompletedProcess:
    script = (
        f"set -euo pipefail\nDG_STATE_DIR='{_state_dir(home)}'\n"
        f"source '{_SIGNAL}'\nsource '{_RECORDS}'\n{snippet}\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


@pytest.fixture
def stopped_sleep():
    p = subprocess.Popen(["sleep", "300"])
    try:
        os.kill(p.pid, signal.SIGSTOP)
        for _ in range(50):
            if _live(p.pid) == "T":
                break
            time.sleep(0.05)
        yield p
    finally:
        # Only while it is still ours: once a test has reaped it, the pid may
        # name an unrelated process (review finding).
        if p.poll() is None:
            try:
                os.kill(p.pid, signal.SIGCONT)
                p.kill()
            except ProcessLookupError:
                pass
        p.wait(timeout=10)


# ── the records library ───────────────────────────────────────────


def test_put_state_promote_drop_round_trip(home):
    out = _lib(
        home,
        """
        lk=""; wg_records_lock lk
        wg_record_put 4242 777 sleep T0 1:2 /x/out pending
        echo S1=$(wg_record_state 4242 777)
        wg_record_promote 4242 777
        echo S2=$(wg_record_state 4242 777)
        wg_record_state 4242 778 || echo OTHER_START=none
        wg_record_drop 4242 777
        wg_record_state 4242 777 || echo S3=none
        wg_records_unlock "$lk"
        """,
    )
    assert out.returncode == 0, out.stderr
    assert "S1=pending" in out.stdout and "S2=paused" in out.stdout
    assert "OTHER_START=none" in out.stdout, "a record applies to one start time only"
    assert "S3=none" in out.stdout


def test_a_pending_record_keeps_its_first_time(home):
    out = _lib(
        home,
        """
        wg_record_put 4242 777 sleep T0 1:2 /x/out pending
        wg_record_put 4242 777 sleep T1 1:2 /x/out pending
        wg_record_put 4242 777 sleep T2 1:2 /x/out paused
        wg_records_list
        """,
    )
    assert out.returncode == 0, out.stderr
    rows = [ln.split("\t") for ln in out.stdout.splitlines()]
    assert len(rows) == 1, "one line per process"
    assert rows[0][3] == "T2" and rows[0][6] == "paused"


def test_a_pending_resend_does_not_reset_the_reminder_clock(home):
    out = _lib(
        home,
        """
        wg_record_put 4242 777 sleep T0 1:2 /x/out pending
        wg_record_put 4242 777 sleep T1 1:2 /x/out pending
        wg_records_list
        """,
    )
    assert out.stdout.split("\t")[3] == "T0"


@pytest.mark.parametrize(
    "args",
    [
        "x 777 sleep T0 1:2 /x/out paused",  # pid not numeric
        "4242 y sleep T0 1:2 /x/out paused",  # start time not numeric
        "4242 777 sleep T0 1:2 /x/out frozen",  # unknown state
        "4242 777 sleep T0 1:2 $'/x/a\\tb' paused",  # tab in the path
        "4242 777 sleep T0 1:2 $'/x/a\\nb' paused",  # newline in the path
        "4242 777 sleep T0 $'1\\t2' /x/out paused",  # tab in the key
    ],
)
def test_a_malformed_record_is_refused(home, args):
    out = _lib(home, f"wg_record_put {args} && echo WROTE || echo REFUSED; wg_records_list | wc -l")
    assert "REFUSED" in out.stdout
    assert out.stdout.strip().endswith("0")


def test_comm_cannot_forge_a_field(home):
    out = _lib(
        home, "wg_record_put 4242 777 $'evil\\tname\\nx' T0 1:2 /x/out paused; wg_records_list"
    )
    rows = [ln.split("\t") for ln in out.stdout.splitlines()]
    assert len(rows) == 1 and len(rows[0]) == 7
    assert rows[0][2] == "evil name x"


def test_records_are_owner_only_and_no_temp_is_left(home):
    _lib(home, "wg_record_put 4242 777 sleep T0 1:2 /x/out paused; wg_record_drop 1 1")
    f = _state_dir(home) / "frozen"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not (_state_dir(home) / "frozen.tmp").exists()


def test_a_lock_that_is_held_times_out_and_leaves_nothing_open(home):
    lock = _state_dir(home) / "frozen.lock"
    lock.touch()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f, fcntl.LOCK_EX); "
            "print('held', flush=True); time.sleep(30)",
            str(lock),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        t0 = time.time()
        out = _lib(
            home,
            'lk=""; if wg_records_lock lk 1; then echo GOT; else echo BUSY; fi; '
            "ls -l /proc/$$/fd | grep -c frozen.lock || true",
        )
        assert "BUSY" in out.stdout
        assert out.stdout.strip().endswith("0"), "the descriptor was closed again"
        assert time.time() - t0 < 10
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_thaw_waits_for_the_lock(home, stopped_sleep):
    """`thaw` never reads records while the guardian holds the lock."""
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    lock = _state_dir(home) / "frozen.lock"
    lock.touch()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f, fcntl.LOCK_EX); "
            "print('held', flush=True); time.sleep(1.5)",
            str(lock),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    t0 = time.time()
    out = _cli(home, "thaw", "all")
    waited = time.time() - t0  # measured BEFORE waiting on the holder
    holder.wait(timeout=10)
    assert waited >= 1.0, "thaw did not wait for the lock"
    assert f"resumed pid {stopped_sleep.pid}" in out.stdout


# ── thaw ──────────────────────────────────────────────────────────


def test_thaw_resumes_the_recorded_process_and_drops_its_record(home, stopped_sleep):
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    out = _cli(home, "thaw", str(stopped_sleep.pid))
    assert out.returncode == 0, out.stderr
    assert f"resumed pid {stopped_sleep.pid}" in out.stdout
    assert _live(stopped_sleep.pid) != "T"
    assert _records(home) == []
    log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert f"THAWED pid {stopped_sleep.pid}" in log


def test_thaw_never_signals_a_recycled_pid(home, stopped_sleep):
    """The record names a different start time: another process now has the
    pid. It is dropped and never signalled (the process stays stopped)."""
    _record(home, stopped_sleep.pid, "1")
    out = _cli(home, "thaw", "all")
    assert "no longer running — record dropped" in out.stdout
    assert _live(stopped_sleep.pid) == "T"
    assert _records(home) == []


def test_thaw_drops_a_record_whose_process_is_running_again(home):
    p = subprocess.Popen(["sleep", "300"])
    try:
        _record(home, p.pid, _starttime(p.pid))
        out = _cli(home, "thaw", "all")
        assert "record dropped, not signalled" in out.stdout
        assert _records(home) == []
    finally:
        p.kill()
        p.wait(timeout=10)


def test_thaw_resumes_a_pending_record_whatever_its_state(home):
    p = subprocess.Popen(["sleep", "300"])
    try:
        _record(home, p.pid, _starttime(p.pid), state="pending")
        out = _cli(home, "thaw", "all")
        assert f"resumed pid {p.pid}" in out.stdout
    finally:
        p.kill()
        p.wait(timeout=10)


def test_thaw_of_an_unknown_pid_fails(home, stopped_sleep):
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    out = _cli(home, "thaw", "999999")
    assert out.returncode == 1
    assert "is not in the paused list" in out.stdout
    assert len(_records(home)) == 1


# ── kill ──────────────────────────────────────────────────────────


def test_kill_ends_the_recorded_process(home, stopped_sleep):
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    out = _cli(home, "kill", str(stopped_sleep.pid), grace=5)
    assert out.returncode == 0, out.stderr
    assert f"ended pid {stopped_sleep.pid}" in out.stdout
    stopped_sleep.wait(timeout=10)
    assert stopped_sleep.returncode == -signal.SIGTERM, "ended by SIGTERM within the grace"
    assert _records(home) == []
    log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert f"KILLED pid {stopped_sleep.pid}" in log


def test_kill_escalates_to_sigkill_after_the_grace(home):
    p = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(300)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert p.stdout.readline().strip() == "ready"
        os.kill(p.pid, signal.SIGSTOP)
        _record(home, p.pid, _starttime(p.pid), comm="python3")
        out = _cli(home, "kill", str(p.pid), grace=1)
        assert out.returncode == 0, out.stderr
        p.wait(timeout=10)
        assert p.returncode == -signal.SIGKILL
        assert _records(home) == []
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=10)


def test_kill_never_touches_a_recycled_pid(home, stopped_sleep):
    _record(home, stopped_sleep.pid, "1")
    out = _cli(home, "kill", str(stopped_sleep.pid), grace=1)
    assert "an earlier process) dropped" in out.stdout
    assert "is no longer running" in out.stdout
    assert _live(stopped_sleep.pid) == "T", "the process holding the pid now was not touched"
    assert _records(home) == []


@pytest.mark.parametrize("arg", ["all", "", "12x"])
def test_kill_takes_exactly_one_pid(home, arg):
    out = _cli(home, "kill", arg)
    assert out.returncode == 2
    assert "kill takes one pid" in out.stderr


def test_kill_of_a_pid_not_in_the_list_does_nothing(home, stopped_sleep):
    out = _cli(home, "kill", str(stopped_sleep.pid))
    assert out.returncode == 1
    assert "is not in the paused list" in out.stdout
    assert _live(stopped_sleep.pid) == "T"


# ── status ────────────────────────────────────────────────────────


def test_status_labels_each_record_by_what_is_true_now(home, stopped_sleep):
    running = subprocess.Popen(["sleep", "300"])
    try:
        _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
        _record(home, running.pid, _starttime(running.pid))
        _record(home, running.pid, "1")
        out = _cli(home, "status")
        lines = [ln for ln in out.stdout.splitlines() if ln.startswith("  pid ")]
        assert len(lines) == 3
        assert "still paused" in lines[0]
        assert "RUNNING, not paused" in lines[1]
        assert lines[2].endswith("gone")
    finally:
        running.kill()
        running.wait(timeout=10)


def test_status_with_no_records_says_none(home):
    out = _cli(home, "status")
    assert "paused: none" in out.stdout


def test_help_lists_every_command(home):
    out = _cli(home, "help")
    for cmd in ("status", "thaw <pid>", "thaw all", "kill <pid>"):
        assert cmd in out.stdout


# ── the generation-bound signal ───────────────────────────────────


@pytest.mark.parametrize("path", ["pidfd", "fallback"])
def test_signal_gen_never_signals_another_generation(home, tmp_path, stopped_sleep, path):
    """wg_signal_gen sends only to the process that started at the given time:
    with a pidfd (the real /proc), and through the start-time-checked kill
    when no pidfd is used (DG_PROC spelled differently skips the pidfd path)."""
    proc = "/proc"
    if path == "fallback":
        alias = tmp_path / "proc-alias"
        alias.symlink_to("/proc")
        proc = str(alias)
    pid = stopped_sleep.pid
    st = _starttime(pid)
    out = _lib(home, f"DG_PROC='{proc}'; rc=0; wg_signal_gen CONT {pid} 1 || rc=$?; echo RC=$rc")
    assert "RC=1" in out.stdout, out.stdout + out.stderr
    assert _live(pid) == "T", "a different start time must not be signalled"
    out = _lib(home, f"DG_PROC='{proc}'; rc=0; wg_signal_gen CONT {pid} {st} || rc=$?; echo RC=$rc")
    assert "RC=0" in out.stdout, out.stdout + out.stderr
    for _ in range(50):
        if _live(pid) != "T":
            break
        time.sleep(0.05)
    assert _live(pid) != "T", "the recorded generation is signalled"


def test_signal_gen_reports_a_gone_process(home):
    p = subprocess.Popen(["sleep", "300"])
    st = _starttime(p.pid)
    p.kill()
    p.wait(timeout=10)
    out = _lib(home, f"rc=0; wg_signal_gen CONT {p.pid} {st} || rc=$?; echo RC=$rc")
    assert "RC=1" in out.stdout


@pytest.mark.parametrize("args", ["CONT 1 5", "CONT 0 5", "CONT x 5", "CONT 99 y"])
def test_signal_gen_refuses_malformed_or_init(home, args):
    """pid 1 (and 0) is never signalled; a non-numeric pid or start time is refused."""
    out = _lib(home, f"rc=0; wg_signal_gen {args} || rc=$?; echo RC=$rc")
    assert "RC=2" in out.stdout


# ── review findings (PR B) ────────────────────────────────────────


def test_kill_skips_a_stale_record_for_the_same_pid(home, stopped_sleep):
    """An older record whose process is gone, listed first, must not shadow the
    live one: kill drops the stale record and ends the recorded process."""
    _record(home, stopped_sleep.pid, "1")
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    out = _cli(home, "kill", str(stopped_sleep.pid), grace=5)
    assert out.returncode == 0, out.stderr
    assert f"ended pid {stopped_sleep.pid}" in out.stdout
    stopped_sleep.wait(timeout=10)
    assert _records(home) == []


def test_an_empty_comm_cannot_shift_the_columns(home):
    """bash's IFS read collapses an empty tab field where awk keeps it: put
    never writes an empty field, so a pending record stays pending to thaw."""
    p = subprocess.Popen(["sleep", "300"])
    try:
        st = _starttime(p.pid)
        out = _lib(home, f"wg_record_put {p.pid} {st} '' T0 1:2 /x/out pending; wg_records_list")
        assert out.stdout.split("\t")[2] == "?"
        out = _cli(home, "thaw", "all")
        assert f"resumed pid {p.pid}" in out.stdout, out.stdout
    finally:
        p.kill()
        p.wait(timeout=10)


@pytest.mark.parametrize("field", ["ts", "key", "path"])
def test_an_empty_field_is_refused(home, field):
    args = {"ts": "T0", "key": "1:2", "path": "/x/out"}
    args[field] = ""
    out = _lib(
        home,
        f"wg_record_put 4242 777 sleep '{args['ts']}' '{args['key']}' '{args['path']}' paused"
        " && echo WROTE || echo REFUSED",
    )
    assert "REFUSED" in out.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 file")
def test_an_unreadable_record_file_is_never_rewritten(home):
    f = _state_dir(home) / "frozen"
    f.write_text(f"1\t1\ta\tT\tk\t/p\tpaused\t{_BOOT}\n2\t2\tb\tT\tk\t/p\tpaused\t{_BOOT}\n")
    f.chmod(0)
    try:
        out = _lib(
            home,
            "rc=0; wg_record_put 555 5 c T0 1:2 /x/out paused || rc=$?; echo PUT=$rc;"
            " rc=0; wg_record_drop 1 1 || rc=$?; echo DROP=$rc",
        )
        assert "PUT=1" in out.stdout and "DROP=1" in out.stdout
        status = _cli(home, "status")
        assert status.returncode == 1 and "cannot be read" in status.stderr
    finally:
        f.chmod(0o600)
    assert f.read_text().count("\n") == 2, "both records survived"


def test_thaw_waits_for_a_first_record_still_being_written(home, stopped_sleep):
    """The guardian holds the lock while it writes the FIRST record: the
    records file does not exist yet, and thaw must still wait, then resume."""
    lock = _state_dir(home) / "frozen.lock"
    rec = _state_dir(home) / "frozen"
    line = f"{stopped_sleep.pid}\t{_starttime(stopped_sleep.pid)}\tsleep\tT\tk\t/x/o\tpaused\t{_BOOT}\n"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f, fcntl.LOCK_EX); "
            "print('held', flush=True); time.sleep(1); open(sys.argv[2],'w').write(sys.argv[3])",
            str(lock),
            str(rec),
            line,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    out = _cli(home, "thaw", "all")
    holder.wait(timeout=10)
    assert f"resumed pid {stopped_sleep.pid}" in out.stdout, out.stdout
    assert _live(stopped_sleep.pid) != "T"


def test_kill_ends_a_process_whose_main_thread_exited(home):
    """Its leader is a zombie while another thread still runs: that process is
    alive, so kill must not call it gone and leave it stopped."""
    code = (
        "import ctypes, threading, time\n"
        "threading.Thread(target=time.sleep, args=(300,)).start()\n"
        "print('ready', flush=True)\n"
        "ctypes.CDLL(None).pthread_exit(None)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"
        for _ in range(50):
            if _live(p.pid) == "Z":
                break
            time.sleep(0.05)
        assert _live(p.pid) == "Z", "guard: the leader really exited"
        os.kill(p.pid, signal.SIGSTOP)
        _record(home, p.pid, _starttime(p.pid), comm="python3")
        out = _cli(home, "kill", str(p.pid), grace=2)
        assert out.returncode == 0, out.stdout + out.stderr
        assert f"ended pid {p.pid}" in out.stdout
        p.wait(timeout=10)
    finally:
        if p.poll() is None:
            os.kill(p.pid, signal.SIGKILL)
            p.wait(timeout=10)


def test_a_grace_with_a_leading_zero_is_decimal(home, stopped_sleep):
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    env = dict(os.environ, HOME=str(home), WATCHGOD_KILL_GRACE_S="08")
    out = subprocess.run(
        [str(_CLI), "kill", str(stopped_sleep.pid)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "value too great" not in out.stderr
    stopped_sleep.wait(timeout=10)
    assert stopped_sleep.returncode == -signal.SIGTERM, "the grace ran, so TERM ended it"


def test_kill_and_thaw_read_a_pid_as_decimal(home, stopped_sleep):
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    out = _cli(home, "thaw", "0" + str(stopped_sleep.pid))
    assert f"resumed pid {stopped_sleep.pid}" in out.stdout


# ── Codex round 1 (PR B) ──────────────────────────────────────────


def test_a_record_from_another_boot_is_not_a_record(home, stopped_sleep):
    """(pid, start time) can recur after a reboot: a record written in another
    boot is never acted on, and the next write drops it."""
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid), boot="0000-old-boot")
    out = _cli(home, "kill", str(stopped_sleep.pid), grace=1)
    assert out.returncode != 0 and "ended" not in out.stdout, out.stdout
    assert _live(stopped_sleep.pid) == "T", "the process was not touched"
    out = _cli(home, "thaw", "all")
    assert "resumed" not in out.stdout
    assert _live(stopped_sleep.pid) == "T"
    _lib(home, "wg_record_put 4242 777 sleep T0 1:2 /x/out paused")
    rows = _records(home)
    assert len(rows) == 1 and rows[0][0] == "4242" and rows[0][7] == _BOOT


def test_a_written_record_carries_this_boot(home):
    _lib(home, "wg_record_put 4242 777 sleep T0 1:2 /x/out paused")
    assert _records(home) == [["4242", "777", "sleep", "T0", "1:2", "/x/out", "paused", _BOOT]]


def test_a_process_name_with_a_newline_is_still_that_process(home):
    """comm may hold a newline: the stat record is read whole, so thaw finds
    the process and resumes it instead of dropping its record."""
    code = (
        "import ctypes, os, signal, time\n"
        "ctypes.CDLL(None).prctl(15, b'a\\nb) x', 0, 0, 0)\n"
        "print('ready', flush=True)\n"
        "time.sleep(300)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"
        assert "\n" in Path(f"/proc/{p.pid}/comm").read_text()[:-1], "guard: comm holds a newline"
        os.kill(p.pid, signal.SIGSTOP)
        for _ in range(50):
            if _live(p.pid) == "T":
                break
            time.sleep(0.05)
        _record(home, p.pid, _starttime(p.pid), comm="x")
        out = _lib(
            home,
            f"wg_starttime {p.pid}; echo; rc=0; wg_same_gen {p.pid} {_starttime(p.pid)} || rc=$?; echo RC=$rc",
        )
        assert out.stdout.splitlines()[0] == _starttime(p.pid)
        assert "RC=0" in out.stdout
        out = _cli(home, "status")
        assert "still paused" in out.stdout, out.stdout
        out = _cli(home, "thaw", str(p.pid))
        assert f"resumed pid {p.pid}" in out.stdout, out.stdout
        assert _live(p.pid) != "T"
    finally:
        p.kill()
        p.wait(timeout=10)


@pytest.mark.parametrize(
    "args", [("kill", "{pid}", "1"), ("thaw", "{pid}", "x"), ("status", "x"), ("kill",), ("thaw",)]
)
def test_a_command_takes_exactly_its_arguments(home, stopped_sleep, args):
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    out = _cli(home, *(a.format(pid=stopped_sleep.pid) for a in args), grace=1)
    assert out.returncode == 2, out.stdout + out.stderr
    assert _live(stopped_sleep.pid) == "T", "nothing was signalled"
    assert len(_records(home)) == 1


@pytest.mark.parametrize("cmd", ["kill", "thaw"])
def test_a_pid_operand_that_would_wrap_is_refused(home, stopped_sleep, cmd):
    """bash arithmetic wraps at 2^64: 2^64 + pid must not name pid (review
    finding: kill ended a process the operator never named)."""
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    out = _cli(home, cmd, str(2**64 + stopped_sleep.pid), grace=1)
    assert out.returncode == 2, out.stdout + out.stderr
    assert _live(stopped_sleep.pid) == "T"
    assert len(_records(home)) == 1


def test_a_pid_with_a_leading_zero_is_not_recorded(home):
    out = _lib(
        home, "wg_record_put 04242 777 sleep T0 1:2 /x/out paused && echo WROTE || echo REFUSED"
    )
    assert "REFUSED" in out.stdout


def test_the_pidfd_path_binds_a_process_whose_name_is_not_utf8(home):
    """A comm that is not valid UTF-8 must not knock the signal off the pidfd
    path: the signal still reaches exactly that process."""
    code = (
        "import ctypes, time\n"
        "ctypes.CDLL(None).prctl(15, b'\\xff\\xfe) q', 0, 0, 0)\n"
        "print('ready', flush=True)\n"
        "time.sleep(300)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"
        st = Path(f"/proc/{p.pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()[19].decode()
        out = subprocess.run(
            ["python3", "-I", "-S", "-c", _pidfd_script(), str(p.pid), st, "TERM"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.returncode == 0, (out.returncode, out.stderr)
        p.wait(timeout=10)
        assert p.returncode == -signal.SIGTERM
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=10)


def _pidfd_script() -> str:
    """The signal library's pidfd helper, exactly as it ships."""
    return subprocess.run(
        ["bash", "-c", f"source '{_SIGNAL}'; printf %s \"$_WG_PIDFD_PY\""],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


# ── Codex/CodeRabbit round 2 (PR B) ───────────────────────────────


def _fake_proc_with_unreadable_stat(home: Path, pid: int) -> Path:
    """A process table where pid's entry exists but its stat cannot be read."""
    proc = home / "fakeproc"
    (proc / str(pid)).mkdir(parents=True)
    s = proc / str(pid) / "stat"
    s.write_text("x")
    s.chmod(0)
    return proc


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 file")
def test_an_unreadable_stat_is_cannot_tell_not_gone(home):
    proc = _fake_proc_with_unreadable_stat(home, 4242)
    out = _lib(
        home,
        f"DG_PROC='{proc}'; rc=0; wg_same_gen 4242 777 || rc=$?; echo SAME=$rc;"
        " rc=0; wg_same_gen 4243 777 || rc=$?; echo ABSENT=$rc;"
        " rc=0; wg_signal_gen CONT 4242 777 || rc=$?; echo SIG=$rc",
    )
    assert "SAME=2" in out.stdout and "ABSENT=1" in out.stdout and "SIG=3" in out.stdout, out.stdout


def test_same_gen_says_yes_and_no_on_a_real_process(home, stopped_sleep):
    st = _starttime(stopped_sleep.pid)
    out = _lib(
        home,
        f"rc=0; wg_same_gen {stopped_sleep.pid} {st} || rc=$?; echo SAME=$rc;"
        f" rc=0; wg_same_gen {stopped_sleep.pid} 1 || rc=$?; echo OTHER=$rc",
    )
    assert "SAME=0" in out.stdout and "OTHER=1" in out.stdout, out.stdout


@pytest.mark.parametrize(("exc", "rc"), [("PermissionError", 13), ("FileNotFoundError", 10)])
def test_the_pidfd_helper_cannot_tell_when_stat_is_unreadable(stopped_sleep, exc, rc):
    """The shipped helper on a real process whose stat read fails: an error
    other than "no such file" is 13 (cannot tell, nothing sent), never 10."""
    prefix = (
        "import builtins\n"
        "_o = builtins.open\n"
        "def _open(f, *a, **k):\n"
        f"    if str(f).endswith('/stat'): raise {exc}('injected')\n"
        "    return _o(f, *a, **k)\n"
        "builtins.open = _open\n"
    )
    st = _starttime(stopped_sleep.pid)
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            prefix + _pidfd_script(),
            str(stopped_sleep.pid),
            st,
            "CONT",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == rc, (out.returncode, out.stderr)
    assert _live(stopped_sleep.pid) == "T", "nothing was sent"


def test_control_bytes_in_a_record_are_not_sent_to_the_terminal(home, stopped_sleep):
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid), comm="ev\x1b[2Jil\r")
    out = _cli(home, "status")
    assert "\x1b" not in out.stdout and "\r" not in out.stdout, repr(out.stdout)
    assert "ev?[2Jil?" in out.stdout
    out = _cli(home, "thaw", str(stopped_sleep.pid))
    assert "\x1b" not in out.stdout and "resumed" in out.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only dir")
def test_kill_reports_a_record_it_could_not_remove(home, stopped_sleep):
    """The process ends, but the records file cannot be replaced: kill says so
    and exits nonzero instead of reporting a clean end."""
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    d = _state_dir(home)
    (d / "frozen.lock").touch()
    d.chmod(0o500)
    try:
        out = _cli(home, "kill", str(stopped_sleep.pid), grace=5)
    finally:
        d.chmod(0o700)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "ended pid" in out.stdout and "could not be removed" in out.stderr
    assert len(_records(home)) == 1


def test_the_help_text_names_the_kill_signal_order(home):
    out = _cli(home, "help")
    assert "SIGTERM, then" in out.stdout and "SIGCONT, then SIGKILL" in out.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 file")
def test_starttime_says_gone_or_cannot_tell(home):
    proc = _fake_proc_with_unreadable_stat(home, 4242)
    out = _lib(
        home,
        f"DG_PROC='{proc}'; rc=0; wg_starttime 4242 || rc=$?; echo UNREADABLE=$rc;"
        " rc=0; wg_starttime 4243 || rc=$?; echo ABSENT=$rc",
    )
    assert "UNREADABLE=2" in out.stdout and "ABSENT=1" in out.stdout, out.stdout


def _cli_cannot_tell(
    home: Path, tmp_path: Path, *args: str, same: int = 2
) -> subprocess.CompletedProcess:
    """The CLI with an identity check that can never tell (a copy whose signal
    library answers 2/3), against the real records and the real process."""
    root = tmp_path / "cli"
    (root / "lib").mkdir(parents=True)
    cli = root / "watchgod"
    cli.write_text(_CLI.read_text())
    cli.chmod(0o755)
    (root / "lib" / "watchgod_records.sh").write_text(_RECORDS.read_text())
    (root / "lib" / "watchgod_signal.sh").write_text(
        _SIGNAL.read_text()
        + f"\nwg_same_gen() {{ return {same}; }}\nwg_signal_gen() {{ return 3; }}\n"
    )
    env = dict(os.environ, HOME=str(home), WATCHGOD_KILL_GRACE_S="1")
    env.pop("DG_STATE_DIR", None)
    return subprocess.run(
        [str(cli), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.parametrize("cmd", ["thaw", "kill"])
def test_the_cli_keeps_a_record_it_cannot_verify(home, tmp_path, stopped_sleep, cmd):
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    out = _cli_cannot_tell(home, tmp_path, cmd, str(stopped_sleep.pid))
    assert out.returncode == 1, out.stdout + out.stderr
    assert "cannot verify" in out.stderr and "record kept" in out.stderr
    assert len(_records(home)) == 1
    assert _live(stopped_sleep.pid) == "T", "nothing was signalled"


def test_status_says_a_record_it_cannot_verify_is_unverified(home, tmp_path, stopped_sleep):
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    out = _cli_cannot_tell(home, tmp_path, "status")
    assert "unverified (record kept)" in out.stdout, out.stdout


def test_c1_controls_are_not_sent_to_the_terminal(home, stopped_sleep):
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid), comm="a\u009b2Jb\u00e9")
    out = subprocess.run(
        [str(_CLI), "status"],
        env=dict(os.environ, HOME=str(home)),
        capture_output=True,
        timeout=60,
    )
    assert b"\xc2\x9b" not in out.stdout
    assert "a?2Jb\u00e9".encode() in out.stdout, out.stdout


@pytest.mark.parametrize("cmd", ["thaw", "kill"])
def test_the_cli_keeps_a_record_when_the_signal_cannot_verify(home, tmp_path, stopped_sleep, cmd):
    """The identity check passed, then the signal's own check could not tell
    (wg_signal_gen 3): nothing is sent, the record is kept, and it says why."""
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    out = _cli_cannot_tell(home, tmp_path, cmd, str(stopped_sleep.pid), same=0)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "cannot verify" in out.stderr and "record kept" in out.stderr
    assert len(_records(home)) == 1
    assert _live(stopped_sleep.pid) == "T"


# ── Codex round 3 (PR B) ──────────────────────────────────────────


def _cli_on_fake_proc(home: Path, tmp_path: Path, proc: Path, *args: str):
    """The CLI pointed at a fake process table. Only for paths that never
    signal (status, and thaw where it keeps the record): the signal fallback
    would aim at whatever real process holds the fake pid number."""
    root = tmp_path / "cli"
    (root / "lib").mkdir(parents=True)
    cli = root / "watchgod"
    text = _CLI.read_text()
    assert text.count("\nDG_PROC=/proc\n") == 1
    cli.write_text(text.replace("\nDG_PROC=/proc\n", f"\nDG_PROC='{proc}'\n"))
    cli.chmod(0o755)
    (root / "lib" / "watchgod_records.sh").write_text(_RECORDS.read_text())
    (root / "lib" / "watchgod_signal.sh").write_text(_SIGNAL.read_text())
    env = dict(os.environ, HOME=str(home))
    env.pop("DG_STATE_DIR", None)
    return subprocess.run(
        [str(cli), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def _fake_stat(pid: int, state: str, st: int) -> str:
    rest = ["0"] * 49
    rest[0] = state
    rest[19] = str(st)
    return f"{pid} (worker) " + " ".join(rest) + "\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 file")
def test_a_worker_whose_state_cannot_be_read_keeps_the_record(home, tmp_path):
    """The main thread is a zombie and the one other thread's stat cannot be
    read: that process may be alive and stopped, so status says unverified and
    thaw keeps the record (review finding)."""
    pid = 4242000
    proc = tmp_path / "fakeproc"
    (proc / str(pid) / "task" / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "task" / "4242001").mkdir(parents=True)
    (proc / str(pid) / "stat").write_text(_fake_stat(pid, "Z", 777))
    (proc / str(pid) / "task" / str(pid) / "stat").write_text(_fake_stat(pid, "Z", 777))
    worker = proc / str(pid) / "task" / "4242001" / "stat"
    worker.write_text(_fake_stat(4242001, "T", 777))
    worker.chmod(0)
    _record(home, pid, "777")
    try:
        out = _cli_on_fake_proc(home, tmp_path, proc, "status")
        assert "cannot read its state" in out.stdout, out.stdout
        out = _cli_on_fake_proc(home, tmp_path / "t2", proc, "thaw", str(pid))
        assert out.returncode == 1 and "record kept" in out.stderr, out.stdout + out.stderr
        assert len(_records(home)) == 1
    finally:
        worker.chmod(0o600)


def test_raw_c1_bytes_are_not_sent_to_the_terminal(home, stopped_sleep):
    """A raw 0x9b (CSI to some terminals) is shown as "?", like its UTF-8
    form; an accented letter is kept (review finding)."""
    f = _state_dir(home) / "frozen"
    line = (
        f"{stopped_sleep.pid}\t{_starttime(stopped_sleep.pid)}\t".encode()
        + b"a\x9b2Jb\xc3\xa9\t2026-01-01T00:00:00Z\t1:2\t/x/o\tpaused\t"
        + _BOOT.encode()
        + b"\n"
    )
    f.write_bytes(line)
    out = subprocess.run(
        [str(_CLI), "status"],
        env=dict(os.environ, HOME=str(home)),
        capture_output=True,
        timeout=60,
    )
    assert b"\x9b" not in out.stdout, out.stdout
    assert "a?2Jb\u00e9".encode() in out.stdout, out.stdout


# ── premise-check follow-ups (PR B) ───────────────────────────────


def test_a_start_time_with_a_leading_zero_is_not_recorded(home):
    out = _lib(
        home, "wg_record_put 4242 0777 sleep T0 1:2 /x/out paused && echo WROTE || echo REFUSED"
    )
    assert "REFUSED" in out.stdout


def test_status_still_lists_records_when_the_state_file_is_bad(home, stopped_sleep):
    (home / ".genesis" / "watchgod_state.json").write_text("{not json")
    _record(home, stopped_sleep.pid, _starttime(stopped_sleep.pid))
    out = _cli(home, "status")
    assert "could not be read" in out.stdout
    assert f"pid {stopped_sleep.pid}" in out.stdout and "still paused" in out.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists a 0o000 dir")
def test_a_zombie_leader_whose_threads_cannot_be_listed_keeps_the_record(home, tmp_path):
    pid = 4242100
    proc = tmp_path / "fakeproc"
    task = proc / str(pid) / "task"
    (task / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "stat").write_text(_fake_stat(pid, "Z", 777))
    task.chmod(0)
    _record(home, pid, "777")
    try:
        out = _cli_on_fake_proc(home, tmp_path, proc, "thaw", str(pid))
        assert out.returncode == 1 and "record kept" in out.stderr, out.stdout + out.stderr
        assert len(_records(home)) == 1
    finally:
        task.chmod(0o700)


def test_boot_id_has_no_fallback(home):
    """A second identity source would let one failed read stamp records as
    another boot; the library names only the kernel boot id."""
    assert "btime" not in _RECORDS.read_text()



def test_status_waits_for_a_record_still_being_written(home, stopped_sleep):
    """The guardian holds the lock while it writes a record: status waits for
    it, then shows the finished record, as thaw does (review finding)."""
    lock = _state_dir(home) / "frozen.lock"
    rec = _state_dir(home) / "frozen"
    line = (
        f"{stopped_sleep.pid}\t{_starttime(stopped_sleep.pid)}\tsleep\tT\tk\t/x/o\tpaused\t{_BOOT}\n"
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f, fcntl.LOCK_EX); "
            "print('held', flush=True); time.sleep(1); open(sys.argv[2],'w').write(sys.argv[3])",
            str(lock),
            str(rec),
            line,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    out = _cli(home, "status")
    holder.wait(timeout=10)
    assert "still paused" in out.stdout, out.stdout + out.stderr
