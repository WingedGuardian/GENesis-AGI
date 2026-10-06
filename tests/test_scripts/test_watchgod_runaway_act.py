"""The runaway response: pause the command, empty its Claude Code task output.

The one place the disk guardian stops a process and empties a live file, so the
tests pin its narrowness from every side:
  * it acts only on cc-tmp/claude-<uid>/<project>/<session>/tasks/<id>.output,
    only while cc-tmp's domain is ORANGE or RED, only in act mode;
  * every holder must descend from a `claude` process and not be one;
  * holders are paused parents-first and recorded for `scripts/watchgod thaw`;
  * the file is emptied, a tail kept only where there is room;
  * the domain is marked relieved, so cc-tmp's reserve waits a poll;
  * against REAL processes: the writer is really stopped, the stand-in session
    is not, the file is emptied, and thaw resumes exactly what was paused.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_WATCHGOD = _ROOT / "scripts" / "tmp_watchgod.sh"
_CLI = _ROOT / "scripts" / "watchgod"
_MB = 1024 * 1024


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
    with open(f, "ab") as fh:
        fh.write(b"head of the output\n")
        fh.truncate(mb * _MB)
    return f


def _proc(
    box, pid: int, comm: str, ppid: int, target: Path | None = None, starttime: int = 1000
) -> None:
    d = box["proc"] / str(pid)
    (d / "fd").mkdir(parents=True, exist_ok=True)
    (d / "fdinfo").mkdir(exist_ok=True)
    (d / "comm").write_text(comm + "\n")
    (d / "status").write_text(f"Name:\t{comm}\nState:\tS (sleeping)\nPPid:\t{ppid}\n")
    (d / "cmdline").write_bytes(comm.encode() + b"\0")
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(starttime)]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
    if target is not None:
        (d / "fd" / "1").symlink_to(target)
        (d / "fdinfo" / "1").write_text("pos:\t0\nflags:\t0102001\n")


def _domains(box, tier: str = "red", total: int = 200, home_tier: str = "green") -> str:
    cc_dev = os.stat(box["cc"]).st_dev
    home_dev = os.stat(box["home"] / "tmp").st_dev
    lines = [f"{cc_dev}m9 {cc_dev} {total} {tier} {box['cc']}"]
    if home_dev != cc_dev:
        lines.append(f"{home_dev}m8 {home_dev} 100000 {home_tier} {box['home']}")
    else:
        # One device in the sandbox: the longer path wins for each file.
        lines.append(f"{cc_dev}m8 {cc_dev} 100000 {home_tier} {box['home']}")
    return "\n".join(lines)


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


def _run(box, snippet: str, act: int = 1, proc: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        HOME=str(box["home"]),
        GENESIS_ALERT_QUEUE_ROOT=str(box["queue"]),
        DG_PROC=str(proc or box["proc"]),
        WG_KILLS=str(box["tmp"] / "kills"),
    )
    script = (
        f"set -euo pipefail\nsource '{_WATCHGOD}'\nload_config\nWATCHGOD_ACT={act}\n"
        f"CC_TMP_DIR='{box['cc']}'\n"
        f'mkdir -p "$DG_STATE_DIR"\n{snippet}\n'
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


def _pages(box) -> list[dict]:
    if not box["queue"].exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(box["queue"].glob("*.json"))]


def _kills(box) -> list[str]:
    f = box["tmp"] / "kills"
    return f.read_text().splitlines() if f.exists() else []


def _frozen(box) -> list[list[str]]:
    f = box["home"] / ".genesis" / "watchgod" / "frozen"
    return [line.split("\t") for line in f.read_text().splitlines()] if f.exists() else []


def _session(box, f: Path) -> None:
    """claude (100) -> bash (200, holds the file) -> grep (201, holds it too)."""
    _proc(box, 100, "claude", 1)
    _proc(box, 200, "bash", 100, f, starttime=2000)
    _proc(box, 201, "grep", 200, f, starttime=2010)


def _respond(box, tier="red", act=1, home_tier="green") -> subprocess.CompletedProcess:
    return _run(
        box,
        _KILL_STUB
        + f"""
        wg_runaway_check '{_domains(box, tier=tier, home_tier=home_tier)}'
        echo "RELIEVED=${{!RUNAWAY_RELIEVED[*]}}"
    """,
        act=act,
    )


# ── when it acts ──────────────────────────────────────────────────


@pytest.mark.parametrize("tier", ["orange", "red"])
def test_a_runaway_task_output_is_paused_parents_first_and_emptied(box, tier):
    f = _task_output(box)
    _session(box, f)
    out = _respond(box, tier=tier)
    assert _kills(box) == ["kill -STOP 200", "kill -STOP 201"], (
        "parent before child, session untouched"
    )
    assert f.stat().st_size == 0
    assert [r[0] for r in _frozen(box)] == ["200", "201"]
    assert [r[1] for r in _frozen(box)] == ["2000", "2010"], "start times recorded for thaw"
    assert "RELIEVED=" in out.stdout and out.stdout.strip() != "RELIEVED="
    act = [p for p in _pages(box) if p["title"].startswith("Paused a runaway Claude Code task")]
    assert len(act) == 1
    body = act[0]["body"]
    assert "pid 200 201" in body and "scripts/watchgod thaw all" in body
    assert "session itself was NOT paused" in body and "Claude Code session: sess-1" in body
    detect = [p for p in _pages(box) if p["title"].startswith("Runaway file on")]
    assert len(detect) == 1 and "paused its writers and emptied it" in detect[0]["body"]


def test_the_tail_is_kept_where_there_is_room(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box, home_tier="green")
    tails = list((box["home"] / "tmp" / "watchgod-truncated").glob("b1.output.*.tail"))
    assert len(tails) == 1
    assert tails[0].stat().st_size == _MB


def test_no_tail_is_kept_on_a_disk_in_trouble_but_the_file_is_still_emptied(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box, home_tier="red")
    assert not (box["home"] / "tmp" / "watchgod-truncated").exists()
    assert f.stat().st_size == 0
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert "no tail kept" in act[0]["body"]


def test_a_pause_that_cannot_be_recorded_still_happens_and_says_how_to_resume(box):
    f = _task_output(box)
    _session(box, f)
    state = box["home"] / ".genesis" / "watchgod"
    state.mkdir(parents=True)
    state.chmod(0o555)
    try:
        _run(box, _KILL_STUB + f"wg_runaway_check '{_domains(box)}'")
    finally:
        state.chmod(0o755)
    assert _kills(box) == ["kill -STOP 200", "kill -STOP 201"]
    body = [p for p in _pages(box) if p["title"].startswith("Paused")][0]["body"]
    assert "Not recorded" in body and "kill -CONT 200 201" in body


# ── when it does NOT act ──────────────────────────────────────────


@pytest.mark.parametrize("tier", ["green", "yellow"])
def test_no_action_before_cc_tmp_is_orange(box, tier):
    f = _task_output(box)
    _session(box, f)
    _respond(box, tier=tier)
    assert not _kills(box)
    assert f.stat().st_size == 60 * _MB
    assert [p for p in _pages(box) if p["title"].startswith("Runaway file on")]


def test_observe_mode_pauses_nothing_and_empties_nothing(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box, act=0)
    assert not _kills(box)
    assert f.stat().st_size == 60 * _MB
    log = (box["home"] / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "OBSERVE: would pause the writers of" in log


@pytest.mark.parametrize(
    "rel",
    [
        "claude-1000/-proj/sess-1/scratch.log",  # not a task output
        "claude-1000/-proj/sess-1/tasks/sub/b1.output",  # one level too deep
        "claude-1000/-proj/sess-1/tasks/b1.log",  # wrong suffix
        "claude-x/-proj/sess-1/tasks/b1.output",  # not a claude-<uid> container
        "other/-proj/sess-1/tasks/b1.output",
        "claude-1000/-proj/sess-1/tasks/d.output/inner",  # inside a dir named like one
    ],
)
def test_only_the_exact_task_output_shape_is_acted_on(box, rel):
    f = _task_output(box, rel=rel)
    _session(box, f)
    _respond(box)
    assert not _kills(box)
    assert f.stat().st_size == 60 * _MB


def test_a_task_output_shape_outside_cc_tmp_is_not_acted_on(box):
    elsewhere = box["tmp"] / "elsewhere"
    f = elsewhere / "claude-1000/-proj/sess-1/tasks/b1.output"
    f.parent.mkdir(parents=True)
    with open(f, "ab") as fh:
        fh.truncate(60 * _MB)
    _session(box, f)
    _respond(box)
    assert not _kills(box)


def test_a_holder_that_is_claude_itself_blocks_the_response(box):
    """Even a claude under another claude (a nested session) is never paused."""
    f = _task_output(box)
    _proc(box, 100, "claude", 1)
    _proc(box, 150, "claude", 100, f)
    _proc(box, 200, "bash", 150, f)
    _respond(box)
    assert not _kills(box)
    assert f.stat().st_size == 60 * _MB


def test_a_holder_with_no_claude_ancestor_blocks_the_response(box):
    f = _task_output(box)
    _proc(box, 100, "claude", 1)
    _proc(box, 200, "bash", 100, f)
    _proc(box, 300, "rsync", 1, f)  # not under any session
    _respond(box)
    assert not _kills(box)


def test_an_already_paused_holder_is_not_recorded_twice(box):
    f = _task_output(box)
    _session(box, f)
    _respond(box)
    with open(f, "ab") as fh:  # the unpaused writer refilled it
        fh.truncate(60 * _MB)
    _respond(box)
    assert [r[0] for r in _frozen(box)] == ["200", "201"]


# ── the cc-tmp reserve ────────────────────────────────────────────


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


# ── against real processes ────────────────────────────────────────


def _state(pid: int) -> str:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("State:"):
            return line.split()[1]
    return "?"


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs a Linux /proc")
def test_real_processes_are_paused_emptied_and_thawed(box):
    """The incident shape, with real processes: a stand-in `claude` session
    runs a command whose output is a cc-tmp task file. The command is stopped,
    the session is not, the file is emptied, and thaw resumes the command."""
    f = _task_output(box)
    stand_in = box["tmp"] / "bin" / "claude"
    stand_in.parent.mkdir()
    stand_in.symlink_to(shutil.which("bash"))
    # The inner bash holds the output (append mode, as Claude Code opens it);
    # the trailing `wait` keeps the stand-in session alive as its parent.
    session = subprocess.Popen(
        [str(stand_in), "-c", f"bash -c 'while :; do sleep 0.2; done' >> '{f}' & wait"],
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        writer = None
        while time.time() < deadline and writer is None:
            for d in Path("/proc").iterdir():
                if d.name.isdigit():
                    try:
                        # The long-lived loop shell; its `sleep` children hold
                        # the file too but live 0.2 s.
                        if (
                            os.readlink(d / "fd" / "1") == str(f)
                            and (d / "comm").read_text().strip() == "bash"
                        ):
                            writer = int(d.name)
                    except OSError:
                        pass
            time.sleep(0.1)
        assert writer, "the writer never appeared"
        _run(box, f"wg_runaway_check '{_domains(box)}'", proc=Path("/proc"))
        assert _state(writer) == "T", "the writer is paused"
        assert _state(session.pid) != "T", "the stand-in session is not"
        assert f.stat().st_size == 0
        env = dict(os.environ, HOME=str(box["home"]))
        out = subprocess.run(
            [str(_CLI), "thaw", "all"], env=env, capture_output=True, text=True, timeout=30
        )
        assert f"resumed pid {writer}" in out.stdout
        assert _state(writer) != "T"
    finally:
        subprocess.run(["pkill", "-CONT", "-P", str(session.pid)], check=False)
        subprocess.run(["pkill", "-P", str(session.pid)], check=False)
        session.kill()
        session.wait(timeout=10)


def test_thaw_never_signals_a_recycled_pid(box, tmp_path):
    state = box["home"] / ".genesis" / "watchgod"
    state.mkdir(parents=True)
    me = os.getpid()
    (state / "frozen").write_text(f"{me}\t1\tpython\t2026-01-01T00:00:00Z\tk\t/x\n")
    env = dict(os.environ, HOME=str(box["home"]))
    out = subprocess.run(
        [str(_CLI), "thaw", "all"], env=env, capture_output=True, text=True, timeout=30
    )
    assert "no longer running — record dropped" in out.stdout
    assert (state / "frozen").read_text() == ""


def test_a_holder_that_already_exited_does_not_block_the_response(box):
    """A loop's short-lived child holds the output too; gone by the ancestry
    check, it must be skipped, not abort the response (found by the
    real-process test: 1 run in 3 never acted)."""
    f = _task_output(box)
    _session(box, f)
    # The walk also reports pid 99999, which has no /proc entry by the time the
    # response checks its ancestry.
    gone = r"""
eval "$(declare -f _rw_scan | sed '1s/^_rw_scan/_rw_scan_real/')"
_rw_scan() {
    _rw_scan_real | awk '$1 != "#rc" && !done { print $1, $2, 99999, 1, $5; done = 1 } { print }'
}
"""
    _run(box, _KILL_STUB + gone + f"wg_runaway_check '{_domains(box)}'")
    assert _kills(box) == ["kill -STOP 200", "kill -STOP 201"]
    assert f.stat().st_size == 0


def test_a_holder_that_exists_without_a_claude_ancestor_still_blocks(box):
    """The skip is for the GONE only: a live non-session holder vetoes."""
    f = _task_output(box)
    _session(box, f)
    _proc(box, 300, "rsync", 1)
    gone = r"""
eval "$(declare -f _rw_scan | sed '1s/^_rw_scan/_rw_scan_real/')"
_rw_scan() {
    _rw_scan_real | awk '$1 != "#rc" && !done { print $1, $2, 300, 1, $5; done = 1 } { print }'
}
"""
    _run(box, _KILL_STUB + gone + f"wg_runaway_check '{_domains(box)}'")
    assert not _kills(box)


# ── review round (PR 2 delta) ─────────────────────────────────────


def test_a_holder_on_two_descriptors_is_paused_and_named_once(box):
    """A command's shell holds its output as fd 1 AND fd 2."""
    f = _task_output(box)
    _session(box, f)
    (box["proc"] / "200" / "fd" / "2").symlink_to(f)
    (box["proc"] / "200" / "fdinfo" / "2").write_text("pos:\t0\nflags:\t0102001\n")
    _respond(box)
    assert _kills(box) == ["kill -STOP 200", "kill -STOP 201"]
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert "pid 200 201." in act[0]["body"]


def test_a_truncate_that_keeps_failing_is_paged_once_not_every_poll(box):
    f = _task_output(box)
    _session(box, f)
    _run(
        box,
        _KILL_STUB
        + f"""
        truncate() {{ return 1; }}
        wg_runaway_check '{_domains(box)}'
        wg_runaway_check '{_domains(box)}'
    """,
    )
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert len(act) == 1 and "could NOT be emptied" in act[0]["body"]
    log = (box["home"] / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "stay paused; the file could not be emptied" in log


def test_the_file_is_emptied_only_through_a_descriptor_still_on_it(box):
    """After the pause, a holder's fd link that no longer leads to the walked
    file (pid recycled) must never be truncated: that is some other file."""
    f = _task_output(box)
    _session(box, f)
    victim = box["cc"] / "victim.db"
    victim.write_bytes(b"x" * 4096)
    swap = rf"""
kill() {{
    echo "kill $*" >> "$WG_KILLS"
    if [[ "$1" == -STOP ]]; then
        sed -i 's/^State:.*/State:\tT (stopped)/' "$DG_PROC/$2/status"
        ln -sfn '{victim}' "$DG_PROC/$2/fd/1"
    fi
    return 0
}}
"""
    _run(box, swap + f"wg_runaway_check '{_domains(box)}'")
    assert victim.stat().st_size == 4096, "an unrelated file was emptied"
    assert f.stat().st_size > 0
    act = [p for p in _pages(box) if p["title"].startswith("Paused")]
    assert "could NOT be emptied" in act[0]["body"]


def test_no_pause_when_the_proc_walk_did_not_complete(box):
    """The 'every holder is a session command' veto is only as complete as the
    walk: an incomplete walk may have missed a holder outside any session."""
    f = _task_output(box)
    _session(box, f)
    _run(
        box,
        _KILL_STUB
        + f"""timeout() {{ while [[ "$1" != find ]]; do shift; done; "$@"; return 124; }}
wg_runaway_check '{_domains(box)}'""",
    )
    assert not _kills(box)
    assert f.stat().st_size > 0
    log = (box["home"] / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "not paused this poll: the /proc walk did not complete" in log


def test_acted_is_per_file_not_per_filesystem(box):
    """Another file already emptied on cc-tmp this poll (the domain is marked
    relieved) must not make a vetoed file's page claim it was acted on."""
    b = _task_output(box, rel="claude-1000/-proj/sess-2/tasks/b2.output")
    _proc(box, 300, "rsync", 1, b)  # no session ancestor: vetoed
    cc_dev = os.stat(box["cc"]).st_dev
    _run(box, _KILL_STUB + f"RUNAWAY_RELIEVED[{cc_dev}m9]=1\nwg_runaway_check '{_domains(box)}'")
    assert not _kills(box) and b.stat().st_size > 0
    detect = [p for p in _pages(box) if p["title"].startswith("Runaway file on")]
    assert len(detect) == 1
    assert "paused its writers and emptied it" not in detect[0]["body"]


def _record(box, pid: int, st: int, hours_ago: float) -> None:
    import datetime as _dt

    ts = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = box["home"] / ".genesis" / "watchgod"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "frozen", "a") as fh:
        fh.write(f"{pid}\t{st}\tbash\t{ts}\t1:2\t/x/tasks/b1.output\n")


def _paused(box, pid: int, st: int) -> None:
    _proc(box, pid, "bash", 1, starttime=st)
    s = box["proc"] / str(pid) / "status"
    s.write_text(s.read_text().replace("S (sleeping)", "T (stopped)"))


def test_a_pause_nobody_resumed_is_paged_again_once_per_period(box):
    _paused(box, 200, 2000)
    _record(box, 200, 2000, hours_ago=7)
    _paused(box, 201, 2010)
    _record(box, 201, 2010, hours_ago=1)  # too recent
    _record(box, 202, 2020, hours_ago=9)  # pid gone
    _paused(box, 203, 9999)
    _record(box, 203, 2030, hours_ago=9)  # pid recycled
    _run(box, f"wg_runaway_check '{_domains(box)}'\nwg_runaway_check '{_domains(box)}'")
    still = [p for p in _pages(box) if p["title"].startswith("Still paused")]
    assert len(still) == 1, "once per period, not every poll"
    body = still[0]["body"]
    assert "pid 200 (bash)" in body
    assert "pid 201" not in body and "pid 202" not in body and "pid 203" not in body
    assert "scripts/watchgod thaw all" in body
