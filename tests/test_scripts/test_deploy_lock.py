"""Deploy-station lock, code-only wrapper, and validation hold (issue #1699).

Install-agnostic by construction: every test runs against a FIXTURE tree via
the ``GENESIS_DEPLOY_ROOT`` seam with a private ``HOME``, a private lock file,
and PATH-shimmed ``systemctl``/``curl`` (the guardian tests add an ``ssh``
shim + a fixture ``guardian_remote.yaml``) — no test touches the real
runtime, venv, or guardian. The scripts under test are the repo's own copies
(resolved relative to this file), run as real subprocesses: these are
end-to-end tests of the shell, not unit tests of a model of it.

The #1699 acceptance criteria covered here:
  * two concurrent exclusive holders serialize; the second QUEUES (not fails)
  * a shared (validation) hold excludes a deploy and vice versa; shared
    holders coexist
  * update.sh and the wrapper contend on the SAME lock file (cross-path)
  * a validation hold forwards termination to its command before releasing
  * timeout leaves no partial state; a killed wrapper cleans its state
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
_LIB = _SCRIPTS / "lib" / "deploy_lock.sh"
_WRAPPER = _SCRIPTS / "deploy_code_only.sh"
_RUN_UNDER = _SCRIPTS / "run_under_deploy_lock.sh"

LOCK_HELD_RC = 200  # DEPLOY_LOCK_HELD_RC in lib/deploy_lock.sh


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def station(tmp_path, monkeypatch):
    """A fixture deploy station: fake HOME, fake target tree (a real git repo
    with a stub venv pip), PATH shims, and env pointing the scripts at it all."""
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=t@local",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "fixture",
        ],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_exec(venv_bin / "pip", "#!/bin/bash\nexit 0\n")

    shims = tmp_path / "shims"
    shims.mkdir()
    # systemctl: restart succeeds; is-active succeeds. curl: succeeds, and
    # RECORDS whether the watchdog PID marker existed when it ran — the
    # mid-window probe that proves the watchdog-standdown signal was up
    # exactly while the deploy was in flight.
    _write_exec(
        shims / "systemctl",
        "#!/bin/bash\nexit 0\n",
    )
    _write_exec(
        shims / "curl",
        "#!/bin/bash\n"
        f'if [ -f "{home}/.genesis/update_in_progress.pid" ]; then echo yes > "{tmp_path}/state_seen"; fi\n'
        "exit 0\n",
    )

    env = dict(os.environ)
    env.update(
        HOME=str(home),
        GENESIS_DEPLOY_ROOT=str(root),
        GENESIS_DEPLOY_LOCK=str(tmp_path / "station.lock"),
        PATH=f"{shims}:{env['PATH']}",
        # Shrink the health-verify envelope (production: 12 x 15s) so the
        # failure-path tests don't burn 3 real minutes each.
        GENESIS_DEPLOY_HEALTH_ATTEMPTS="2",
        GENESIS_DEPLOY_HEALTH_INTERVAL="1",
    )
    # The marker path honours GENESIS_HOME; an inherited one would point this
    # test's writes at a real install instead of the fixture HOME.
    env.pop("GENESIS_HOME", None)
    # Likewise the alert queue root: the failure-path tests queue CRITICAL
    # alerts, which must land in the fixture HOME, never a real queue.
    env.pop("GENESIS_ALERT_QUEUE_ROOT", None)
    return {"env": env, "home": home, "root": root, "sha": sha, "tmp": tmp_path, "shims": shims}


def _hold_lock(env, mode: str, seconds: float) -> subprocess.Popen:
    """Background bash that sources the lib, takes the lock, prints HELD, and
    holds it for `seconds`."""
    p = subprocess.Popen(
        [
            "bash",
            "-c",
            f'source "{_LIB}"; acquire_deploy_lock_{mode} 30 || exit $?; '
            # exec: the sleep BECOMES this process, so _kill(p.pid) terminates
            # the very process owning the inherited lock fd — killing a bash
            # parent instead would leave the sleep holding the lock.
            f"echo HELD; exec sleep {seconds}",
        ],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert p.stdout.readline().strip() == "HELD"
    return p


def _alive(pid: int) -> bool:
    """Is `pid` still running? Used to prove a leak test is not vacuous — an
    orphan that has already exited releases the lock, so an assertion made after
    it dies proves nothing about the fix under test.

    Refuses pid <= 1: signal 0 to pid 1 is a live-process probe of init, and the
    NEGATIVE forms are catastrophic in a container (kill(-1) hits every process
    this user owns). A pid that small means the fixture failed to record a real
    one, which is a test bug to surface, not to signal.
    """
    if pid <= 1:
        raise AssertionError(f"refusing to probe implausible pid {pid}")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _kill(pid: int) -> None:
    """Teardown kill with the same pid-sanity floor as _alive."""
    if pid <= 1:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, 9)


class TestLockPrimitives:
    def test_exclusive_excludes_exclusive_and_queues(self, station):
        env = station["env"]
        holder = _hold_lock(env, "ex", 1.5)
        try:
            # wait 0 → immediate refusal with the conventional rc
            r = subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_ex 0'],
                env=env,
            )
            assert r.returncode == LOCK_HELD_RC
            # a bounded wait QUEUES and succeeds once the holder exits
            t0 = time.monotonic()
            r = subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_ex 30'],
                env=env,
            )
            assert r.returncode == 0
            assert time.monotonic() - t0 > 0.5, "returned before the holder released"
        finally:
            holder.wait()

    def test_shared_holders_coexist(self, station):
        env = station["env"]
        holder = _hold_lock(env, "sh", 1.5)
        try:
            r = subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_sh 0'],
                env=env,
            )
            assert r.returncode == 0, "two validation holds must coexist"
        finally:
            holder.wait()

    @pytest.mark.parametrize("held,wanted", [("sh", "ex"), ("ex", "sh")])
    def test_reader_writer_exclusion_both_directions(self, station, held, wanted):
        env = station["env"]
        holder = _hold_lock(env, held, 1.5)
        try:
            r = subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_{wanted} 0'],
                env=env,
            )
            assert r.returncode == LOCK_HELD_RC
        finally:
            holder.wait()

    def test_cross_path_raw_flock_contends(self, station):
        """update.sh takes the same FILE with its own inline exec/flock; a raw
        exclusive flock stands in for it here (its path identity is pinned by
        test_update_sh_shares_the_lock_path below)."""
        env = station["env"]
        lock = Path(env["GENESIS_DEPLOY_LOCK"])
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            r = subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_ex 0'],
                env=env,
            )
            assert r.returncode == LOCK_HELD_RC

    def test_update_sh_shares_the_lock_path(self):
        """The cross-path property is a one-line constant: update.sh's inline
        lock must read the lib's path, not carry its own literal."""
        text = (_SCRIPTS / "update.sh").read_text()
        assert 'UPDATE_LOCK_FILE="$GENESIS_DEPLOY_LOCK"' in text
        assert 'source "$SCRIPT_DIR/lib/deploy_lock.sh"' in text


class TestCodeOnlyWrapper:
    def test_full_flow_healthy(self, station):
        env = station["env"]
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        assert station["sha"] in r.stdout
        # the watchdog-standdown marker was UP while the deploy ran…
        assert (station["tmp"] / "state_seen").exists(), (
            "update_in_progress.pid was not present when the health probe ran"
        )
        # …removed on exit, and update.sh's recovery record never touched
        assert not (station["home"] / ".genesis" / "update_in_progress.pid").exists()
        assert not (station["home"] / ".genesis" / "update_state.json").exists()

    def test_health_fail_alerts_and_holds(self, station):
        env, sha, shims = station["env"], station["sha"], station["shims"]
        _write_exec(shims / "curl", "#!/bin/bash\nexit 22\n")
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1
        # ALERT AND HOLD: tree untouched, alert queued, marker removed
        head = subprocess.run(
            ["git", "-C", str(station["root"]), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head == sha, "a failed health check must never move the tree"
        queue = station["home"] / ".genesis" / "alerts" / "queue"
        alerts = list(queue.glob("*.json")) if queue.exists() else []
        assert len(alerts) == 1, "exactly one critical alert must be queued"
        assert json.loads(alerts[0].read_text())["severity"] == "critical"
        assert not (station["home"] / ".genesis" / "update_in_progress.pid").exists()

    def test_lock_timeout_leaves_no_partial_state(self, station):
        env = station["env"]
        holder = _hold_lock(env, "ex", 3)
        try:
            r = subprocess.run(
                ["bash", str(_WRAPPER), "--no-pull", "--wait", "1"],
                env=env,
                capture_output=True,
                text=True,
            )
            assert r.returncode == LOCK_HELD_RC
            assert not (station["home"] / ".genesis" / "update_in_progress.pid").exists()
            assert not (station["home"] / ".genesis" / "update_state.json").exists()
        finally:
            holder.wait()

    def test_sigterm_mid_run_cleans_state(self, station):
        env, shims = station["env"], station["shims"]
        # A hanging health probe gives us a window to signal in. Bash runs a
        # trap only AFTER the current foreground child returns, so the cleanup
        # is DEFERRED until this probe ends — that is the real contract being
        # pinned (cleanup always runs; it is not instant), hence a short hang.
        _write_exec(shims / "curl", "#!/bin/bash\nsleep 4\n")
        p = subprocess.Popen(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        marker = station["home"] / ".genesis" / "update_in_progress.pid"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.1)
        assert marker.exists(), "wrapper never reached its deploy window"
        p.terminate()
        p.wait(timeout=15)
        assert not marker.exists(), "SIGTERM must run the cleanup trap"

    def test_sigkill_leaves_nothing_bootstrap_would_act_on(self, station):
        """Devin 🔴 (#1804): a SIGKILLed code-only run used to leave its entry in
        update.sh's update_state.json, which bootstrap reads as a crashed FULL
        update and recovers by resetting the tree — discarding tracked edits.
        The run now writes only the bare-PID marker: SIGKILL leaves a DEAD pid
        there (read as "no deploy" by every reader) and no update_state.json."""
        env, shims = station["env"], station["shims"]
        _write_exec(shims / "curl", "#!/bin/bash\nsleep 30\n")
        p = subprocess.Popen(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Own session, so teardown can kill the whole group: SIGKILL to the
            # wrapper alone orphans its health probe, which keeps running (and
            # holding the inherited lock fd) until it exits by itself.
            start_new_session=True,
        )
        marker = station["home"] / ".genesis" / "update_in_progress.pid"
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.1)
            assert marker.exists(), "wrapper never reached its deploy window"
            _kill(p.pid)
            p.wait(timeout=15)
        finally:
            _kill(p.pid)
            if p.pid > 1:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, 9)
        assert not (station["home"] / ".genesis" / "update_state.json").exists(), (
            "a code-only run must never write update.sh's crash-recovery record"
        )
        left = int(marker.read_text().strip())
        assert left == p.pid and not _alive(left), (
            "a killed run leaves only its own dead PID, which env.update_in_progress() ignores"
        )

    def test_refuses_foreign_unfinished_state(self, station):
        """REFUSE-DON'T-CLOBBER (architect SF1): update.sh's crash/conflict
        path leaves update_state.json carrying the rollback identity that
        `update.sh --post-merge` reads back. Deploying over it would build on a
        half-recovered tree, so a code-only run refuses while it exists."""
        env = station["env"]
        state = station["home"] / ".genesis" / "update_state.json"
        state.write_text(
            '{"phase": "merging", "rollback_tag": "pre-update-x", '
            '"old_commit": "abc", "started_at": "2026-01-01T00:00:00", "pid": 1}'
        )
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1
        assert "post-merge" in r.stderr
        assert state.read_text().startswith('{"phase": "merging"'), (
            "the recovery state must survive the refusal untouched"
        )
        assert not (station["home"] / ".genesis" / "update_in_progress.pid").exists()

    def test_dead_pid_marker_is_replaced(self, station):
        """A dead PID in the marker is a stale leftover every reader already
        ignores — the next run proceeds over it and cleans up after itself."""
        env = station["env"]
        marker = station["home"] / ".genesis" / "update_in_progress.pid"
        dead = subprocess.Popen(["true"])
        dead.wait()
        marker.write_text(f"{dead.pid}\n")
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        assert not marker.exists()

    def test_a_marker_holding_zero_is_not_a_live_holder(self, station):
        """`kill -0 0` SUCCEEDS (it addresses the caller's process group), so a
        bare liveness probe would read a marker holding 0 as a live holder and
        refuse every deploy forever, while env.update_in_progress() (pid > 1)
        reports no deploy at all. The shared predicate floors it."""
        marker = station["home"] / ".genesis" / "update_in_progress.pid"
        marker.write_text("0\n")
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=station["env"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        assert not marker.exists()

    def test_refuses_a_live_foreign_marker(self, station):
        """A LIVE foreign holder (a dashboard update or a restore holding the
        server stopped) is refused, exactly as restore.sh refuses us — and its
        marker is left alone."""
        env = station["env"]
        marker = station["home"] / ".genesis" / "update_in_progress.pid"
        holder = subprocess.Popen(["sleep", "30"])
        try:
            marker.write_text(f"{holder.pid}\n")
            r = subprocess.run(
                ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
                env=env,
                capture_output=True,
                text=True,
            )
            assert r.returncode == 1
            assert "held by a live process" in r.stderr
            assert marker.read_text().strip() == str(holder.pid)
        finally:
            _kill(holder.pid)
            holder.wait()

    @staticmethod
    def _with_upstream(station) -> Path:
        """Give the fixture tree a real upstream: a clone it tracks as
        origin/main, so the wrapper's fetch + `merge --ff-only @{u}` runs
        against real git rather than a shim."""
        root = station["root"]
        up = station["tmp"] / "upstream"
        subprocess.run(["git", "clone", "-q", str(root), str(up)], check=True)
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", str(up)], check=True)
        subprocess.run(["git", "-C", str(root), "fetch", "-q", "origin"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "branch", "-q", "--set-upstream-to=origin/main", "main"],
            check=True,
        )
        return up

    @staticmethod
    def _commit(repo: Path, msg: str) -> str:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=t@local",
                "-c",
                "user.name=t",
                "commit",
                "--allow-empty",
                "-qm",
                msg,
            ],
            check=True,
        )
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    @staticmethod
    def _head(repo: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_pull_fast_forwards_to_the_upstream_tip(self, station):
        """The one line that changes the tree in production: fetch + ff-only
        merge must land exactly where `git pull --ff-only` would."""
        up = self._with_upstream(station)
        tip = self._commit(up, "upstream advanced")
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--wait", "5"],
            env=station["env"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        assert self._head(station["root"]) == tip
        assert tip in r.stdout

    def test_a_diverged_tree_aborts_untouched_and_quiet(self, station):
        """A local commit the upstream lacks: ff-only must refuse, leave the
        tree exactly where it was, and raise NO alert — nothing advanced, so
        there is nothing for a human to converge."""
        up = self._with_upstream(station)
        self._commit(up, "upstream advanced")
        local = self._commit(station["root"], "local-only commit")
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--wait", "5"],
            env=station["env"],
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0
        assert self._head(station["root"]) == local
        queue = station["home"] / ".genesis" / "alerts" / "queue"
        assert not (queue.exists() and list(queue.glob("*.json"))), (
            "a refused merge advanced nothing and must not page anyone"
        )
        assert not (station["home"] / ".genesis" / "update_in_progress.pid").exists()

    def test_pull_refuses_a_non_main_branch(self, station):
        """A pull advances whatever is checked out while every message says
        main — so off main it refuses rather than deploy the wrong branch."""
        self._with_upstream(station)
        subprocess.run(
            ["git", "-C", str(station["root"]), "checkout", "-qb", "feature"], check=True
        )
        before = self._head(station["root"])
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--wait", "5"],
            env=station["env"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1
        assert "refusing to pull" in r.stderr
        assert self._head(station["root"]) == before

    def test_a_zero_fetch_timeout_is_refused(self, station):
        """`timeout 0` means NO limit, so a zero knob must not silently
        disable the bound."""
        self._with_upstream(station)
        env = dict(station["env"], GENESIS_DEPLOY_FETCH_TIMEOUT="0")
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1
        assert "must be a positive integer" in r.stderr

    def test_a_hung_fetch_is_bounded(self, station, tmp_path):
        """Codex P2 (#1804): a remote that accepts the connection then goes
        silent must not hold the exclusive station lock indefinitely. The
        fetch runs under a timeout; the knob exists for the suite only."""
        env = dict(station["env"])
        real_git = subprocess.run(
            ["bash", "-c", "command -v git"], capture_output=True, text=True, check=True
        ).stdout.strip()
        _write_exec(
            station["shims"] / "git",
            "#!/bin/bash\n"
            'for a in "$@"; do [ "$a" = fetch ] && exec sleep 60; done\n'
            f'exec "{real_git}" "$@"\n',
        )
        env["GENESIS_DEPLOY_FETCH_TIMEOUT"] = "2"
        t0 = time.monotonic()
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert r.returncode == 1
        assert "fetch failed or timed out" in r.stderr
        assert time.monotonic() - t0 < 30, "the fetch bound did not hold"
        assert not (station["home"] / ".genesis" / "update_in_progress.pid").exists()

    def test_install_failure_alerts_and_holds(self, station):
        """Codex P1 (#1804), --no-pull shape: pip install -e removes the old
        dist BEFORE installing, so a failed install can leave the venv without
        the package — the running server breaks on its next lazy import, and
        the tree on disk no longer matches the code in memory. The 'installing'
        phase marker (set BEFORE the pip call) is what makes cleanup see this
        as a post-advance failure: a critical alert, not silence."""
        env = station["env"]
        _write_exec(station["root"] / ".venv" / "bin" / "pip", "#!/bin/bash\nexit 1\n")
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1
        queue = station["home"] / ".genesis" / "alerts" / "queue"
        alerts = list(queue.glob("*.json")) if queue.exists() else []
        assert len(alerts) == 1, "an install failure must raise exactly one critical alert"
        assert json.loads(alerts[0].read_text())["severity"] == "critical"

    def test_restart_failure_alerts_and_holds(self, station):
        """Codex P1 (#1804): a failed restart leaves the server down or running
        stale in-memory code against the on-disk tree — indefinitely, because
        nothing surfaced the failure live. The
        cleanup path must raise a critical alert, same doctrine as
        health_failed."""
        env, shims = station["env"], station["shims"]
        _write_exec(shims / "systemctl", "#!/bin/bash\nexit 1\n")
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0
        queue = station["home"] / ".genesis" / "alerts" / "queue"
        alerts = list(queue.glob("*.json")) if queue.exists() else []
        assert len(alerts) == 1, "a post-install failure must raise exactly one critical alert"
        alert = json.loads(alerts[0].read_text())
        assert alert["severity"] == "critical"
        assert alert["source"] == "deploy-code-only"

    def test_worktree_refusal(self, station, tmp_path):
        """The refusal must hold for a linked worktree at an ARBITRARY path —
        `git worktree add /anywhere` carries no marker substring, so a pathname
        test waves it through while pip install -e redirects the live server's
        editable install at it (Codex P2, #1804). Detected via Git's
        git-dir/common-dir split, not the path."""
        env = dict(station["env"])
        wt = tmp_path / "anywhere" / "feature"  # deliberately no marker substring
        subprocess.run(
            ["git", "-C", str(station["root"]), "worktree", "add", "--detach", "-q", str(wt)],
            check=True,
        )
        env["GENESIS_DEPLOY_ROOT"] = str(wt)
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1
        assert "must not run from a worktree" in r.stderr

    def test_main_checkout_is_not_misread_as_a_worktree(self, station):
        """The git-based detector's other half: the fixture's MAIN tree must
        NOT be refused, or every deploy fails."""
        env = station["env"]
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr


class TestRunUnderDeployLock:
    def test_failure_propagates(self, station):
        env = station["env"]
        r = subprocess.run(
            ["bash", str(_RUN_UNDER), "--wait", "5", "--", "false"],
            env=env,
        )
        assert r.returncode == 1

    def test_receipt_flag_is_gone(self, station):
        """The receipt ledger was cut from this PR (owner decision, #1804):
        the flag must be refused, not silently ignored."""
        r = subprocess.run(
            ["bash", str(_RUN_UNDER), "--receipt", "--", "true"],
            env=station["env"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1
        assert "unknown argument" in r.stderr

    def test_shared_hold_blocks_a_deploy_for_its_whole_run(self, station):
        """#1699's core: the wrapper QUEUES behind a live validation hold."""
        env = station["env"]
        holder = subprocess.Popen(
            ["bash", str(_RUN_UNDER), "--wait", "5", "--", "sleep", "2"],
            env=env,
        )
        # Condition-based sync (never a bare sleep): the holder HAS the shared
        # lock exactly when an exclusive probe starts failing.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_ex 0'],
                env=env,
            )
            if probe.returncode == LOCK_HELD_RC:
                break
            time.sleep(0.05)
        else:
            holder.wait()
            pytest.fail("holder never acquired the shared lock")
        try:
            r = subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_ex 0'],
                env=env,
            )
            assert r.returncode == LOCK_HELD_RC
        finally:
            holder.wait()

    def test_sigterm_to_the_wrapper_kills_the_wrapped_command(self, station):
        """The lock fd is CLOSED in the wrapped command (leak guard), so a
        command that outlives its wrapper would keep running with NO lock held
        — and a deploy could then restart the live server mid-validation
        (Codex P2, #1804). The wrapper must forward TERM to the tracked child
        before the lock releases."""
        env = station["env"]
        pidfile = station["tmp"] / "child.pid"
        p = subprocess.Popen(
            [
                "bash",
                str(_RUN_UNDER),
                "--shared",
                "--wait",
                "5",
                "--",
                # exec: the sleep IS the tracked child, so a forwarded TERM
                # settles it; no grandchild is left to linger in CI.
                "bash",
                "-c",
                f'echo $$ > "{pidfile}"; exec sleep 60',
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pidfile.exists():
            time.sleep(0.05)
        assert pidfile.exists(), "the wrapped command never started"
        child = int(pidfile.read_text().strip())
        p.terminate()
        p.wait(timeout=15)
        assert not _alive(child), (
            "SIGTERM to the wrapper never reached the wrapped command — it is "
            "still running while the station lock has been released"
        )
        acq = subprocess.run(
            ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_ex 0'],
            env=env,
        )
        assert acq.returncode == 0, "the lock must be free once wrapper AND child are gone"

    def test_sigterm_wedges_no_station_when_the_child_ignores_term(self, station):
        """The forwarded TERM has a bounded grace: a child that IGNORES TERM
        must be SIGKILLed after 10s, not waited on forever — otherwise the
        wrapper (and the station lock) wedges behind it, worse than the
        pre-forwarding behaviour where the wrapper died and the kernel
        released the lock (adversarial audit F2, #1804)."""
        env = station["env"]
        pidfile = station["tmp"] / "child.pid"
        p = subprocess.Popen(
            [
                "bash",
                str(_RUN_UNDER),
                "--shared",
                "--wait",
                "5",
                "--",
                # SIG_IGN survives the exec, so the sleep itself ignores TERM.
                "bash",
                "-c",
                f'trap "" TERM; echo $$ > "{pidfile}"; exec sleep 60',
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pidfile.exists():
            time.sleep(0.05)
        assert pidfile.exists(), "the wrapped command never started"
        child = int(pidfile.read_text().strip())
        t0 = time.monotonic()
        p.terminate()
        p.wait(timeout=25)
        elapsed = time.monotonic() - t0
        assert not _alive(child), "a TERM-ignoring child must be SIGKILLed after the grace window"
        assert elapsed < 20, f"wrapper wedged {elapsed:.0f}s behind a TERM-ignoring child"
        acq = subprocess.run(
            ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_ex 0'],
            env=env,
        )
        assert acq.returncode == 0, "the lock must be free once wrapper AND child are gone"


class TestGuardianComposition:
    """The wrapper's guardian pause/resume leg, with a fixture gateway: a
    guardian_remote.yaml in the fake HOME, an ssh shim that logs verbs, and a
    fake venv python that answers the three yaml lookups. Asserts the TTL
    override (300, not the lib's 1800 default) and that resume fires even on
    the alert-and-hold exit."""

    def test_pause_ttl_and_resume_on_health_fail(self, station):
        env, home, root, shims = (
            station["env"],
            station["home"],
            station["root"],
            station["shims"],
        )
        key = home / "fake_key"
        key.write_text("k")
        (home / ".genesis" / "guardian_remote.yaml").write_text(
            f"host_ip: 127.0.0.1\nhost_user: tester\nssh_key: {key}\n"
        )
        # The lib resolves coords via "$VENV_DIR/bin/python" -c 'import yaml…'.
        # The fixture venv answers by pattern on the -c source — no yaml dep.
        _write_exec(
            root / ".venv" / "bin" / "python",
            "#!/bin/bash\n"
            'case "$2" in\n'
            "  *host_ip*) echo 127.0.0.1 ;;\n"
            "  *host_user*) echo tester ;;\n"
            f"  *ssh_key*) echo {key} ;;\n"
            "esac\n",
        )
        sshlog = station["tmp"] / "ssh.log"
        _write_exec(
            shims / "ssh",
            "#!/bin/bash\n"
            f'echo "$@" >> "{sshlog}"\n'
            # `paused` query → no JSON (no pre-existing pause); others accept.
            "exit 0\n",
        )
        _write_exec(shims / "curl", "#!/bin/bash\nexit 22\n")  # health-fail path
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1
        log = sshlog.read_text()
        assert "pause-if-absent 300" in log, "the wrapper's short TTL must reach the gateway"
        assert "pause 1800" not in log, "the lib default must not win over the override"
        assert "resume" in log.splitlines()[-1], (
            "resume must fire on the alert-and-hold exit (cleanup composition)"
        )


class TestLockFdIsNotLeakedToChildren:
    """The lock is released when the LAST copy of the fd closes — so any process
    that outlives its parent holding an inherited copy keeps the whole station
    blocked, with no lock file to clean up and no holder named in the error.

    This is not theory: MEASURED 2026-09-06, a backgrounded grandchild held the
    flock after the acquiring script exited and a fresh acquirer was refused.
    update.sh already guards its own nohup fallback for exactly this reason
    (test_update_mutex.test_nohup_fallback_closes_lock_fd) — these are the two
    remaining places that hand the fd to something that can outlive them.
    """

    def test_wrapped_command_child_cannot_hold_the_lock_after_exit(self, station):
        """A validation whose command leaks a background process (an E2E suite
        orphaning a helper is the ordinary case) must not block every later
        deploy until someone hunts down the stray pid.

        NOTE FOR ANYONE EDITING THIS: the orphan's stdout/stderr MUST be
        redirected away from the pipe, and the assertion MUST happen while the
        orphan is still alive. The first version of this test did neither, so
        ``subprocess.run(capture_output=True)`` blocked until the orphan closed
        the inherited pipe — i.e. it slept out the entire leak and then asserted
        on a lock that had just been released. It passed against the UNFIXED
        wrapper. Only running the mutation exposed it.
        """
        env = station["env"]
        pidfile = station["tmp"] / "orphan.pid"
        r = subprocess.run(
            [
                "bash",
                str(_RUN_UNDER),
                "--shared",
                "--wait",
                "5",
                "--",
                # The leak: a grandchild that outlives the wrapped command. Its
                # std fds go to /dev/null so the ONLY thing it can still hold is
                # the lock fd — which is the whole subject of the test.
                "bash",
                "-c",
                f'( sleep 60 ) >/dev/null 2>&1 & echo $! > "{pidfile}"',
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, r.stderr
        orphan = int(pidfile.read_text().strip())
        try:
            assert _alive(orphan), (
                "the orphan died before the assertion — this test would pass "
                "vacuously; see the note in the docstring"
            )
            acq = subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_ex 0'],
                env=env,
            )
            assert acq.returncode == 0, (
                "an orphaned grandchild of the wrapped command is still holding "
                "the deploy lock — every later deploy would queue its full "
                "--wait and fail naming no holder"
            )
        finally:
            _kill(orphan)

    def test_guardian_renewer_does_not_hold_the_lock(self, station):
        """A SIGKILLed deploy leaves its guardian renewer running (bounded at
        RENEW_MAX x TTL/2 ~ 10 min). If that orphan inherited the EXCLUSIVE lock
        fd, a retry on the default 600s wait can time out against a deploy that
        is already dead.

        This drives the REAL ``_guardian_pause`` — an inline stand-in for how it
        backgrounds the renewer would grade bash, not this repo's code.
        """
        env, home, root, shims = (
            station["env"],
            station["home"],
            station["root"],
            station["shims"],
        )
        key = home / "fake_key"
        key.write_text("k")
        (home / ".genesis" / "guardian_remote.yaml").write_text(
            f"host_ip: 127.0.0.1\nhost_user: tester\nssh_key: {key}\n"
        )
        _write_exec(
            root / ".venv" / "bin" / "python",
            "#!/bin/bash\n"
            'case "$2" in\n'
            "  *host_ip*) echo 127.0.0.1 ;;\n"
            "  *host_user*) echo tester ;;\n"
            f"  *ssh_key*) echo {key} ;;\n"
            "esac\n",
        )
        _write_exec(shims / "ssh", "#!/bin/bash\nexit 0\n")
        lib_g = _SCRIPTS / "lib" / "guardian_pause.sh"
        pidfile = station["tmp"] / "renew.pid"
        r = subprocess.run(
            [
                "bash",
                "-c",
                f'VENV_DIR="{root}/.venv"; GENESIS_ROOT="{root}"; '
                f'source "{_LIB}"; source "{lib_g}"; '
                "acquire_deploy_lock_ex 5 || exit 9; "
                "GUARDIAN_PAUSE_TTL=300; _guardian_pause; "
                f'echo "$_GUARDIAN_RENEW_PID" > "{pidfile}"',
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, f"stdout={r.stdout} stderr={r.stderr}"
        raw = pidfile.read_text().strip()
        assert raw, "no renewer was started — this test would prove nothing"
        renewer = int(raw)
        try:
            assert _alive(renewer), "the renewer exited before the assertion — vacuous otherwise"
            acq = subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_ex 0'],
                env=env,
            )
            assert acq.returncode == 0, (
                "the orphaned guardian renewer is holding the exclusive lock after its deploy died"
            )
        finally:
            _kill(renewer)

    def test_guardian_renewer_does_not_hold_updates_lock_fd(self, station):
        """update.sh takes the SAME lock file with its own inline flock, kept in
        _UPDATE_LOCK_FD rather than _DEPLOY_LOCK_FD. A SIGKILLed update.sh left
        the orphaned renewer holding the exclusive station lock for up to
        RENEW_MAX x TTL/2 (update.sh: 60 min), timing out code-only deploys and
        validations against a dead deploy (Codex P2 / Devin, #1804)."""
        env, home, root, shims = (
            station["env"],
            station["home"],
            station["root"],
            station["shims"],
        )
        key = home / "fake_key"
        key.write_text("k")
        (home / ".genesis" / "guardian_remote.yaml").write_text(
            f"host_ip: 127.0.0.1\nhost_user: tester\nssh_key: {key}\n"
        )
        _write_exec(
            root / ".venv" / "bin" / "python",
            "#!/bin/bash\n"
            'case "$2" in\n'
            "  *host_ip*) echo 127.0.0.1 ;;\n"
            "  *host_user*) echo tester ;;\n"
            f"  *ssh_key*) echo {key} ;;\n"
            "esac\n",
        )
        _write_exec(shims / "ssh", "#!/bin/bash\nexit 0\n")
        lib_g = _SCRIPTS / "lib" / "guardian_pause.sh"
        pidfile = station["tmp"] / "renew.pid"
        r = subprocess.run(
            [
                "bash",
                "-c",
                f'VENV_DIR="{root}/.venv"; GENESIS_ROOT="{root}"; '
                f'source "{_LIB}"; source "{lib_g}"; '
                # update.sh's own shape: inline flock on the shared path, kept
                # in _UPDATE_LOCK_FD — never in _DEPLOY_LOCK_FD.
                'exec {_UPDATE_LOCK_FD}>>"$GENESIS_DEPLOY_LOCK"; '
                'flock -x "$_UPDATE_LOCK_FD" || exit 9; '
                "GUARDIAN_PAUSE_TTL=300; _guardian_pause; "
                f'echo "$_GUARDIAN_RENEW_PID" > "{pidfile}"',
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, f"stdout={r.stdout} stderr={r.stderr}"
        raw = pidfile.read_text().strip()
        assert raw, "no renewer was started — this test would prove nothing"
        renewer = int(raw)
        try:
            assert _alive(renewer), "the renewer exited before the assertion — vacuous otherwise"
            acq = subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; acquire_deploy_lock_ex 0'],
                env=env,
            )
            assert acq.returncode == 0, (
                "the orphaned guardian renewer inherited update.sh's _UPDATE_LOCK_FD "
                "and is still holding the exclusive lock after its deploy died"
            )
        finally:
            _kill(renewer)


class TestLockErrorsAreReportedHonestly:
    def test_only_a_timeout_reports_as_contention(self, station):
        """`flock` exits 1 on timeout and something else on a setup failure
        (64 for a usage error, measured). Mapping every failure to
        DEPLOY_LOCK_HELD_RC sends an operator hunting for a holder that does not
        exist."""
        env = station["env"]
        held = subprocess.run(
            ["bash", "-c", f'source "{_LIB}"; _acquire_deploy_lock -x 0'],
            env=env,
        )
        assert held.returncode == 0, "uncontended: should acquire"
        broken = subprocess.run(
            ["bash", "-c", f'source "{_LIB}"; _acquire_deploy_lock --not-a-mode 0'],
            env=env,
            capture_output=True,
        )
        assert broken.returncode != LOCK_HELD_RC, (
            "a flock usage error must not be reported as 'the lock is held'"
        )
        assert broken.returncode == 1
