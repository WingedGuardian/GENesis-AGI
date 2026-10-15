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
  * the file is emptied once every writer paused here is confirmed stopped
    and nothing in the command was stopped by something else, through a
    paused writer's own descriptor, its last MB kept (once per episode) where
    there is room; a writer it never saw only refills it;
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
    home_tier: str = "green",
    act: int = 1,
    grow_mb: int = 40,
    between: str = "",
    before: str = "",
) -> subprocess.CompletedProcess:
    """Two polls: the second sees the file grow by grow_mb (10% of 400 per poll)."""
    cc_key = f"{os.stat(box['cc']).st_dev}m9"
    home_key = f"{os.stat(box['home'] / 'tmp').st_dev}m8"
    doms = _domains(box)
    return _run(
        box,
        f"""
        RW_TIER[{cc_key}]={tier}
        RW_TIER[{home_key}]={home_tier}
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
    assert f.stat().st_size == 0, "every writer paused: emptied"
    assert "ACTED=emptied" in out.stdout
    act = [
        p
        for p in _pages(box)
        if p["title"] == "Paused a runaway Claude Code task and emptied its output"
    ]
    assert len(act) == 1
    body = act[0]["body"]
    assert "scripts/watchgod thaw 200" in body and "scripts/watchgod kill 201" in body
    assert "session itself was NOT paused" in body and "Claude Code session: sess-1" in body
    assert "was emptied (last 1 MB kept in" in body
    _no_raw_kill(box)


def test_an_emptied_file_sends_no_detection_page_for_the_episode_it_closed(box):
    """The action page says everything; the episode the truncate closed must
    not page "again" for a size the file no longer has."""
    f = _task_output(box)
    _session(box, f)
    _respond(box, f)
    assert not [p for p in _pages(box) if p["title"].startswith("Runaway file on")]


def test_a_paused_but_not_emptied_file_says_so_and_never_that_it_did_not_act(box):
    f = _task_output(box)
    _session(box, f)
    refuse = """
    eval "_orig_sig() $(declare -f wg_signal_gen | tail -n +2)"
    wg_signal_gen() { [[ "$2" == 201 ]] && return 2; _orig_sig "$@"; }
    """
    _respond(box, f, between=refuse)
    assert f.stat().st_size >= 60 * _MB, "a writer was not stopped: never emptied"
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert act[0]["title"] == "Paused a runaway Claude Code task; its output could NOT be emptied"
    assert "not every writer is confirmed stopped" in act[0]["body"]
    detect = [p for p in _pages(box) if p["title"].startswith("Runaway file on")]
    assert "could not be emptied yet" in detect[0]["body"]
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


def test_every_process_between_the_root_and_a_holder_is_paused(box):
    """claude -> bash (root) -> supervisor -> worker (holds the file): the
    supervisor holds nothing but could restart the worker, so it is paused
    and recorded too, parents first (review finding)."""
    f = _task_output(box)
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, starttime=2000)
    _proc(box, 250, "supervisor", 200, starttime=2005)
    _proc(box, 300, "worker", 250, f, starttime=2010)
    _respond(box, f)
    assert _kills(box) == ["kill -STOP 200", "kill -STOP 250", "kill -STOP 300"]
    assert sorted((r[0], r[1], r[2]) for r in _frozen(box)) == [
        ("200", "2000", "bash"),
        ("250", "2005", "supervisor"),
        ("300", "2010", "worker"),
    ]


def test_slow_confirmations_never_leave_a_later_holder_unsignalled(box):
    """Stops that never confirm each spend up to a second; every process is
    recorded and signalled before any confirmation is waited for, so the
    bounded deadline can never leave a later holder running (review finding).
    With no stop confirmed, the detection page never says "paused"."""
    f = _task_output(box)
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, f, starttime=2000)
    holders = [210 + i for i in range(7)]
    for i, pid in enumerate(holders):
        _proc(box, pid, f"w{i}", 200, f, starttime=2100 + i)
    nostop = 'kill() { echo "kill $*" >> "$WG_KILLS"; return 0; }'
    out = _respond(box, f, between=nostop)
    assert sorted(_kills(box)) == sorted(f"kill -STOP {p}" for p in [200, *holders])
    assert sorted(r[0] for r in _frozen(box)) == sorted(str(p) for p in [200, *holders])
    assert "ACTED=unconfirmed" in out.stdout
    detect = [p for p in _pages(box) if p["title"].startswith("Runaway file on")]
    assert detect and "paused the command" not in detect[0]["body"]
    assert "no stop is confirmed yet" in detect[0]["body"]


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
    """A task output's path is shown in the action page, the reminder, the
    kept tail's name and the log; a Unicode line separator or bidi control in
    it is shown as "?" there, like a process name. The pause record keeps the
    real path, and the file is still acted on (paused and emptied)."""
    f = _task_output(box, rel="claude-1000/-pr\u2028oj/se\u202ess-1/tasks/b\u2029x.output")
    _session(box, f)
    out = _respond(box, f)
    assert "ACTED=emptied" in out.stdout
    assert [r[5] for r in _frozen(box)] == [str(f), str(f)], "the record keeps the real path"
    _run(box, f"_rw_repage {int(time.time()) + 7 * 3600}")
    pages = _pages(box)
    assert {p["title"].split(" ")[0] for p in pages} >= {"Paused", "Still"}
    for text in [x for p in pages for x in (p["title"], p["body"])] + [_log(box)]:
        for ch in ("\u2028", "\u2029", "\u202e"):
            assert ch not in text, text
    act = next(p for p in pages if p["title"].startswith("Paused"))
    assert "-pr?oj/se?ss-1/tasks/b?x.output" in act["body"]
    assert "Claude Code session: se?ss-1" in act["body"]
    assert "b?x.output." in act["body"], "the kept tail's name is shown safe too"


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
        assert out.stat().st_size == 0, "and its output emptied"
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
    # It acted: signalled again, and honest that the stop is not confirmed.
    assert "ACTED=unconfirmed" in out.stdout, "the second response did act"
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


# ── emptying (C2) ─────────────────────────────────────────────────


def test_no_tail_is_kept_on_a_disk_in_trouble_but_the_file_is_still_emptied(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box, f, home_tier="red")
    assert f.stat().st_size == 0
    assert not (box["home"] / "tmp" / "watchgod-truncated").exists()
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert (
        "was emptied (no tail kept: the disk holding ~/tmp has no room to spare)" in act[0]["body"]
    )


def test_the_tail_is_private(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box, f)
    d = box["home"] / "tmp" / "watchgod-truncated"
    tails = list(d.glob("b1.output.*.tail"))
    assert len(tails) == 1 and tails[0].stat().st_size == _MB
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    assert oct(tails[0].stat().st_mode & 0o777) == "0o600"


def test_a_file_whose_writers_are_all_paused_is_emptied_without_new_growth(box):
    """A pause that could not empty on its poll (or one from before emptying
    existed): the emptying is still owed, size alone is enough."""
    f = _task_output(box, mb=150)
    _session(box, f)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="T")
    _proc(box, 201, "grep", 200, f, starttime=2010, state="T")
    put = (
        "wg_records_lock lk; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        f"wg_record_put 200 2000 bash \"$ts\" {os.stat(f).st_dev}:{os.stat(f).st_ino} '{f}' paused\n"
        f"wg_record_put 201 2010 grep \"$ts\" {os.stat(f).st_dev}:{os.stat(f).st_ino} '{f}' paused\n"
        'wg_records_unlock "$lk"'
    )
    _respond(box, f, grow_mb=0, before=put)
    # Emptied on the first poll (the helper's growth step then re-extends it
    # to 60 MB, still far below the 150 MB it was).
    assert f.stat().st_size < 150 * _MB
    assert _kills(box) == [], "nothing new to pause"
    act = [p for p in _pages(box) if p["title"].startswith("Emptied")]
    assert (
        act
        and act[0]["title"]
        == "Emptied a runaway Claude Code task output (its writers were already paused)"
    )


def test_a_thaw_between_the_owed_check_and_the_lock_pauses_nothing(box):
    """The owed emptying is decided before the records lock; a thaw that
    lands before the lock is taken resumes the writers and drops their
    records. The response re-checks under the lock: nothing is recorded,
    signalled or emptied, as the file did not grow (review finding)."""
    f = _task_output(box, mb=150)
    _session(box, f)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="T")
    _proc(box, 201, "grep", 200, f, starttime=2010, state="T")
    proc = box["proc"]
    thaw_once = (
        _paused_records(box, f)
        + "\n"
        + "eval \"$(declare -f wg_records_lock | sed '1s/wg_records_lock/_wg_lock_orig/')\"\n"
        + "_THAWED=0\n"
        + "wg_records_lock() {\n"
        + "  if (( ! _THAWED )); then _THAWED=1\n"
        + '    : > "$(wg_records_file)"\n'
        + f"    sed -i 's/^State:.*/State:\\tS (x)/' '{proc}/200/status' '{proc}/201/status'\n"
        + f"    sed -i 's/) T /) S /' '{proc}/200/stat' '{proc}/201/stat'\n"
        + "  fi\n"
        + '  _wg_lock_orig "$@"\n'
        + "}"
    )
    out = _respond(box, f, grow_mb=0, before=thaw_once)
    assert _kills(box) == [], "a resumed writer is not paused again on size"
    assert _frozen(box) == [], "and not recorded again"
    assert f.stat().st_size >= 150 * _MB, "nor emptied"
    assert "resumed" in _log(box), out.stdout + out.stderr


def test_an_owed_emptying_never_stops_a_root_the_operator_resumed(box):
    """The command root need not hold the file. Its holder is still paused,
    but the root was thawed: with no growth, nothing is stopped, recorded or
    emptied (review finding: the re-check covered the holders only)."""
    f = _task_output(box, mb=150)
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, starttime=2000)  # the root: no fd, no record, running
    _proc(box, 201, "grep", 200, f, starttime=2010, state="T")
    put = (
        "wg_records_lock lk; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        f"wg_record_put 201 2010 grep \"$ts\" {os.stat(f).st_dev}:{os.stat(f).st_ino} '{f}' paused\n"
        'wg_records_unlock "$lk"'
    )
    _respond(box, f, grow_mb=0, before=put)
    assert _kills(box) == [], "the resumed root is not stopped again on size"
    assert [r[0] for r in _frozen(box)] == ["201"], "and not recorded"
    assert f.stat().st_size >= 150 * _MB, "nor is the file emptied"


def test_an_owed_emptying_accepts_a_recorded_traced_stop(box):
    """A holder this guardian paused can show t (its group stop under a
    tracer) and still carry its "paused" record: the owed emptying accepts
    it, as the pause and the re-walk do (review finding)."""
    f = _task_output(box, mb=150)
    _session(box, f)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="t")
    _proc(box, 201, "grep", 200, f, starttime=2010, state="t")
    _respond(box, f, grow_mb=0, before=_paused_records(box, f))
    assert f.stat().st_size < 150 * _MB, "emptied"
    assert _kills(box) == [], "nothing new to pause"


def test_a_thawed_writer_is_not_paused_again_for_the_size_it_left(box):
    """No record (thawed) and no growth: size alone never acts."""
    f = _task_output(box, mb=150)
    _session(box, f)
    _respond(box, f, grow_mb=0)
    assert _kills(box) == [] and f.stat().st_size >= 150 * _MB


def test_emptying_opens_a_new_episode(box):
    """After a confirmed truncate the episode is over: the generation carries
    an episode start, so a refill pages and is acted on again."""
    f = _task_output(box)
    _session(box, f)
    out = _respond(box, f, before="", between="")
    assert f.stat().st_size == 0 and "ACTED=emptied" in out.stdout
    r = _run(
        box,
        f"""
        RW_TIER[{os.stat(box["cc"]).st_dev}m9]=red
        RW_TIER[{os.stat(box["home"] / "tmp").st_dev}m8]=green
        python3 -c 'import os,sys; fd=os.open(sys.argv[1], os.O_WRONLY); os.posix_fallocate(fd, 0, 60*1048576)' '{f}'
        sed -i 's/^State:.*/State:\tS (x)/' '{box["proc"]}/200/status' '{box["proc"]}/201/status'
        wg_runaway_check '{_domains(box)}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 ))
        python3 -c 'import os,sys; fd=os.open(sys.argv[1], os.O_WRONLY); os.posix_fallocate(fd, 0, 100*1048576)' '{f}'
        wg_runaway_check '{_domains(box)}'
        for g in "${{!_RW_EP[@]}}"; do echo "EP=$g"; done
    """,
    )
    assert "EP=" in r.stdout, r.stdout + r.stderr
    assert f.stat().st_size == 0, "the refill was acted on again"


def _paused_records(box, f: Path) -> str:
    return (
        "wg_records_lock lk; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        f"wg_record_put 200 2000 bash \"$ts\" {os.stat(f).st_dev}:{os.stat(f).st_ino} '{f}' paused\n"
        f"wg_record_put 201 2010 grep \"$ts\" {os.stat(f).st_dev}:{os.stat(f).st_ino} '{f}' paused\n"
        'wg_records_unlock "$lk"'
    )


def test_a_paused_record_whose_process_runs_again_is_not_emptied_on_size(box):
    """Resumed outside thaw (the record is still there, the process runs):
    size alone does not act."""
    f = _task_output(box, mb=150)
    _session(box, f)
    _respond(box, f, grow_mb=0, before=_paused_records(box, f))
    assert _kills(box) == [] and f.stat().st_size >= 150 * _MB


def test_a_foreign_stop_is_not_emptied_on_size(box):
    """Stopped by something else (no record of ours): size alone does not act."""
    f = _task_output(box, mb=150)
    _session(box, f)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="T")
    _proc(box, 201, "grep", 200, f, starttime=2010, state="T")
    _respond(box, f, grow_mb=0)
    assert _kills(box) == [] and f.stat().st_size >= 150 * _MB
    assert "no writer could be paused" not in _log(box), "the response never ran"


def test_a_writer_the_walk_never_saw_does_not_stop_the_emptying(box):
    """No proof is attempted that no other process writes the file: a task
    output is opened O_APPEND, so a writer missed here only refills it, and
    that refill is detected and paused again (premise check). A holder that
    appears after the pause therefore does not block the emptying."""
    f = _task_output(box)
    _session(box, f)
    fork = f"""
    eval "_orig_sig() $(declare -f wg_signal_gen | tail -n +2)"
    wg_signal_gen() {{
        local rc=0; _orig_sig "$@" || rc=$?
        if [[ "$2" == 201 ]]; then
            mkdir -p '{box["proc"]}/202/fd' '{box["proc"]}/202/fdinfo'
            ln -sf '{f}' '{box["proc"]}/202/fd/1'
            printf 'pos:\t0\nflags:\t0102001\n' > '{box["proc"]}/202/fdinfo/1'
            printf 'Name:\tgrep\nState:\tS (x)\nPPid:\t201\nThreads:\t1\n' > '{box["proc"]}/202/status'
            printf '202 (grep) S 201 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 2020 0\n' > '{box["proc"]}/202/stat'
        fi
        return $rc
    }}
    """
    out = _respond(box, f, between=fork)
    assert "ACTED=emptied" in out.stdout
    assert f.stat().st_size == 0


def test_an_o_append_writer_missed_by_the_emptying_only_refills_it(tmp_path):
    """The premise the emptying rests on, on a real file: truncating under a
    running O_APPEND writer (the flags of every live task output) frees the
    space, and the writer goes on from the new end; nothing is corrupted."""
    f = tmp_path / "out"
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, b"x" * (4 * _MB))
        os.truncate(f, 0)
        os.write(fd, b"y" * 1000)
        st = os.stat(f)
        assert st.st_size == 1000, "an O_APPEND writer writes from the new end"
        assert st.st_blocks * 512 < _MB, "the space was freed"
    finally:
        os.close(fd)


def test_a_process_stopped_by_something_else_keeps_the_file(box):
    """Something other than this guardian stopped a process of the command: it
    may resume it, so the file is not emptied (review finding)."""
    f = _task_output(box)
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="T")
    _proc(box, 201, "grep", 200, f, starttime=2010)
    _respond(box, f)
    assert f.stat().st_size >= 60 * _MB
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert "stopped by something else, which can resume it" in act[0]["body"]


def test_an_ancestor_stopped_by_something_else_keeps_the_file(box):
    """A process of the command that holds nothing (the root here) but was
    stopped by something else may be resumed by it and restart the writer:
    the file is not emptied (review finding)."""
    f = _task_output(box)
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, starttime=2000, state="T")
    _proc(box, 201, "grep", 200, f, starttime=2010)
    _respond(box, f)
    assert f.stat().st_size >= 60 * _MB
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert "pid 200 in its command was stopped by something else" in act[0]["body"]


def test_an_ancestor_resumed_outside_thaw_is_not_stopped_again_on_size(box):
    """A direct SIGCONT leaves an ancestor's "paused" record in place: the
    owed emptying checks its state too, so a running root is never stopped
    again, nor the file emptied, without new growth (review finding)."""
    f = _task_output(box, mb=150)
    _proc(box, 100, "claude", 1, starttime=500)
    _proc(box, 200, "bash", 100, starttime=2000)  # the root: running again
    _proc(box, 201, "grep", 200, f, starttime=2010, state="T")
    put = (
        "wg_records_lock lk; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        f"wg_record_put 200 2000 bash \"$ts\" {os.stat(f).st_dev}:{os.stat(f).st_ino} '{f}' paused\n"
        f"wg_record_put 201 2010 grep \"$ts\" {os.stat(f).st_dev}:{os.stat(f).st_ino} '{f}' paused\n"
        'wg_records_unlock "$lk"'
    )
    _respond(box, f, grow_mb=0, before=put)
    assert _kills(box) == [], "the running root is not stopped again on size"
    assert f.stat().st_size >= 150 * _MB, "nor is the file emptied"


def test_a_slow_refill_after_an_emptying_is_paused_again(box):
    """A writer the emptying missed, refilling slowly: within the episode this
    guardian's own emptying opened, crossing the size threshold is acted on
    like growth, though it never grows fast (review finding)."""
    f = _task_output(box, mb=150)
    _session(box, f)
    cc_key = f"{os.stat(box['cc']).st_dev}m9"
    r = _run(
        box,
        f"""
        RW_TIER[{cc_key}]=red
        _RW_EMPTIED_EP[$(stat -c %d:%i:%W '{f}')]=1000
        _RW_EP[$(stat -c %d:%i:%W '{f}')]=1000
        wg_runaway_check '{_domains(box)}'
        echo "ACTED=$_RW_ACTED"
        """,
    )
    assert "ACTED=emptied" in r.stdout or "ACTED=paused" in r.stdout, r.stdout + r.stderr
    assert "kill -STOP 200" in _kills(box)


def test_a_slow_refill_after_a_real_emptying_and_a_thaw_is_paused_again(box):
    """End to end: the guardian empties the file, the operator thaws the
    command, and it refills slowly past the size threshold: it is paused
    again, because that size was all written after the emptying."""
    f = _task_output(box, mb=150)
    _session(box, f)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="T")
    _proc(box, 201, "grep", 200, f, starttime=2010, state="T")
    cc_key = f"{os.stat(box['cc']).st_dev}m9"
    doms = _domains(box)
    proc = box["proc"]
    r = _run(
        box,
        f"""
        RW_TIER[{cc_key}]=red
        {_paused_records(box, f)}
        wg_runaway_check '{doms}'
        echo "A1=$_RW_ACTED"
        : > "$(wg_records_file)"
        sed -i 's/^State:.*/State:\tS (x)/' '{proc}/200/status' '{proc}/201/status'
        sed -i 's/) T /) S /' '{proc}/200/stat' '{proc}/201/stat'
        for g in "${{!_RW_PREV_AT[@]}}"; do _RW_PREV_AT[$g]=$(( _RW_PREV_AT[$g] - 100000 )); done
        _RW_PREV_T=$(( _RW_PREV_T - 100000 ))
        python3 -c 'import os,sys; fd=os.open(sys.argv[1], os.O_WRONLY); os.posix_fallocate(fd, 0, 150*1048576)' '{f}'
        wg_runaway_check '{doms}'
        echo "A2=$_RW_ACTED"
        """,
    )
    assert "A1=emptied" in r.stdout, r.stdout + r.stderr
    assert "kill -STOP 200" in _kills(box) and "kill -STOP 201" in _kills(box), r.stdout
    assert "A2=emptied" in r.stdout


def test_a_slow_size_alone_outside_an_emptied_episode_is_never_acted_on(box):
    """The control: the same size with no emptying of ours behind it (e.g. a
    writer someone thawed) is paged, never paused."""
    f = _task_output(box, mb=150)
    _session(box, f)
    cc_key = f"{os.stat(box['cc']).st_dev}m9"
    _run(box, f"RW_TIER[{cc_key}]=red\nwg_runaway_check '{_domains(box)}'")
    assert _kills(box) == []


def test_a_failed_tail_copy_is_never_reported_as_kept(box):
    """The copy fails and so does the truncate; on the retry the page must not
    claim a tail exists (review finding)."""
    f = _task_output(box, mb=150)
    _session(box, f)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="T")
    _proc(box, 201, "grep", 200, f, starttime=2010, state="T")
    shim = box["tmp"] / "shim"
    shim.mkdir()
    for name in ("mktemp", "truncate"):
        (shim / name).write_text("#!/bin/sh\nexit 1\n")
        (shim / name).chmod(0o755)
    # The retry's note is deduplicated in the log: read it from the shell.
    trap = "trap 'echo \"NOTE=$_RW_EMPTY_NOTE\"' EXIT"
    out = _respond(
        box, f, grow_mb=0, before=_paused_records(box, f) + f'\nPATH="{shim}:$PATH"\n{trap}'
    )
    assert "copy failed earlier in this episode" in out.stdout, out.stdout
    assert "already kept" not in out.stdout


def test_a_failing_truncate_keeps_one_tail_per_episode(box):
    """A truncate that keeps failing is retried every poll; the 1 MB tail is
    kept once per episode, never once per poll (review finding)."""
    f = _task_output(box, mb=150)
    _session(box, f)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="T")
    _proc(box, 201, "grep", 200, f, starttime=2010, state="T")
    shim = box["tmp"] / "shim"
    shim.mkdir()
    calls = box["tmp"] / "truncate-calls"
    (shim / "truncate").write_text(f"#!/bin/sh\necho x >> '{calls}'\nexit 0\n")
    (shim / "truncate").chmod(0o755)
    _respond(box, f, grow_mb=0, before=_paused_records(box, f) + f'\nPATH="{shim}:$PATH"')
    assert len(calls.read_text().split()) >= 2, "the emptying was retried on the next poll"
    tails = list((box["home"] / "tmp" / "watchgod-truncated").glob("*.tail"))
    assert len(tails) == 1, tails


def test_the_records_lock_is_free_while_the_file_is_emptied(box):
    """The tail copy and the truncate run after the records lock is released,
    so a stalled filesystem there can never hold up an operator's thaw
    (review finding)."""
    f = _task_output(box)
    _session(box, f)
    lock = box["home"] / ".genesis" / "watchgod" / "frozen.lock"
    seen = box["tmp"] / "lock-during-truncate"
    shim = box["tmp"] / "shim"
    shim.mkdir()
    (shim / "truncate").write_text(
        "#!/bin/sh\n"
        f"if flock -n '{lock}' true; then echo free > '{seen}'; else echo held > '{seen}'; fi\n"
        'exec /usr/bin/truncate "$@"\n'
    )
    (shim / "truncate").chmod(0o755)
    out = _respond(box, f, between=f'PATH="{shim}:$PATH"')
    assert "ACTED=emptied" in out.stdout
    assert seen.read_text().strip() == "free"


def test_a_stalled_tail_copy_never_hangs_the_poll(box):
    """Every step of the tail copy runs in one child under one bound: a copy
    that hangs is cut off, the file is still emptied, and the page says the
    tail could not be kept (review finding)."""
    f = _task_output(box)
    _session(box, f)
    shim = box["tmp"] / "shim"
    shim.mkdir()
    # mktemp: the step the old code ran with no bound at all.
    (shim / "mktemp").write_text("#!/bin/sh\nsleep 120\n")
    (shim / "mktemp").chmod(0o755)
    start = time.monotonic()
    out = _respond(box, f, between=f'PATH="{shim}:$PATH"')
    assert time.monotonic() - start < 60
    assert "ACTED=emptied" in out.stdout and f.stat().st_size == 0
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert "the tail could not be kept" in act[0]["body"]


def test_a_truncate_that_leaves_the_file_is_not_called_emptied(box):
    f = _task_output(box)
    _session(box, f)
    # truncate runs under timeout (an executable), so the stub is a program on PATH.
    shim = box["tmp"] / "shim"
    shim.mkdir()
    (shim / "truncate").write_text("#!/bin/sh\nexit 0\n")
    (shim / "truncate").chmod(0o755)
    _respond(box, f, between=f'PATH="{shim}:$PATH"')
    assert f.stat().st_size >= 60 * _MB
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert act[0]["title"] == "Paused a runaway Claude Code task; its output could NOT be emptied"
    assert "the truncate failed" in act[0]["body"]


def test_an_owed_emptying_is_retried_below_the_size_threshold(box):
    """A file paused for growth at 15% of its domain whose emptying could not
    happen on its poll is emptied on a later one, though it no longer grows and
    is under the size threshold (review finding: it was never retried)."""
    f = _task_output(box, mb=60)
    _session(box, f)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="T")
    _proc(box, 201, "grep", 200, f, starttime=2010, state="T")
    key = f"{os.stat(f).st_dev}:{os.stat(f).st_ino}"
    put = (
        "wg_records_lock lk; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        f"wg_record_put 200 2000 bash \"$ts\" {key} '{f}' paused\n"
        f"wg_record_put 201 2010 grep \"$ts\" {key} '{f}' paused\n"
        'wg_records_unlock "$lk"'
    )
    _respond(box, f, grow_mb=0, before=put)
    assert [p for p in _pages(box) if p["title"].startswith("Emptied")], _log(box)


def test_a_paused_record_for_another_file_does_not_empty_this_one(box):
    f = _task_output(box, mb=150)
    _session(box, f)
    _proc(box, 200, "bash", 100, f, starttime=2000, state="T")
    _proc(box, 201, "grep", 200, f, starttime=2010, state="T")
    put = (
        "wg_records_lock lk; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        f"wg_record_put 200 2000 bash \"$ts\" 1:1 '{f}' paused\n"
        f"wg_record_put 201 2010 grep \"$ts\" 1:1 '{f}' paused\n"
        'wg_records_unlock "$lk"'
    )
    _respond(box, f, grow_mb=0, before=put)
    assert f.stat().st_size >= 150 * _MB and not [
        p for p in _pages(box) if p["title"].startswith("Emptied")
    ]


# ── cc-tmp's reserve (D) ──────────────────────────────────────────


def test_cc_tmp_reserve_is_created_green_and_spared_by_the_sweep(box):
    cc_dev = os.stat(box["cc"]).st_dev
    _run(
        box,
        f"CC_KEY='{cc_dev}'; HOME_KEY=none\nhandle_fs '{box['cc']}' '{cc_dev}' green 1800 2048 - '' btrfs",
    )
    reserve = box["cc"] / ".watchgod-reserve"
    try:
        assert reserve.stat().st_size == 102 * _MB  # 5% of 2048, under the 128 MB cap
        out = _run(box, '_cc_sweep_units "$CC_TMP_DIR" | tr "\\0" "\\n"')
        assert ".watchgod-reserve" not in out.stdout
    finally:
        reserve.unlink(missing_ok=True)


def test_cc_tmp_red_releases_the_reserve_unless_the_runaway_response_relieved_it(box):
    cc_dev = os.stat(box["cc"]).st_dev
    reserve = box["cc"] / ".watchgod-reserve"
    with open(reserve, "wb") as fh:
        fh.truncate(16 * _MB)
    held = _run(
        box,
        f"""CC_KEY='{cc_dev}'; HOME_KEY=none; RUNAWAY_RELIEVED[{cc_dev}]=1
handle_fs '{box["cc"]}' '{cc_dev}' red 10 2048 - '' btrfs""",
    )
    assert reserve.exists(), held.stdout
    _run(
        box,
        f"CC_KEY='{cc_dev}'; HOME_KEY=none\nhandle_fs '{box['cc']}' '{cc_dev}' red 10 2048 - '' btrfs",
    )
    assert not reserve.exists()
    body = [p for p in _pages(box) if "RED" in p["title"]][0]["body"]
    assert "reserve held" in body


def test_a_cc_reserve_that_cannot_be_removed_is_reported_not_released(box):
    cc_dev = os.stat(box["cc"]).st_dev
    reserve = box["cc"] / ".watchgod-reserve"
    with open(reserve, "wb") as fh:
        fh.truncate(16 * _MB)
    _run(
        box,
        f"""rm() {{ return 1; }}
CC_KEY='{cc_dev}'; HOME_KEY=none
handle_fs '{box["cc"]}' '{cc_dev}' red 10 2048 - '' btrfs""",
    )
    assert reserve.exists()
    body = [p for p in _pages(box) if "RED" in p["title"]][0]["body"]
    assert "NOT released" in body


def test_the_home_reserve_release_reports_a_failed_removal(box):
    out = _run(
        box,
        """: > "$RESERVE_FILE"
rm() { return 1; }
rc=0; msg="$(reserve_release)" || rc=$?
echo "MSG=$msg RC=$rc"
""",
    )
    assert "NOT released" in out.stdout and "RC=1" in out.stdout


def test_a_reserve_never_reads_as_growth(box):
    """The rate is kept net of the guardian's own reserves: 128 MB of reserve
    appearing between two samples is not the disk filling."""
    out = _run(
        box,
        """r1=$(dg_rate_update k1 1000 1000 fs 0)
r2=$(dg_rate_update k1 1128 1060 fs 128)
r3=$(dg_rate_update k1 1000 1120 fs 0)
echo "R=$r1,$r2,$r3"
# A sample written before the reserve field existed: no step on upgrade.
printf '%s %s %s %s\\n' 1000 3048 0 fs > "$DG_STATE_DIR/rate_k2_fs"
r4=$(dg_rate_update k2 3048 1060 fs 2048)
echo "U=$r4"
""",
    )
    assert "R=0,0,0" in out.stdout, out.stdout
    assert "U=0" in out.stdout, out.stdout


def test_an_emptying_marks_its_domain_relieved(box):
    f = _task_output(box)
    _session(box, f)
    cc_key = f"{os.stat(box['cc']).st_dev}m9"
    home_key = f"{os.stat(box['home'] / 'tmp').st_dev}m8"
    out = _run(
        box,
        f"""
        RW_TIER[{cc_key}]=red
        RW_TIER[{home_key}]=green
        wg_runaway_check '{_domains(box)}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 ))
        python3 -c 'import os,sys; fd=os.open(sys.argv[1], os.O_WRONLY); os.posix_fallocate(fd, 0, 100*1048576)' '{f}'
        RUNAWAY_RELIEVED=()
        wg_runaway_check '{_domains(box)}'
        echo "RELIEVED=${{!RUNAWAY_RELIEVED[*]}}"
        """,
    )
    assert f.stat().st_size == 0
    assert f"RELIEVED={cc_key}" in out.stdout, out.stdout + out.stderr


def test_a_link_where_the_cc_reserve_goes_is_removed_never_followed(box):
    cc_dev = os.stat(box["cc"]).st_dev
    victim = box["tmp"] / "elsewhere" / "victim"
    victim.parent.mkdir()
    (box["cc"] / ".watchgod-reserve").symlink_to(victim)
    _run(
        box,
        f"CC_KEY='{cc_dev}'; HOME_KEY=none\nhandle_fs '{box['cc']}' '{cc_dev}' green 1800 2048 - '' btrfs",
    )
    assert not victim.exists(), "fallocate never followed the link out of cc-tmp"
    assert not (box["cc"] / ".watchgod-reserve").is_symlink()


def test_the_cc_reserve_never_takes_cc_tmp_into_yellow(box):
    """Room for the reserve above ORANGE but not above YELLOW: none is placed
    (YELLOW sheds routing call sites; review finding)."""
    cc_dev = os.stat(box["cc"]).st_dev
    # 2048 MB volume: YELLOW line 307 MB, ORANGE 163 MB, reserve 102 MB.
    _run(
        box,
        f"CC_KEY='{cc_dev}'; HOME_KEY=none\nhandle_fs '{box['cc']}' '{cc_dev}' green 400 2048 - '' btrfs",
    )
    assert not (box["cc"] / ".watchgod-reserve").exists()
