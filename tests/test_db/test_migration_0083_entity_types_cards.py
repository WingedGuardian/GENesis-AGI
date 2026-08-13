"""MW-3 — 0083 entity typing + card columns: CHECK rebuild + metadata columns.

PR-1 (schema + safety rails). Three things must hold on BOTH build paths
(numbered migration for legacy DBs, canonical CREATE for fresh DBs):

  1. The ``entities`` ``entity_type`` CHECK gains ``host``/``install``/``project``
     (the MW-3 §6.4 first-card classes) — existing rows preserved, UNIQUE intact.
  2. Two card columns exist: ``summary_updated_at TEXT`` and
     ``summary_dirty INTEGER NOT NULL DEFAULT 0`` (B3 card materialization state;
     NULL/0 = never carded = today's behavior).
  3. The rebuild is idempotent and re-run-safe.

NOTHING reads/writes the new columns in production yet (later MW-3 PRs). The old
11-type set is a SUBSET of the new 14-type set, so no existing row can violate
the new CHECK; the rebuild needs no dedup (UNIQUE(norm_name, entity_type) already
held).
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.schema._migrations import create_all_tables
from genesis.db.schema._tables import TABLES

_mig = importlib.import_module("genesis.db.migrations.0083_mw3_entity_types_cards")

_NEW_TYPES = {"host", "install", "project"}
_CARD_COLS = {"summary_updated_at", "summary_dirty"}

# An ``entities`` table shaped like a legacy DB predating MW-3: the old 11-type
# CHECK, no card columns.
_LEGACY_DDL = """
    CREATE TABLE entities (
        entity_id   TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        norm_name   TEXT NOT NULL,
        entity_type TEXT NOT NULL CHECK (entity_type IN (
            'code_file','code_symbol','pr','commit',
            'product','device','repo','subsystem','person','org','concept'
        )),
        summary     TEXT,
        source      TEXT NOT NULL DEFAULT 'extracted',
        status      TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','merged','gone')),
        merged_into TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        UNIQUE (norm_name, entity_type)
    )
"""


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _insert(conn, eid, norm, etype, *, status="active", merged_into=None):
    await conn.execute(
        "INSERT INTO entities (entity_id, name, norm_name, entity_type, source, "
        "status, merged_into, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            eid,
            eid,
            norm,
            etype,
            "extracted",
            status,
            merged_into,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )


async def _legacy_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute(_LEGACY_DDL)
    # A representative spread incl. active + a merged tombstone + a gone row.
    await _insert(conn, "e1", "omi", "device")
    await _insert(conn, "e2", "genesis", "concept")
    await _insert(conn, "e3", "old", "concept", status="merged", merged_into="e2")
    await _insert(conn, "e4", "dead", "concept", status="gone")
    await conn.commit()
    return conn


# ── numbered-migration path (legacy DB) ─────────────────────────────────────


async def test_numbered_up_adds_types_and_columns():
    conn = await _legacy_db()
    await _mig.up(conn)
    await conn.commit()

    cols = await _columns(conn, "entities")
    assert cols >= _CARD_COLS
    # New CHECK accepts the three MW-3 types.
    for i, t in enumerate(sorted(_NEW_TYPES)):
        await _insert(conn, f"new{i}", f"host{i}", t)
    await conn.commit()
    # summary_dirty default is 0 (never carded).
    cur = await conn.execute("SELECT summary_dirty FROM entities WHERE entity_id='e1'")
    assert (await cur.fetchone())[0] == 0
    await conn.close()


async def test_numbered_up_preserves_rows_and_statuses():
    conn = await _legacy_db()
    await _mig.up(conn)
    await conn.commit()
    cur = await conn.execute(
        "SELECT entity_id, norm_name, entity_type, status, merged_into "
        "FROM entities ORDER BY entity_id"
    )
    rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [
        ("e1", "omi", "device", "active", None),
        ("e2", "genesis", "concept", "active", None),
        ("e3", "old", "concept", "merged", "e2"),
        ("e4", "dead", "concept", "gone", None),
    ]
    await conn.close()


async def test_numbered_up_preserves_unique_constraint():
    conn = await _legacy_db()
    await _mig.up(conn)
    await conn.commit()
    # UNIQUE(norm_name, entity_type) must survive the rebuild.
    with pytest.raises(aiosqlite.IntegrityError):
        await _insert(conn, "dup", "omi", "device")
        await conn.commit()
    await conn.close()


async def test_numbered_up_idempotent():
    conn = await _legacy_db()
    await _mig.up(conn)
    await conn.commit()
    await _mig.up(conn)  # second run must be a no-op, not a failure
    await conn.commit()
    cols = await _columns(conn, "entities")
    assert cols >= _CARD_COLS
    cur = await conn.execute("SELECT COUNT(*) FROM entities")
    assert (await cur.fetchone())[0] == 4
    await conn.close()


async def test_numbered_up_refuses_on_column_drift():
    """A legacy entities table carrying an EXTRA column the rebuild target lacks
    must RAISE (drift guard), never copy-a-subset-then-drop and lose that data."""
    conn = await _legacy_db()
    await conn.execute("ALTER TABLE entities ADD COLUMN extra_note TEXT")
    await conn.execute("UPDATE entities SET extra_note = 'keepme' WHERE entity_id='e1'")
    await conn.commit()
    with pytest.raises(RuntimeError, match="not in the rebuild target|drift|refusing"):
        await _mig.up(conn)
    # The source table (and its data) must be intact after the refusal.
    cur = await conn.execute("SELECT extra_note FROM entities WHERE entity_id='e1'")
    assert (await cur.fetchone())[0] == "keepme"
    await conn.close()


async def test_numbered_up_rebuilds_on_partial_migration():
    """If a table has 'host' in the CHECK but is MISSING a card column (a partial
    upgrade), the guard must NOT skip — it must complete the rebuild."""
    conn = await _legacy_db()
    await _mig.up(conn)  # full migrate
    await conn.commit()
    # Simulate a partial state: drop one card column but keep the new CHECK.
    await conn.execute("ALTER TABLE entities DROP COLUMN summary_dirty")
    await conn.commit()
    assert "summary_dirty" not in await _columns(conn, "entities")
    await _mig.up(conn)  # must detect the partial state and re-add
    await conn.commit()
    assert "summary_dirty" in await _columns(conn, "entities")
    await conn.close()


async def test_numbered_up_noop_on_missing_table():
    conn = await aiosqlite.connect(":memory:")
    await _mig.up(conn)  # no entities table → clean no-op (fresh DB path)
    await conn.commit()
    await conn.close()


# ── base path (create_all_tables → _migrate_add_columns) ────────────────────


async def test_base_path_rebuilds_legacy_entities():
    """create_all_tables runs _migrate_add_columns but NOT the numbered runner —
    a legacy DB upgraded via the base path must still gain the types + columns
    (schema_both_build_paths). A pre-existing LEGACY entities table survives the
    CREATE-IF-NOT-EXISTS no-op, then the base-path rebuild upgrades it."""
    conn = await _legacy_db()
    await create_all_tables(conn)
    await conn.commit()
    cols = await _columns(conn, "entities")
    assert cols >= _CARD_COLS
    # Legacy rows preserved through the base-path rebuild.
    cur = await conn.execute("SELECT COUNT(*) FROM entities")
    assert (await cur.fetchone())[0] == 4
    await _insert(conn, "h1", "some-host", "host")
    await conn.commit()
    await conn.close()


async def test_base_path_raises_loud_on_entities_drift():
    """Codex round-7: a failed base-path rebuild (e.g. drift-refused copy) was
    logged and SWALLOWED, letting init commit an unmigrated table — and since
    round-6 every entity read names the card columns explicitly, reads would
    then crash with 'no such column' at runtime. The failure must be LOUD at
    init (matching the numbered runner's posture), never a deferred read crash."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute(_LEGACY_DDL)
    await conn.execute("ALTER TABLE entities ADD COLUMN local_note TEXT")
    await _insert(conn, "e1", "omi", "device")
    await conn.commit()
    with pytest.raises(Exception, match="entities|local_note|drift"):
        await create_all_tables(conn)
    # The savepoint kept the original table intact (rows readable, old shape).
    cur = await conn.execute("SELECT COUNT(*) FROM entities")
    assert (await cur.fetchone())[0] == 1
    await conn.close()


async def test_base_path_rebuild_rolls_back_on_cancellation():
    """Codex round-8: asyncio.CancelledError derives from BaseException, so the
    rebuild's `except Exception` rollback didn't fire for a cancellation landing
    in the DROP→RENAME window — on a caller-owned connection later reused,
    `entities` stayed dropped inside an open savepoint. Cancellation must roll
    back too. The cancel is injected at the RENAME statement, i.e. AFTER the
    real `DROP TABLE entities` ran — the exact vulnerable window."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute(_LEGACY_DDL)
    await _insert(conn, "e1", "omi", "device")
    await conn.commit()

    real_execute = conn.execute

    class _CancelLike(BaseException):
        """BaseException-not-Exception, like asyncio.CancelledError — but
        without the event-loop cancellation semantics that would tangle the
        test task itself."""

    def exec_cancel_on_rename(sql, *a, **k):
        if "RENAME TO entities" in sql:
            raise _CancelLike()
        return real_execute(sql, *a, **k)

    conn.execute = exec_cancel_on_rename  # type: ignore[method-assign]
    try:
        with pytest.raises(_CancelLike):
            await create_all_tables(conn)
        conn.execute = real_execute  # type: ignore[method-assign]
        # Savepoint rolled back: entities intact and readable on the same conn.
        cur = await conn.execute("SELECT COUNT(*) FROM entities")
        assert (await cur.fetchone())[0] == 1
    finally:
        # Close even on assertion failure — an unclosed aiosqlite connection's
        # worker thread hangs the whole pytest session at teardown.
        conn.execute = real_execute  # type: ignore[method-assign]
        await conn.close()


async def test_base_path_idempotent_on_fresh_schema():
    """On a fresh DB the canonical CREATE already carries the new schema, so the
    base-path rebuild guard (CHECK-text 'host') skips — no double rebuild."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await conn.commit()
    cols = await _columns(conn, "entities")
    assert cols >= _CARD_COLS
    await _insert(conn, "p1", "some-project", "project")
    await conn.commit()
    await conn.close()


# ── canonical CREATE carries everything (fresh install) ─────────────────────


async def test_down_reverses_when_no_new_types_present():
    """down() (dev/testing) reverts to the 11-type CHECK and drops card columns,
    preserving rows that are legal under the old CHECK."""
    conn = await _legacy_db()
    await _mig.up(conn)
    await conn.commit()
    await _mig.down(conn)
    await conn.commit()
    cols = await _columns(conn, "entities")
    assert not (_CARD_COLS & cols)  # card columns gone
    cur = await conn.execute("SELECT COUNT(*) FROM entities")
    assert (await cur.fetchone())[0] == 4  # rows preserved
    # Old CHECK back in force: host is rejected.
    with pytest.raises(aiosqlite.IntegrityError):
        await _insert(conn, "h", "h", "host")
        await conn.commit()
    await conn.close()


async def test_down_refuses_when_new_type_rows_exist():
    """down() must refuse (not lossily drop) when host/install/project rows exist
    — they violate the old CHECK."""
    conn = await _legacy_db()
    await _mig.up(conn)
    await conn.commit()
    await _insert(conn, "h1", "a-host", "host")
    await conn.commit()
    with pytest.raises(RuntimeError, match="host/install/project"):
        await _mig.down(conn)
    await conn.close()


async def test_down_refuses_on_unrecognized_column_drift():
    """Codex P2 (freshness pass): down() had a FIXED old-column projection, so a
    later/locally-added column would be silently dropped on the way down (the
    same class up() was fixed for). It must refuse rather than lose data."""
    conn = await _legacy_db()
    await _mig.up(conn)
    await conn.commit()
    # Simulate a drifted table: a column the down-target doesn't know about.
    await conn.execute("ALTER TABLE entities ADD COLUMN extra_note TEXT")
    await conn.commit()
    with pytest.raises(RuntimeError, match="extra_note|drift|not in"):
        await _mig.down(conn)
    await conn.close()


async def test_fresh_create_has_types_and_columns():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute(TABLES["entities"])
    await conn.commit()
    cols = await _columns(conn, "entities")
    assert cols >= _CARD_COLS
    for i, t in enumerate(sorted(_NEW_TYPES)):
        await _insert(conn, f"f{i}", f"n{i}", t)
    await conn.commit()
    await conn.close()
