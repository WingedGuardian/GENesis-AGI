"""The runaway response: pause the command writing a Claude Code task output.

The one place the disk guardian stops a process, so the tests pin its
narrowness from every side:
  * it acts only on cc-tmp/claude-<uid>/<project>/<session>/tasks/<id>.output,
    only while it is GROWING fast this poll (never on size alone), only while
    its domain is ORANGE or RED, only in act mode;
  * every holder must descend from a `claude` process and not be one;
  * the command root and every holder are paused parents-first, each bound to
    its start time, and recorded BEFORE its signal (no record, no signal);
  * a pid reused at any step is never signalled;
  * a lock held by an operator command means nothing is paused;
  * no page or reminder carries a raw `kill <pid>`;
  * the file is NOT emptied (that comes later);
  * against REAL processes: the writer is really stopped, the stand-in session
    is not, and `scripts/watchgod thaw` resumes it.

The fake process table is driven through a bash `kill` stub: on a fake table
wg_signal_gen falls back to `kill`, so no real process is ever signalled.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_WATCHGOD = _ROOT / "scripts" / "tmp_watchgod.sh"
_CLI = _ROOT / "scripts" / "watchgod"
_MB = 1024 * 1024
_RAW_KILL = re.compile(r"(?<!watchgod )\bkill\b(?: -\w+)? \d")


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "config").mkdir(parents=True)
    (home / "tmp").mkdir()
    cc = tmp_path / "cc-tmp"
    cc.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    return {"home": home, "cc": cc, "proc": proc, "queue": tmp_path / "queue", "tmp": tmp_path}


def _task_output(box, mb: int = 60, rel: str = "claude-1000/-proj/sess-1/tasks/b1.output") -> Path:
    f = box["cc"] / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    _grow(f, mb)
    return f


def _grow(f: Path, mb: int) -> None:
    """Allocated, not sparse: the detector measures space in use."""
    with open(f, "ab") as fh:
        os.posix_fallocate(fh.fileno(), 0, mb * _MB)


def _proc(
    box,
    pid: int,
    comm: str,
    ppid: int,
    target: Path | None = None,
    starttime: int | None = 1000,
    state: str = "S",
) -> None:
    d = box["proc"] / str(pid)
    (d / "fd").mkdir(parents=True, exist_ok=True)
    (d / "fdinfo").mkdir(exist_ok=True)
    (d / "comm").write_text(comm + "\n")
    (d / "status").write_text(f"Name:\t{comm}\nState:\t{state} (x)\nPPid:\t{ppid}\nThreads:\t1\n")
    if starttime is not None:
        fields = [state, str(ppid)] + ["0"] * 15 + ["1", "0"] + [str(starttime)] + ["0"] * 10
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
    elif (d / "stat").exists():
        (d / "stat").unlink()
    if target is not None:
        link = d / "fd" / "1"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        (d / "fdinfo" / "1").write_text("pos:\t0\nflags:\t0102001\n")


def _domains(box, total: int = 400) -> str:
    """The cc-tmp domain (and a big home one). The tier comes from RW_TIER."""
    cc_dev = os.stat(box["cc"]).st_dev
    home_dev = os.stat(box["home"] / "tmp").st_dev
    return f"{cc_dev}m9 {cc_dev} {total} {box['cc']}\n{home_dev}m8 {home_dev} 100000 {box['home']}"


# kill stub for the fake process table: records every call and marks a
# stopped pid's status as T, the way the kernel would.
_KILL_STUB = r"""
kill() {
    echo "kill $*" >> "$WG_KILLS"
    if [[ "$1" == -STOP ]]; then
        sed -i 's/^State:.*/State:\tT (stopped)/' "$DG_PROC/$2/status"
    fi
    return 0
}
"""


def _run(box, snippet: str, act: int = 1) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        HOME=str(box["home"]),
        GENESIS_ALERT_QUEUE_ROOT=str(box["queue"]),
        DG_PROC=str(box["proc"]),
        WG_KILLS=str(box["tmp"] / "kills"),
    )
    script = (
        f"set -euo pipefail\nsource '{_WATCHGOD}'\nload_config\nWATCHGOD_ACT={act}\n"
        f"CC_TMP_DIR='{box['cc']}'\n"
        f'mkdir -p "$DG_STATE_DIR"\n{_KILL_STUB}\n{snippet}\n'
    )
    out = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    assert out.returncode == 0, f"rc={out.returncode}\n{out.stdout}\n{out.stderr}"
    return out


def _respond(
    box,
    f: Path,
    tier: str = "red",
    act: int = 1,
    grow_mb: int = 40,
    between: str = "",
    before: str = "",
) -> subprocess.CompletedProcess:
    """Two polls: the second sees the file grow by grow_mb (10% of 400 per poll)."""
    cc_key = f"{os.stat(box['cc']).st_dev}m9"
    doms = _domains(box)
    return _run(
        box,
        f"""
        RW_TIER[{cc_key}]={tier}
        {before}
        wg_runaway_check '{doms}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 ))
        python3 -c 'import os,sys; fd=os.open(sys.argv[1], os.O_WRONLY); os.posix_fallocate(fd, 0, int(sys.argv[2])*1048576)' '{f}' {60 + grow_mb}
        {between}
        wg_runaway_check '{doms}'
        echo "ACTED=$_RW_ACTED"
        """,
        act=act,
    )


def _pages(box) -> list[dict]:
    if not box["queue"].exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(box["queue"].glob("*.json"))]


def _kills(box) -> list[str]:
    f = box["tmp"] / "kills"
    return f.read_text().splitlines() if f.exists() else []


def _frozen(box) -> list[list[str]]:
    f = box["home"] / ".genesis" / "watchgod" / "frozen"
    # Split on "\n" only, as the watchgod's bash readers do: str.splitlines()
    # also breaks at U+2028 and other separators a path may hold.
    if not f.exists():
        return []
    return [line.split("\t") for line in f.read_text().split("\n") if line]


def _log(box) -> str:
    f = box["home"] / ".genesis" / "logs" / "tmp_watchgod.log"
    return f.read_text() if f.exists() else ""


def _session(box, f: Path) -> None:
    """claude (100) -> bash (200, holds the file) -> grep (201, holds it too)."""
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, f, starttime=2000)
    _proc(box, 201, "grep", 200, f, starttime=2010)


def _no_raw_kill(box) -> None:
    for p in _pages(box):
        assert not _RAW_KILL.search(p["body"]), p["body"]
        assert not _RAW_KILL.search(p["title"]), p["title"]


# ── when it acts ──────────────────────────────────────────────────


@pytest.mark.parametrize("tier", ["orange", "red"])
def test_a_growing_task_output_is_paused_parents_first_and_recorded(box, tier):
    f = _task_output(box)
    _session(box, f)
    out = _respond(box, f, tier=tier)
    assert _kills(box) == ["kill -STOP 200", "kill -STOP 201"], (
        "parent before child, session untouched"
    )
    assert [r[:3] for r in _frozen(box)] == [["200", "2000", "bash"], ["201", "2010", "grep"]]
    assert [r[6] for r in _frozen(box)] == ["paused", "paused"]
    assert f.stat().st_size >= 60 * _MB, "C1 never empties the file"
    assert "ACTED=paused" in out.stdout
    act = [p for p in _pages(box) if p["title"].startswith("Paused a runaway Claude Code task")]
    assert len(act) == 1
    body = act[0]["body"]
    assert "scripts/watchgod thaw 200" in body and "scripts/watchgod kill 201" in body
    assert "session itself was NOT paused" in body and "Claude Code session: sess-1" in body
    assert "was NOT emptied" in body
    _no_raw_kill(box)


def test_the_detection_page_says_it_paused_never_that_it_did_not_act(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box, f)
    detect = [p for p in _pages(box) if p["title"].startswith("Runaway file on")]
    assert len(detect) == 1
    assert "paused the command writing it" in detect[0]["body"]
    assert "has not acted" not in detect[0]["body"]


def test_a_root_that_closed_its_descriptor_is_paused_too(box):
    """Invariant 3: the command root is paused even when only a child holds
    the file, or it could start the writer again."""
    f = _task_output(box)
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, None, starttime=2000)  # holds nothing
    _proc(box, 201, "grep", 200, f, starttime=2010)
    _respond(box, f)
    assert _kills(box) == ["kill -STOP 200", "kill -STOP 201"]
    assert [r[0] for r in _frozen(box)] == ["200", "201"]


# ── when it does not ──────────────────────────────────────────────


def test_size_alone_never_pauses(box):
    """A big file that stopped growing (paused, or done) is not paused again:
    otherwise every thaw would be followed by another pause."""
    f = _task_output(box, mb=150)
    _session(box, f)
    _respond(box, f, grow_mb=0)
    assert _kills(box) == [] and _frozen(box) == []


@pytest.mark.parametrize("tier", ["green", "yellow"])
def test_a_domain_not_in_trouble_is_not_acted_on(box, tier):
    f = _task_output(box)
    _session(box, f)
    _respond(box, f, tier=tier)
    assert _kills(box) == []


def test_a_file_that_is_not_a_task_output_is_never_paused(box):
    f = _task_output(box, rel="claude-1000/-proj/sess-1/scratch/big.log")
    _session(box, f)
    _respond(box, f)
    assert _kills(box) == []


def test_observe_mode_pauses_nothing_and_says_what_it_would_do(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box, f, act=0)
    assert _kills(box) == [] and _frozen(box) == []
    assert "OBSERVE: would pause the command writing" in _log(box)


def test_the_session_itself_is_never_paused(box):
    f = _task_output(box)
    _proc(box, 100, "claude", 1, f, starttime=500)
    _respond(box, f)
    assert _kills(box) == []
    assert "is the Claude Code session itself" in _log(box)


def test_a_holder_outside_any_session_vetoes(box):
    f = _task_output(box)
    _proc(box, 300, "python", 1, f, starttime=3000)
    _respond(box, f)
    assert _kills(box) == []
    assert "not a command of a Claude Code session" in _log(box)


def test_a_holder_whose_start_time_cannot_be_read_vetoes(box):
    f = _task_output(box)
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, f, starttime=None)
    _respond(box, f)
    assert _kills(box) == []
    assert "start time could not be read" in _log(box)


def test_an_uninspectable_process_in_the_command_vetoes(box):
    f = _task_output(box)
    _session(box, f)
    _proc(box, 202, "sudo", 201, None, starttime=2020)
    (box["proc"] / "202" / "fd").chmod(0)
    try:
        _respond(box, f)
    finally:
        (box["proc"] / "202" / "fd").chmod(0o700)
    if os.geteuid() == 0:
        pytest.skip("root lists a 0o000 dir")
    assert _kills(box) == []
    assert "cannot be inspected" in _log(box)


def test_an_uninspectable_single_threaded_zombie_does_not_veto(box):
    f = _task_output(box)
    _session(box, f)
    _proc(box, 202, "sleep", 201, None, starttime=2020, state="Z")
    (box["proc"] / "202" / "fd").chmod(0)
    try:
        _respond(box, f)
    finally:
        (box["proc"] / "202" / "fd").chmod(0o700)
    assert _kills(box) == ["kill -STOP 200", "kill -STOP 201"]


def test_a_process_already_stopped_by_something_else_is_left_unrecorded(box):
    f = _task_output(box)
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="T")
    _proc(box, 201, "grep", 200, f, starttime=2010)
    _respond(box, f)
    assert _kills(box) == ["kill -STOP 201"]
    assert [r[0] for r in _frozen(box)] == ["201"], "a stop someone else made is never adopted"
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert "Already stopped by something else" in act[0]["body"]


# ── invariants: identity, lock, record ────────────────────────────


def test_a_pid_reused_before_the_signal_is_never_signalled(box):
    """Invariant 1 at the signal: the record is written, then the process at
    that pid is a different one; nothing is sent and the record is dropped."""
    f = _task_output(box)
    _session(box, f)
    reuse = f"""
    eval "_orig_put() $(declare -f wg_record_put | tail -n +2)"
    wg_record_put() {{
        # Only on 201's own record: the reuse lands between ITS record and
        # ITS signal.
        [[ "$1" == 201 ]] && sed -i 's/ 2010 / 9999 /' '{box["proc"]}/201/stat'
        _orig_put "$@"
    }}
    """
    _respond(box, f, between=reuse)
    assert _kills(box) == ["kill -STOP 200"], "200 paused; 201 never signalled"
    assert [r[0] for r in _frozen(box)] == ["200"], "201's record was dropped: nothing was sent"
    assert "Could not pause" not in "".join(p["body"] for p in _pages(box)), (
        "a reused pid is gone, not refused"
    )


def test_a_pid_reused_before_the_ancestry_read_is_not_acted_on(box):
    """Invariant 1 at the ancestry: the holder exited and its pid was reused
    after discovery; the chain read sees another process and drops it."""
    f = _task_output(box)
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, f, starttime=2000)
    reuse = f"""
    eval "_orig_chain() $(declare -f _rw_chain | tail -n +2)"
    _rw_chain() {{
        sed -i 's/ 2000 / 9999 /' '{box["proc"]}/200/stat'
        _orig_chain "$@"
    }}
    """
    _respond(box, f, between=reuse)
    assert _kills(box) == []


def test_a_held_records_lock_pauses_nothing(box):
    """Invariant 2: an operator command holds the lock; nothing is paused and
    the page says why."""
    f = _task_output(box)
    _session(box, f)
    lockf = box["home"] / ".genesis" / "watchgod" / "frozen.lock"
    lockf.parent.mkdir(parents=True, exist_ok=True)
    lockf.touch()
    holder = subprocess.Popen(["flock", str(lockf), "sleep", "60"])
    try:
        time.sleep(0.3)
        _respond(box, f)
    finally:
        holder.kill()
        holder.wait()
    assert _kills(box) == [] and _frozen(box) == []
    detect = [p for p in _pages(box) if p["title"].startswith("Runaway file on")]
    assert "an operator command holds it" in detect[0]["body"]


def test_no_record_means_no_signal(box):
    """The record is written before the signal: a record that cannot be
    written leaves the process running."""
    f = _task_output(box)
    _session(box, f)
    _respond(box, f, between="wg_record_put() { return 1; }")
    assert _kills(box) == []
    assert "no writer could be paused" in _log(box)


def test_a_stop_that_is_not_confirmed_stays_pending_and_is_promoted_later(box):
    f = _task_output(box)
    _session(box, f)
    # A stub that sends but leaves the state running (a D-state process).
    nostop = 'kill() { echo "kill $*" >> "$WG_KILLS"; return 0; }; _rw_stopped() { return 1; }'
    _respond(box, f, between=nostop)
    assert [r[6] for r in _frozen(box)] == ["pending", "pending"]
    act = [p for p in _pages(box) if "NOT yet confirmed" in p["title"]]
    assert len(act) == 1
    # The stops land; the reminder pass promotes them under the lock.
    for pid in (200, 201):
        st = box["proc"] / str(pid) / "status"
        st.write_text(st.read_text().replace("State:\tS (x)", "State:\tT (stopped)"))
    _run(box, "_rw_repage $(date +%s)")
    assert [r[6] for r in _frozen(box)] == ["paused", "paused"]


# ── reminders ─────────────────────────────────────────────────────


def test_a_pause_left_in_place_is_reminded_without_raw_pids(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box, f)
    later = int(time.time()) + 7 * 3600
    _run(box, f"_rw_repage {later}")
    rem = [p for p in _pages(box) if p["title"].startswith("Still paused")]
    assert len(rem) == 1
    assert (
        "scripts/watchgod thaw 200" in rem[0]["body"]
        and "scripts/watchgod kill 201" in rem[0]["body"]
    )
    _no_raw_kill(box)


def test_a_task_output_name_cannot_forge_or_disguise_page_lines(box):
    """A task output's path is shown in the action page, the detection page,
    the reminder and the log; a Unicode line separator or bidi control in it
    is shown as "?" there, like a process name. The pause record keeps the
    real path, and the file is still acted on."""
    f = _task_output(box, rel="claude-1000/-pr\u2028oj/se\u202ess-1/tasks/b\u2029x.output")
    _session(box, f)
    out = _respond(box, f)
    assert "ACTED=paused" in out.stdout
    assert [r[5] for r in _frozen(box)] == [str(f), str(f)], "the record keeps the real path"
    _run(box, f"_rw_repage {int(time.time()) + 7 * 3600}")
    pages = _pages(box)
    assert {p["title"].split(" ")[0] for p in pages} >= {"Paused", "Runaway", "Still"}
    for text in [x for p in pages for x in (p["title"], p["body"])] + [_log(box)]:
        for ch in ("\u2028", "\u2029", "\u202e"):
            assert ch not in text, text
    act = next(p for p in pages if p["title"].startswith("Paused"))
    assert "-pr?oj/se?ss-1/tasks/b?x.output" in act["body"]
    assert "Claude Code session: se?ss-1" in act["body"]


def test_a_record_of_another_boot_is_not_reminded(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box, f)
    frozen = box["home"] / ".genesis" / "watchgod" / "frozen"
    frozen.write_text(
        "".join(
            "\t".join(line.split("\t")[:7] + ["another-boot"]) + "\n"
            for line in frozen.read_text().splitlines()
        )
    )
    later = int(time.time()) + 7 * 3600
    _run(box, f"_rw_repage {later}")
    assert not [p for p in _pages(box) if p["title"].startswith("Still paused")]


# ── against real processes ────────────────────────────────────────


def test_a_real_runaway_command_is_paused_and_thaw_resumes_it(tmp_path):
    """A stand-in session (a process named `claude`) runs a bash command that
    holds a task output under a sandbox cc-tmp. Real /proc, real signals."""
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "config").mkdir(parents=True)
    cc = tmp_path / "cc-tmp"
    out = cc / "claude-1000" / "-proj" / "sess-9" / "tasks" / "r1.output"
    out.parent.mkdir(parents=True)
    out.touch()
    session = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,subprocess,sys,time; open('/proc/self/comm','w').write('claude');"
            "p=subprocess.Popen(['bash','-c','exec 1>>\"$0\"; sleep 120 & wait', sys.argv[1]]);"
            "print(p.pid, flush=True); time.sleep(120)",
            str(out),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    bash_pid = int(session.stdout.readline())
    try:
        time.sleep(0.5)
        with open(out, "ab") as fh:
            os.posix_fallocate(fh.fileno(), 0, 60 * _MB)
        cc_dev = os.stat(cc).st_dev
        doms = f"{cc_dev}m9 {cc_dev} 400 {cc}"
        env = dict(os.environ, HOME=str(home), GENESIS_ALERT_QUEUE_ROOT=str(tmp_path / "queue"))
        env.pop("DG_PROC", None)
        script = f"""set -euo pipefail
source '{_WATCHGOD}'; load_config; WATCHGOD_ACT=1; CC_TMP_DIR='{cc}'; mkdir -p "$DG_STATE_DIR"
RW_TIER[{cc_dev}m9]=red
wg_runaway_check '{doms}'
_RW_PREV_T=$(( _RW_PREV_T - 30 ))
python3 -c 'import os,sys; fd=os.open(sys.argv[1], os.O_WRONLY); os.posix_fallocate(fd, 0, 110*1048576)' '{out}'
wg_runaway_check '{doms}'
echo "ACTED=$_RW_ACTED"
"""
        r = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=120
        )
        # Other big files on the real box are checked too (and reset
        # _RW_ACTED per file), so the evidence is the record and the state.
        frozen = (home / ".genesis" / "watchgod" / "frozen").read_text()
        assert f"{bash_pid}\t" in frozen, r.stdout + r.stderr
        state = Path(f"/proc/{bash_pid}/status").read_text()
        assert "State:\tT" in state, "the command's shell is really stopped"
        assert "State:\tT" not in Path(f"/proc/{session.pid}/status").read_text(), (
            "the session is not"
        )
        t = subprocess.run(
            [str(_CLI), "thaw", "all"], env=env, capture_output=True, text=True, timeout=60
        )
        assert t.returncode == 0, t.stdout + t.stderr
        time.sleep(0.3)
        assert "State:\tT" not in Path(f"/proc/{bash_pid}/status").read_text(), "thaw resumed it"
    finally:
        subprocess.run(["pkill", "-CONT", "-P", str(session.pid)], check=False)
        session.kill()
        session.wait()
        subprocess.run(["pkill", "-KILL", "-P", str(bash_pid)], check=False)
        subprocess.run(["kill", "-KILL", str(bash_pid)], check=False)


def test_a_traced_holder_is_not_called_paused_on_t(box):
    """pending-t: the holder was in a tracing stop before the SIGSTOP, so only
    T confirms it; t never promotes it (review finding)."""
    f = _task_output(box)
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="t")
    # The tracer swallows the stop: the state stays t.
    nostop = 'kill() { echo "kill $*" >> "$WG_KILLS"; return 0; }'
    _respond(box, f, between=nostop)
    assert [r[6] for r in _frozen(box)] == ["pending-t"]
    # A second process (fresh state) that sees the file grow again: it acts.
    out = _respond(box, f, between=nostop, grow_mb=80)
    assert "ACTED=paused" in out.stdout, "the second response did act"
    assert [r[6] for r in _frozen(box)] == ["pending-t"], "still t: never promoted"


def test_the_record_of_a_process_that_exited_while_paused_is_retired(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box, f)
    assert len(_frozen(box)) == 2
    import shutil

    shutil.rmtree(box["proc"] / "201")
    _run(box, "_rw_repage $(date +%s)")
    assert [r[0] for r in _frozen(box)] == ["200"]
    assert "dropped: the process has exited" in _log(box)


def test_the_action_page_says_a_resumed_runaway_is_paused_again(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box, f)
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert "still growing fast while the disk is in trouble is paused again" in act[0]["body"]


def test_a_refused_signal_leaves_no_record(box):
    """The record is written first; when the signal is then refused (the
    process is still there), the record would name a stop that never happened:
    it is dropped at once, not left for the reminder pass."""
    f = _task_output(box)
    _session(box, f)
    refuse = """
    eval "_orig_sig() $(declare -f wg_signal_gen | tail -n +2)"
    wg_signal_gen() { [[ "$2" == 201 ]] && return 2; _orig_sig "$@"; }
    """
    _respond(box, f, between=refuse)
    assert _kills(box) == ["kill -STOP 200"]
    assert [r[0] for r in _frozen(box)] == ["200"]
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert "Could not pause: pid 201" in act[0]["body"]
