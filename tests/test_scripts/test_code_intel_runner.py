"""The incomplete automatic protocol refuses before every automatic queue action.

Positive provider dispatch belongs to the checked consumer. These transitional
tests exercise actual refusal, unchanged idle predicates and explicit queue CLI
replay; explicit replay is not an automatic-runner or native qualification.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
import time
from contextlib import closing
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _ROOT / "scripts/code_intel_runner.sh"
_MARKER_PY = _ROOT / "scripts/lib/index_marker.py"
_spec = importlib.util.spec_from_file_location("provider_runner_marker", _MARKER_PY)
im = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(im)


def _cli(tmp_path, *args):
    return subprocess.run(
        ["python3", str(_MARKER_PY), *args],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")},
    )


def _seed_marker(tmp_path, *, tools="both", mode="fast"):
    repo = str(tmp_path / "primary")
    result = _cli(tmp_path, "write", "--repo", repo, "--tools", tools, "--mode", mode)
    assert result.returncode == 0, result.stderr
    return im.marker_hash(repo)


def _mdir(tmp_path):
    return tmp_path / ".genesis/index-requests"


def _inventory(tmp_path):
    directory = _mdir(tmp_path)
    return (
        {
            path.name: (
                path.stat().st_ino,
                path.stat().st_mtime_ns,
                path.stat().st_mode,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in directory.iterdir()
            if path.is_file()
        }
        if directory.exists()
        else {}
    )


def _run_runner(tmp_path, *, runner=_RUNNER, extra_env=None):
    entry = tmp_path / "fake_entry.sh"
    entry.write_text(
        "#!/usr/bin/env bash\nprintf 'ENTRY\\n' >> "
        + shlex.quote(str(tmp_path / "entry.log"))
        + "\n"
    )
    return subprocess.run(
        ["bash", str(runner)],
        capture_output=True,
        text=True,
        timeout=20,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "GENESIS_HOME": str(tmp_path / ".genesis"),
            "CODE_INTEL_ENTRYPOINT": str(entry),
            **(extra_env or {}),
        },
    )


def _runner_log(tmp_path):
    path = tmp_path / ".genesis/code-intelligence-runner.log"
    return path.read_text() if path.exists() else ""


def test_unopenable_runner_lock_refuses_indexing(tmp_path):
    _seed_marker(tmp_path)
    lock = tmp_path / ".genesis/locks/code-intel-runner.lock"
    lock.mkdir(parents=True)
    before = _inventory(tmp_path)
    result = _run_runner(tmp_path)
    assert result.returncode == 75 and not (tmp_path / "entry.log").exists()
    assert _inventory(tmp_path) == before


def test_unavailable_lock_directory_does_not_use_an_alternate_lock(tmp_path):
    _seed_marker(tmp_path)
    (tmp_path / ".genesis/locks").write_text("not a directory")
    before = _inventory(tmp_path)
    result = _run_runner(tmp_path)
    assert result.returncode == 75 and not (tmp_path / "entry.log").exists()
    assert _inventory(tmp_path) == before


def test_busy_canonical_runner_lock_does_not_read_or_change_queue(tmp_path):
    _seed_marker(tmp_path)
    locks = tmp_path / ".genesis/locks"
    locks.mkdir()
    before = _inventory(tmp_path)
    with (locks / "code-intel-runner.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run_runner(tmp_path)
    assert result.returncode == 0 and not (tmp_path / "entry.log").exists()
    assert _inventory(tmp_path) == before
    assert "another runner tick" in (tmp_path / ".genesis/code-intelligence-runner.log").read_text()


@pytest.mark.parametrize(
    "schema,expected", [(None, 0), (0, 76), (1, 0), (2, 0), (3, 0), (4, 76)]
)
def test_actual_readiness_refuses_without_initializing_or_migrating(tmp_path, schema, expected):
    if schema is not None:
        _mdir(tmp_path).mkdir(parents=True)
        with closing(
            sqlite3.connect(_mdir(tmp_path) / "queue.sqlite3", isolation_level=None)
        ) as db:
            db.executescript(im._SCHEMA)
            db.execute(f"PRAGMA user_version={schema}")
            if schema == 3:
                db.execute("INSERT INTO queue_meta VALUES('automatic_protocol_ready','0')")
    before = _inventory(tmp_path)
    result = _run_runner(tmp_path)
    assert result.returncode == expected and not (tmp_path / "entry.log").exists(), result.stderr
    if expected == 0:
        # Exit 0 must come from the readiness refusal, not a busy lock or empty tick.
        assert "automatic queue protocol incomplete" in _runner_log(tmp_path)
    assert _inventory(tmp_path) == before
    if schema is None:
        assert not _mdir(tmp_path).exists()


@pytest.mark.parametrize("state", ["flag-enabled", "flag-missing", "corrupt", "busy"])
def test_readiness_observation_errors_stop_before_queue_actions(tmp_path, state):
    h = _seed_marker(tmp_path)
    database = _mdir(tmp_path) / "queue.sqlite3"
    with closing(sqlite3.connect(database, isolation_level=None)) as db:
        if state == "flag-enabled":
            # Only the observational schema3 fixture has this protocol field;
            # this predecessor's explicit writer still creates schema2.
            db.execute("PRAGMA user_version=3")
            db.execute("INSERT INTO queue_meta VALUES('automatic_protocol_ready','1')")
        elif state == "flag-missing":
            db.execute("PRAGMA user_version=3")
            db.execute("DELETE FROM queue_meta WHERE key='automatic_protocol_ready'")
        if state == "corrupt":
            database.write_bytes(b"owned corrupt queue")
        before = _inventory(tmp_path)
        if state == "busy":
            # Read/hash before locking: closing any same-process descriptor on
            # this inode would release SQLite's POSIX locks, invalidating the probe.
            db.execute("BEGIN EXCLUSIVE")
            refused = _cli(tmp_path, "automatic-ready")
            assert refused.returncode == 2 and "locked" in refused.stderr, refused.stderr
        result = _run_runner(tmp_path)
        log = (tmp_path / ".genesis/code-intelligence-runner.log").read_text()
        assert result.returncode == 76 and not (tmp_path / "entry.log").exists(), (
            log + result.stderr
        )
    assert _inventory(tmp_path) == before
    assert (
        h
        and "protocol observation failed"
        in (tmp_path / ".genesis/code-intelligence-runner.log").read_text()
    )


def test_missing_marker_helper_is_explicit_deferral(tmp_path):
    private = tmp_path / "runner"
    private.mkdir()
    runner = private / "code_intel_runner.sh"
    runner.write_text(_RUNNER.read_text())
    (private / "lib").mkdir()
    (private / "lib/proc_pressure.sh").symlink_to(_RUNNER.parent / "lib/proc_pressure.sh")
    result = _run_runner(tmp_path, runner=runner)
    assert result.returncode == 75 and not _mdir(tmp_path).exists()
    assert "helper missing" in (tmp_path / ".genesis/code-intelligence-runner.log").read_text()


def test_cli_exceptions_cannot_look_like_empty_claims(tmp_path):
    _seed_marker(tmp_path, tools="cbm")
    empty = _cli(tmp_path, "claim", "--hash", "0" * 16)
    assert empty.returncode == 1 and not empty.stdout
    (_mdir(tmp_path) / "queue.sqlite3").write_bytes(b"owned corrupt queue")
    failed = _cli(tmp_path, "list")
    assert failed.returncode == 2 and not failed.stdout
    assert "Traceback" in failed.stderr and "DatabaseError" in failed.stderr


@pytest.mark.parametrize("tools", ["cbm", "gitnexus", "both"])
@pytest.mark.parametrize("state", ["pending", "claimed", "remembered"])
def test_protocol_fence_preserves_each_durable_queue_state(tmp_path, tools, state):
    h = _seed_marker(tmp_path, tools=tools)
    if state != "pending":
        claim = _cli(tmp_path, "claim", "--hash", h)
        assert claim.returncode == 0, claim.stderr
        nonce = claim.stdout.rstrip("\n").split("\t")[4]
        if state == "remembered":
            result = _cli(
                tmp_path,
                "remember-outcome",
                "--hash",
                h,
                "--claim-id",
                nonce,
                "--action",
                "consume",
            )
            assert result.returncode == 0, result.stderr
    before = _inventory(tmp_path)
    for _ in range(2):
        result = _run_runner(tmp_path)
        assert result.returncode == 0 and not (tmp_path / "entry.log").exists()
        assert "automatic queue protocol incomplete" in _runner_log(tmp_path)
        assert _inventory(tmp_path) == before


@pytest.mark.parametrize(
    "age,load,iowait,cpu,expected",
    [
        (0, "0.1", "0", "0", 0),
        (0, "9", "0", "0", 1),
        (0, "0", "80", "0", 1),
        (0, "0", "0", "95", 1),
        (86401, "3", "0", "95", 0),
        (86401, "7", "0", "0", 1),
    ],
)
def test_existing_idle_predicate_keeps_its_strict_and_relaxed_contract(
    tmp_path, age, load, iowait, cpu, expected
):
    # Exercise the actual unchanged predicate, without its fenced caller.
    prefix, separator, _ = _RUNNER.read_text().partition("# Runner self-lock:")
    assert separator
    script = tmp_path / "idle.sh"
    script.write_text(prefix + '\n_idle_ok "$1"\n')
    (tmp_path / "lib").symlink_to(_RUNNER.parent / "lib")
    result = subprocess.run(
        ["bash", str(script), str(age)],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "GENESIS_HOME": str(tmp_path / "owned-state"),
            "CODE_INTEL_FAKE_LOADAVG": load,
            "CODE_INTEL_FAKE_IOWAIT": iowait,
            "CODE_INTEL_FAKE_CLAUDE_CPU": cpu,
        },
    )
    assert result.returncode == expected, result.stderr
    assert not _mdir(tmp_path).exists()


def test_explicit_cli_spools_busy_success_and_replays_without_reindex(tmp_path):
    """The inherited schema2 CLI still durably completes explicit queue work."""
    h = _seed_marker(tmp_path, tools="gitnexus")
    claimed = _cli(tmp_path, "claim", "--hash", h)
    assert claimed.returncode == 0, claimed.stderr
    nonce = claimed.stdout.rstrip("\n").split("\t")[4]
    database = _mdir(tmp_path) / "queue.sqlite3"
    ready = tmp_path / "blocker.ready"
    locker = tmp_path / "hold_queue.py"
    release = tmp_path / "blocker.release"
    # Hold the write lock until the test says so, not for a fixed time: a timed
    # hold raced the 5 s outcome wait plus two interpreter starts under load.
    locker.write_text(
        "import os,sqlite3,sys,time\n"
        "db=sqlite3.connect(sys.argv[1],isolation_level=None)\n"
        "db.execute('BEGIN IMMEDIATE')\n"
        "open(sys.argv[2],'w').close()\n"
        f"release={str(release)!r}\n"
        "deadline=time.monotonic()+60\n"
        "while not os.path.exists(release) and time.monotonic()<deadline:\n"
        "    time.sleep(0.05)\n"
        "db.rollback()\ndb.close()\n"
    )
    entry = _contention_entrypoint(tmp_path, locker, database, ready)
    locks = tmp_path / ".genesis/locks"
    locks.mkdir()
    lock = locks / "code-intel-runner.lock"
    with lock.open("a") as owner, (tmp_path / "provider.stdout").open("w") as output:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        provider = subprocess.run(
            ["bash", str(entry)],
            stdout=output,
            stderr=output,
            pass_fds=(owner.fileno(),),
            timeout=10,
        )
        try:
            assert provider.returncode == 0 and ready.exists(), _contention_diagnostics(tmp_path, ready)
            remembered = _cli(
                tmp_path, "remember-outcome", "--hash", h, "--claim-id", nonce, "--action", "consume"
            )
            assert remembered.returncode == 0, remembered.stderr
            spools = list(_mdir(tmp_path).glob(f".outcome-spool-{h}-*.spool"))
            assert len(spools) == 1
            raw = spools[0].read_bytes()
            assert json.loads(raw)["claim_id"] == nonce
            assert _cli(tmp_path, "apply-outcome", "--hash", h).returncode == 2
        finally:
            release.touch()
        _wait_for_write_lock(database)
    released = subprocess.run(
        ["flock", "-w", "10", str(lock), "true"], capture_output=True, text=True, timeout=12
    )
    assert released.returncode == 0
    assert _cli(tmp_path, "reconcile-inflight").returncode == 0
    assert _run_runner(tmp_path).returncode == 0
    assert "automatic queue protocol incomplete" in _runner_log(tmp_path)
    assert (tmp_path / "entry.log").read_text().count("ENTRY ") == 1
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        remaining = {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("pending", "inflight", "failed", "failed_outcomes")
        }
        receipt = db.execute("SELECT content_sha256,raw_payload FROM legacy_imports").fetchone()
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert remaining == dict(pending=0, inflight=0, failed=0, failed_outcomes=0)
    assert receipt == (hashlib.sha256(raw).hexdigest(), None)
    assert not spools[0].exists() and list(_mdir(tmp_path).glob(".outcome-spool-*")) == []


def _wait_for_write_lock(database, timeout=30.0):
    """Poll until the queue database accepts a write transaction again."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with closing(sqlite3.connect(database, timeout=0, isolation_level=None)) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("ROLLBACK")
            return
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _contention_entrypoint(tmp_path, locker, db_path, ready):
    """Fake successful provider that waits for its own SQLite locker."""
    entry = tmp_path / "contention_entry.sh"
    entry.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "ENTRY repo=$1 tools=$2 mode=$3" >> {shlex.quote(str(tmp_path / "entry.log"))}\n'
        f"python3 {shlex.quote(str(locker))} {shlex.quote(str(db_path))} {shlex.quote(str(ready))} &\n"
        "locker_pid=$!\n"
        f'printf "LOCKER pid=%s ready=%s\\n" "$locker_pid" {shlex.quote(str(ready))} >&2\n'
        "deadline=$((SECONDS + 5))\n"
        f"while [ ! -e {shlex.quote(str(ready))} ]; do\n"
        '  if ! kill -0 "$locker_pid" 2>/dev/null; then\n'
        '    wait "$locker_pid"\n'
        '    printf "locker pid=%s exited rc=%s before ready\\n" "$locker_pid" "$?" >&2\n'
        "    exit 76\n"
        "  fi\n"
        '  if [ "$SECONDS" -ge "$deadline" ]; then\n'
        '    printf "locker pid=%s did not become ready within 5s\\n" "$locker_pid" >&2\n'
        "    exit 76\n"
        "  fi\n"
        "  sleep 0.01\n"
        "done\n"
        "exit 0\n"
    )
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    return entry


def _contention_diagnostics(tmp_path, ready):
    log = tmp_path / ".genesis" / "code-intelligence-runner.log"
    return f"fixture={tmp_path} ready={ready.exists()}\n" + (
        log.read_text() if log.exists() else "runner log absent"
    )


@pytest.mark.parametrize(
    "locker_source",
    ["raise SystemExit(23)\n", "import time; time.sleep(6)\n"],
    ids=["exits-before-ready", "never-ready"],
)
def test_contention_fixture_refuses_without_ready(tmp_path, locker_source):
    locker = tmp_path / "not_ready.py"
    locker.write_text(locker_source)
    entry = _contention_entrypoint(tmp_path, locker, tmp_path / "unused.db", tmp_path / "ready")
    # The outer guard owns only this fake fixture's process group. It makes the
    # broken unbounded loop a visible failure, rather than leaving an orphan.
    result = subprocess.run(
        ["timeout", "--kill-after=1", "8", "bash", str(entry)],
        capture_output=True,
        text=True,
        start_new_session=True,
        timeout=12,
    )
    assert result.returncode == 76, result.stderr
    assert "locker" in result.stderr and "ready" in result.stderr


@pytest.mark.parametrize("schema,state", [(None, "absent"), (2, "legacy-schema")])
def test_automatic_ready_reports_dedicated_incomplete_status(tmp_path, schema, state):
    if schema is not None:
        _seed_marker(tmp_path)
    before = _inventory(tmp_path)
    result = _cli(tmp_path, "automatic-ready")
    # 10, not 1: an interpreter crash also exits 1 and must never read as a pause.
    assert result.returncode == im.AUTOMATIC_PROTOCOL_INCOMPLETE == 10, result.stderr
    assert json.loads(result.stdout)["state"] == state
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("fault", ["syntax-error", "missing-helper-module"])
def test_crashed_readiness_helper_is_a_failure_not_a_pause(tmp_path, fault):
    _seed_marker(tmp_path)
    copy = tmp_path / "copy/scripts"
    shutil.copytree(_RUNNER.parent / "lib", copy / "lib")
    shutil.copy2(_RUNNER, copy / "code_intel_runner.sh")
    legacy = copy / "lib/index_marker_legacy.py"
    if fault == "syntax-error":
        legacy.write_text("def broken(:\n")
    else:
        legacy.unlink()
    crashed = subprocess.run(
        ["python3", str(copy / "lib/index_marker.py"), "automatic-ready"],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")},
    )
    # Guard the guard: the injected fault must make the helper exit 1, the
    # status that used to be read as "incomplete".
    assert crashed.returncode == 1, crashed.stderr
    before = _inventory(tmp_path)
    result = _run_runner(tmp_path, runner=copy / "code_intel_runner.sh")
    assert result.returncode == 76 and not (tmp_path / "entry.log").exists(), result.stderr
    log = _runner_log(tmp_path)
    assert "protocol observation failed" in log and "protocol incomplete" not in log
    assert _inventory(tmp_path) == before


def test_import_status_keeps_schema2_contract_on_schema3_queue(tmp_path):
    _mdir(tmp_path).mkdir(parents=True)
    with closing(sqlite3.connect(_mdir(tmp_path) / "queue.sqlite3", isolation_level=None)) as db:
        db.executescript(im._SCHEMA)
        db.execute("PRAGMA user_version=3")
        db.execute("INSERT INTO queue_meta VALUES('automatic_protocol_ready','0')")
    before = _inventory(tmp_path)
    listed = _cli(tmp_path, "import-status")
    assert listed.returncode == 2 and "unsupported queue schema 3" in listed.stdout, listed.stdout
    observed = _cli(tmp_path, "automatic-ready")
    assert observed.returncode == 10, observed.stderr
    assert json.loads(observed.stdout)["state"] == "protocol-incomplete"
    assert _inventory(tmp_path) == before


def test_ready_status_without_a_ready_protocol_stops_the_tick(tmp_path):
    # This protocol has no ready state; a helper reporting 0 is an anomaly and
    # must never open the gate to reconcile, claim or launch.
    _seed_marker(tmp_path)
    copy = tmp_path / "copy/scripts"
    shutil.copytree(_RUNNER.parent / "lib", copy / "lib")
    shutil.copy2(_RUNNER, copy / "code_intel_runner.sh")
    calls = tmp_path / "helper.calls"
    (copy / "lib/index_marker.py").write_text(
        "import sys\n"
        f"open({str(calls)!r}, 'a').write(sys.argv[1] + '\\n')\n"
        "sys.exit(0)\n"
    )
    before = _inventory(tmp_path)
    result = _run_runner(tmp_path, runner=copy / "code_intel_runner.sh")
    assert result.returncode == 76 and not (tmp_path / "entry.log").exists(), result.stderr
    assert "has no ready state" in _runner_log(tmp_path)
    assert calls.read_text().split() == ["automatic-ready"]  # no reconcile, list or claim
    assert _inventory(tmp_path) == before


def test_unopenable_runner_log_is_a_failure_not_a_pause(tmp_path):
    _seed_marker(tmp_path)
    log = tmp_path / ".genesis/code-intelligence-runner.log"
    log.mkdir(parents=True)  # the redirection into the log fails with status 1
    before = _inventory(tmp_path)
    result = _run_runner(tmp_path)
    assert result.returncode == 76 and not (tmp_path / "entry.log").exists(), result.stderr
    assert _inventory(tmp_path) == before
