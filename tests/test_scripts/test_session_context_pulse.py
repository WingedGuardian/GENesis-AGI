"""SessionStart repo-pulse wiring (session-manager PR-4a commit 6).

Covers the Pulse sub-block in the charter emission (render with confirm
hint, floor filter, cap, byte-identical block when the pulse tables are
missing) and the fail-open worker spawn.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

# Load the hook script as a module (not a package — use importlib)
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
_ctx_spec = importlib.util.spec_from_file_location(
    "genesis_session_context_pulse", _SCRIPTS_DIR / "genesis_session_context.py"
)
_ctx = importlib.util.module_from_spec(_ctx_spec)
_ctx_spec.loader.exec_module(_ctx)

SID = "sid-pulse"


def _make_db(tmp_path: Path, *, with_pulse: bool = True) -> Path:
    """tmp data/genesis.db carrying the charter tables (± pulse tables),
    using the canonical DDL from db/schema/_tables.py."""
    from genesis.db.schema._tables import TABLES

    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True, exist_ok=True)
    db_file = root / "data" / "genesis.db"
    conn = sqlite3.connect(db_file)
    conn.execute(TABLES["session_charters"])
    conn.execute(TABLES["session_ledger"])
    if with_pulse:
        conn.execute(TABLES["repo_pulse_runs"])
        conn.execute(TABLES["repo_pulse_annotations"])
    conn.execute(
        "INSERT INTO session_charters (session_id, transcript_path, origin_prompt,"
        " origin_ts, pointers, compaction_count, created_at)"
        " VALUES (?, '/tmp/t.jsonl', 'The origin prompt.',"
        " '2026-06-30T15:21:06.000Z', '[]', 2, '2026-07-13T02:00:00+00:00')",
        (SID,),
    )
    conn.commit()
    conn.close()
    return db_file


def _seed_proposal(
    db_file: Path,
    ann_id: str,
    *,
    confidence: float | None = 0.9,
    status: str = "proposed",
    session_id: str = SID,
    observed_at: str = "2026-07-16T00:00:00+00:00",
    pr_number: int = 1080,
) -> None:
    conn = sqlite3.connect(db_file)
    conn.execute(
        "INSERT INTO repo_pulse_annotations (id, run_id, observed_at, tier,"
        " item_id, item_session_id, item_text, pr_number, pr_title,"
        " confidence, status)"
        " VALUES (?, 'r1', ?, 'fuzzy', ?, ?, 'ship the annotator', ?,"
        " 'feat: annotator', ?, ?)",
        (ann_id, observed_at, f"item-{ann_id}", session_id, pr_number, confidence, status),
    )
    conn.commit()
    conn.close()


def _block(db_file: Path) -> str:
    return _ctx._charter_emission_block(SID, "compact", db_path=db_file)


# ── Pulse sub-block rendering ────────────────────────────────────────────


def test_proposal_renders_with_confirm_hint(tmp_path):
    db_file = _make_db(tmp_path)
    _seed_proposal(db_file, "a1")
    block = _block(db_file)
    assert "**Pulse (proposed — confirm or ignore):**" in block
    assert "'ship the annotator' looks shipped by PR #1080" in block
    # the hint carries the PR evidence so a user-confirmed absorb reconciles
    # to 'confirmed' (same-PR attribution guard), not 'superseded'
    assert "session_ledger_update('item-a1', status='absorbed', evidence='PR #1080')" in block


def test_missing_pulse_tables_block_byte_identical(tmp_path):
    """Pre-0062 installs: the charter block must not change by a byte."""
    with_tables = _block(_make_db(tmp_path / "a", with_pulse=True))
    without_tables = _block(_make_db(tmp_path / "b", with_pulse=False))
    assert with_tables == without_tables
    assert "Pulse" not in without_tables


def test_floor_filters_low_confidence_null_passes(tmp_path):
    db_file = _make_db(tmp_path)
    _seed_proposal(db_file, "a-low", confidence=0.4)
    _seed_proposal(db_file, "a-high", confidence=0.95, pr_number=1081)
    # exact-tier bare-hex proposals carry NULL confidence — deterministic
    # id evidence outranks any judge score, so they always surface
    _seed_proposal(db_file, "a-null", confidence=None, pr_number=1082)
    block = _block(db_file)
    assert "#1081" in block
    assert "#1082" in block
    assert "#1080" not in block  # below the 0.7 floor


def test_cap_three_newest_first(tmp_path):
    db_file = _make_db(tmp_path)
    for i in range(5):
        _seed_proposal(
            db_file,
            f"a{i}",
            pr_number=1080 + i,
            observed_at=f"2026-07-16T00:00:0{i}+00:00",
        )
    block = _block(db_file)
    for pr in (1084, 1083, 1082):  # newest three
        assert f"#{pr}" in block
    for pr in (1081, 1080):
        assert f"#{pr}" not in block


def test_only_this_sessions_proposals_and_only_proposed(tmp_path):
    db_file = _make_db(tmp_path)
    _seed_proposal(db_file, "a-other", session_id="sid-other")
    _seed_proposal(db_file, "a-applied", status="applied", pr_number=1081)
    _seed_proposal(db_file, "a-rejected", status="rejected", pr_number=1082)
    assert "Pulse" not in _block(db_file)


def test_clear_emits_nothing(tmp_path):
    db_file = _make_db(tmp_path)
    _seed_proposal(db_file, "a1")
    assert _ctx._charter_emission_block(SID, "clear", db_path=db_file) == ""


# ── floor config ─────────────────────────────────────────────────────────


def test_pulse_floor_reads_config_and_defaults(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(root))
    monkeypatch.setattr(_ctx.Path, "home", staticmethod(lambda: tmp_path / "home"))
    assert _ctx._pulse_floor() == 0.7  # no files → default
    (root / "config" / "repo_pulse.yaml").write_text("inject_confidence_floor: 0.5\n")
    assert _ctx._pulse_floor() == 0.5
    overlay_dir = tmp_path / "home" / ".genesis" / "config"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "repo_pulse.local.yaml").write_text("inject_confidence_floor: 0.9\n")
    assert _ctx._pulse_floor() == 0.9  # overlay wins
    (overlay_dir / "repo_pulse.local.yaml").write_text("inject_confidence_floor: 5\n")
    assert _ctx._pulse_floor() == 0.7  # garbage → default


# ── worker spawn ─────────────────────────────────────────────────────────


def test_spawn_invokes_worker_with_home_anchored_db(tmp_path, monkeypatch):
    calls: list[dict] = []

    def fake_popen(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(_ctx.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("GENESIS_REPO_PULSE_DISABLED", raising=False)
    monkeypatch.delenv("GENESIS_REPO_ROOT", raising=False)
    _ctx._spawn_repo_pulse_worker("startup")
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[1].endswith("scripts/repo_pulse_worker.py")
    assert argv[argv.index("--trigger") + 1] == "session_start"
    assert argv[argv.index("--db-path") + 1] == str(_ctx._charter_db_path())
    assert calls[0]["start_new_session"] is True
    # stderr wired to the append log, not inherited
    err_log = tmp_path / ".genesis" / "session_awareness" / "repo_pulse_err.log"
    assert err_log.parent.exists()


def test_spawn_skipped_on_clear_and_kill_switch(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(_ctx.Path, "home", staticmethod(lambda: tmp_path))
    _ctx._spawn_repo_pulse_worker("clear")
    monkeypatch.setenv("GENESIS_REPO_PULSE_DISABLED", "1")
    _ctx._spawn_repo_pulse_worker("startup")
    assert calls == []


def test_spawn_failure_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise OSError("no fds left")

    monkeypatch.setattr("subprocess.Popen", boom)
    monkeypatch.delenv("GENESIS_REPO_PULSE_DISABLED", raising=False)
    _ctx._spawn_repo_pulse_worker("startup")  # must not raise
