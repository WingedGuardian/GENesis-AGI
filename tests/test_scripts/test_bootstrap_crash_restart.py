"""A crash that stopped genesis-server never leaves it down for the next update (#3451).

bootstrap.sh never starts genesis-server, and update.sh's recovery detection
used to fire only when nothing at all was running and a failure artifact said
so. Four ways that left the server down after a crashed update:

1. a kill after the stop but before the "merging" state recorded nothing about
   the stop (the state on disk was the "fetching" one, written before the
   services were inspected);
2. a running genesis-bridge (the fallback that runs precisely while the server
   is down) made WERE_RUNNING non-empty, so recovery detection never ran;
3. the version collector archives last_update_failure.json, the only signal
   bootstrap's recovery left;
4. the "Already up to date" path exits before recovery detection.

The fix: update.sh records the services it is about to stop in its state AND
writes ~/.genesis/server_restart_owed.json before it stops genesis-server. Only
update.sh retires that file, and only once the server is back (health-checked on
the update paths, active after the pre-stop interrupt handler's restart).
Recovery detection runs whenever genesis-server is not running (a running bridge
no longer hides it) and the no-delta path runs the full activation when a restart
is owed. A server nobody's update stopped stays stopped. bootstrap's crash
recovery still writes last_update_failure.json when the crashed state says the
server was stopped: that is the alert, and the dashboard's record of the crash.

Shell runs extracted from the shipped scripts in a temporary HOME, with FAKE
systemctl/pgrep (and, where no git is wanted, git) first on PATH, from an empty
directory with GIT_CEILING_DIRECTORIES set. bootstrap's recovery runs real git,
against a throwaway repository only. The readers (the dashboard status route and
the genesis_version collector) are called for real against what the writers left.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from tests.test_scripts.test_bootstrap_guards import (
    UPDATE,
    _assert_recovered,
    _history,
    _migrated_genesis_root,
    _recover,
    _recovery_commit_file,
    _recovery_repo,
    _run_crash_recovery,
    _write_recovery_state,
)
from tests.test_scripts.test_update_rollback_recovery import _extract_recovery_block
from tests.test_scripts.test_update_success_records import _block, _no_delta_block


def _failure_file(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".genesis" / "last_update_failure.json"


def _update_sh_failure_keys() -> set[str]:
    """The keys update.sh's own writer puts in last_update_failure.json."""
    text = UPDATE.read_text()
    start = text.index("data = {", text.index("# Write failure context for CC to pick up."))
    block = text[start : text.index("}", start)]
    keys = set(re.findall(r"'(\w+)':\s+sys\.argv", block))
    assert len(keys) == 10, keys
    return keys


def _with_observations(db: Path) -> None:
    """The migration chain alone does not create ``observations`` (bootstrap's
    create_all_tables does); add it from the shipped schema for the readers."""
    import sqlite3

    from genesis.db.schema import TABLES

    con = sqlite3.connect(db)
    try:
        con.executescript(TABLES["observations"])
    finally:
        con.close()


def _crashed_merge(tmp_path: Path, **state):
    root, _, rollback = _recovery_repo(tmp_path)
    deploy = _recovery_commit_file(root, "changed.txt", "deployed\n", "deploy")
    db = _migrated_genesis_root(tmp_path, root)
    proc, state_path = _recover(
        tmp_path,
        root,
        own_head=deploy,
        deploy_head=deploy,
        old_tag="v1.0.0",
        old_commit=rollback[:7],
        started_at="2026-10-18T00:00:00+00:00",
        **state,
    )
    _assert_recovered(proc, state_path, root, rollback)
    _with_observations(db)
    return root, db, deploy, proc


def _update_detects_recovery(tmp_path: Path, root: Path) -> bool:
    """Run update.sh's shipped recovery detection with nothing running, in the
    same HOME and against the same database bootstrap just recovered."""
    harness = (
        "#!/bin/bash\nset -Eeuo pipefail\nWERE_RUNNING=()\n"
        f'GENESIS_ROOT="{root}"\nVENV_DIR="{tmp_path / "missing-venv"}"\n'
        + _extract_recovery_block(UPDATE.read_text())
        + '\nprintf "%s\\n" "${WERE_RUNNING[@]:-}"\n'
    )
    out = subprocess.run(
        ["bash", "-c", harness],
        env={**os.environ, "HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return "genesis-server" in out.stdout.split()


def test_a_crash_that_stopped_the_server_leaves_update_sh_failure_record(tmp_path):
    root, db, deploy, proc = _crashed_merge(
        tmp_path, services_stopped=["genesis-server", "genesis-bridge"]
    )

    failure = _failure_file(tmp_path)
    assert failure.is_file(), proc.stdout + proc.stderr
    data = json.loads(failure.read_text())
    assert set(data) == _update_sh_failure_keys()
    assert data["old_tag"] == "v1.0.0"
    assert data["rollback_tag"] == "rollback"
    assert data["original_branch"] == "main"
    assert data["new_commit"] == deploy[:7]
    assert data["new_tag"] == "untagged"  # the fixture has no v* tag; update.sh's fallback
    assert data["rollback_complete"] is True
    assert "phase 'merging'" in data["reason"] and "bootstrap" in data["reason"]
    assert isinstance(data["timestamp"], str) and data["timestamp"]
    # The update_history row is still the crash row (PR #3446), alongside the file.
    assert [row[0] for row in _history(db)] == ["crashed_recovered"]
    assert "WARNING" not in proc.stdout + proc.stderr


def test_the_dashboard_status_route_reports_the_bootstrap_written_failure(tmp_path, monkeypatch):
    import sqlite3

    from flask import Flask

    from tests._update_routes_sandbox import isolate_update_routes

    root, db, _, _ = _crashed_merge(tmp_path, services_stopped=["genesis-server"])
    written = json.loads(_failure_file(tmp_path).read_text())
    isolate_update_routes(monkeypatch, tmp_path / "home", root)
    from genesis.dashboard.routes import updates

    def plain_query(sql, params=()):
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    with (
        Flask(__name__).test_request_context(),
        patch.object(updates, "_git", return_value=None),
        patch.object(updates, "_query_db", side_effect=plain_query),
    ):
        body = updates.update_status().get_json()
    assert body["last_failure"] == written
    assert body["last_update"]["status"] == "crashed_recovered"


def test_the_version_collector_records_and_archives_the_bootstrap_written_failure(
    tmp_path, monkeypatch
):
    import aiosqlite

    from genesis.learning.signals import genesis_version

    _, db, _, _ = _crashed_merge(tmp_path, services_stopped=["genesis-server"])
    failure = _failure_file(tmp_path)
    written = json.loads(failure.read_text())
    archive = tmp_path / "home" / ".genesis" / "update-failures"
    monkeypatch.setattr(genesis_version, "_FAILURE_FILE", failure)
    monkeypatch.setattr(genesis_version, "_FAILURE_ARCHIVE_DIR", archive)

    async def _consume() -> list[tuple[str, str]]:
        async with aiosqlite.connect(str(db)) as conn:
            collector = genesis_version.GenesisVersionCollector(conn)
            with patch.object(genesis_version, "update_in_progress", return_value=False):
                await collector._check_failure_file()
                # A second tick neither re-reads nor re-records it.
                await collector._check_failure_file()
            cursor = await conn.execute(
                "SELECT type, content FROM observations WHERE source = 'genesis_version'"
            )
            return list(await cursor.fetchall())

    rows = asyncio.run(_consume())
    assert len(rows) == 1 and rows[0][0] == "genesis_update_failed"
    assert json.loads(rows[0][1]) == written
    assert not failure.exists()
    archived = list(archive.glob("last_update_failure.*.json"))
    assert len(archived) == 1 and json.loads(archived[0].read_text()) == written


def test_the_next_update_restores_the_server_the_crash_stopped(tmp_path):
    root, _, _, _ = _crashed_merge(tmp_path, services_stopped=["genesis-server"])
    assert _update_detects_recovery(tmp_path, root) is True


def test_a_crash_before_the_stop_writes_nothing_and_keeps_an_operator_stop(tmp_path):
    """fetching is written before services are inspected, so services_stopped is
    empty: the server's state is the operator's, and the next update keeps it."""
    root, _, rollback = _recovery_repo(tmp_path)
    _migrated_genesis_root(tmp_path, root)
    proc, state_path = _recover(tmp_path, root, phase="fetching", services_stopped=[])

    _assert_recovered(proc, state_path, root, rollback)
    assert not _failure_file(tmp_path).exists()
    assert _update_detects_recovery(tmp_path, root) is False


def test_a_crash_that_stopped_only_the_bridge_writes_nothing(tmp_path):
    _crashed_merge(tmp_path, services_stopped=["genesis-bridge"])
    assert not _failure_file(tmp_path).exists()


def test_an_existing_failure_record_is_left_untouched(tmp_path):
    failure = _failure_file(tmp_path)
    failure.parent.mkdir(parents=True)
    original = '{"reason": "update.sh rolled back", "timestamp": "earlier"}\n'
    failure.write_text(original)

    _crashed_merge(tmp_path, services_stopped=["genesis-server"])
    assert failure.read_text() == original


# ── #3451 rework: update.sh owns the restart it owes ────────────────────────

# Fake systemctl / pgrep / git. A unit is "active" iff FAKE_ACTIVE names it;
# `show` prints FAKE_ACTIVE_ENTER (the unit's ActiveEnterTimestamp); every other
# systemctl call is logged and succeeds. git refuses loudly: none of
# the update.sh slices below may run it. Nothing here reaches a real service
# manager or repository.
_FAKE_TOOL = r"""#!/bin/bash
name="${0##*/}"
# A unit the test stops mid-run reads its running set from a file.
if [ -n "${FAKE_ACTIVE_FILE:-}" ] && [ -f "$FAKE_ACTIVE_FILE" ]; then
    FAKE_ACTIVE="$(cat "$FAKE_ACTIVE_FILE")"
fi
case "$name" in
    git) echo "fake git refused: $*" >&2; exit 97 ;;
    pgrep) [[ " ${FAKE_ACTIVE:-} " == *" genesis-server "* ]]; exit ;;
esac
for arg in "$@"; do
    if [ "$arg" = show ]; then
        # A unit the test restarts mid-run keeps its activation time in a file.
        if [ -n "${FAKE_ACTIVE_ENTER_FILE:-}" ] && [ -f "$FAKE_ACTIVE_ENTER_FILE" ]; then
            cat "$FAKE_ACTIVE_ENTER_FILE"
        else
            printf '%s\n' "${FAKE_ACTIVE_ENTER:-}"
        fi
        exit 0
    fi
    if [ "$arg" = is-active ]; then
        unit="${*: -1}"
        unit="${unit%.service}"
        [[ " ${FAKE_ACTIVE:-} " == *" $unit "* ]]
        exit
    fi
done
printf '%s\n' "$*" >> "${FAKE_LOG:-/dev/null}"
exit 0
"""


def _owed(home: Path) -> Path:
    return home / ".genesis" / "server_restart_owed.json"


def _sh(tmp_path: Path, home: Path, script: str, *, active: str = "", active_enter: str = ""):
    bin_ = tmp_path / "fakebin"
    bin_.mkdir(exist_ok=True)
    for name in ("systemctl", "pgrep", "git"):
        (bin_ / name).write_text(_FAKE_TOOL)
        (bin_ / name).chmod(0o755)
    work = tmp_path / "cwd"
    work.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(home),
        PATH=f"{bin_}{os.pathsep}{env['PATH']}",
        FAKE_ACTIVE=active,
        FAKE_ACTIVE_FILE=str(tmp_path / "active-now"),
        FAKE_ACTIVE_ENTER=active_enter,
        FAKE_ACTIVE_ENTER_FILE=str(tmp_path / "active-enter"),
        FAKE_LOG=str(tmp_path / "fake.log"),
        GIT_CEILING_DIRECTORIES=str(tmp_path),
    )
    return subprocess.run(
        ["bash", "-c", script], cwd=work, env=env, capture_output=True, text=True, timeout=60
    )


def _func(text: str, name: str) -> str:
    m = re.search(rf"\n({re.escape(name)}\(\) \{{\n.*?\n\}}\n)", text, re.DOTALL)
    assert m, f"{name} not found in update.sh"
    return m.group(1)


def _owed_block(text: str) -> str:
    """update.sh's restart-owed block, or nothing for a script without one."""
    m = re.search(r"# BEGIN restart-owed[^\n]*\n(.*?)# END restart-owed", text, re.DOTALL)
    return m.group(1) if m else ""


def _run_update_until_stop(
    tmp_path: Path,
    home: Path,
    *,
    active: str,
    rollback: str,
    on_live: str = "0",
    pre: str = "",
    post_merge: str = "false",
    tail: str = "echo REACHED-END\n",
) -> subprocess.CompletedProcess:
    """update.sh from its "fetching" state through the service stop, as shipped.

    The stop itself is replaced by a SIGKILL of the run: what is on disk
    afterwards is exactly what a power loss at the stop leaves. With nothing to
    stop the slice runs to its end, and the run is taken to die later (so its
    EXIT-trap cleanup never ran either)."""
    text = UPDATE.read_text()
    script = (
        "set -Eeuo pipefail\n"
        'STATE_FILE="$HOME/.genesis/update_state.json"\n'
        'STARTED_AT="2026-10-18T00:00:00+00:00"\n'
        "ROLLBACK_TAG=rollback\nORIGINAL_BRANCH=main\nUPDATE_OWN_HEAD=''\n"
        f"DEPLOY_HEAD={rollback}\nOLD_TAG=v1.0.0\nOLD_COMMIT={rollback[:7]}\n"
        f"POST_MERGE={post_merge}\nON_LIVE={on_live}\n_LIVE_WERE_RUNNING=()\n"
        '_SELF_COPY="$HOME/self-copy"\n'
        # Not a repository; the fake git refuses anything run against it anyway.
        f'GENESIS_ROOT="{tmp_path / "cwd"}"\n'
        + _owed_block(text)
        + _func(text, "_write_state")
        + "_guardian_pause() { :; }\n_guardian_resume() { :; }\n"
        "_drop_own_prestop_state() { :; }\n_flush_deploy_summary() { :; }\n"
        "_live_prestop_exit() { :; }\n_ensure_server_down() { return 0; }\n"
        "_stop_genesis_server() { kill -KILL $$; }\n"
        '_clear_deploy_state() { rm -f "$STATE_FILE"; echo CLEARED; }\n'
        '_live_abandon() { echo "LIVE-ABANDON $1"; exit "$1"; }\n'
        "_live_stop_failed_note() { :; }\n"
        + pre
        # update.sh's own "fetching" write, made before the services are inspected.
        + 'WERE_RUNNING=()\nif [ "$ON_LIVE" != 1 ]; then _write_state "fetching"; fi\n'
        + text[
            text.index("\nWERE_RUNNING=()\n") + 1 : text.index(
                '[[ ${#WERE_RUNNING[@]} -gt 0 ]] && echo "  Stopped:'
            )
        ]
        + tail
    )
    return _sh(tmp_path, home, script, active=active)


def _detect(
    tmp_path: Path, home: Path, root: Path, *were_running: str, active_enter: str = ""
) -> tuple[list[str], str]:
    """update.sh's shipped recovery detection: (WERE_RUNNING after, _OPERATOR_STOP)."""
    script = (
        "set -Eeuo pipefail\n"
        f"WERE_RUNNING=({' '.join(were_running)})\n"
        f'GENESIS_ROOT="{root}"\nVENV_DIR="{tmp_path / "missing-venv"}"\n'
        + _extract_recovery_block(UPDATE.read_text())
        + '\necho "WERE=${WERE_RUNNING[*]:-}"\necho "OPSTOP=$_OPERATOR_STOP"\n'
    )
    r = _sh(tmp_path, home, script, active_enter=active_enter)
    assert r.returncode == 0, r.stdout + r.stderr
    were = re.search(r"^WERE=(.*)$", r.stdout, re.MULTILINE).group(1).split()
    return were, re.search(r"^OPSTOP=(.*)$", r.stdout, re.MULTILINE).group(1)


def _crash_at_stop(tmp_path: Path, active: str = "genesis-server", on_live: str = "0"):
    root, _, rollback = _recovery_repo(tmp_path)
    db = _migrated_genesis_root(tmp_path, root)
    home = tmp_path / "home"
    proc = _run_update_until_stop(tmp_path, home, active=active, rollback=rollback, on_live=on_live)
    return root, db, home, proc


def test_a_kill_at_the_stop_leaves_the_stop_on_disk(tmp_path):
    """Codex P1 (4308850475): the state names what is being stopped, and the
    restart obligation exists, before the physical stop."""
    _, _, home, proc = _crash_at_stop(tmp_path, active="genesis-server genesis-bridge")
    assert proc.returncode == -9, proc.stdout + proc.stderr
    state = json.loads((home / ".genesis" / "update_state.json").read_text())
    assert state["phase"] == "fetching"
    assert state["services_stopped"] == ["genesis-server", "genesis-bridge"]
    assert _owed(home).is_file()


def test_the_next_update_restores_a_server_killed_at_the_stop_without_bootstrap(tmp_path):
    """The usual path: update.sh's prior-state guard lets a "fetching" state
    through and the next run overwrites it, so bootstrap's crash recovery never
    sees it. The obligation update.sh wrote is what restores the server."""
    root, _, home, proc = _crash_at_stop(tmp_path)
    assert proc.returncode == -9, proc.stdout + proc.stderr
    assert _detect(tmp_path, home, root) == (["genesis-server"], "false")


def test_on_live_a_kill_at_the_stop_still_restores_the_server(tmp_path):
    """On `live` no state is written before the stop (by design); the
    obligation still is."""
    root, _, home, proc = _crash_at_stop(tmp_path, on_live="1")
    assert proc.returncode == -9, proc.stdout + proc.stderr
    assert not (home / ".genesis" / "update_state.json").exists()
    assert _detect(tmp_path, home, root) == (["genesis-server"], "false")


def test_the_restore_survives_bootstrap_recovery_and_the_collector_archiving_its_record(
    tmp_path, monkeypatch
):
    """Devin severe (4308836196): bootstrap's recovery (run by hand) records the
    crash; the collector, running in the bridge's runtime, archives that record;
    the next update still restores the server."""
    import aiosqlite

    from genesis.learning.signals import genesis_version

    root, db, home, proc = _crash_at_stop(tmp_path)
    assert proc.returncode == -9, proc.stdout + proc.stderr
    rec = _run_crash_recovery(root, home)
    assert rec.returncode == 0 and "Crash recovery complete" in rec.stdout, rec.stdout + rec.stderr
    failure = _failure_file(tmp_path)
    assert failure.is_file(), rec.stdout + rec.stderr
    # The kill came before the merge: the record says the code was found in place.
    record = json.loads(failure.read_text())
    assert "found the code still at rollback" in record["reason"]
    assert record["rollback_complete"] is True
    _with_observations(db)
    monkeypatch.setattr(genesis_version, "_FAILURE_FILE", failure)
    monkeypatch.setattr(genesis_version, "_FAILURE_ARCHIVE_DIR", home / ".genesis" / "uf")

    async def _consume() -> None:
        async with aiosqlite.connect(str(db)) as conn:
            collector = genesis_version.GenesisVersionCollector(conn)
            with patch.object(genesis_version, "update_in_progress", return_value=False):
                await collector._check_failure_file()

    asyncio.run(_consume())
    assert not failure.exists()
    assert [row[0] for row in _history(db)] == ["crashed_recovered"]
    assert _detect(tmp_path, home, root) == (["genesis-server"], "false")


def test_an_update_that_never_stopped_the_server_keeps_an_operator_stop(tmp_path):
    """The operator stopped the server, then an update crashed: nothing says an
    update stopped it, so it stays stopped (and bootstrap reports nothing)."""
    root, _, home, proc = _crash_at_stop(tmp_path, active="")
    assert proc.returncode == 0 and "REACHED-END" in proc.stdout, proc.stdout + proc.stderr
    state = json.loads((home / ".genesis" / "update_state.json").read_text())
    assert state["phase"] == "fetching" and state["services_stopped"] == []
    assert not _owed(home).exists()
    rec = _run_crash_recovery(root, home)
    assert rec.returncode == 0, rec.stdout + rec.stderr
    assert not _failure_file(tmp_path).exists()
    assert _detect(tmp_path, home, root) == ([], "true")


def test_a_running_bridge_does_not_hide_a_restart_owed(tmp_path):
    """Devin severe (4308836128): the bridge came up while the server was down.
    The server is restored, and listed first so the bridge yields to it."""
    root, _, home, proc = _crash_at_stop(tmp_path, active="genesis-server genesis-bridge")
    assert proc.returncode == -9, proc.stdout + proc.stderr
    assert _detect(tmp_path, home, root, "genesis-bridge") == (
        ["genesis-server", "genesis-bridge"],
        "false",
    )


def test_a_running_bridge_does_not_hide_an_update_failure_record(tmp_path):
    home = tmp_path / "home"
    _failure_file(tmp_path).parent.mkdir(parents=True)
    _failure_file(tmp_path).write_text("{}")
    assert _detect(tmp_path, home, tmp_path / "no-root", "genesis-bridge") == (
        ["genesis-server", "genesis-bridge"],
        "false",
    )


def test_a_bridge_alone_with_nothing_owed_is_an_operator_stop(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    assert _detect(tmp_path, home, tmp_path / "no-root", "genesis-bridge") == (
        ["genesis-bridge"],
        "true",
    )


def _verify_loop() -> str:
    text = UPDATE.read_text()
    start = text.index("    # Verify services are active\n")
    return text[start : text.index("    if [[ ${#SVC_FAILED[@]} -gt 0 ]]; then", start)]


def test_a_bridge_that_yielded_to_the_restored_server_is_not_a_failure(tmp_path):
    """With the server back, the bridge exits by design (it yields): rolling the
    update back for that would undo the recovery."""
    home = tmp_path / "home"
    script = (
        "set -Eeuo pipefail\nWERE_RUNNING=(genesis-server genesis-bridge)\n"
        + _verify_loop()
        + 'echo "FAILED=${SVC_FAILED[*]:-}"\n'
    )
    r = _sh(tmp_path, home, script, active="genesis-server")
    assert r.returncode == 0, r.stderr
    assert "FAILED=\n" in r.stdout, r.stdout
    # A server that is down is still a failure, and so is a bridge with no server.
    r = _sh(tmp_path, home, script, active="")
    assert "FAILED=genesis-server genesis-bridge\n" in r.stdout, r.stdout


def _no_delta_route(
    tmp_path: Path,
    home: Path,
    *were_running: str,
    root: Path | None = None,
    active_enter: str = "",
) -> tuple[str, list[str]]:
    """update.sh's nothing-new dispatch with nothing new, then (if it falls
    through to the full activation) its recovery detection."""
    text = UPDATE.read_text()
    full = _extract_recovery_block(text)
    seg = text[text.find("_OPERATOR_STOP=false") : text.find("# ── Restart services")]
    defs = full[: len(full) - len(seg)]
    start = text.index('if _nothing_new && [ -n "$GENESIS_TAKE_TEMPLATES" ]; then')
    dispatch = text[start : text.index('elif _nothing_new; then\n    echo "  Already up to date')]
    script = (
        "set -Eeuo pipefail\n"
        f"WERE_RUNNING=({' '.join(were_running)})\n"
        f'GENESIS_ROOT="{root or tmp_path / "no-root"}"\nVENV_DIR="{tmp_path / "missing-venv"}"\n'
        'GENESIS_TAKE_TEMPLATES=""\n'
        "_nothing_new() { return 0; }\n_tier2_pending_since_baseline() { return 1; }\n"
        + defs
        + dispatch
        + "elif _nothing_new; then echo ROUTE=no-delta; exit 0\nfi\necho ROUTE=full\n"
        + seg
        + '\necho "WERE=${WERE_RUNNING[*]:-}"\n'
    )
    r = _sh(tmp_path, home, script, active_enter=active_enter)
    assert r.returncode == 0, r.stdout + r.stderr
    route = re.search(r"^ROUTE=(.*)$", r.stdout, re.MULTILINE).group(1)
    m = re.search(r"^WERE=(.*)$", r.stdout, re.MULTILINE)
    return route, (m.group(1).split() if m else [])


def test_no_delta_with_a_restart_owed_runs_the_full_activation(tmp_path):
    """Codex P2 (4308850477): nothing new to merge, but a restart is owed: the
    run goes through bootstrap, migrations and recovery detection, which hands
    genesis-server to the restart and health-check block (and its rollback)."""
    home = tmp_path / "home"
    _with_owed(home)
    assert _no_delta_route(tmp_path, home) == ("full", ["genesis-server"])


def test_no_delta_with_nothing_owed_keeps_an_operator_stop(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    assert _no_delta_route(tmp_path, home) == ("no-delta", [])


def test_no_delta_with_the_server_running_is_unchanged(tmp_path):
    home = tmp_path / "home"
    _with_owed(home)
    assert _no_delta_route(tmp_path, home, "genesis-server") == ("no-delta", [])


# ── only update.sh retires the obligation, once the server is back ──────────


def _with_owed(home: Path) -> None:
    _owed(home).parent.mkdir(parents=True, exist_ok=True)
    # A confirmed stop's record (see the round-3 tests for a "stopping" one).
    _owed(home).write_text('{"stage": "stopped"}')


def test_a_successful_update_retires_the_obligation(tmp_path):
    text = UPDATE.read_text()
    start = text.index("# ── Clear update failure file on success")
    block = text[start : text.index("# ── Record success in update_history", start)]
    home = tmp_path / "home"
    _with_owed(home)
    script = "set -Eeuo pipefail\nWERE_RUNNING=(genesis-server)\n" + _owed_block(text) + block
    r = _sh(tmp_path, home, script)
    assert r.returncode == 0, r.stderr
    assert not _owed(home).exists()


def test_the_no_delta_path_retires_it_only_when_the_server_answers_health(tmp_path):
    text = UPDATE.read_text()
    home = tmp_path / "home"
    for running, healthy, kept in ((True, True, False), (True, False, True), (False, True, True)):
        _with_owed(home)
        script = (
            "set -Eeuo pipefail\n"
            + _block(text, "success-degraded-subsystems")
            + f"_server_health_ok() {{ {'return 0' if healthy else 'return 1'}; }}\n"
            "_latest_update_status() { echo success; }\n"
            "_record_update_history() { :; }\n"
            f"WERE_RUNNING=({'genesis-server' if running else ''})\n"
            'HOST_CC_DEGRADED=""\nPRE_UPDATE_DEGRADED=""\n' + _no_delta_block(text)
        )
        r = _sh(tmp_path, home, script)
        assert r.returncode == 0, r.stderr
        assert _owed(home).exists() is kept, (running, healthy)


def test_the_prestop_handler_retires_it_only_when_the_server_is_back(tmp_path):
    text = UPDATE.read_text()
    home = tmp_path / "home"
    for active, kept in (("genesis-server", False), ("", True)):
        _with_owed(home)
        script = (
            "set -Eeuo pipefail\n"
            + _owed_block(text)
            + "_start_genesis_server() { :; }\n_clear_deploy_state() { :; }\n"
            "_live_abandon() { :; }\nON_LIVE=0\n_LIVE_WERE_RUNNING=()\n"
            "WERE_RUNNING=(genesis-server)\n"
            + _func(text, "_on_signal_prestop")
            + "_on_signal_prestop TERM\n"
        )
        r = _sh(tmp_path, home, script, active=active)
        assert r.returncode == 1, r.stdout + r.stderr
        assert _owed(home).exists() is kept, active


def _crash_after_confirmed_stop(tmp_path: Path):
    """The run stops the server (no restart during the stop), confirms it down,
    and is killed before "merging"."""
    root, _, rollback = _recovery_repo(tmp_path)
    db = _migrated_genesis_root(tmp_path, root)
    home = tmp_path / "home"
    proc = _run_update_until_stop(
        tmp_path,
        home,
        active="genesis-server",
        rollback=rollback,
        pre="_stop_genesis_server() { :; }\n",
        tail="kill -KILL $$\n",
    )
    assert '"stage": "stopped"' in _owed(home).read_text()
    return root, db, home, proc


def test_a_kill_at_the_stop_leaves_an_unconfirmed_record_no_activation_pays(tmp_path):
    """Killed during the stop: the stop was never confirmed, so a later activation
    may be the stop's own restart. The record stays owed (the price: a server
    revived after such a kill and then stopped by the operator is restarted by
    the next update)."""
    root, _, home, proc = _crash_at_stop(tmp_path)
    assert proc.returncode == -9, proc.stdout + proc.stderr
    assert '"stage": "stopping"' in _owed(home).read_text()
    later = _systemd_time(time.time() + 120)
    assert _detect(tmp_path, home, root, active_enter=later) == (["genesis-server"], "false")


def test_a_restart_owed_is_paid_once_anything_else_brought_the_server_back(tmp_path):
    """Architect review: the watchdog (or a hand start, deploy_code_only.sh, the
    conflict exit, a rollback) revived the server after the crash; the operator
    then stopped it. The unit came up after the obligation was dated (at the
    confirmed stop), so the stop is the operator's and stays; the file is retired."""
    root, _, home, proc = _crash_after_confirmed_stop(tmp_path)
    assert proc.returncode == -9, proc.stdout + proc.stderr
    later = time.strftime("%a %Y-%m-%d %H:%M:%S %Z", time.localtime(time.time() + 120))
    assert _detect(tmp_path, home, root, active_enter=later) == ([], "true")
    assert not _owed(home).exists()


def test_a_unit_last_up_before_the_obligation_still_owes_the_restart(tmp_path):
    root, _, home, proc = _crash_after_confirmed_stop(tmp_path)
    assert proc.returncode == -9, proc.stdout + proc.stderr
    # Written two days ago: an empty value read by `date -d` would be midnight
    # TODAY, which is after it, and would wrongly pay the debt.
    written = time.time() - 2 * 86400
    os.utime(_owed(home), (written, written))
    earlier = time.strftime("%a %Y-%m-%d %H:%M:%S %Z", time.localtime(written - 3600))
    for value in (earlier, "n/a", ""):
        assert _detect(tmp_path, home, root, active_enter=value) == (["genesis-server"], "false")
    assert _owed(home).exists()


# ── round 2: no stop without the record; a later activation outranks older evidence ──


def _systemd_time(epoch: float) -> str:
    """An ActiveEnterTimestamp as `systemctl show` prints it."""
    return time.strftime("%a %Y-%m-%d %H:%M:%S %Z", time.localtime(epoch))


def _archive_failure(db: Path, home: Path, monkeypatch) -> None:
    """The version collector, as the bridge's runtime runs it, consumes the record."""
    import aiosqlite

    from genesis.learning.signals import genesis_version

    failure = home / ".genesis" / "last_update_failure.json"
    monkeypatch.setattr(genesis_version, "_FAILURE_FILE", failure)
    monkeypatch.setattr(genesis_version, "_FAILURE_ARCHIVE_DIR", home / ".genesis" / "uf")

    async def _consume() -> None:
        async with aiosqlite.connect(str(db)) as conn:
            collector = genesis_version.GenesisVersionCollector(conn)
            with patch.object(genesis_version, "update_in_progress", return_value=False):
                await collector._check_failure_file()

    asyncio.run(_consume())
    assert not failure.exists()


def test_the_server_is_never_stopped_without_its_restart_record(tmp_path):
    """Codex P1 (4309637542): the restart record cannot be written, so nothing is
    stopped: the run clears its state and exits, the server still running."""
    root, _, rollback = _recovery_repo(tmp_path)
    home = tmp_path / "home"
    # The record's temporary file cannot be created.
    (home / ".genesis" / "server_restart_owed.json.tmp").mkdir(parents=True)
    proc = _run_update_until_stop(
        tmp_path, home, active="genesis-server genesis-bridge", rollback=rollback
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out  # -9 would mean the stop ran
    assert "NOT stopped" in out and "CLEARED" in out, out
    assert not _owed(home).exists()
    log = tmp_path / "fake.log"
    assert "stop" not in (log.read_text() if log.exists() else ""), "nothing may be stopped"


def test_a_post_merge_refusal_keeps_the_earlier_runs_rollback_tag(tmp_path):
    """--post-merge reuses the crashed run's rollback tag, which its state still
    names: refusing to stop must not delete it (a plain run's tag is its own)."""
    root, _, rollback = _recovery_repo(tmp_path)
    home = tmp_path / "home"
    (home / ".genesis" / "server_restart_owed.json.tmp").mkdir(parents=True)
    # A function in the same shell, so it is what update.sh's `git` resolves to;
    # the fake executable first on PATH still refuses anything else.
    log_git = 'git() { printf "GIT %s\\n" "$*" >> "$FAKE_LOG"; return 0; }\n'
    for post_merge, deleted in (("true", False), ("false", True)):
        log = tmp_path / "fake.log"
        log.unlink(missing_ok=True)
        proc = _run_update_until_stop(
            tmp_path,
            home,
            active="genesis-server",
            rollback=rollback,
            pre=log_git,
            post_merge=post_merge,
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        calls = log.read_text() if log.exists() else ""
        assert ("tag -d rollback" in calls) is deleted, (post_merge, calls)


def test_on_live_the_server_is_never_stopped_without_its_restart_record(tmp_path):
    root, _, rollback = _recovery_repo(tmp_path)
    home = tmp_path / "home"
    (home / ".genesis" / "server_restart_owed.json.tmp").mkdir(parents=True)
    proc = _run_update_until_stop(
        tmp_path, home, active="genesis-server", rollback=rollback, on_live="1"
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1 and "LIVE-ABANDON 1" in out, out


def test_a_pre_marker_updaters_crash_is_restored_after_its_record_was_archived(
    tmp_path, monkeypatch
):
    """Codex P1 (4309637548): the crashed update predates the restart record, so
    only bootstrap's recovery knows the server was stopped. It writes the record
    itself; the collector archiving the failure record does not lose it, and a
    run with nothing to merge restores the server. (crashed_recovered stays out
    of the recovery statuses: a crash before the stop must not start a server the
    operator stopped.)"""
    root, db, _, _ = _crashed_merge(tmp_path, services_stopped=["genesis-server"])
    home = tmp_path / "home"
    assert _owed(home).is_file()
    _archive_failure(db, home, monkeypatch)
    assert _no_delta_route(tmp_path, home, root=root) == ("full", ["genesis-server"])


def test_bootstrap_writes_no_restart_record_for_a_crash_before_the_stop(tmp_path):
    _crashed_merge(tmp_path, services_stopped=[])
    assert not _owed(tmp_path / "home").exists()


def test_bootstrap_dates_its_records_to_the_crash_not_to_itself(tmp_path):
    """The records bootstrap writes carry the crashed state's time, so a server
    revived after the crash (the watchdog) and stopped by the operator before
    bootstrap ran is not started again."""
    root, _, rollback = _recovery_repo(tmp_path)
    deploy = _recovery_commit_file(root, "changed.txt", "deployed\n", "deploy")
    _migrated_genesis_root(tmp_path, root)
    home = tmp_path / "home"
    state = _write_recovery_state(
        home,
        own_head=deploy,
        deploy_head=deploy,
        old_tag="v1.0.0",
        old_commit=rollback[:7],
        started_at="2026-10-18T00:00:00+00:00",
        services_stopped=["genesis-server"],
    )
    crashed = time.time() - 3600
    os.utime(state, (crashed, crashed))
    rec = _run_crash_recovery(root, home)
    assert rec.returncode == 0 and "Crash recovery complete" in rec.stdout, rec.stdout + rec.stderr
    for record in (_owed(home), _failure_file(tmp_path)):
        assert abs(record.stat().st_mtime - crashed) < 2, record
    revived = _systemd_time(crashed + 600)
    assert _detect(tmp_path, home, root, active_enter=revived) == ([], "true")


def test_a_later_activation_outranks_an_older_failure_record(tmp_path):
    """Codex P2 (4309637546): the restart was paid by a later activation; the
    failure record from the same crash must not restart a server the operator
    has stopped since. The alert record itself is left for its readers."""
    home = tmp_path / "home"
    _with_owed(home)
    failure = _failure_file(tmp_path)
    failure.write_text("{}")
    crash = time.time() - 7200
    for record in (_owed(home), failure):
        os.utime(record, (crash, crash))
    revived = _systemd_time(crash + 3600)
    assert _detect(tmp_path, home, tmp_path / "no-root", active_enter=revived) == ([], "true")
    assert not _owed(home).exists()
    assert failure.exists()


def test_failure_evidence_newer_than_the_last_activation_still_restores(tmp_path):
    home = tmp_path / "home"
    failure = _failure_file(tmp_path)
    failure.parent.mkdir(parents=True)
    failure.write_text("{}")
    revived = _systemd_time(time.time() - 3600)
    for value in (revived, "", "n/a"):
        assert _detect(tmp_path, home, tmp_path / "no-root", active_enter=value) == (
            ["genesis-server"],
            "false",
        )


def _history_root(tmp_path: Path, status: str | None, completed_at: str) -> Path:
    import sqlite3

    root = tmp_path / f"root-{status}-{abs(hash(completed_at))}"
    (root / "data").mkdir(parents=True)
    con = sqlite3.connect(root / "data" / "genesis.db")
    con.execute("CREATE TABLE update_history (status TEXT, started_at TEXT, completed_at TEXT)")
    if status is not None:
        con.execute(
            "INSERT INTO update_history VALUES (?, ?, ?)",
            (status, "2026-10-18T00:00:00+00:00", completed_at),
        )
    con.commit()
    con.close()
    return root


def test_a_later_activation_outranks_an_older_failed_status(tmp_path):
    from datetime import UTC, datetime

    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    now = time.time()
    revived = _systemd_time(now - 3600)
    older = datetime.fromtimestamp(now - 7200, UTC).isoformat()
    newer = datetime.fromtimestamp(now - 600, UTC).isoformat()
    for status in ("rolled_back", "failed"):
        stale = _history_root(tmp_path, status, older)
        assert _detect(tmp_path, home, stale, active_enter=revived) == ([], "true"), status
        fresh = _history_root(tmp_path, status, newer)
        assert _detect(tmp_path, home, fresh, active_enter=revived) == (
            ["genesis-server"],
            "false",
        ), status
        # No readable activation, or no readable completion time: still a recovery.
        assert _detect(tmp_path, home, stale, active_enter="") == (["genesis-server"], "false")
        bad = _history_root(tmp_path, status, "not a time")
        assert _detect(tmp_path, home, bad, active_enter=revived) == (
            ["genesis-server"],
            "false",
        )


# ── round 3: only an activation after the CONFIRMED stop pays the debt ──────

# The stop's own kill reads as a failure to systemd, so Restart=on-failure can
# bring the server back between the first kill and _ensure_server_down's final
# stop. That activation is the stop's, not a restart by someone else.
_RESURRECTING_STOP = (
    "_stop_genesis_server() {\n"
    "    sleep 1.2\n"
    '    date "+%a %Y-%m-%d %H:%M:%S %Z" > "$FAKE_ACTIVE_ENTER_FILE"\n'
    "}\n"
)


def _resurrection(tmp_path: Path) -> str:
    return (tmp_path / "active-enter").read_text().strip()


def test_a_restart_during_the_stop_does_not_pay_the_debt(tmp_path):
    """Codex P1 (4310414810): the unit came back during the stop and was stopped
    again; the run is then killed before "merging". The next update restores it."""
    root, _, rollback = _recovery_repo(tmp_path)
    _migrated_genesis_root(tmp_path, root)
    home = tmp_path / "home"
    proc = _run_update_until_stop(
        tmp_path,
        home,
        active="genesis-server",
        rollback=rollback,
        pre=_RESURRECTING_STOP,
        tail="kill -KILL $$\n",
    )
    assert proc.returncode == -9, proc.stdout + proc.stderr
    # Re-dated at the confirmed stop, so a revival after it can still pay it.
    owed = _owed(home)
    assert '"stage": "stopped"' in owed.read_text()
    local_time = _resurrection(tmp_path).rsplit(" ", 1)[0]  # drop the zone name
    revived = time.mktime(time.strptime(local_time, "%a %Y-%m-%d %H:%M:%S"))
    assert owed.stat().st_mtime >= revived
    assert _detect(tmp_path, home, root, active_enter=_resurrection(tmp_path)) == (
        ["genesis-server"],
        "false",
    )


def test_a_kill_between_the_restart_and_the_confirmed_stop_keeps_the_debt(tmp_path):
    root, _, rollback = _recovery_repo(tmp_path)
    _migrated_genesis_root(tmp_path, root)
    home = tmp_path / "home"
    proc = _run_update_until_stop(
        tmp_path,
        home,
        active="genesis-server",
        rollback=rollback,
        pre=_RESURRECTING_STOP + "_ensure_server_down() { kill -KILL $$; }\n",
    )
    assert proc.returncode == -9, proc.stdout + proc.stderr
    assert _detect(tmp_path, home, root, active_enter=_resurrection(tmp_path)) == (
        ["genesis-server"],
        "false",
    )


def test_a_debt_that_cannot_be_dated_after_the_stop_restores_the_server_and_stops(tmp_path):
    """The record cannot be rewritten once the server is confirmed down: the run
    must neither go on (the debt could then read as paid) nor leave the server
    down. It restores what it stopped and exits, like an interrupt there."""
    text = UPDATE.read_text()
    root, _, rollback = _recovery_repo(tmp_path)
    home = tmp_path / "home"
    proc = _run_update_until_stop(
        tmp_path,
        home,
        active="genesis-server",
        rollback=rollback,
        pre=(
            _func(text, "_on_signal_prestop")
            + "_start_genesis_server() { echo SERVER-STARTED; }\n"
            + '_ensure_server_down() { mkdir -p "$HOME/.genesis/server_restart_owed.json.tmp"; }\n'
            + "_stop_genesis_server() { :; }\n"
        ),
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "SERVER-STARTED" in out and "REACHED-END" not in out, out


def test_bootstrap_does_not_let_a_restart_during_the_stop_pay_the_debt(tmp_path):
    """A crash in "fetching" with the server in services_stopped: the state was
    written before the stop, so an activation after it may be the stop's own.
    The record bootstrap writes for it is not paid by one."""
    root, _, rollback = _recovery_repo(tmp_path)
    _migrated_genesis_root(tmp_path, root)
    home = tmp_path / "home"
    state = _write_recovery_state(
        home,
        phase="fetching",
        deploy_head=rollback,
        post_merge=False,
        services_stopped=["genesis-server"],
    )
    written = time.time() - 3600
    os.utime(state, (written, written))
    rec = _run_crash_recovery(root, home)
    assert rec.returncode == 0 and "Crash recovery complete" in rec.stdout, rec.stdout + rec.stderr
    later = _systemd_time(written + 5)
    assert _detect(tmp_path, home, root, active_enter=later) == (["genesis-server"], "false")


# ── round 5: a durable record; sub-second ordering; no debt for a live server ──


def _owed_writer_source(text: str, name: str) -> str:
    """The Python a script runs to write the restart record (the heredoc that
    follows ``name`` in it)."""
    at = text.index(name)
    start = text.index("<<'PY'\n", at) + len("<<'PY'\n")
    return text[start : text.index("\nPY\n", start) + 1]


def _run_writer(monkeypatch, tmp_path: Path, source: str, argv: list[str]) -> list[str]:
    """Run a record writer in-process, as shipped, logging every fsync (file or
    directory) and rename, in order."""
    import stat as stat_mod

    events: list[str] = []
    real_fsync, real_replace, real_rename = os.fsync, os.replace, os.rename

    def fsync(fd):
        events.append("fsync-dir" if stat_mod.S_ISDIR(os.fstat(fd).st_mode) else "fsync-file")
        return real_fsync(fd)

    def replace(a, b, *args, **kwargs):
        events.append("rename")
        return real_replace(a, b, *args, **kwargs)

    def rename(a, b, *args, **kwargs):
        events.append("rename")
        return real_rename(a, b, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(sys, "argv", ["-", *argv])
    exec(compile(source, "<restart-record-writer>", "exec"), {"__name__": "__main__"})  # noqa: S102 - the shipped writer, extracted
    return events


def test_update_sh_makes_the_restart_record_durable_before_it_counts(monkeypatch, tmp_path):
    """Codex P1 (4316956958): a rename alone is atomic, not durable. The record is
    fsynced before the rename and its directory after it, so a power loss after
    the stop cannot lose it."""
    source = _owed_writer_source(UPDATE.read_text(), "_owe_server_restart() {")
    (tmp_path / "g").mkdir()
    path = tmp_path / "g" / "server_restart_owed.json"
    events = _run_writer(monkeypatch, tmp_path, source, [str(path), "123", "s", "stopped"])
    assert events == ["fsync-file", "rename", "fsync-dir"], events
    assert json.loads(path.read_text())["stage"] == "stopped"


def test_a_record_that_cannot_be_synced_refuses_the_stop(monkeypatch, tmp_path):
    source = _owed_writer_source(UPDATE.read_text(), "_owe_server_restart() {")
    (tmp_path / "g").mkdir()
    path = tmp_path / "g" / "server_restart_owed.json"

    def broken(fd):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "fsync", broken)
    monkeypatch.setattr(sys, "argv", ["-", str(path), "123", "s", "stopping"])
    try:
        exec(compile(source, "<restart-record-writer>", "exec"), {"__name__": "__main__"})  # noqa: S102 - the shipped writer, extracted
    except (OSError, SystemExit) as exc:
        assert not isinstance(exc, SystemExit) or exc.code not in (0, None)
    else:
        raise AssertionError("a record that could not be synced was reported written")


def test_bootstrap_makes_its_restart_record_durable_and_dated_to_the_crash(monkeypatch, tmp_path):
    from tests.test_scripts.test_bootstrap_guards import BOOTSTRAP

    source = _owed_writer_source(BOOTSTRAP.read_text(), 'OWED_FILE="$HOME/.genesis/')
    (tmp_path / "g").mkdir()
    state = tmp_path / "g" / "update_state.json"
    state.write_text("{}")
    crashed_ns = time.time_ns() - 3_600_123_456_789
    os.utime(state, ns=(crashed_ns, crashed_ns))
    path = tmp_path / "g" / "server_restart_owed.json"
    events = _run_writer(monkeypatch, tmp_path, source, [str(path), str(state), "123", "stopped"])
    assert events == ["fsync-file", "rename", "fsync-dir"], events
    assert path.stat().st_mtime_ns == crashed_ns  # to the nanosecond
    assert json.loads(path.read_text())["stage"] == "stopped"


def _set_mtime_ns(path: Path, ns: int) -> None:
    os.utime(path, ns=(ns, ns))


def _us_utc(ns: int) -> str:
    """An ActiveEnterTimestamp as `systemctl show --timestamp=us+utc` prints it."""
    secs, rem = divmod(ns, 1_000_000_000)
    return time.strftime("%a %Y-%m-%d %H:%M:%S", time.gmtime(secs)) + f".{rem // 1000:06d} UTC"


def test_an_activation_later_in_the_same_second_pays_the_debt(tmp_path):
    """Codex P2 (4316956950): ordering within one second is kept. Rule: an
    activation STRICTLY after the confirmed stop pays; the same instant, or any
    earlier one, or an unreadable one, leaves the restart owed."""
    home = tmp_path / "home"
    second = (time.time_ns() // 1_000_000_000 - 600) * 1_000_000_000
    stop = second + 300_000_000
    _with_owed(home)
    _set_mtime_ns(_owed(home), stop)
    assert _detect(
        tmp_path, home, tmp_path / "no-root", active_enter=_us_utc(stop + 400_000_000)
    ) == (
        [],
        "true",
    )
    for activation in (stop, stop - 200_000_000):
        _with_owed(home)
        _set_mtime_ns(_owed(home), stop)
        assert _detect(tmp_path, home, tmp_path / "no-root", active_enter=_us_utc(activation)) == (
            ["genesis-server"],
            "false",
        ), activation


def test_a_failure_record_from_earlier_in_the_same_second_is_stale(tmp_path):
    home = tmp_path / "home"
    failure = _failure_file(tmp_path)
    failure.parent.mkdir(parents=True)
    failure.write_text("{}")
    second = (time.time_ns() // 1_000_000_000 - 600) * 1_000_000_000
    _set_mtime_ns(failure, second + 100_000_000)
    assert _detect(
        tmp_path, home, tmp_path / "no-root", active_enter=_us_utc(second + 900_000_000)
    ) == ([], "true")


def test_no_debt_is_left_for_a_server_the_stop_could_not_bring_down(tmp_path):
    """CodeRabbit (4317170486): the stop failed and the server is still running.
    The run leaves it running and exits; a "stopping" record left behind would
    restart it after a later deliberate stop. A server that did go down keeps it."""
    root, _, rollback = _recovery_repo(tmp_path)
    home = tmp_path / "home"
    proc = _run_update_until_stop(
        tmp_path,
        home,
        active="genesis-server",
        rollback=rollback,
        pre="_stop_genesis_server() { :; }\n_ensure_server_down() { return 1; }\n",
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert not _owed(home).exists()

    home2 = tmp_path / "home2"
    proc = _run_update_until_stop(
        tmp_path,
        home2,
        active="genesis-server",
        rollback=rollback,
        pre=(
            '_stop_genesis_server() { : > "$FAKE_ACTIVE_FILE"; }\n'
            "_ensure_server_down() { return 1; }\n"
        ),
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _owed(home2).exists()
