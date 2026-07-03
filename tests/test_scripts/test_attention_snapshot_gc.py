"""Tests for scripts/attention_snapshot_gc.py — label-aware attention-snapshot GC.

The crown-jewel invariant: a snapshot db is deleted ONLY when it is older than its
window AND no LABELED attention_event references it. A referenced-and-labeled snapshot
is kept FOREVER — purging it makes its labeled events review-read-only (a permanent
410 in resolve_window_text). These tests lock that invariant plus the age windows,
the dry-run contract, and fail-safe handling of unknown filenames.

Age is derived from the deterministic filename stamp (ambient_YYYYMMDDTHHMMSSZ.db /
omi_YYYYMMDD.db) against an injected ``now`` — no dependence on real wall-clock.
"""

import importlib.util
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Load the stdlib script as a module (not a package — use importlib).
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "attention_snapshot_gc.py"
_spec = importlib.util.spec_from_file_location("attention_snapshot_gc", _SCRIPT_PATH)
_gc = importlib.util.module_from_spec(_spec)
sys.modules["attention_snapshot_gc"] = _gc
_spec.loader.exec_module(_gc)

# A fixed "now" so age comes purely from the filename stamp (wall-clock-independent).
NOW = datetime(2026, 7, 3, tzinfo=UTC).timestamp()

# Real-world stamps (verified against the live DB/snapshots):
#   file ambient_<ID>.db  ->  attention_events.snapshot_id == bare <ID>
OLD_HOME = "ambient_20200101T000000Z.db"     # ~2380d old
RECENT_HOME = "ambient_20260701T000000Z.db"  # ~2d old (< 60d window)
OLD_HOME_ID = "20200101T000000Z"
RECENT_HOME_ID = "20260701T000000Z"
OLD_OMI = "omi_20200101.db"                   # ~2380d old
RECENT_OMI = "omi_20260701.db"                # ~2d old (< 14d window)
OLD_OMI_ID = "omi_20200101"


def _mk_snap(d: Path, name: str) -> Path:
    p = d / name
    p.write_bytes(b"\x00")
    return p


def _mk_db(path: Path, rows: list[tuple[str, str, str | None]] | None = None) -> None:
    """rows = list of (event_id, snapshot_id, acceptance_signal)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE attention_events "
        "(id TEXT, snapshot_id TEXT, acceptance_signal TEXT)"
    )
    for r in rows or []:
        conn.execute("INSERT INTO attention_events VALUES (?,?,?)", r)
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path):
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    db = tmp_path / "genesis.db"
    return snaps, db


def _run(env, *, home_days=60, omi_days=14, dry_run=False):
    snaps, db = env
    return _gc.run_gc(
        snapshots_dir=snaps, db_path=db,
        home_days=home_days, omi_days=omi_days, dry_run=dry_run, now=NOW,
    )


def test_old_unreferenced_home_snapshot_deleted(env):
    snaps, db = env
    old = _mk_snap(snaps, OLD_HOME)
    _mk_db(db)
    deleted = _run(env)
    assert not old.exists()
    assert deleted == 1


def test_labeled_referenced_snapshot_kept_forever(env):
    """A labeled event referencing the snapshot protects it even when ancient."""
    snaps, db = env
    keep = _mk_snap(snaps, OLD_HOME)
    _mk_db(db, [("e1", OLD_HOME_ID, "should")])  # labeled
    deleted = _run(env)
    assert keep.exists(), "labeled-referenced snapshot must survive (review-read-only)"
    assert deleted == 0


def test_referenced_but_unlabeled_and_old_is_deleted(env):
    """Mere reference does NOT protect — only a LABEL does. Old + unlabeled -> gone."""
    snaps, db = env
    old = _mk_snap(snaps, OLD_HOME)
    _mk_db(db, [("e1", OLD_HOME_ID, None)])  # referenced but acceptance_signal NULL
    deleted = _run(env)
    assert not old.exists()
    assert deleted == 1


def test_recent_home_snapshot_kept_by_age_gate(env):
    snaps, db = env
    keep = _mk_snap(snaps, RECENT_HOME)
    _mk_db(db)
    deleted = _run(env)
    assert keep.exists()
    assert deleted == 0


def test_old_omi_unreferenced_deleted_at_14d(env):
    snaps, db = env
    omi = _mk_snap(snaps, OLD_OMI)
    _mk_db(db)
    deleted = _run(env)
    assert not omi.exists()
    assert deleted == 1


def test_recent_omi_kept_within_14d(env):
    snaps, db = env
    keep = _mk_snap(snaps, RECENT_OMI)
    _mk_db(db)
    deleted = _run(env)
    assert keep.exists()
    assert deleted == 0


def test_labeled_omi_kept_forever(env):
    snaps, db = env
    keep = _mk_snap(snaps, OLD_OMI)
    _mk_db(db, [("e1", OLD_OMI_ID, "shouldnt")])
    deleted = _run(env)
    assert keep.exists()
    assert deleted == 0


def test_unknown_filename_never_touched(env):
    """Anything not matching ambient_*.db / omi_*.db is skipped (fail-safe)."""
    snaps, db = env
    other = _mk_snap(snaps, "random_backup.db")
    _mk_db(db)
    deleted = _run(env)
    assert other.exists()
    assert deleted == 0


def test_dry_run_deletes_nothing(env):
    snaps, db = env
    old = _mk_snap(snaps, OLD_HOME)
    _mk_db(db)
    deleted = _run(env, dry_run=True)
    assert old.exists()
    assert deleted == 0


def test_missing_db_is_conservative_keep(env):
    """If genesis.db is absent we cannot check labels -> keep everything (fail-safe)."""
    snaps, db = env
    old = _mk_snap(snaps, OLD_HOME)  # db path never created
    deleted = _run(env)
    assert old.exists(), "no DB to verify labels -> must not delete"
    assert deleted == 0


def test_bare_id_matches_runner_snapshot_id_derivation():
    """GROUNDING (not self-confirming): the GC's snapshot-id derivation must EQUAL the
    runner's, so the label lookup finds exactly what the runner persisted to
    attention_events.snapshot_id. This locks the OMI crown-jewel contract against the real
    production derivation — if either side's prefix handling changes, this fails loudly.
    """
    from genesis.attention.runner import _snapshot_id_from_path

    for name in ("ambient_20200101T000000Z.db", "omi_20200101.db"):
        p = Path(name)
        assert _gc._bare_id(p) == _snapshot_id_from_path(p), (
            f"GC id derivation diverged from runner for {name!r}"
        )


def test_missing_snapshots_dir_is_noop(tmp_path):
    deleted = _gc.run_gc(
        snapshots_dir=tmp_path / "nope", db_path=tmp_path / "genesis.db",
        home_days=60, omi_days=14, dry_run=False, now=NOW,
    )
    assert deleted == 0
