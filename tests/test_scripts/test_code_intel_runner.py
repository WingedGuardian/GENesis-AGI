"""Tests for scripts/code_intel_runner.sh — the idle-gated marker consumer.

The runner is the ONLY thing that turns a queued index request into an actual
index, and only when the box is idle. These tests drive it with a FAKE
entrypoint (CODE_INTEL_ENTRYPOINT seam) that returns a chosen rc, and fake
pressure readings (CODE_INTEL_FAKE_* seams), so no real indexing, load, or
systemd is needed. They lock down the rc contract that keeps the host freeze
safe and the index from being euthanized:

  * idle gate defers when the box is busy (marker kept)
  * rc 0  -> consume; rc 75 (frozen) / rc 3 (tool missing) -> keep, no penalty
  * genuine failure -> attempts++ -> .failed at the cap
  * escalated-full failure -> fall back to fast + back off, no penalty
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / "scripts" / "code_intel_runner.sh"
_MARKER_PY = _REPO_ROOT / "scripts" / "lib" / "index_marker.py"

_spec = importlib.util.spec_from_file_location("index_marker", _MARKER_PY)
im = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(im)

_REPO = "/home/ubuntu/genesis"  # canonical; hash is stable


def _fake_entrypoint(tmp_path: Path, rc: int) -> Path:
    """A stand-in entrypoint: log argv (esp. the mode arg) and exit `rc`."""
    p = tmp_path / "fake_entry.sh"
    p.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "ENTRY repo=$1 tools=$2 mode=$3" >> "{tmp_path}/entry.log"\n'
        f"exit {rc}\n"
    )
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def _run_runner(
    tmp_path: Path, entry_rc: int, *, load="0.1", iowait="0", claude_cpu="0", extra_env=None
):
    home = tmp_path / ".genesis"
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(home),
        "CODE_INTEL_ENTRYPOINT": str(_fake_entrypoint(tmp_path, entry_rc)),
        "CODE_INTEL_FAKE_LOADAVG": load,
        "CODE_INTEL_FAKE_IOWAIT": iowait,
        "CODE_INTEL_FAKE_CLAUDE_CPU": claude_cpu,
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", str(_RUNNER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _seed_marker(tmp_path, tools="both", mode="fast"):
    """Write a marker into the runner's GENESIS_HOME and return its hash."""
    env = {**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")}
    subprocess.run(
        ["python3", str(_MARKER_PY), "write", "--repo", _REPO, "--tools", tools, "--mode", mode],
        env=env,
        check=True,
        capture_output=True,
    )
    return im.marker_hash(_REPO)


def test_unopenable_runner_lock_refuses_indexing(tmp_path):
    _seed_marker(tmp_path)
    lock = tmp_path / ".genesis" / "locks" / "code-intel-runner.lock"
    lock.mkdir(parents=True)
    result = _run_runner(tmp_path, 0)
    assert result.returncode == 75
    assert not (tmp_path / "entry.log").exists()
    assert len(_markers(tmp_path)) == 1


def test_unavailable_lock_directory_does_not_use_an_alternate_lock(tmp_path):
    _seed_marker(tmp_path)
    (tmp_path / ".genesis" / "locks").write_text("not a directory")
    result = _run_runner(tmp_path, 0)
    assert result.returncode == 75
    assert not (tmp_path / "entry.log").exists()
    assert len(_markers(tmp_path)) == 1


def _markers(tmp_path):
    env = {**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")}
    out = subprocess.run(
        ["python3", str(_MARKER_PY), "list"],
        env=env,
        capture_output=True,
        text=True,
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def _mdir(tmp_path) -> Path:
    return tmp_path / ".genesis" / "index-requests"


def _db(tmp_path):
    return sqlite3.connect(_mdir(tmp_path) / "queue.sqlite3")


# ── idle gate ──────────────────────────────────────────────────────────────


def test_busy_box_defers_no_index(tmp_path):
    _seed_marker(tmp_path)
    res = _run_runner(tmp_path, entry_rc=0, load="9.0")
    assert res.returncode == 0
    assert not (tmp_path / "entry.log").exists()  # entrypoint never invoked
    assert len(_markers(tmp_path)) == 1  # marker kept


def test_high_iowait_defers(tmp_path):
    _seed_marker(tmp_path)
    _run_runner(tmp_path, entry_rc=0, iowait="80")
    assert not (tmp_path / "entry.log").exists()
    assert len(_markers(tmp_path)) == 1


def test_busy_cc_session_defers(tmp_path):
    _seed_marker(tmp_path)
    _run_runner(tmp_path, entry_rc=0, claude_cpu="95")
    assert not (tmp_path / "entry.log").exists()
    assert len(_markers(tmp_path)) == 1


def test_starved_marker_runs_under_relaxed_gate(tmp_path):
    h = _seed_marker(tmp_path)
    # Backdate requested_at so age > relax window; a moderate load then passes.
    with _db(tmp_path) as db:
        db.execute("UPDATE pending SET requested_at=0 WHERE hash=?", (h,))
    _run_runner(
        tmp_path,
        entry_rc=0,
        load="3.0",  # >2 strict, <6 relaxed
        extra_env={"CODE_INTEL_RUNNER_RELAX_AFTER_S": "1"},
    )
    assert (tmp_path / "entry.log").exists()  # relaxed gate let it run
    assert _markers(tmp_path) == []  # consumed


# ── rc contract ────────────────────────────────────────────────────────────


def test_rc0_consumes_marker(tmp_path):
    _seed_marker(tmp_path)
    _run_runner(tmp_path, entry_rc=0)
    assert _markers(tmp_path) == []


def test_rc75_frozen_keeps_marker(tmp_path):
    _seed_marker(tmp_path)
    _run_runner(tmp_path, entry_rc=75)
    assert len(_markers(tmp_path)) == 1  # host-frozen: marker survives


def test_rc3_tool_missing_keeps_marker_no_penalty(tmp_path):
    h = _seed_marker(tmp_path)
    _run_runner(tmp_path, entry_rc=3)
    listed = _markers(tmp_path)
    assert len(listed) == 1
    assert listed[0].split("\t")[4] == "0"  # attempts NOT incremented
    with _db(tmp_path) as db:
        assert db.execute("SELECT 1 FROM failed WHERE hash=?", (h,)).fetchone() is None


def test_genuine_failure_increments_attempts(tmp_path):
    h = _seed_marker(tmp_path, mode="fast")
    # Suppress escalation so rc1 is treated as a genuine fast-run failure.
    subprocess.run(
        ["python3", str(_MARKER_PY), "stamp-full", "--hash", h],
        env={**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")},
        check=True,
        capture_output=True,
    )
    _run_runner(tmp_path, entry_rc=1)
    listed = _markers(tmp_path)
    assert len(listed) == 1
    assert listed[0].split("\t")[4] == "1"  # attempts incremented


def test_repeated_failures_euthanize(tmp_path):
    h = _seed_marker(tmp_path, mode="fast")
    env_home = {**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")}
    subprocess.run(
        ["python3", str(_MARKER_PY), "stamp-full", "--hash", h],
        env=env_home,
        check=True,
        capture_output=True,
    )
    for _ in range(im.MAX_ATTEMPTS):
        _run_runner(tmp_path, entry_rc=1)
    assert _markers(tmp_path) == []
    with _db(tmp_path) as db:
        assert (
            db.execute("SELECT attempts FROM failed WHERE hash=?", (h,)).fetchone()[0]
            == im.MAX_ATTEMPTS
        )


def test_rc4_consumes_cbm_and_requeues_gitnexus_leg(tmp_path):
    """cbm done + gitnexus leg incomplete: the combined marker must not be
    discarded whole — cbm's work is consumed AND a gitnexus-only marker is
    requeued, so the skipped/failed leg survives a cleared condition."""
    _seed_marker(tmp_path, tools="both", mode="fast")
    _run_runner(tmp_path, entry_rc=4)
    listed = _markers(tmp_path)
    assert len(listed) == 1
    assert listed[0].split("\t")[2] == "gitnexus"
    assert listed[0].split("\t")[4] == "0"  # requeue is not a failure penalty


def test_rc5_consumes_gitnexus_and_requeues_cbm_leg(tmp_path):
    """gitnexus done + cbm leg skipped: consume gitnexus, requeue cbm-only —
    and never stamp the shared full-success clock for cbm work that did not
    run."""
    h = _seed_marker(tmp_path, tools="both", mode="fast")
    _run_runner(tmp_path, entry_rc=5)
    listed = _markers(tmp_path)
    assert len(listed) == 1
    assert listed[0].split("\t")[2] == "cbm"
    with _db(tmp_path) as db:
        row = db.execute("SELECT last_full FROM repo_state WHERE hash=?", (h,)).fetchone()
        assert row is None or row[0] is None  # cbm never ran — no full stamp


# ── escalation ─────────────────────────────────────────────────────────────


def test_escalates_fast_marker_to_full_when_due(tmp_path):
    _seed_marker(tmp_path, mode="fast")  # no .last-full → due
    _run_runner(tmp_path, entry_rc=0)
    assert "mode=full" in (tmp_path / "entry.log").read_text()


def test_escalation_query_error_restores_marker_and_stops_tick(tmp_path):
    """A real helper exception must not be interpreted as "full not due"."""
    h = _seed_marker(tmp_path, mode="fast")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python_wrapper = bin_dir / "python3"
    python_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${2:-}\" = should-escalate ]; then\n"
        "  /usr/bin/python3 -c 'import os,sqlite3,subprocess,sys; "
        "db=sqlite3.connect(sys.argv[1],isolation_level=None); db.execute(\"BEGIN EXCLUSIVE\"); "
        "result=subprocess.run([\"/usr/bin/python3\",*sys.argv[2:]],env=os.environ); "
        "db.rollback(); db.close(); raise SystemExit(result.returncode)' "
        "\"$GENESIS_HOME/index-requests/queue.sqlite3\" \"$@\"\n"
        "  exit $?\n"
        "fi\n"
        "exec /usr/bin/python3 \"$@\"\n"
    )
    python_wrapper.chmod(python_wrapper.stat().st_mode | stat.S_IXUSR)

    result = _run_runner(
        tmp_path,
        entry_rc=0,
        extra_env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )

    assert result.returncode == 76
    assert not (tmp_path / "entry.log").exists()
    rows = _markers(tmp_path)
    assert len(rows) == 1
    assert rows[0].split("\t")[0] == h


def test_escalated_full_failure_falls_back_no_penalty(tmp_path):
    h = _seed_marker(tmp_path, mode="fast")  # escalates to full, then fails
    _run_runner(tmp_path, entry_rc=1)
    listed = _markers(tmp_path)
    assert len(listed) == 1
    # marker stays fast, attempts NOT burned, and full is backed off
    assert listed[0].split("\t")[3] == "fast"
    assert listed[0].split("\t")[4] == "0"
    with _db(tmp_path) as db:
        assert db.execute("SELECT full_backoff FROM repo_state WHERE hash=?", (h,)).fetchone()[0]


def test_gitnexus_only_marker_does_not_escalate_or_stamp(tmp_path):
    # "full" is cbm-only; a gitnexus-only marker (from the surplus job) must NOT
    # escalate or stamp .last-full, or it would falsely satisfy cbm's weekly gate.
    h = _seed_marker(tmp_path, tools="gitnexus", mode="fast")  # due (no .last-full)
    _run_runner(tmp_path, entry_rc=0)
    assert "mode=fast" in (tmp_path / "entry.log").read_text()  # NOT escalated
    with _db(tmp_path) as db:
        row = db.execute("SELECT last_full FROM repo_state WHERE hash=?", (h,)).fetchone()
        assert row is None or row[0] is None  # NOT stamped


def test_rc3_on_escalated_full_backs_off(tmp_path):
    # A missing sibling tool (rc3) after an escalated full must back off full so
    # it doesn't re-escalate a heavy cbm full every idle tick.
    h = _seed_marker(tmp_path, tools="both", mode="fast")  # escalates to full
    _run_runner(tmp_path, entry_rc=3)
    listed = _markers(tmp_path)
    assert len(listed) == 1
    assert listed[0].split("\t")[4] == "0"  # no attempts penalty
    with _db(tmp_path) as db:
        assert db.execute("SELECT full_backoff FROM repo_state WHERE hash=?", (h,)).fetchone()[0]


def test_runner_reconciles_orphaned_inflight(tmp_path):
    # A marker stranded as .inflight by a dead prior run is re-pended at tick start.
    h = _seed_marker(tmp_path, tools="both", mode="fast")
    subprocess.run(
        ["python3", str(_MARKER_PY), "claim", "--hash", h],
        env={**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")},
        check=True,
        capture_output=True,
    )
    assert _markers(tmp_path) == []  # stranded as inflight
    _run_runner(tmp_path, entry_rc=0, load="9.0")  # busy → defers running, but re-pends
    assert len(_markers(tmp_path)) == 1


def test_persisted_success_outcome_replays_without_reindex(tmp_path):
    """A terminal result persisted before runner death must never run twice."""
    h = _seed_marker(tmp_path, tools="gitnexus", mode="fast")
    env = {**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")}
    claimed = subprocess.run(
        ["python3", str(_MARKER_PY), "claim", "--hash", h],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    claim_id = claimed.stdout.strip().split("\t")[4]
    subprocess.run(
        [
            "python3",
            str(_MARKER_PY),
            "remember-outcome",
            "--hash",
            h,
            "--action",
            "consume",
            "--claim-id",
            claim_id,
        ],
        env=env,
        check=True,
    )

    second = _run_runner(tmp_path, entry_rc=0)
    assert second.returncode == 0
    assert _markers(tmp_path) == []
    assert not (tmp_path / "entry.log").exists()


def test_runner_spools_busy_success_and_replays_without_reindex(tmp_path):
    """Exercise the exact shell claim/outcome path across a real DB lock."""
    h = _seed_marker(tmp_path, tools="gitnexus", mode="fast")
    db_path = _mdir(tmp_path) / "queue.sqlite3"
    ready = tmp_path / "blocker.ready"
    locker = tmp_path / "hold_queue.py"
    locker.write_text(
        "import sqlite3,sys,time\n"
        "db=sqlite3.connect(sys.argv[1],isolation_level=None)\n"
        "db.execute('BEGIN IMMEDIATE')\n"
        "open(sys.argv[2],'w').close()\n"
        "time.sleep(7)\n"
        "db.rollback()\n"
        "db.close()\n"
    )
    entry = tmp_path / "contention_entry.sh"
    entry.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "ENTRY repo=$1 tools=$2 mode=$3" >> "{tmp_path}/entry.log"\n'
        f'python3 "{locker}" "{db_path}" "{ready}" &\n'
        f'while [ ! -e "{ready}" ]; do sleep 0.01; done\n'
        "exit 0\n"
    )
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(tmp_path / ".genesis"),
        "CODE_INTEL_ENTRYPOINT": str(entry),
        "CODE_INTEL_FAKE_LOADAVG": "0.1",
        "CODE_INTEL_FAKE_IOWAIT": "0",
        "CODE_INTEL_FAKE_CLAUDE_CPU": "0",
    }

    first = subprocess.run(
        ["bash", str(_RUNNER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert first.returncode == 76
    assert len(list(_mdir(tmp_path).glob(f".outcome-spool-{h}-*.spool"))) == 1
    assert (tmp_path / "entry.log").read_text().count("ENTRY ") == 1

    second = _run_runner(tmp_path, entry_rc=0)
    assert second.returncode == 0
    assert _markers(tmp_path) == []
    assert (tmp_path / "entry.log").read_text().count("ENTRY ") == 1


def test_uses_claimed_state_not_stale_list_snapshot(tmp_path):
    """P2: a commit that coalesces the marker during the idle-sampling window
    must be honored — the run uses the CLAIMED state, not the list snapshot.

    Seed a gitnexus-only marker, then race a `both`/`full` coalesce into the
    runner's ~1s real-iowait sampling window; the entrypoint must be invoked
    with tools=both (the coalesced request), not the stale tools=gitnexus.
    """
    import threading
    import time

    home = tmp_path / ".genesis"
    _seed_marker(tmp_path, tools="gitnexus", mode="fast")

    def _coalesce():
        time.sleep(0.4)  # well inside the runner's 1s iowait sample
        subprocess.run(
            [
                "python3",
                str(_MARKER_PY),
                "write",
                "--repo",
                _REPO,
                "--tools",
                "cbm",
                "--mode",
                "full",
            ],  # gitnexus ∪ cbm = both; full
            env={**os.environ, "GENESIS_HOME": str(home)},
            check=True,
            capture_output=True,
        )

    t = threading.Thread(target=_coalesce)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(home),
        "CODE_INTEL_ENTRYPOINT": str(_fake_entrypoint(tmp_path, 0)),
        "CODE_INTEL_FAKE_LOADAVG": "0",
        "CODE_INTEL_FAKE_CLAUDE_CPU": "0",
        # NOT faking iowait → real ~1s /proc/stat sampling = the race window;
        # a high threshold so real iowait never trips the gate.
        "CODE_INTEL_RUNNER_IDLE_IOWAIT": "100",
    }
    t.start()
    subprocess.run(["bash", str(_RUNNER)], env=env, capture_output=True, text=True, timeout=60)
    t.join()
    log = (tmp_path / "entry.log").read_text()
    assert "tools=both" in log, f"ran with stale list state, not claimed: {log}"
    assert "mode=full" in log  # coalesced to full and escalation isn't needed


def test_no_markers_is_quiet_noop(tmp_path):
    res = _run_runner(tmp_path, entry_rc=0)
    assert res.returncode == 0
    assert not (tmp_path / "entry.log").exists()
