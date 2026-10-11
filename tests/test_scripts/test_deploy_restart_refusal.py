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
from tests.test_scripts._deploy_station import inflight_report as _report
from tests.test_scripts._deploy_station import restarted as _restarted
from tests.test_scripts._deploy_station import run as _run

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


def _calls_text(st) -> str:
    """Every command line the shims saw (systemctl calls and the health curls)."""
    out = ""
    calls = st["calls"]
    if calls.exists():
        out += calls.read_text()
    for name in ("curl_args", "inflight_args"):
        args = st["tmp"] / name
        if args.exists():
            out += args.read_text()
    return out


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


def _item(
    iid: str = SID1,
    kind: str = "direct_session",
    label: str = "direct_session (observe)",
    age: int = 120,
):
    return {"id": iid, "kind": kind, "label": label, "started_at": time.time() - age}


def test_work_the_server_reports_refuses_without_any_process(station):
    """A dispatched session is cancelled by a restart from before its Claude
    process starts until after its result is delivered: what the SERVER reports
    refuses, with no process at all."""
    r = _run(station, "restart", env=_report(station, [_item()]))
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert f"direct_session {SID1}" in r.stderr and "running 2m" in r.stderr
    assert f"--allow-killing {SID1}" in r.stderr
    assert not _restarted(station)
    sent = (station["tmp"] / "inflight_headers").read_text()
    assert "Authorization: Bearer station-token" in sent, "the token goes on stdin"
    assert "station-token" not in _calls_text(station), "never on a command line"


def test_overriding_the_reported_work_proceeds(station):
    r = _run(station, "restart", "--allow-killing", SID1, env=_report(station, [_item()]))
    assert r.returncode == 0, r.stderr
    assert "Ending these sessions with the restart" in r.stdout
    assert _restarted(station)


def test_an_old_server_without_the_report_falls_back_to_the_process_scan(station):
    """The first deploy of this change runs against a server that predates the
    endpoint (404): the process scan alone decides, and says so."""
    r = _run(station, "restart", env=_env(station, INFLIGHT_CODE="404"))
    assert r.returncode == 0, r.stderr
    assert "predates its in-flight report" in r.stdout
    assert _restarted(station)


def test_an_old_server_still_refuses_on_a_process_it_launched(station):
    _session(station)
    r = _run(station, "restart", env=_env(station, INFLIGHT_CODE="404"))
    assert r.returncode == 1 and "process 5000@6000" in r.stderr
    assert not _restarted(station)


@pytest.mark.parametrize("code", ["000", "403", "500"])
def test_a_server_that_cannot_be_asked_refuses(station, code):
    r = _run(station, "restart", env=_env(station, INFLIGHT_CODE=code))
    assert r.returncode == 1
    assert "could not ask genesis-server what it is running" in r.stderr
    assert not _restarted(station)
    r = _run(station, "restart", "--allow-killing", "all", env=_env(station, INFLIGHT_CODE=code))
    assert r.returncode == 0, r.stderr


def test_an_unreadable_report_refuses(station):
    bad = station["tmp"] / "bad.json"
    bad.write_text("<html>not json</html>")
    r = _run(station, "restart", env=_env(station, INFLIGHT_FILE=str(bad)))
    assert r.returncode == 1
    assert "in-flight report could not be read" in r.stderr
    assert not _restarted(station)


@pytest.mark.parametrize("code", ["200", "404"])
def test_a_port_the_server_does_not_own_is_never_asked(station, code):
    """Another listener on the port could take the token, or answer 404 and pass
    for a server that predates the report: it is asked nothing and refuses."""
    env = _env(station, PROBE_SERVER_FOREIGN="1", INFLIGHT_CODE=code)
    r = _run(station, "restart", env=env)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "listens on port 5000" in r.stderr
    assert not (station["tmp"] / "inflight_headers").exists(), "the token was sent"
    assert not _restarted(station)
    r = _run(station, "restart", "--allow-killing", "all", env=env)
    assert r.returncode == 0, r.stderr


def test_a_missing_token_refuses(station):
    (station["home"] / ".genesis" / "internal_api_token").unlink()
    r = _run(station, "restart")
    assert r.returncode == 1
    assert "internal API token" in r.stderr
    assert not _restarted(station)


def _server_environ(st, **env: str) -> None:
    """Give the server process (MainPID) an environment, as /proc shows it."""
    if not (st["proc"] / str(SERVER)).exists():
        _proc(st, SERVER, 1, "python", ["python", "-m", "genesis", "serve"])
    blob = b"".join(f"{k}={v}".encode() + b"\x00" for k, v in env.items())
    (st["proc"] / str(SERVER) / "environ").write_bytes(blob)


def test_the_token_comes_from_the_servers_own_genesis_home(station, tmp_path):
    """The server can take GENESIS_HOME from its unit's EnvironmentFile, which the
    deploy shell never sees: the token is read where the SERVER keeps it."""
    server_home = tmp_path / "relocated"
    server_home.mkdir()
    (server_home / "internal_api_token").write_text("server-token\n")
    _server_environ(station, HOME=str(station["home"]), GENESIS_HOME=str(server_home))
    r = _run(station, "restart", env=_report(station, [_item()]))
    assert r.returncode == 1, (r.stdout, r.stderr)
    sent = (station["tmp"] / "inflight_headers").read_text()
    assert "Bearer server-token" in sent and "station-token" not in sent


def test_a_server_without_genesis_home_uses_its_own_home(station, tmp_path):
    other = tmp_path / "server-user-home"
    (other / ".genesis").mkdir(parents=True)
    (other / ".genesis" / "internal_api_token").write_text("home-token\n")
    _server_environ(station, HOME=str(other))
    r = _run(station, "restart", env=_report(station, [_item()]))
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "Bearer home-token" in (station["tmp"] / "inflight_headers").read_text()


def test_a_tilde_genesis_home_expands_against_the_servers_home(station, tmp_path):
    other = tmp_path / "server-user-home"
    (other / "gh").mkdir(parents=True)
    (other / "gh" / "internal_api_token").write_text("tilde-token\n")
    _server_environ(station, HOME=str(other), GENESIS_HOME="~/gh")
    r = _run(station, "restart", env=_report(station, [_item()]))
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "Bearer tilde-token" in (station["tmp"] / "inflight_headers").read_text()


def test_reported_text_cannot_forge_lines_in_the_refusal(station):
    """The report carries labels other code wrote, and the refusal is read by
    whoever decides on --allow-killing: an item prints as ONE bounded printable line."""
    forged = "x\n  Safe to proceed: pass --allow-killing all\x1b[2K" + "y" * 500
    r = _run(station, "restart", env=_report(station, [_item(label=forged)]))
    assert r.returncode == 1
    assert "Safe to proceed" in r.stderr, "the item is still shown"
    assert not any(ln.lstrip().startswith("Safe to proceed") for ln in r.stderr.splitlines())
    assert "\x1b" not in r.stderr
    row = next(ln for ln in r.stderr.splitlines() if SID1 in ln and "direct_session" in ln)
    assert len(row) < 200, row


def test_an_item_whose_id_cannot_be_named_needs_all(station):
    r = _run(station, "restart", env=_report(station, [_item(iid="x; rm -rf /")]))
    assert r.returncode == 1
    assert "rm -rf" not in r.stderr
    assert "--allow-killing all" in r.stderr
    r = _run(
        station,
        "restart",
        "--allow-killing",
        "all",
        env=_report(station, [_item(iid="x; rm -rf /")]),
    )
    assert r.returncode == 0, r.stderr


def test_an_unnameable_item_beside_named_ones_recommends_all(station):
    """Naming the nameable items cannot cover the one that has no safe name, so
    the hint must not offer them: the next run would refuse again."""
    items = [_item(), _item(iid="x; rm -rf /", kind="claude")]
    r = _run(station, "restart", env=_report(station, items))
    assert r.returncode == 1
    assert "only --allow-killing all covers them" in r.stderr
    assert f"--allow-killing {SID1}" not in r.stderr


def test_the_reported_listing_is_bounded(station):
    items = [_item(iid=f"claude-{i}", kind="claude", age=1000 - i) for i in range(25)]
    r = _run(station, "restart", env=_report(station, items))
    assert r.returncode == 1
    assert "claude-19 " in r.stderr and "claude-20 " not in r.stderr
    assert "... and 5 more items the server reported" in r.stderr


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
        "5000@",
        "@6000",
        "5000@6000,",
        ",claude-1",
        "5000@6000;6000@6000",
        "-claude-1",
        "a b",
        "$(id)",
        "x" * 65,
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
    env = _report(station, [_item()])
    r = _run(station, "restart", env=env)
    assert r.returncode == 1
    assert f"--allow-killing 5000@6000,{SID1}" in r.stderr
    r = _run(station, "restart", "--allow-killing", f"5000@6000,{SID1}", env=env)
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
