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
        assert not (station["home"] / ".genesis" / "deploy_window.json").exists()

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
            assert not (station["home"] / ".genesis" / "deploy_window.json").exists()
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
        assert not (station["home"] / ".genesis" / "deploy_window.json").exists()

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
