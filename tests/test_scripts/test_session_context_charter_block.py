"""The charter block renders the OPEN part of a charter in full.

A session's charter is one paragraph plus a handful of open ledger rows. The
block used to cut it five ways — six rows, 120 chars per row, 200 chars of
mission, 2800 chars in total — sized for a pathological charter and biting a
normal one: a founding ask rendered as "audit which of Genesis's" and stopped.
These tests pin the opposite contract: open rows and the mission render uncut,
and when a charter really is pathological the degrade is structured — every
open row id and the charter.md path survive, never a mid-row cut.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
_ctx_spec = importlib.util.spec_from_file_location(
    "genesis_session_context_charter", _SCRIPTS_DIR / "genesis_session_context.py"
)
_ctx = importlib.util.module_from_spec(_ctx_spec)
_ctx_spec.loader.exec_module(_ctx)

SID = "sid-charter-block"


def _make_db(tmp_path: Path, *, with_follow_ups: bool = True) -> Path:
    from genesis.db.schema._tables import TABLES

    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True, exist_ok=True)
    db_file = root / "data" / "genesis.db"
    conn = sqlite3.connect(db_file)
    conn.execute(TABLES["session_charters"])
    conn.execute(TABLES["session_ledger"])
    if with_follow_ups:
        conn.execute(TABLES["follow_ups"])
    conn.commit()
    conn.close()
    return db_file


def _seed_charter(
    db_file: Path,
    *,
    mission: str | None = None,
    compaction_count: int = 2,
    origin: str = "The origin prompt.",
) -> None:
    conn = sqlite3.connect(db_file)
    conn.execute(
        "INSERT INTO session_charters (session_id, transcript_path, origin_prompt,"
        " origin_ts, mission, pointers, compaction_count, created_at)"
        " VALUES (?, '/tmp/t.jsonl', ?, '2026-06-30T15:21:06.000Z', ?, '[]', ?,"
        " '2026-07-13T02:00:00+00:00')",
        (SID, origin, mission, compaction_count),
    )
    conn.commit()
    conn.close()


def _seed_rows(db_file: Path, texts: list[str], *, status: str = "open") -> list[str]:
    conn = sqlite3.connect(db_file)
    ids = []
    for i, text in enumerate(texts):
        row_id = f"{i:032x}"
        ids.append(row_id)
        conn.execute(
            "INSERT INTO session_ledger (id, session_id, text, status, added_by, created_at)"
            f" VALUES (?, ?, ?, ?, 'foreground', '2026-07-13T00:00:{i:02d}+00:00')",
            (row_id, SID, text, status),
        )
    conn.commit()
    conn.close()
    return ids


def _seed_escalation(db_file: Path, ledger_id: str, follow_up_id: str) -> None:
    key = hashlib.sha256(f"ledger_escalation|{ledger_id}".encode()).hexdigest()
    conn = sqlite3.connect(db_file)
    conn.execute(
        "INSERT INTO follow_ups (id, content, source, strategy, status, priority,"
        " created_at, dedup_key, kind)"
        " VALUES (?, 'x', 'ledger_escalation', 'user_input_needed', 'pending', 'high',"
        " '2026-07-20T00:00:00+00:00', ?, 'follow_up')",
        (follow_up_id, key),
    )
    conn.commit()
    conn.close()


def _block(db_file: Path) -> str:
    return _ctx._charter_emission_block(SID, "compact", db_path=db_file)


# ── open rows render in full ────────────────────────────────────────────


def test_seven_open_rows_all_render(tmp_path):
    db = _make_db(tmp_path)
    _seed_charter(db, mission="m")
    ids = _seed_rows(db, [f"row number {i}" for i in range(7)])
    block = _block(db)
    for row_id in ids:
        assert row_id in block, f"open row {row_id[:8]} missing from the block"


def test_open_row_text_renders_uncut(tmp_path):
    db = _make_db(tmp_path)
    _seed_charter(db, mission="m")
    text = "audit which of Genesis's own CBM patches become redundant " * 7  # ~400 chars
    _seed_rows(db, [text.strip()])
    assert text.strip() in _block(db)


def test_mission_up_to_1000_chars_uncut(tmp_path):
    db = _make_db(tmp_path)
    mission = ("UNDONE FOUNDING ASKS outrank every follow-up: " * 25)[:1000]
    _seed_charter(db, mission=mission)
    assert mission in _block(db)


# ── structured degrade for a pathological charter ────────────────────────


def test_pathological_charter_keeps_every_open_id_and_charter_md_path(tmp_path):
    db = _make_db(tmp_path)
    _seed_charter(db, mission="m" * 1000, origin="o" * 1200)
    ids = _seed_rows(db, [f"row {i} " + ("x" * 900) for i in range(30)])
    block = _block(db)
    assert len(block) <= _ctx._CHARTER_BLOCK_MAX + 400, len(block)
    for row_id in ids:
        assert row_id in block, f"degrade dropped open row id {row_id[:8]}"
    assert f"~/.genesis/sessions/{SID}/charter.md" in block
    # never a mid-row cut: every rendered row line is a whole line
    assert not block.rstrip().endswith("x" * 10)


# ── mission drift ───────────────────────────────────────────────────────


def test_mission_unset_after_compaction_emits_drift_line(tmp_path):
    db = _make_db(tmp_path)
    _seed_charter(db, mission=None, compaction_count=3)
    block = _block(db)
    assert "not set after 3 compactions" in block
    assert "session_charter_update" in block


def test_mission_unset_before_first_compaction_is_silent(tmp_path):
    """Control for the drift line: a session that has not compacted yet is not nagged."""
    db = _make_db(tmp_path)
    _seed_charter(db, mission=None, compaction_count=0)
    block = _block(db)
    assert "not set after" not in block


# ── escalation link (reverse link for the ledger-escalation sweep) ──────


def test_escalated_row_shows_follow_up_link(tmp_path):
    db = _make_db(tmp_path)
    _seed_charter(db, mission="m")
    (row_id,) = _seed_rows(db, ["reach into the sister machine"])
    _seed_escalation(db, row_id, "f" * 32)
    block = _block(db)
    assert f"escalated: follow_up {'f' * 8}" in block


def test_row_without_escalation_has_no_link(tmp_path):
    """Control: the link renders only for a row that actually has an escalation."""
    db = _make_db(tmp_path)
    _seed_charter(db, mission="m")
    _seed_rows(db, ["reach into the sister machine"])
    assert "escalated:" not in _block(db)


def test_missing_follow_ups_table_renders_block_without_link(tmp_path):
    db = _make_db(tmp_path, with_follow_ups=False)
    _seed_charter(db, mission="m")
    (row_id,) = _seed_rows(db, ["reach into the sister machine"])
    block = _block(db)
    assert row_id in block
    assert "escalated:" not in block


def test_hook_dedup_formula_matches_package_formula():
    """The import-free hook inlines the sha256 formula; the package owns it."""
    from genesis.session_awareness.ledger_escalation_link import escalation_dedup_key

    ledger_id = "a" * 32
    assert _ctx._escalation_dedup_key(ledger_id) == escalation_dedup_key(ledger_id)
