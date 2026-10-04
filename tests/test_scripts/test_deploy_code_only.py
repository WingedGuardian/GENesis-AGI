"""scripts/deploy_code_only.sh: the code-only deploy, serialized with update.sh (#1699).

Every test runs the REAL script against the fixture in ``_deploy_station`` (a
bare upstream, a clone, a private HOME, shims for systemctl and curl). No test
touches the real runtime, venv, lock or Guardian.

What is pinned here:
  * refusals happen BEFORE anything changes, and change nothing: a dirty tree,
    a live foreign deploy marker, an unfinished update.sh run (a finished one
    left behind does not block), a branch other than main, a diverged tree, a
    venv not installed from this checkout's pyproject.toml, a unit that runs
    another venv, a linked worktree, a zero fetch timeout, a hung fetch;
  * the lock: a validation's SHARED hold makes a deploy queue for its whole
    run, and a deploy that cannot get the lock in time exits 200 untouched;
  * the happy path: fast-forward, marker held across the restart and gone
    after, health confirmed;
  * a failed health check alerts and HOLDS (the tree is not reverted);
  * the Guardian is paused across the restart and resumed after;
  * the health window matches update.sh's for the same inputs.
The modes, the boot-commit report and the hook sync are in
test_deploy_code_only_modes.py; the dependency gate's own cases in
test_venv_matches_pyproject.py.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_scripts._deploy_station import (
    LOCK_HELD_RC,
    PYPROJECT_OK,
    PYPROJECT_UNMET,
    REPO,
    SCRIPT,
    UPDATE_SH,
)
from tests.test_scripts._deploy_station import advance_upstream as _advance_upstream
from tests.test_scripts._deploy_station import alerts as _alerts
from tests.test_scripts._deploy_station import assert_untouched as _assert_untouched
from tests.test_scripts._deploy_station import commit as _commit
from tests.test_scripts._deploy_station import exec_file as _exec
from tests.test_scripts._deploy_station import git as _git
from tests.test_scripts._deploy_station import install_fixture as _install_fixture
from tests.test_scripts._deploy_station import restarted as _restarted
from tests.test_scripts._deploy_station import run as _run
from tests.test_scripts._deploy_station import systemctl_shim as _systemctl_shim

pytestmark = pytest.mark.skipif(__import__("sys").platform.startswith("win"), reason="bash-only")


# ── the happy path ──────────────────────────────────────────────────────────
def test_deploys_the_upstream_tip_and_releases_everything(station):
    tip = _advance_upstream(station)
    r = _run(station)
    assert r.returncode == 0, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == tip
    assert _restarted(station)
    assert (station["tmp"] / "marker_seen").exists(), (
        "the marker was not held while health was checked"
    )
    assert not station["marker"].exists(), "the marker outlived the deploy"
    assert f"Healthy — deployed {tip}" in r.stdout
    free = subprocess.run(["flock", "-n", str(station["lock"]), "true"])
    assert free.returncode == 0, "the lock outlived the deploy"


def test_pull_is_a_locked_pull_only(station):
    tip = _advance_upstream(station)
    r = _run(station, "pull")
    assert r.returncode == 0, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == tip
    assert not _restarted(station)
    assert not station["marker"].exists()


def test_an_ephemeral_dirty_file_does_not_block(station):
    """The same excused paths update.sh excuses (one list, in the shared lib)."""
    (station["root"] / "AGENTS.md").write_text("rewritten by an indexer\n")
    tip = _advance_upstream(station)
    r = _run(station)
    assert r.returncode == 0, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == tip


# ── refusals: nothing changes ───────────────────────────────────────────────
def test_a_dirty_tree_is_refused_untouched(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station)
    (station["root"] / "pyproject.toml").write_text(PYPROJECT_OK + "# local edit\n")
    r = _run(station)
    _assert_untouched(station, head, r)
    assert "uncommitted tracked changes" in r.stderr
    assert "pyproject.toml" in r.stderr


def test_an_unmet_dependency_upstream_is_refused_before_the_merge(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station, "needs a newer packaging", {"pyproject.toml": PYPROJECT_UNMET})
    r = _run(station)
    _assert_untouched(station, head, r)
    assert "run scripts/update.sh instead" in r.stderr
    assert "packaging" in r.stderr


def test_restart_still_checks_the_working_trees_dependencies(station):
    """Nothing is merged, so plain update.sh would stop at "Already up to date";
    the refusal names the command that does reinstall."""
    head = _commit(station["root"], "unmet locally", {"pyproject.toml": PYPROJECT_UNMET})
    r = _run(station, "restart")
    _assert_untouched(station, head, r)
    assert "run scripts/update.sh --post-merge instead" in r.stderr


def test_already_at_the_tip_still_checks_dependencies(station):
    """Someone may have pulled by hand; a restart must still refuse a venv that
    does not satisfy the tree it is about to run."""
    tip = _advance_upstream(station, "unmet upstream", {"pyproject.toml": PYPROJECT_UNMET})
    _git(station["root"], "pull", "-q", "--ff-only")
    r = _run(station)
    _assert_untouched(station, tip, r)
    assert "run scripts/update.sh --post-merge instead" in r.stderr


def test_a_live_foreign_marker_is_refused_before_the_merge(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station)
    holder = subprocess.Popen(["sleep", "30"])
    try:
        station["marker"].write_text(f"{holder.pid}\n")
        r = _run(station)
        assert r.returncode == 1
        assert _git(station["root"], "rev-parse", "HEAD") == head
        assert not _restarted(station)
        assert station["marker"].read_text().strip() == str(holder.pid), (
            "a foreign marker was clobbered"
        )
        assert "live deploy" in r.stderr
    finally:
        holder.kill()
        holder.wait()


def test_a_dead_marker_is_replaced(station):
    dead = subprocess.Popen(["true"])
    dead.wait()
    station["marker"].write_text(f"{dead.pid}\n")
    tip = _advance_upstream(station)
    r = _run(station)
    assert r.returncode == 0, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == tip
    assert not station["marker"].exists()


@pytest.mark.parametrize(
    "state",
    ['{"phase": "merged"}', "{not json", '["done"]', '{"pid": 7}', '{"phase": 1}'],
    ids=["mid-run", "malformed", "not-an-object", "no-phase", "non-string-phase"],
)
def test_an_unfinished_update_is_refused(station, state):
    head = _git(station["root"], "rev-parse", "HEAD")
    (station["home"] / ".genesis" / "update_state.json").write_text(state)
    _advance_upstream(station)
    r = _run(station)
    _assert_untouched(station, head, r)
    assert "update.sh --post-merge" in r.stderr


def test_a_finished_update_left_behind_does_not_block(station):
    """update.sh writes phase "done" just before deleting its state file; one
    killed in between left a FINISHED run, and the deploy proceeds."""
    (station["home"] / ".genesis" / "update_state.json").write_text('{"phase": "done", "pid": 7}')
    _advance_upstream(station)
    r = _run(station)
    assert r.returncode == 0, r.stderr
    assert "genesis-server pid 2222" in r.stdout, r.stdout


def test_a_branch_other_than_main_is_refused(station):
    _git(station["root"], "checkout", "-qb", "feature")
    head = _git(station["root"], "rev-parse", "HEAD")
    r = _run(station)
    _assert_untouched(station, head, r)
    assert "not main" in r.stderr


def test_a_diverged_tree_is_refused_untouched(station):
    _advance_upstream(station)
    local = _commit(station["root"], "local only")
    r = _run(station)
    _assert_untouched(station, local, r)
    assert "diverged" in r.stderr


def test_a_linked_worktree_is_refused(station):
    wt = station["tmp"] / "anywhere" / "wt"
    _git(station["root"], "worktree", "add", "-q", "-b", "wt", str(wt))
    env = dict(station["env"], GENESIS_DEPLOY_ROOT=str(wt))
    r = _run(station, env=env)
    assert r.returncode == 1
    assert "not a worktree" in r.stderr
    assert not station["lock"].exists(), "a worktree run must not even take the lock"


def test_the_main_checkout_is_not_mistaken_for_a_worktree(station):
    r = _run(station, "pull")
    assert r.returncode == 0, r.stderr


def test_a_zero_fetch_timeout_is_refused(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    env = dict(station["env"], GENESIS_DEPLOY_FETCH_TIMEOUT="0")
    r = _run(station, env=env)
    _assert_untouched(station, head, r)
    assert "positive number of seconds" in r.stderr


def test_a_hung_fetch_is_bounded(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    real_git = subprocess.run(
        ["bash", "-c", "command -v git"], capture_output=True, text=True, check=True
    )
    _exec(
        station["shims"] / "git",
        '#!/bin/bash\nfor a in "$@"; do [ "$a" = fetch ] && exec sleep 60; done\n'
        f'exec "{real_git.stdout.strip()}" "$@"\n',
    )
    env = dict(station["env"], GENESIS_DEPLOY_FETCH_TIMEOUT="2")
    t0 = time.monotonic()
    r = _run(station, env=env)
    assert time.monotonic() - t0 < 30, "the fetch bound did not hold"
    _assert_untouched(station, head, r)
    assert "fetch failed or timed out" in r.stderr


# ── the lock ────────────────────────────────────────────────────────────────
def _hold_shared(lock: Path, seconds: float) -> subprocess.Popen:
    lock.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        ["flock", "-s", str(lock), "bash", "-c", f"echo HELD; exec sleep {seconds}"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert p.stdout is not None and p.stdout.readline().strip() == "HELD"
    return p


def test_a_validation_hold_makes_a_deploy_queue_for_its_whole_run(station):
    tip = _advance_upstream(station)
    holder = _hold_shared(station["lock"], 3)
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--wait", "30"],
            env=station["env"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        holder.wait()
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - t0 >= 2.5, "the deploy did not wait for the validation hold"
    assert _git(station["root"], "rev-parse", "HEAD") == tip


def test_a_lock_timeout_exits_200_and_changes_nothing(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station)
    holder = _hold_shared(station["lock"], 10)
    try:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--wait", "1"],
            env=station["env"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        holder.kill()
        holder.wait()
    assert r.returncode == LOCK_HELD_RC, (r.returncode, r.stderr)
    assert _git(station["root"], "rev-parse", "HEAD") == head
    assert not station["marker"].exists()
    assert not _alerts(station)
    assert "validation hold" in r.stderr


def test_a_deploy_excludes_update_sh_while_it_runs(station):
    """update.sh takes the same lock with `flock -n` and must be refused while a
    deploy holds it: probe the lock from inside the restart."""
    _exec(
        station["shims"] / "systemctl",
        _systemctl_shim(
            station["calls"],
            station["manifest"],
            on_restart=f'flock -n "{station["lock"]}" true; '
            f'echo "probe=$?" >> "{station["tmp"]}/probe"',
        ),
    )
    r = _run(station)
    assert r.returncode == 0, r.stderr
    assert (station["tmp"] / "probe").read_text().strip() == "probe=1", (
        "the lock was free mid-deploy"
    )


# ── failure: alert and hold ─────────────────────────────────────────────────
def test_an_unhealthy_restart_alerts_and_holds(station):
    tip = _advance_upstream(station)
    env = dict(station["env"], CURL_RC="7", UNIT_STATE="failed")
    r = _run(station, env=env)
    assert r.returncode == 1
    assert _git(station["root"], "rev-parse", "HEAD") == tip, (
        "a failed deploy must NOT revert the tree"
    )
    alerts = _alerts(station)
    assert len(alerts) == 1, alerts
    assert "unhealthy" in alerts[0].read_text()
    assert not station["marker"].exists()


@pytest.mark.parametrize(
    "case, extra",
    [
        # The unit reports active, and each case defeats ONE identity check, so
        # the check under test is what decides (a unit reported failed would be
        # refused before any of them ran).
        # The new process exited on the process lock an old server holds.
        ("new process exited", {"NEW_PID": "0"}),
        # The restarted unit is up, but ANOTHER process listens on the port (a
        # bind failure in the unit's Flask thread leaves the process running).
        ("another process holds the port", {"PROBE_OWNER": "3333"}),
        # The unit listens, but so does another process.
        ("a second listener", {"PROBE_FOREIGN_TOO": "1"}),
        # Nothing listens, or ownership cannot be read: never proven (Codex P1,
        # round 2 — there is no manifest fallback to fall through to).
        ("ownership cannot be established", {"PROBE_NONE": "1"}),
        # systemd still reports the OLD invocation (and pid): the restart did not
        # take effect, and the old process's manifest and socket are what answer.
        (
            "the invocation did not change",
            {"NEW_PID": "1111", "INVOCATION_AFTER": "11111111111111111111111111111111"},
        ),
    ],
)
def test_an_answer_from_another_server_is_not_a_healthy_deploy(station, case, extra):
    """THE REPRO (Codex P1 / Devin severe on #2494): after an earlier nohup
    fallback, an old server outside systemd keeps answering on the port while
    the restarted unit is not serving. curl alone said "Healthy"."""
    tip = _advance_upstream(station)
    env = dict(station["env"], CURL_RC="0", UNIT_STATE="active", UNIT_STATE_LATER="failed", **extra)
    r = _run(station, env=env)
    assert r.returncode == 1, (case, r.stdout, r.stderr)
    assert "Healthy" not in r.stdout, case
    assert "not from the restarted genesis-server unit" in r.stderr, (case, r.stderr)
    alerts = _alerts(station)
    assert len(alerts) == 1 and "critical" in alerts[0].read_text(), (case, alerts)
    assert _git(station["root"], "rev-parse", "HEAD") == tip, "never revert the tree"


def test_a_restarted_unit_that_reuses_the_old_pid_is_healthy(station):
    """Codex, #2557 round 3: the kernel can hand the new process the pid the old
    one had. A new systemd invocation is what proves the restart; comparing pids
    failed the whole health window and paged critical on a healthy server."""
    tip = _advance_upstream(station)
    r = _run(station, env=dict(station["env"], NEW_PID="1111"))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "Healthy" in r.stdout and "pid 1111" in r.stdout, r.stdout
    assert not _alerts(station)
    assert _git(station["root"], "rev-parse", "HEAD") == tip


@pytest.mark.parametrize(("new_pid", "healthy"), [("2222", True), ("1111", False)])
def test_with_no_readable_invocation_a_new_pid_stands_in(station, new_pid, healthy):
    """When systemd gave no invocation id before the restart, only a changed pid
    can show the restart happened."""
    _advance_upstream(station)
    env = dict(station["env"], INVOCATION="-", NEW_PID=new_pid, UNIT_STATE_LATER="failed")
    r = _run(station, env=env)
    assert (r.returncode == 0) is healthy, (r.stdout, r.stderr)


def test_a_healthy_restart_reports_the_new_pid_and_no_alert(station):
    _advance_upstream(station)
    r = _run(station)
    assert r.returncode == 0, r.stderr
    assert "genesis-server pid 2222" in r.stdout, r.stdout
    assert not _alerts(station), "a clean deploy pages nobody"


def test_no_baseline_is_reported_but_does_not_page(station):
    """A restart with no pre-restart manifest to compare (a first deploy, or a
    stopped unit) reports `check:no-baseline` and queues no alert."""
    station["manifest"].unlink()
    _advance_upstream(station)
    r = _run(station)
    assert r.returncode == 0, r.stderr
    assert "check:no-baseline" in r.stdout, r.stdout
    assert not _alerts(station), "nothing actionable, nobody paged"


def test_a_subsystem_regression_across_the_restart_is_surfaced(station):
    """Codex P2 on #2494: the health endpoint stays 200 when a non-critical
    subsystem regresses. The shared manifest delta (the check update.sh runs)
    compares the new pid's manifest with the pre-restart baseline."""
    _advance_upstream(station)
    env = dict(station["env"], MANIFEST_AFTER='{"db": "ok", "perception": "degraded"}')
    r = _run(station, env=env)
    assert r.returncode == 0, r.stderr  # advisory: the deploy stands
    assert "subsystems not ok after the restart: perception" in r.stdout, r.stdout
    alerts = _alerts(station)
    assert len(alerts) == 1, alerts
    body = alerts[0].read_text()
    assert "warning" in body and "perception" in body, body


def test_a_failed_restart_after_the_merge_alerts(station):
    tip = _advance_upstream(station)
    _exec(
        station["shims"] / "systemctl",
        _systemctl_shim(station["calls"], station["manifest"], on_restart="exit 1"),
    )
    r = _run(station)
    assert r.returncode != 0
    assert _git(station["root"], "rev-parse", "HEAD") == tip
    alerts = _alerts(station)
    assert len(alerts) == 1 and "restarting" in alerts[0].read_text(), alerts


def test_sigterm_mid_wait_releases_the_marker_and_lock(station):
    _advance_upstream(station)
    env = dict(station["env"], CURL_RC="7", UNIT_STATE="activating")
    p = subprocess.Popen(
        ["bash", str(SCRIPT), "--wait", "5"], env=env, stdout=subprocess.PIPE, text=True
    )
    deadline = time.monotonic() + 30
    while not station["marker"].exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert station["marker"].exists(), "the deploy never reached the restart"
    p.terminate()
    assert p.wait(timeout=30) == 143
    assert not station["marker"].exists()
    with station["lock"].open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held


# ── the Guardian ────────────────────────────────────────────────────────────
def test_the_guardian_is_paused_across_the_restart_and_resumed(station):
    home = station["home"]
    (home / ".genesis" / "guardian_remote.yaml").write_text("host_ip: 1.2.3.4\nhost_user: u\n")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "genesis_guardian_ed25519").write_text("")
    log = station["tmp"] / "ssh.log"
    _exec(
        station["shims"] / "ssh",
        '#!/bin/bash\nverb="${@: -1}"\n'
        f'echo "$verb" >> "{log}"\n'
        'if [ "$verb" = paused ]; then echo \'{"paused": false}\'; fi\nexit 0\n',
    )
    _exec(
        station["shims"] / "systemctl",
        _systemctl_shim(
            station["calls"], station["manifest"], on_restart=f'echo RESTART >> "{log}"'
        ),
    )
    _advance_upstream(station)
    r = _run(station)
    assert r.returncode == 0, r.stderr
    events = log.read_text().split("\n")
    assert events.index("pause 1800") < events.index("RESTART") < events.index("resume"), events


# ── parity with update.sh ───────────────────────────────────────────────────
def _window(block: str, value: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_DEPLOY_HEALTH_WINDOW_SECS"}
    if value is not None:
        env["GENESIS_DEPLOY_HEALTH_WINDOW_SECS"] = value
    lib = REPO / "scripts" / "lib" / "guardian_pause.sh"
    r = subprocess.run(
        ["bash", "-c", f'set -Eeuo pipefail\n. "{lib}"\n{block}\necho "W=$HEALTH_WINDOW_SECS"'],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    m = re.search(r"W=(\d+)", r.stdout)
    assert m, (r.stdout, r.stderr)
    return m.group(1)


def _window_block(text: str, first: str, last_re: str) -> str:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip().startswith(first))
    end = next(i for i in range(start, len(lines)) if re.search(last_re, lines[i]))
    return "\n".join(
        ln.strip() for ln in lines[start : end + 1] if not ln.strip().startswith("echo")
    )


@pytest.mark.parametrize(
    "value", [None, "", "abc", "0", "0600", "0000180", "100", "5000", "99999999"]
)
def test_the_health_window_matches_update_sh(value):
    # End at the final CAP line; the earlier length clamp assigns the same value.
    ours = _window_block(
        SCRIPT.read_text(), "HEALTH_GUARDIAN_COVER=", r'-gt "\$HEALTH_WINDOW_MAX" \] &&'
    )
    theirs = _window_block(UPDATE_SH.read_text(), "HEALTH_GUARDIAN_COVER=", r"^\s*fi$")
    assert _window(ours, value) == _window(theirs, value)


def test_the_script_and_update_sh_share_one_excused_path_list():
    """One definition, in the lib both SOURCE (a comment naming it is not enough),
    so the two paths cannot drift."""
    for script in (SCRIPT, UPDATE_SH):
        text = script.read_text()
        assert not re.search(r"^\s*EPHEMERAL_DIRTY_RE=", text, re.MULTILINE), script.name
        assert re.search(r'^\s*\. "\$\w+/lib/deploy_marker\.sh"$', text, re.MULTILINE), script.name


def test_both_callers_name_the_lock_fd_the_way_the_lib_expects():
    """guardian_pause.sh closes the lock for its renewer only when the caller keeps
    the fd in _UPDATE_LOCK_FD; any other name silently leaks the lock again."""
    for script in (SCRIPT, UPDATE_SH):
        assert "exec {_UPDATE_LOCK_FD}>" in script.read_text(), script.name


def _update_sh_dirty(root: Path) -> str:
    """update.sh's REAL dirty-tree query, run against *root*. Both scripts call the
    one query in scripts/lib/deploy_checkout.sh, so parity is that they call it and
    that it behaves; the assignment is extracted from update.sh, not retyped."""
    code = SCRIPT.read_text()
    assert '_dirty="$(genesis_tracked_dirty_paths "$GENESIS_ROOT")"' in code
    matches = re.findall(
        r'DIRTY_FILES="\$\(genesis_tracked_dirty_paths "\$GENESIS_ROOT"\)"', UPDATE_SH.read_text()
    )
    assert len(matches) == 1, matches
    marker_lib = REPO / "scripts" / "lib" / "deploy_marker.sh"
    checkout_lib = REPO / "scripts" / "lib" / "deploy_checkout.sh"
    r = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail\nHOME=/nonexistent\n. "{marker_lib}"\n. "{checkout_lib}"\n'
            f'GENESIS_ROOT="{root}"\n{matches[0]}\nprintf %s "$DIRTY_FILES"',
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.parametrize(
    "edit, refuses",
    [
        ("AGENTS.md", False),  # excused by both
        ("pyproject.toml", True),  # a real tracked change
        (None, False),  # clean
    ],
)
def test_the_dirty_tree_verdict_matches_update_sh(station, edit, refuses):
    """Behavioural parity: the same working tree gets the same verdict from both."""
    if edit:
        (station["root"] / edit).write_text((station["root"] / edit).read_text() + "local\n")
    assert bool(_update_sh_dirty(station["root"])) is refuses
    head = _git(station["root"], "rev-parse", "HEAD")
    r = _run(station, "pull")
    assert (r.returncode == 1 and "uncommitted tracked changes" in r.stderr) is refuses, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == head


# ── findings from the adversarial audit ─────────────────────────────────────
def test_an_excused_file_changed_upstream_does_not_block_the_merge(station):
    """AGENTS.md is excused when dirty; if upstream also changes it, the local
    rewrite is discarded (it regenerates), exactly as update.sh does before its
    merge. Without that, the merge aborts on it on every run."""
    (station["root"] / "AGENTS.md").write_text("rewritten by an indexer\n")
    tip = _advance_upstream(station, "upstream stats", {"AGENTS.md": "new stats\n"})
    r = _run(station)
    assert r.returncode == 0, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == tip
    assert (station["root"] / "AGENTS.md").read_text() == "new stats\n"


def _late_collision(station, files: dict[str, str]) -> tuple[str, dict, object]:
    """Upstream adds config/secrets.local.yaml (ignored here) plus *files*, and an
    ignored copy of it appears at the stop, after the collision scan: git refuses
    the merge. Returns HEAD before the run, the env, and the local file."""
    root = station["root"]
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("secrets.local.yaml\n")
    head = _git(root, "rev-parse", "HEAD")
    _advance_upstream(
        station, "a new config file", {"config/secrets.local.yaml": "upstream\n", **files}
    )
    local = root / "config" / "secrets.local.yaml"
    env = dict(station["env"], ON_STOP=f"mkdir -p {root}/config && echo LOCAL > {local}")
    return head, env, local


def test_a_refused_merge_names_the_excused_files_it_reset(station):
    """Devin, #2557: an excused file the range also changes is reset before the
    fast-forward. When git then refuses the merge (an ignored file that appears
    after the collision scan), "nothing merged" must not hide that reset: the
    refusal names it, and pages nobody (the file regenerates, as update.sh treats
    it)."""
    root = station["root"]
    (root / "AGENTS.md").write_text("rewritten by an indexer\n")
    head, env, local = _late_collision(station, {"AGENTS.md": "new stats\n"})
    r = _run(station, env=env)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "git refused the fast-forward" in r.stderr and "nothing merged" in r.stderr, r.stderr
    assert "Reset to HEAD for it" in r.stderr and "AGENTS.md" in r.stderr, r.stderr
    assert "Resetting AGENTS.md" in r.stdout, "each reset is named as it happens"
    assert _git(root, "rev-parse", "HEAD") == head
    assert local.read_text() == "LOCAL\n"
    assert not _alerts(station)


def test_a_staged_excused_file_and_a_refused_merge_page_nobody(station):
    """The reset takes a staged edit too; the refusal still names it, and is still
    a refusal, not a failure to page about."""
    root = station["root"]
    (root / "AGENTS.md").write_text("staged by hand\n")
    _git(root, "add", "AGENTS.md")
    (root / "AGENTS.md").write_text("and edited again\n")
    head, env, _ = _late_collision(station, {"AGENTS.md": "new stats\n"})
    r = _run(station, env=env)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "nothing merged" in r.stderr and "AGENTS.md" in r.stderr, r.stderr
    assert _git(root, "rev-parse", "HEAD") == head
    assert not _alerts(station)


def test_a_locally_deleted_excused_file_does_not_block_the_merge(station):
    """A deleted AGENTS.md is excused dirt; the reset simply restores it for the
    merge, which then lands."""
    root = station["root"]
    (root / "AGENTS.md").unlink()
    tip = _advance_upstream(station, "stats and code", {"AGENTS.md": "new stats\n"})
    r = _run(station)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _git(root, "rev-parse", "HEAD") == tip
    assert (root / "AGENTS.md").read_text() == "new stats\n"
    assert not _alerts(station)


def test_a_run_killed_after_the_resets_names_them_in_the_alert(station):
    """A run stopped between the resets and the merge (its transient unit stopped,
    say) leaves the excused files reset: the exit alert must name them."""
    import shutil

    root = station["root"]
    (root / "AGENTS.md").write_text("rewritten by an indexer\n")
    _advance_upstream(
        station, "stats and code", {"AGENTS.md": "new stats\n", "src/genesis/y.py": "y\n"}
    )
    real_git = shutil.which("git")
    # The script's git: at the merge, SIGTERM the script (its parent) and fail.
    _exec(
        station["shims"] / "git",
        "#!/bin/bash\n"
        'for a in "$@"; do [ "$a" = merge ] && { kill -TERM "$PPID"; exit 1; }; done\n'
        f'exec {real_git} "$@"\n',
    )
    r = _run(station)
    assert r.returncode == 143, (r.returncode, r.stdout, r.stderr)
    alerts = _alerts(station)
    assert len(alerts) == 1, alerts
    body = alerts[0].read_text()
    assert "Reset to HEAD for the merge" in body and "AGENTS.md" in body, body


def test_a_transitional_file_edited_locally_and_upstream_goes_to_update_sh(station):
    """These hold live install-local data update.sh backs up first."""
    _advance_upstream(station, "track USER.md", {"USER.md": "template\n"})
    user_md = Path("src/genesis/identity/USER.md")
    (station["seed"] / user_md.parent).mkdir(parents=True, exist_ok=True)
    _advance_upstream(station, "track it where it lives", {str(user_md): "template\n"})
    _git(station["root"], "pull", "-q", "--ff-only")
    (station["root"] / user_md).write_text("my real profile\n")
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station, "upstream edits the template", {str(user_md): "template v2\n"})
    r = _run(station)
    _assert_untouched(station, head, r)
    assert "run scripts/update.sh" in r.stderr
    assert (station["root"] / user_md).read_text() == "my real profile\n", "the live copy was lost"


def test_a_zero_fetch_timeout_spelled_with_padding_is_refused(station):
    """`timeout 00` runs unbounded, exactly like `timeout 0`."""
    head = _git(station["root"], "rev-parse", "HEAD")
    env = dict(station["env"], GENESIS_DEPLOY_FETCH_TIMEOUT="00")
    r = _run(station, env=env)
    _assert_untouched(station, head, r)
    assert "positive number of seconds" in r.stderr


def test_a_guardian_code_change_is_named(station):
    deploy_health = REPO / "src" / "genesis" / "observability" / "snapshots" / "deploy_health.py"
    target = station["seed"] / "src" / "genesis" / "observability" / "snapshots"
    target.mkdir(parents=True)
    _advance_upstream(
        station,
        "ship the snapshot",
        {str(target.relative_to(station["seed"]) / "deploy_health.py"): deploy_health.read_text()},
    )
    _git(station["root"], "pull", "-q", "--ff-only")
    (station["seed"] / "src" / "genesis" / "guardian").mkdir(parents=True)
    _advance_upstream(station, "guardian change", {"src/genesis/guardian/check.py": "x = 1\n"})
    r = _run(station)
    assert r.returncode == 0, r.stderr
    assert "host Guardian runs" in r.stdout
    assert "src/genesis/guardian/check.py" in r.stdout


def test_no_child_outlives_the_script_holding_the_lock(station):
    """git (fetch, merge) and systemctl are run with the lock fd closed. Each shim
    here leaves a long-lived child behind, as an auto-gc or a forking unit could;
    the lock must still be free the moment the script exits."""
    real_git = subprocess.run(
        ["bash", "-c", "command -v git"], capture_output=True, text=True, check=True
    )
    marker_arg = "31.7"  # a distinctive sleep length, so teardown can find them
    # The child's stdio goes to /dev/null: inheriting the test's capture pipes would
    # make subprocess.run wait for the child, and it would be gone before the check.
    # Only the fds the script passed down (the lock, unless closed) are inherited.
    child = f"(sleep {marker_arg} </dev/null >/dev/null 2>&1 &)"
    _exec(
        station["shims"] / "git",
        '#!/bin/bash\nfor a in "$@"; do case "$a" in fetch|merge) '
        f'{child} ;; esac; done\nexec "{real_git.stdout.strip()}" "$@"\n',
    )
    _exec(
        station["shims"] / "systemctl",
        _systemctl_shim(station["calls"], station["manifest"], on_restart=child),
    )
    _advance_upstream(station)
    try:
        r = _run(station)
        assert r.returncode == 0, r.stderr
        leftovers = subprocess.run(
            ["pgrep", "-f", f"sleep {marker_arg}"], capture_output=True, text=True
        )
        assert leftovers.stdout.strip(), (
            "the shims left no child behind; the test would prove nothing"
        )
        free = subprocess.run(["flock", "-n", str(station["lock"]), "true"])
        assert free.returncode == 0, "a child of the deploy still holds update.lock"
    finally:
        subprocess.run(["pkill", "-f", f"sleep {marker_arg}"])


def test_a_venv_installed_from_another_checkout_is_refused(station):
    """Codex P1 on #2494: the deploy checks what the venv imports, not just its
    dependency versions."""
    head = _git(station["root"], "rev-parse", "HEAD")
    _install_fixture(station["site"], station["tmp"] / "some-worktree")
    _advance_upstream(station)
    r = _run(station)
    _assert_untouched(station, head, r)
    assert "is installed from" in r.stderr and "run scripts/update.sh instead" in r.stderr


@pytest.mark.parametrize(
    ("unit_python", "needle"),
    [("/opt/elsewhere/.venv/bin/python", "genesis-server runs"), ("-", "cannot read which python")],
    ids=["another-venv", "unreadable"],
)
def test_a_unit_that_runs_another_venv_is_refused(station, unit_python, needle):
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station)
    r = _run(station, env={**station["env"], "UNIT_PYTHON": unit_python})
    _assert_untouched(station, head, r)
    assert needle in r.stderr, r.stderr


def test_a_path_argument_in_the_unit_is_not_read_as_its_python(station):
    """Only systemd's leading `path=` names the executable; an argument such as
    `--db-path=` later on the same line must not be taken for it."""
    _advance_upstream(station)
    r = _run(station, env={**station["env"], "UNIT_ARGS": "--db-path=/data/x.db"})
    assert r.returncode == 0, r.stderr
