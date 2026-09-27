"""scripts/deploy_code_only.sh: the code-only deploy, serialized with update.sh (#1699).

Every test runs the REAL script as a subprocess against a FIXTURE: a bare
upstream and a clone of it (the ``GENESIS_DEPLOY_ROOT`` seam), a private HOME
(so the lock file, the deploy marker and the alert queue are private), the test
interpreter's own environment as the venv, and PATH shims for ``systemctl`` and
``curl``. No test touches the real runtime, venv, lock or Guardian.

What is pinned:
  * refusals happen BEFORE anything changes, and change nothing: a dirty tree,
    a live foreign deploy marker, an unfinished update.sh run, a branch other
    than main, a diverged tree, a pull the venv's dependencies do not satisfy,
    a linked worktree, a zero fetch timeout, a hung fetch;
  * the lock: a validation's SHARED hold makes a deploy queue for its whole
    run, and a deploy that cannot get the lock in time exits 200 untouched;
  * the happy path: fast-forward, marker held across the restart and gone
    after, health confirmed;
  * a failed health check alerts and HOLDS (the tree is not reverted);
  * the Guardian is paused across the restart and resumed after;
  * the health window matches update.sh's for the same inputs.
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "deploy_code_only.sh"
UPDATE_SH = REPO / "scripts" / "update.sh"
# sys.prefix, never a resolved sys.executable: a venv's python is a symlink to the
# base interpreter, and resolving it lands outside the venv.
VENV = Path(sys.prefix)
LOCK_HELD_RC = 200

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="bash-only")


def _exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@local", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, msg: str, files: dict[str, str] | None = None) -> str:
    for name, body in (files or {}).items():
        (repo / name).write_text(body)
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _systemctl_shim(calls: Path, manifest: Path, on_restart: str = "") -> str:
    """The station's systemctl: logs argv; `is-active` prints $UNIT_STATE. It models
    a restart the way systemd does: MainPID is the old server's (1111) until a
    restart, then a NEW pid ($NEW_PID, default 2222; "0" = the new process
    exited). A restart also stands in for a completed bootstrap by writing a
    manifest owned by the new pid ($MANIFEST_AFTER, a JSON mapping), unless
    $NO_BOOTSTRAP is set. `on_restart` is extra shell run at the restart."""
    return (
        "#!/bin/bash\n"
        f'echo "$*" >> "{calls}"\n'
        # $UNIT_STATE_LATER: the state every is-active call AFTER the first reports
        # (the first reports $UNIT_STATE) — lets a test hold the unit "active" for
        # the identity check and still end the health wait at once.
        'if [[ " $* " == *" is-active "* ]]; then\n'
        '  s="${UNIT_STATE:-active}"\n'
        f'  if [ -n "${{UNIT_STATE_LATER:-}}" ] && [ -f "{calls}.isactive" ]; then '
        's="$UNIT_STATE_LATER"; fi\n'
        f'  touch "{calls}.isactive"; echo "$s"; [ "$s" = active ]; exit\n'
        "fi\n"
        'if [[ " $* " == *" MainPID "* ]]; then\n'
        f'  if grep -q "restart genesis-server" "{calls}"; then echo "${{NEW_PID:-2222}}"; '
        "else echo 1111; fi; exit 0\n"
        "fi\n"
        'if [[ " $* " == *" restart "* ]]; then\n'
        f"  {on_restart or ':'}\n"
        '  if [ -z "${NO_BOOTSTRAP:-}" ]; then\n'
        '    m="${MANIFEST_AFTER:-}"\n'
        '    [ -n "$m" ] || m=\'{"db": "ok", "perception": "ok"}\'\n'
        f'    printf \'{{"pid": %s, "manifest": %s}}\' "${{NEW_PID:-2222}}" "$m" > "{manifest}"\n'
        "  fi\n"
        "fi\n"
        "exit 0\n"
    )


PYPROJECT_OK = '[project]\nname = "fixture"\ndependencies = ["packaging"]\n'
PYPROJECT_UNMET = '[project]\nname = "fixture"\ndependencies = ["packaging>=9999"]\n'


@pytest.fixture()
def station(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(upstream)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(seed)], check=True, capture_output=True
    )
    _git(seed, "checkout", "-qb", "main")
    _commit(seed, "seed", {"pyproject.toml": PYPROJECT_OK, "AGENTS.md": "stats\n"})
    _git(seed, "push", "-q", "origin", "main")
    root = tmp_path / "root"
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(upstream), str(root)],
        check=True,
        capture_output=True,
    )

    shims = tmp_path / "shims"
    shims.mkdir()
    calls = tmp_path / "systemctl.log"
    marker = home / ".genesis" / "update_in_progress.pid"
    # systemctl logs its argv; `is-active` prints the state in $UNIT_STATE.
    # It models a restart the way systemd does: MainPID is the old server's
    # (1111) until a restart, then a NEW pid ($NEW_PID, default 2222; "0" = the
    # new process exited). A restart also stands in for a completed bootstrap by
    # writing a manifest owned by the new pid ($MANIFEST_AFTER, a JSON mapping),
    # unless $NO_BOOTSTRAP is set.
    manifest = home / ".genesis" / "bootstrap_manifest.json"
    _exec(shims / "systemctl", _systemctl_shim(calls, manifest))
    # The old server's manifest, as a running install has it before a deploy.
    manifest.write_text('{"pid": 1111, "manifest": {"db": "ok", "perception": "ok"}}')
    # curl answers per $CURL_RC, and records whether the deploy marker was held
    # at the moment the health check ran.
    _exec(
        shims / "curl",
        "#!/bin/bash\n"
        f'[ -f "{marker}" ] && echo held >> "{tmp_path}/marker_seen"\n'
        "exit ${CURL_RC:-0}\n",
    )
    # ss reports who LISTENS on the health port: the restarted unit's pid by
    # default ($NEW_PID, or 2222), $LISTEN_PID for another process, and no
    # listener at all under $SS_NO_LISTENER (the manifest fallback). $SS_PIDLESS
    # prints the line without its users:(...) part, as ss does for a socket owned
    # by another uid; $SS_EXTRA_PID adds a SECOND listener. Never the host's real
    # ss: the live server listens on that port.
    _exec(
        shims / "ss",
        "#!/bin/bash\n"
        '[ -n "${SS_NO_LISTENER:-}" ] && exit 0\n'
        'if [ -n "${SS_PIDLESS:-}" ]; then echo "LISTEN 0 128 0.0.0.0:5000 0.0.0.0:*"; exit 0; fi\n'
        'p="${LISTEN_PID:-${NEW_PID:-2222}}"\n'
        'echo "LISTEN 0 128 [::]:5000 [::]:* users:((\\"python\\",pid=$p,fd=3))"\n'
        'if [ -n "${SS_EXTRA_PID:-}" ]; then echo "LISTEN 0 128 0.0.0.0:5000 0.0.0.0:* '
        'users:((\\"python\\",pid=$SS_EXTRA_PID,fd=4))"; fi\n'
        'if [ -n "${SS_PIDLESS_EXTRA:-}" ]; then echo "LISTEN 0 128 0.0.0.0:5000 0.0.0.0:*"; fi\n',
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            (
                "GENESIS_",
                "UNIT_STATE",
                "CURL_RC",
                "NEW_PID",
                "NO_BOOTSTRAP",
                "MANIFEST_AFTER",
                "LISTEN_PID",
                "SS_NO_LISTENER",
                "SS_PIDLESS",
                "SS_EXTRA_PID",
                "SS_PIDLESS_EXTRA",
            )
        )
    }
    env.update(
        HOME=str(home),
        PATH=f"{shims}:{env['PATH']}",
        GENESIS_DEPLOY_ROOT=str(root),
        GENESIS_DEPLOY_VENV=str(VENV),
        GENESIS_DEPLOY_HEALTH_POLL="1",
        GENESIS_ALERT_QUEUE_ROOT=str(home / ".genesis" / "alerts" / "queue"),
    )
    return {
        "env": env,
        "home": home,
        "root": root,
        "seed": seed,
        "tmp": tmp_path,
        "shims": shims,
        "calls": calls,
        "manifest": manifest,
        "marker": marker,
        "lock": home / ".genesis" / "locks" / "update.lock",
        "queue": home / ".genesis" / "alerts" / "queue",
    }


def _advance_upstream(
    st, msg: str = "upstream advanced", files: dict[str, str] | None = None
) -> str:
    tip = _commit(st["seed"], msg, files)
    _git(st["seed"], "push", "-q", "origin", "main")
    return tip


def _run(
    st, *args: str, env: dict | None = None, timeout: float = 60
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), "--wait", "5", *args],
        env=env or st["env"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _restarted(st) -> bool:
    return st["calls"].exists() and "restart genesis-server" in st["calls"].read_text()


def _alerts(st) -> list[Path]:
    return sorted(st["queue"].glob("*.json")) if st["queue"].exists() else []


def _assert_untouched(st, head: str, r: subprocess.CompletedProcess) -> None:
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert _git(st["root"], "rev-parse", "HEAD") == head, "a refusal moved the tree"
    assert not _restarted(st), "a refusal restarted the server"
    assert not st["marker"].exists(), "a refusal left the deploy marker behind"
    assert not _alerts(st), "a refusal changed nothing and must not page anyone"


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


def test_no_restart_is_a_locked_pull_only(station):
    tip = _advance_upstream(station)
    r = _run(station, "--no-restart")
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


def test_no_pull_still_checks_the_working_trees_dependencies(station):
    head = _commit(station["root"], "unmet locally", {"pyproject.toml": PYPROJECT_UNMET})
    r = _run(station, "--no-pull")
    _assert_untouched(station, head, r)
    assert "run scripts/update.sh instead" in r.stderr


def test_already_at_the_tip_still_checks_dependencies(station):
    """Someone may have pulled by hand; a restart must still refuse a venv that
    does not satisfy the tree it is about to run."""
    tip = _advance_upstream(station, "unmet upstream", {"pyproject.toml": PYPROJECT_UNMET})
    _git(station["root"], "pull", "-q", "--ff-only")
    r = _run(station)
    _assert_untouched(station, tip, r)


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


def test_an_unfinished_update_is_refused(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    (station["home"] / ".genesis" / "update_state.json").write_text('{"phase": "merged"}')
    _advance_upstream(station)
    r = _run(station)
    _assert_untouched(station, head, r)
    assert "update.sh --post-merge" in r.stderr


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
    r = _run(station, "--no-restart")
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
        ("another process holds the port", {"LISTEN_PID": "3333"}),
        # A listener ss cannot attribute (another uid's socket prints no pid) is
        # not proof — it must not fall through to the manifest check.
        ("a listener with no visible pid", {"SS_PIDLESS": "1"}),
        # The unit listens, but so does another process.
        ("a second listener", {"SS_EXTRA_PID": "3333"}),
        # The unit listens (v6), and an unattributable socket holds v4: the pids
        # that ARE visible all match, so only counting the lines catches it.
        ("a second listener with no visible pid", {"SS_PIDLESS_EXTRA": "1"}),
        # ss cannot see a listener, so the manifest decides: a new pid exists,
        # but no bootstrap completed under it (the manifest is still pid 1111).
        ("no bootstrap under the new pid", {"NO_BOOTSTRAP": "1", "SS_NO_LISTENER": "1"}),
        # The unit still reports the OLD pid, whose manifest it is.
        ("old pid still reported", {"NEW_PID": "1111"}),
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


@pytest.mark.parametrize("ss_sees", [True, False])
def test_a_healthy_restart_reports_the_new_pid_and_no_alert(station, ss_sees):
    """Both proofs accept the real case: the listener, and (where ss sees no
    listener) the manifest written by the new pid."""
    _advance_upstream(station)
    env = dict(station["env"]) if ss_sees else dict(station["env"], SS_NO_LISTENER="1")
    r = _run(station, env=env)
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
        '#!/bin/bash\n[ "$1 $2" = "--user restart" ] && exit 1\nexit 0\n',
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
    """update.sh's REAL dirty-tree pipeline, extracted, run against *root*."""
    lines = UPDATE_SH.read_text().splitlines()
    start = next(
        i
        for i, ln in enumerate(lines)
        if ln.strip().startswith('DIRTY_FILES=$(git -C "$GENESIS_ROOT"')
    )
    block = "\n".join(ln.strip() for ln in lines[start : start + 3])
    lib = REPO / "scripts" / "lib" / "deploy_marker.sh"
    r = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail\nHOME=/nonexistent\n. "{lib}"\nGENESIS_ROOT="{root}"\n{block}\nprintf %s "$DIRTY_FILES"',
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
    r = _run(station, "--no-restart")
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


def test_no_restart_refuses_a_range_that_changes_runtime_code(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    (station["seed"] / "src").mkdir()
    _advance_upstream(station, "runtime change", {"src/mod.py": "x = 1\n"})
    r = _run(station, "--no-restart")
    _assert_untouched(station, head, r)
    assert "without --no-restart" in r.stderr


def test_no_restart_refuses_startup_loaded_config(station):
    """Codex P2 / Devin on #2494: config/ is read at server start (model routing,
    profiles), so a config-only range under --no-restart would advance HEAD while
    the live server keeps the old values. The refusal names the path."""
    head = _git(station["root"], "rev-parse", "HEAD")
    (station["seed"] / "config").mkdir()
    _advance_upstream(station, "routing change", {"config/model_routing.yaml": "a: 1\n"})
    r = _run(station, "--no-restart")
    _assert_untouched(station, head, r)
    assert "config/model_routing.yaml" in r.stderr and "without --no-restart" in r.stderr


def test_no_restart_refuses_a_module_moved_out_of_src(station):
    """Review finding on #2494: with rename detection on (git's default),
    `diff --name-only` lists a move src/mod.py -> docs/mod.py as docs/mod.py
    alone, so the runtime module it removes passed as a docs change."""
    (station["seed"] / "src").mkdir()
    _advance_upstream(station, "add a module", {"src/mod.py": "x = 1\n" * 20})
    _git(station["root"], "pull", "-q", "--ff-only")
    head = _git(station["root"], "rev-parse", "HEAD")
    (station["seed"] / "docs").mkdir()
    _git(station["seed"], "mv", "src/mod.py", "docs/mod.py")
    _git(station["seed"], "commit", "-qm", "move it")
    _git(station["seed"], "push", "-q", "origin", "main")
    r = _run(station, "--no-restart")
    _assert_untouched(station, head, r)
    assert "src/mod.py" in r.stderr, r.stderr


def test_no_restart_accepts_hooks_and_docs(station):
    """Control for the refusal above: the allowlisted paths do deploy."""
    for d in ("docs", ".claude", "scripts/hooks"):
        (station["seed"] / d).mkdir(parents=True, exist_ok=True)
    tip = _advance_upstream(
        station,
        "hooks and docs",
        {
            "docs/x.md": "d\n",
            ".claude/y.md": "c\n",
            "scripts/hooks/z.py": "h = 1\n",
            "README.md": "r\n",
        },
    )
    r = _run(station, "--no-restart")
    assert r.returncode == 0, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == tip
    assert not _restarted(station)


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


@pytest.mark.parametrize(
    "pyproject, rc",
    [
        ('[project]\ndependencies = ["packaging"]\n', 0),
        ('[project]\ndependencies = ["packaging>=9999"]\n', 1),
        ('[project]\nrequires-python = ">=3.99"\ndependencies = []\n', 1),
        ('[project]\ndependencies = ["foo @ https://example.invalid/foo.whl"]\n', 2),
        ('[project]\ndependencies = ["nope; python_version < \\"3\\""]\n', 0),
        ("not toml [[[", 2),
        ("[project]\n", 2),
        # Optional groups (Devin on #2494). "packaging" is installed here, so a
        # group containing it is one this install uses: a package newly added to
        # it must be installed too.
        (
            "[project]\ndependencies = []\n[project.optional-dependencies]\n"
            'used = ["packaging", "zz-not-installed-pkg"]\n',
            1,
        ),
        # A group none of whose packages is installed is not in use: silent.
        (
            "[project]\ndependencies = []\n[project.optional-dependencies]\n"
            'unused = ["zz-not-installed-pkg", "zz-also-absent"]\n',
            0,
        ),
        # A used group, fully satisfied.
        (
            '[project]\ndependencies = []\n[project.optional-dependencies]\nused = ["packaging"]\n',
            0,
        ),
        ('[project]\ndependencies = []\n[project.optional-dependencies]\nbad = "packaging"\n', 2),
        # A package the BASE dependencies already install says nothing about the
        # extra (review finding on #2494: openai arrives through litellm), so it
        # must not mark the group "in use" — directly shared...
        (
            '[project]\ndependencies = ["packaging"]\n[project.optional-dependencies]\n'
            'shared = ["packaging", "zz-not-installed-pkg"]\n',
            0,
        ),
        # ...or transitively (anyio is a dependency of httpx).
        (
            '[project]\ndependencies = ["httpx"]\n[project.optional-dependencies]\n'
            'shared = ["anyio", "zz-not-installed-pkg"]\n',
            0,
        ),
        # Control for the two above: without the base dependency, the same
        # installed package IS the group's own, so the group is in use and refuses.
        (
            "[project]\ndependencies = []\n[project.optional-dependencies]\n"
            'own = ["anyio", "zz-not-installed-pkg"]\n',
            1,
        ),
    ],
)
def test_the_dependency_gate_answers(pyproject, rc):
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lib" / "venv_satisfies_pyproject.py")],
        input=pyproject,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == rc, (r.stdout, r.stderr)
