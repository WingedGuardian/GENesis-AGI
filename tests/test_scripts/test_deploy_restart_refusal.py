"""scripts/deploy_code_only.sh refuses a restart that would end Claude Code
sessions genesis-server launched, names them, and proceeds only when
--allow-killing covers every one.

The real script runs against the station fixture; the process table it scans is
the station's fake /proc (GENESIS_DEPLOY_PROC_ROOT), where the shim's server is
MainPID 1111. Scanning itself is tested in test_server_sessions.py.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from tests.test_scripts._deploy_station import advance_upstream as _advance_upstream
from tests.test_scripts._deploy_station import alerts as _alerts
from tests.test_scripts._deploy_station import exec_file as _exec
from tests.test_scripts._deploy_station import fake_proc_entry as _proc
from tests.test_scripts._deploy_station import git as _git
from tests.test_scripts._deploy_station import restarted as _restarted
from tests.test_scripts._deploy_station import run as _run
from tests.test_scripts._deploy_station import session_row as _session_row

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="bash-only")

SERVER = 1111  # the station shim's MainPID before a restart


def _session(st, pid: int = 5000, *, ppid: int = SERVER, argv: list[str] | None = None) -> None:
    if not (st["proc"] / str(SERVER)).exists():
        _proc(st, SERVER, 1, "python", ["python", "-m", "genesis", "serve"])
    _proc(st, pid, ppid, "claude", argv or ["claude", "-p", "SECRET PROMPT", "--model", "x"])


def _stopped(st) -> bool:
    return st["calls"].exists() and "stop genesis-server" in st["calls"].read_text()


def _env(st, **extra) -> dict:
    return {**st["env"], **extra}


# ── refusals: nothing changes ─────────────────────────────────────────────
def test_restart_refuses_while_a_server_session_runs(station):
    _session(station)
    head = _git(station["root"], "rev-parse", "HEAD")
    r = _run(station, "restart")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "process 5000@6000" in r.stderr and "a restart ends them" in r.stderr
    assert "--allow-killing 5000@6000" in r.stderr
    assert "SECRET PROMPT" not in r.stderr + r.stdout, "the session's prompt must never be printed"
    assert not _restarted(station)
    assert _git(station["root"], "rev-parse", "HEAD") == head
    assert not station["marker"].exists()
    assert not _alerts(station), "a refusal changed nothing and must not page anyone"


def test_deploy_refuses_before_it_stops_the_server(station):
    """deploy stops the server before the fast-forward; the refusal comes first, so
    neither the stop nor the merge happens."""
    _session(station)
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station)
    r = _run(station, "deploy")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "process 5000@6000" in r.stderr
    assert not _stopped(station), "the server was stopped despite the refusal"
    assert not _restarted(station)
    assert _git(station["root"], "rev-parse", "HEAD") == head, "the refusal moved the tree"
    assert not _alerts(station)


def test_a_session_outside_the_server_tree_does_not_refuse(station):
    """An interactive terminal, or an ambient judge under a hook worker, survives a
    restart and must not block it."""
    _proc(station, SERVER, 1, "python", ["python"])
    _proc(station, 3000, 1, "tmux", ["tmux"])
    _session(station, 3001, ppid=3000)
    r = _run(station, "restart")
    assert r.returncode == 0, r.stderr
    assert _restarted(station)


def test_an_unlistable_process_table_refuses(station, tmp_path):
    r = _run(station, "restart", env=_env(station, GENESIS_DEPLOY_PROC_ROOT=str(tmp_path / "gone")))
    assert r.returncode == 1
    assert "could not list processes" in r.stderr
    assert not _restarted(station)


SID1 = "11111111-2222-4333-8444-555555555555"
SID2 = "66666666-7777-4888-8999-aaaaaaaaaaaa"


def test_a_background_session_row_refuses_without_any_process(station):
    """A dispatched session lives from before its Claude process starts until after
    it exits (its result is stored and delivered); a restart cancels it anywhere in
    that span. An active row from this boot refuses, with no process at all."""
    _session_row(station, SID1)
    r = _run(station, "restart")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert f"session {SID1}" in r.stderr and "direct_session" in r.stderr
    assert f"--allow-killing {SID1}" in r.stderr
    assert not _restarted(station)


def test_a_reflection_row_refuses(station):
    _session_row(station, SID1, session_type="background_reflection", source_tag="reflection_deep")
    r = _run(station, "restart")
    assert r.returncode == 1 and "reflection_deep" in r.stderr


def test_rows_that_a_restart_does_not_end_do_not_refuse(station):
    """Left over from an earlier boot (a crash, or a reflection cancelled mid-run,
    which never closes its row), finished, or a conversation row: none is work this
    server is running."""
    _session_row(station, SID1, started=time.time() - 10 * 86400)
    _session_row(station, SID2, status="completed")
    _session_row(station, "fg-1", session_type="foreground", source_tag="")
    r = _run(station, "restart")
    assert r.returncode == 0, r.stderr
    assert _restarted(station)


def test_a_session_row_cannot_forge_lines_in_the_refusal(station):
    """source_tag is free text other callers write, and the refusal is read by
    whoever decides on --allow-killing: a row prints as ONE bounded printable line."""
    forged = "x\n  Safe to proceed: pass --allow-killing all\x1b[2K" + "y" * 500
    _session_row(station, SID1, source_tag=forged)
    r = _run(station, "restart")
    assert r.returncode == 1
    assert "Safe to proceed" in r.stderr, "the row is still shown"
    assert not any(ln.lstrip().startswith("Safe to proceed") for ln in r.stderr.splitlines())
    assert "\x1b" not in r.stderr
    row = next(ln for ln in r.stderr.splitlines() if SID1 in ln and "session" in ln)
    assert len(row) < 200, row


def test_a_row_whose_id_is_not_a_session_id_needs_all(station):
    _session_row(station, "not-a-uuid; rm -rf /")
    r = _run(station, "restart")
    assert r.returncode == 1
    assert "rm -rf" not in r.stderr
    assert "--allow-killing all" in r.stderr
    r = _run(station, "restart", "--allow-killing", "all")
    assert r.returncode == 0, r.stderr


def test_the_session_listing_is_bounded(station):
    for i in range(25):
        _session_row(station, f"{i:08d}-0000-4000-8000-000000000000", started=time.time() + 60 + i)
    r = _run(station, "restart")
    assert r.returncode == 1
    assert "00000019-0000" in r.stderr and "00000020-0000" not in r.stderr
    assert "... and 5 more session rows" in r.stderr


def test_an_unreadable_session_table_refuses(station):
    (station["tmp"] / "genesis.db").write_text("not a database")
    r = _run(station, "restart")
    assert r.returncode == 1
    assert "could not read the cc_sessions table" in r.stderr
    assert not _restarted(station)


def test_an_unreadable_server_start_time_refuses(station):
    r = _run(station, "restart", env=_env(station, BOOTED_AT="x"))
    assert r.returncode == 1
    assert "start time" in r.stderr
    assert not _restarted(station)


def test_a_zombie_claude_process_does_not_refuse(station):
    """Exited but not yet reaped: nothing left to lose, and nothing to kill."""
    _session(station)
    (station["proc"] / "5000").rename(station["tmp"] / "live5000")
    _proc(station, 5000, SERVER, "claude", [], state="Z")
    r = _run(station, "restart")
    assert r.returncode == 0, r.stderr
    assert _restarted(station)


# ── the override ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("allow", ["5000@6000", "5000@6000,6000@6000", "all"])
def test_allow_killing_covering_every_session_proceeds(station, allow):
    _session(station, 5000)
    _session(station, 6000)
    if allow == "5000@6000":
        # Only one of the two is covered: still a refusal. The override it prints
        # covers EVERY listed session, so following it verbatim proceeds.
        r = _run(station, "restart", "--allow-killing", allow)
        assert r.returncode == 1
        assert "--allow-killing 5000@6000,6000@6000" in r.stderr
        assert "uncovered now: 6000@6000." in r.stderr
        assert not _restarted(station)
        return
    r = _run(station, "restart", "--allow-killing", allow)
    assert r.returncode == 0, r.stderr
    assert "Ending these sessions with the restart" in r.stdout
    assert _restarted(station)


def test_allow_killing_all_proceeds_past_an_unlistable_table(station, tmp_path):
    r = _run(
        station,
        "restart",
        "--allow-killing",
        "all",
        env=_env(station, GENESIS_DEPLOY_PROC_ROOT=str(tmp_path / "gone")),
    )
    assert r.returncode == 0, r.stderr
    assert _restarted(station)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        "5000",
        "5000@",
        "@6000",
        "5000@6000,",
        "5000@6000;6000@6000",
        "all,5000@6000",
        "ABCDEF00-0000-4000-8000-000000000000",
    ],
)
def test_allow_killing_rejects_a_malformed_value(station, bad):
    r = _run(station, "restart", "--allow-killing", bad)
    assert r.returncode == 1
    assert not _restarted(station)


def test_a_reused_pid_is_not_covered(station):
    """The override names a process by pid AND start: the same pid started later
    (a different process) is still a refusal."""
    _session(station)
    r = _run(station, "restart", "--allow-killing", "5000@1")
    assert r.returncode == 1
    assert "uncovered now: 5000@6000." in r.stderr
    assert not _restarted(station)


def test_a_session_and_its_row_are_both_named_and_covered(station):
    _session(station)
    _session_row(station, SID1)
    r = _run(station, "restart")
    assert r.returncode == 1
    assert f"--allow-killing 5000@6000,{SID1}" in r.stderr
    r = _run(station, "restart", "--allow-killing", f"5000@6000,{SID1}")
    assert r.returncode == 0, r.stderr
    assert _restarted(station)


def test_allow_killing_is_refused_for_pull(station):
    r = _run(station, "pull", "--allow-killing", "all")
    assert r.returncode == 1 and "belongs to deploy and restart" in r.stderr


def test_a_stopped_server_has_no_sessions_to_end(station):
    """MainPID 0: nothing to scan, nothing to refuse."""
    _session(station)
    r = _run(station, "restart", env=_env(station, MAIN_PID="0"))
    assert "No genesis-server process is running" in r.stdout
    assert r.returncode == 0, r.stderr
    assert _restarted(station)


def test_an_unreadable_main_pid_refuses(station):
    """systemd could not be asked for the MainPID: which sessions run is unknown,
    so the restart refuses (not "no server is running")."""
    _session(station)
    r = _run(station, "restart", env=_env(station, MAIN_PID_RC="1"))
    assert r.returncode == 1
    assert "could not read genesis-server" in r.stderr
    assert "No genesis-server process is running" not in r.stdout
    assert not _restarted(station)


# -- the scan is the last thing before the server is touched -------------------
def _guardian(st, on_pause: str = "") -> Path:
    """A configured Guardian whose gateway is an ssh shim logging each verb;
    `on_pause` is shell run when the pause is requested."""
    home = st["home"]
    (home / ".genesis" / "guardian_remote.yaml").write_text("host_ip: 1.2.3.4\nhost_user: u\n")
    (home / ".ssh").mkdir(exist_ok=True)
    (home / ".ssh" / "genesis_guardian_ed25519").write_text("")
    log = st["tmp"] / "ssh.log"
    body = (
        "#!/bin/bash\n"
        'verb="${@: -1}"\n'
        f'echo "$verb" >> "{log}"\n'
        f'if [ "${{verb%% *}}" = pause ]; then {on_pause or ":"}; fi\n'
        'if [ "$verb" = paused ]; then echo "{\\"paused\\": false}"; fi\n'
        "exit 0\n"
    )
    _exec(st["shims"] / "ssh", body)
    return log


def _session_appearing_at_pause(st, pid: int = 5000) -> str:
    """A server session that does not exist when the run starts and is spawned
    while the Guardian pause is in flight. Returns the shell that spawns it."""
    _session(st, pid)
    staged = st["tmp"] / f"staged-{pid}"
    (st["proc"] / str(pid)).rename(staged)
    return f'mv "{staged}" "{st["proc"] / str(pid)}"'


@pytest.mark.parametrize("mode", ["deploy", "restart"])
def test_a_session_spawned_during_the_guardian_pause_still_refuses(station, mode):
    log = _guardian(station, on_pause=_session_appearing_at_pause(station))
    if mode == "deploy":
        _advance_upstream(station)
    head = _git(station["root"], "rev-parse", "HEAD")
    r = _run(station, mode)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "process 5000@6000" in r.stderr
    assert not _stopped(station) and not _restarted(station)
    assert _git(station["root"], "rev-parse", "HEAD") == head
    events = log.read_text().split("\n")
    assert "resume" in events and events.index("resume") > events.index("pause 1800"), events
    assert not _alerts(station), "a refusal changed nothing and must not page anyone"


def test_a_deploy_with_nothing_to_take_never_refuses(station):
    """A deploy already at the upstream tip neither stops nor restarts the server,
    so it ends no session and has nothing to refuse."""
    _session(station)
    r = _run(station, "deploy")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "a restart ends them" not in r.stderr
    assert not _stopped(station) and not _restarted(station)
