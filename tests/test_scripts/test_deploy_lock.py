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
  * a validation's recorded SHA is the serving SHA for its whole run
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
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
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
    # RECORDS whether the update-state file existed when it ran — the
    # mid-window probe that proves the watchdog-standdown signal was up
    # exactly while the deploy was in flight.
    _write_exec(
        shims / "systemctl",
        "#!/bin/bash\nexit 0\n",
    )
    _write_exec(
        shims / "curl",
        "#!/bin/bash\n"
        f'if [ -f "{home}/.genesis/update_state.json" ]; then echo yes > "{tmp_path}/state_seen"; fi\n'
        "exit 0\n",
    )

    env = dict(os.environ)
    env.update(
        HOME=str(home),
        GENESIS_DEPLOY_ROOT=str(root),
        GENESIS_DEPLOY_LOCK=str(tmp_path / "station.lock"),
        GENESIS_DEPLOY_RECEIPTS=str(tmp_path / "receipts.jsonl"),
        PATH=f"{shims}:{env['PATH']}",
        # Shrink the health-verify envelope (production: 12 x 15s) so the
        # failure-path tests don't burn 3 real minutes each.
        GENESIS_DEPLOY_HEALTH_ATTEMPTS="2",
        GENESIS_DEPLOY_HEALTH_INTERVAL="1",
    )
    return {"env": env, "home": home, "root": root, "sha": sha, "tmp": tmp_path, "shims": shims}


def _hold_lock(env, mode: str, seconds: float) -> subprocess.Popen:
    """Background bash that sources the lib, takes the lock, prints HELD, and
    holds it for `seconds`."""
    p = subprocess.Popen(
        [
            "bash",
            "-c",
            f'source "{_LIB}"; acquire_deploy_lock_{mode} 30 || exit $?; '
            f"echo HELD; sleep {seconds}",
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


def _receipts(env) -> list[dict]:
    p = Path(env["GENESIS_DEPLOY_RECEIPTS"])
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


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
        env, sha = station["env"], station["sha"]
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        rows = _receipts(env)
        assert [row["status"] for row in rows] == ["deployed"]
        assert rows[0]["sha"] == sha
        assert rows[0]["path"] == "code-only"
        # the watchdog-standdown state file was UP while the deploy ran…
        assert (station["tmp"] / "state_seen").exists(), (
            "update_state.json was not present when the health probe ran"
        )
        # …and cleaned on exit, with the advisory window marker
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
        rows = _receipts(env)
        assert [row["status"] for row in rows] == ["health_failed"]
        # ALERT AND HOLD: tree untouched, alert queued, state cleaned
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
        assert not (station["home"] / ".genesis" / "update_state.json").exists()

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
            assert not (station["home"] / ".genesis" / "update_state.json").exists()
            assert _receipts(env) == []
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
        state = station["home"] / ".genesis" / "update_state.json"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not state.exists():
            time.sleep(0.1)
        assert state.exists(), "wrapper never reached its deploy window"
        p.terminate()
        p.wait(timeout=15)
        assert not state.exists(), "SIGTERM must run the cleanup trap"

    def test_refuses_foreign_unfinished_state(self, station):
        """REFUSE-DON'T-CLOBBER (architect SF1): update.sh's crash/conflict
        path leaves update_state.json carrying the rollback identity that
        `update.sh --post-merge` reads back. The dead owner's flock is free,
        so only this refusal keeps a code-only deploy from destroying the
        recovery state."""
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
        assert _receipts(env) == []

    def test_stale_code_only_leftover_is_replaced(self, station):
        """Our OWN dead leftover (a SIGKILLed code-only run) carries no
        recovery state — the next run proceeds over it."""
        env = station["env"]
        state = station["home"] / ".genesis" / "update_state.json"
        state.write_text(
            '{"phase": "code-only", "started_at": "2026-01-01T00:00:00", '
            '"pid": 999999, "path": "code-only"}'
        )
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        assert [row["status"] for row in _receipts(env)] == ["deployed"]

    def test_restart_failure_writes_deploy_failed_receipt(self, station):
        """A failure AFTER the tree/install advanced must leave a ledger row
        (architect SF6) — otherwise the next validation hold records
        'validated' at a HEAD the server never loaded."""
        env, shims = station["env"], station["shims"]
        _write_exec(shims / "systemctl", "#!/bin/bash\nexit 1\n")
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull", "--wait", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0
        rows = _receipts(env)
        assert [row["status"] for row in rows] == ["deploy_failed"]
        assert rows[0]["note"] == "failed at installed"
        assert not (station["home"] / ".genesis" / "update_state.json").exists()

    def test_worktree_refusal(self, station, tmp_path):
        env = dict(station["env"])
        fake = tmp_path / "x" / ".claude" / "worktrees" / "wt"
        fake.mkdir(parents=True)
        env["GENESIS_DEPLOY_ROOT"] = str(fake)
        r = subprocess.run(
            ["bash", str(_WRAPPER), "--no-pull"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1
        assert "must not run from a worktree" in r.stderr


class TestRunUnderDeployLock:
    def test_shared_hold_records_validated_sha_on_success(self, station):
        env, sha = station["env"], station["sha"]
        r = subprocess.run(
            ["bash", str(_RUN_UNDER), "--receipt", "--wait", "5", "--", "true"],
            env=env,
        )
        assert r.returncode == 0
        rows = _receipts(env)
        assert [row["status"] for row in rows] == ["validated"]
        assert rows[0]["sha"] == sha

    def test_failure_propagates_and_writes_no_receipt(self, station):
        env = station["env"]
        r = subprocess.run(
            ["bash", str(_RUN_UNDER), "--receipt", "--wait", "5", "--", "false"],
            env=env,
        )
        assert r.returncode == 1
        assert _receipts(env) == []

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


class TestRunUnderReceiptScope:
    def test_receipt_refused_with_an_exclusive_hold(self, station):
        """A `validated` receipt claims the recorded SHA was SERVING for the whole
        run. The SHA is read before the command, and an exclusive hold is the writer
        mode — it permits a command that moves the checkout, after which the receipt
        names the old SHA. A false claim in the ledger built to make the claim
        trustworthy (CodeRabbit Major, 2026-09-06)."""
        r = subprocess.run(
            ["bash", str(_RUN_UNDER), "--exclusive", "--receipt", "--", "true"],
            env=station["env"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1
        assert "requires a SHARED hold" in r.stderr
        assert _receipts(station["env"]) == []

    def test_receipts_write_into_a_directory_that_does_not_exist_yet(self, station, tmp_path):
        """Append mode raises when the parent is missing and the appender only WARNS,
        so the row would vanish with a stderr line nobody reads — in the ledger that
        is the whole point of the feature. A validation hold writes no state file, so
        nothing else creates the directory for it."""
        env = dict(station["env"])
        env["GENESIS_DEPLOY_RECEIPTS"] = str(tmp_path / "fresh" / "nested" / "receipts.jsonl")
        r = subprocess.run(
            ["bash", str(_RUN_UNDER), "--receipt", "--wait", "5", "--", "true"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        rows = _receipts(env)
        assert [row["status"] for row in rows] == ["validated"]

    def test_receipt_refused_from_a_worktree_copy(self, station, tmp_path):
        """--receipt's SHA claim is about the SERVING tree (architect SF4): a
        worktree copy recording its branch HEAD as 'validated' would falsify
        the ledger's core attribution."""
        env = dict(station["env"])
        fake = tmp_path / "y" / ".claude" / "worktrees" / "wt"
        fake.mkdir(parents=True)
        env["GENESIS_DEPLOY_ROOT"] = str(fake)
        r = subprocess.run(
            ["bash", str(_RUN_UNDER), "--receipt", "--", "true"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1
        assert "serving tree" in r.stderr
        assert _receipts(env) == []


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
        assert "pause 300" in log, "the wrapper's short TTL must reach the gateway"
        assert "pause 1800" not in log, "the lib default must not win over the override"
        assert "resume" in log.splitlines()[-1], (
            "resume must fire on the alert-and-hold exit (cleanup composition)"
        )


class TestReceipts:
    def test_appender_emits_parseable_ordered_lines(self, station):
        env = station["env"]
        for i, status in enumerate(["deployed", "validated"]):
            subprocess.run(
                [
                    "bash",
                    "-c",
                    f'source "{_LIB}"; append_deploy_receipt "{status}" "sha{i}" "code-only"',
                ],
                env=env,
                check=True,
            )
        rows = _receipts(env)
        assert [(r["status"], r["sha"]) for r in rows] == [
            ("deployed", "sha0"),
            ("validated", "sha1"),
        ], "receipts must append in order — the ledger's ordering IS the claim"
        assert all(r["ts"] for r in rows)


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


class TestReceiptsPrune:
    def test_prunes_to_the_cap_keeping_the_newest(self, station):
        env = station["env"]
        receipts = Path(env["GENESIS_DEPLOY_RECEIPTS"])
        keep = int(
            subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; echo "$_DEPLOY_RECEIPTS_KEEP"'],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
        receipts.write_text("".join(f'{{"n": {i}}}\n' for i in range(keep + 5)))
        r = subprocess.run(
            ["bash", "-c", f'source "{_LIB}"; prune_deploy_receipts'],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        rows = [json.loads(x) for x in receipts.read_text().splitlines() if x.strip()]
        assert len(rows) == keep
        assert rows[-1]["n"] == keep + 4, "the NEWEST receipts are the ones kept"
        assert not (receipts.parent / f"{receipts.name}.tmp").exists()

    def test_under_the_cap_is_left_alone(self, station):
        env = station["env"]
        receipts = Path(env["GENESIS_DEPLOY_RECEIPTS"])
        receipts.write_text('{"n": 0}\n{"n": 1}\n')
        subprocess.run(
            ["bash", "-c", f'source "{_LIB}"; prune_deploy_receipts'],
            env=env,
            check=True,
        )
        assert receipts.read_text() == '{"n": 0}\n{"n": 1}\n'

    def test_busy_station_skips_rather_than_queues(self, station):
        """A daily groom must never queue behind a 2h validation hold."""
        env = station["env"]
        receipts = Path(env["GENESIS_DEPLOY_RECEIPTS"])
        receipts.write_text('{"n": 0}\n')
        holder = _hold_lock(env, "sh", 1.5)
        try:
            r = subprocess.run(
                ["bash", "-c", f'source "{_LIB}"; prune_deploy_receipts'],
                env=env,
            )
            assert r.returncode == 2, "busy station must report skip, not success"
        finally:
            holder.wait()

    def test_disk_hygiene_calls_the_lib_not_its_own_copy(self):
        """The prune moved into the lib so it could be tested; the groom must
        CALL it rather than keep a second implementation (replica drift)."""
        text = (_SCRIPTS / "disk_hygiene.sh").read_text()
        assert "prune_deploy_receipts" in text
        assert "_DEPLOY_RECEIPTS_KEEP" not in text, (
            "the cap belongs to the lib; a copy here would drift"
        )
