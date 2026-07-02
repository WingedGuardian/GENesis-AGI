"""The proactive memory hook must filter invalid + deprecated memories.

Parity with the main retrieval path (``memory/retrieval.py`` / ``search_ranked``)
and the hook's own Qdrant path: a memory whose ``invalid_at`` is in the past, or
which has been superseded (``deprecated = 1``), must NOT be injected into the CC
prompt context. The main path filters both; the hook's FTS path historically
filtered neither. These tests lock in the in-SQL FTS filters plus the post-query
``_expired_memory_ids`` id filter for the Qdrant union (which mirrors
``_expired_candidate_ids``; the Qdrant search itself already drops deprecated).

NULL ``invalid_at`` = "valid forever" — never dropped. This is the safety
property: a memory can only be excluded if it carries a concrete past
timestamp, so the filter can never over-drop valid context.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Hook lives outside the package tree — add scripts/ to import path.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import proactive_memory_hook as hook  # noqa: E402

# A safely-past and safely-future ISO timestamp in the canonical T-sep+offset
# format the modern writer emits (matches datetime.now(UTC).isoformat()).
_PAST = "2020-01-01T00:00:00+00:00"
_FUTURE = "2099-01-01T00:00:00+00:00"


def _build_db(path: str) -> None:
    """Create a temp DB mirroring the live memory_fts + memory_metadata shape.

    Three episodic rows, all containing the FTS token ``row``:
      - ``alive``   : NULL invalid_at   → valid forever
      - ``future``  : invalid_at 2099   → still valid
      - ``expired`` : invalid_at 2020   → past → must be dropped
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE memory_metadata (
                memory_id        TEXT PRIMARY KEY,
                created_at       TEXT NOT NULL,
                collection       TEXT NOT NULL DEFAULT 'episodic_memory',
                confidence       REAL,
                embedding_status TEXT NOT NULL DEFAULT 'embedded',
                memory_class     TEXT DEFAULT 'fact',
                wing             TEXT,
                room             TEXT,
                valid_at         TEXT,
                invalid_at       TEXT,
                source_subsystem TEXT,
                deprecated       INTEGER NOT NULL DEFAULT 0,
                dream_cycle_run_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                memory_id, content, source_type, tags, collection
            )
            """
        )
        rows = [
            ("alive", None, "this row never expires"),
            ("future", _FUTURE, "this row is valid until 2099"),
            ("expired", _PAST, "this row expired long ago"),
        ]
        for mid, inv, content in rows:
            conn.execute(
                "INSERT INTO memory_metadata "
                "(memory_id, created_at, invalid_at) VALUES (?, ?, ?)",
                (mid, _PAST, inv),
            )
            conn.execute(
                "INSERT INTO memory_fts "
                "(memory_id, content, source_type, tags, collection) "
                "VALUES (?, ?, 'memory', '', 'episodic_memory')",
                (mid, content),
            )
        conn.commit()
    finally:
        conn.close()


# ── _search_fts5: in-SQL invalid_at filter (parity with search_ranked) ───────


def test_search_fts5_drops_expired_by_default(tmp_path) -> None:
    """Default now_iso drops the past-invalid row, keeps NULL + future."""
    db = tmp_path / "t.db"
    _build_db(str(db))
    rows = hook._search_fts5(db, ["row"], collection="episodic_memory")
    ids = {r["memory_id"] for r in rows}
    assert ids == {"alive", "future"}
    assert "expired" not in ids


def test_search_fts5_null_invalid_at_always_kept(tmp_path) -> None:
    """The NULL-safety property: a NULL invalid_at is never filtered."""
    db = tmp_path / "t.db"
    _build_db(str(db))
    rows = hook._search_fts5(db, ["row"], collection="episodic_memory")
    assert "alive" in {r["memory_id"] for r in rows}


def test_search_fts5_explicit_past_as_of_keeps_expired(tmp_path) -> None:
    """An explicit historical now_iso returns rows valid at that point.

    Proves the parameter threads correctly (no silent hard-coded now).
    """
    db = tmp_path / "t.db"
    _build_db(str(db))
    rows = hook._search_fts5(
        db, ["row"], collection="episodic_memory",
        now_iso="2019-01-01T00:00:00+00:00",
    )
    ids = {r["memory_id"] for r in rows}
    # At 2019, 'expired' (invalid 2020) was still valid.
    assert ids == {"alive", "future", "expired"}


def test_search_fts5_invalid_at_combines_with_subsystem(tmp_path) -> None:
    """invalid_at + source_subsystem both apply on the same LEFT JOIN."""
    db = tmp_path / "t.db"
    _build_db(str(db))
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE memory_metadata SET source_subsystem='reflection' "
            "WHERE memory_id='future'"
        )
        conn.commit()
    finally:
        conn.close()
    rows = hook._search_fts5(db, ["row"], collection="episodic_memory")
    ids = {r["memory_id"] for r in rows}
    # alive: NULL/NULL → kept; future: reflection → excluded;
    # expired: past invalid_at → excluded.
    assert ids == {"alive"}


# ── _search_fts5: deprecated (superseded) filter, parity with search_ranked ──


def _insert_extra(db: str, memory_id: str, content: str, *, deprecated: int) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, deprecated) "
            "VALUES (?, ?, ?)",
            (memory_id, _PAST, deprecated),
        )
        conn.execute(
            "INSERT INTO memory_fts "
            "(memory_id, content, source_type, tags, collection) "
            "VALUES (?, ?, 'memory', '', 'episodic_memory')",
            (memory_id, content),
        )
        conn.commit()
    finally:
        conn.close()


def test_search_fts5_drops_deprecated(tmp_path) -> None:
    """A deprecated=1 (superseded) row must not surface; valid rows stay."""
    db = tmp_path / "t.db"
    _build_db(str(db))
    _insert_extra(str(db), "dep", "this row is a superseded duplicate", deprecated=1)
    rows = hook._search_fts5(db, ["row"], collection="episodic_memory")
    ids = {r["memory_id"] for r in rows}
    assert "dep" not in ids
    assert {"alive", "future"} <= ids  # non-deprecated valid rows untouched


def test_search_fts5_keeps_deprecated_zero(tmp_path) -> None:
    """deprecated=0 (the common case) is explicitly preserved."""
    db = tmp_path / "t.db"
    _build_db(str(db))
    _insert_extra(str(db), "live", "this row is a live current fact", deprecated=0)
    rows = hook._search_fts5(db, ["row"], collection="episodic_memory")
    assert "live" in {r["memory_id"] for r in rows}


def test_search_fts5_deprecated_and_invalid_at_both_apply(tmp_path) -> None:
    """The deprecated and invalid_at filters compose on the same JOIN."""
    db = tmp_path / "t.db"
    _build_db(str(db))
    _insert_extra(str(db), "dep", "this row is deprecated but unexpired", deprecated=1)
    rows = hook._search_fts5(db, ["row"], collection="episodic_memory")
    ids = {r["memory_id"] for r in rows}
    # dep: deprecated → out; expired: past invalid_at → out; alive/future stay.
    assert ids == {"alive", "future"}


# ── _expired_memory_ids: post-query filter for the Qdrant union ──────────────


def test_expired_memory_ids_returns_only_past(tmp_path) -> None:
    db = tmp_path / "t.db"
    _build_db(str(db))
    got = hook._expired_memory_ids(
        db, {"alive", "future", "expired"},
        now_iso=datetime.now(UTC).isoformat(),
    )
    assert got == {"expired"}


def test_expired_memory_ids_empty_input(tmp_path) -> None:
    db = tmp_path / "t.db"
    _build_db(str(db))
    assert hook._expired_memory_ids(db, set(), now_iso=_FUTURE) == set()


def test_expired_memory_ids_ignores_unknown_ids(tmp_path) -> None:
    """Ids absent from memory_metadata (e.g. knowledge_base points) never match."""
    db = tmp_path / "t.db"
    _build_db(str(db))
    got = hook._expired_memory_ids(
        db, {"nonexistent-kb-id", "alive"},
        now_iso=datetime.now(UTC).isoformat(),
    )
    assert got == set()


def test_expired_memory_ids_default_now(tmp_path) -> None:
    """Default now_iso (None) drops the past-invalid row."""
    db = tmp_path / "t.db"
    _build_db(str(db))
    got = hook._expired_memory_ids(db, {"alive", "future", "expired"})
    assert got == {"expired"}


def test_expired_memory_ids_boundary_future_kept(tmp_path) -> None:
    """A row invalidated one hour from now is not yet expired."""
    db = tmp_path / "t.db"
    _build_db(str(db))
    conn = sqlite3.connect(str(db))
    try:
        soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        conn.execute(
            "UPDATE memory_metadata SET invalid_at=? WHERE memory_id='alive'",
            (soon,),
        )
        conn.commit()
    finally:
        conn.close()
    got = hook._expired_memory_ids(
        db, {"alive", "expired"}, now_iso=datetime.now(UTC).isoformat(),
    )
    assert got == {"expired"}
    assert "alive" not in got
