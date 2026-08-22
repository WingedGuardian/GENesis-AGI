"""Migration 0085: backfill observations.origin_class (WS-3, fail-closed)."""

from __future__ import annotations

import importlib

import aiosqlite

m0085 = importlib.import_module("genesis.db.migrations.0085_backfill_observation_origin")


async def _db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE observations (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, type TEXT NOT NULL,
            content TEXT NOT NULL, priority TEXT NOT NULL, created_at TEXT NOT NULL,
            origin_class TEXT)"""
    )
    await db.execute(
        "CREATE TABLE cc_sessions (id TEXT PRIMARY KEY, channel TEXT, "
        "source_tag TEXT, origin_class TEXT)"
    )
    await db.commit()
    return db


async def _ins(db, oid, source, origin=None):
    await db.execute(
        "INSERT INTO observations (id, source, type, content, priority, created_at, origin_class) "
        "VALUES (?,?,?,?,?,?,?)",
        (oid, source, "finding", "c", "low", "2026-01-01T00:00:00", origin),
    )


async def _origin(db, oid):
    cur = await db.execute("SELECT origin_class FROM observations WHERE id=?", (oid,))
    return (await cur.fetchone())[0]


async def test_backfill_classifies_by_source_and_session():
    db = await _db()
    try:
        await db.execute("INSERT INTO cc_sessions (id, origin_class) VALUES ('S1','owner')")
        await db.execute("INSERT INTO cc_sessions (id, origin_class) VALUES ('S2', NULL)")
        await _ins(db, "o_recon", "recon")
        await _ins(db, "o_aware", "awareness_loop")
        await _ins(db, "o_intake_ar", "intake:anticipatory_research")
        await _ins(db, "o_intake_mi", "intake:model_intelligence")
        await _ins(db, "o_unknown", "brand_new_writer_xyz")
        await _ins(db, "o_module", "module:automaton_supervisor")
        await _ins(db, "o_sess_owner", "session:S1")
        await _ins(db, "o_sess_null", "session:S2")
        await _ins(db, "o_preexist", "recon", origin="first_party")  # already stamped
        await db.commit()

        await m0085.up(db)

        # source-classified
        assert await _origin(db, "o_recon") == "external_untrusted"
        assert await _origin(db, "o_aware") == "first_party"
        assert await _origin(db, "o_intake_ar") == "first_party"  # Genesis-authored intake
        assert await _origin(db, "o_intake_mi") == "external_untrusted"  # crawled intake
        # fail-closed: unknown / unmapped sources stay NULL
        assert await _origin(db, "o_unknown") is None
        assert await _origin(db, "o_module") is None
        # session-attributed: inherit the session's origin; NULL session → stays NULL
        assert await _origin(db, "o_sess_owner") == "owner"
        assert await _origin(db, "o_sess_null") is None
        # guard: a row already stamped is never overwritten
        assert await _origin(db, "o_preexist") == "first_party"
    finally:
        await db.close()


async def test_backfill_new_first_party_sources():
    """Codex PR #1431 finding C: follow_up_watchdog + ego_domain_redirect: classify
    (snapshot kept in lockstep with the live classifier). retrospective/cc_debrief
    are channel-stamped, NOT snapshot-classified → stay NULL (fail-closed)."""
    db = await _db()
    try:
        await _ins(db, "o_fuw", "follow_up_watchdog")
        await _ins(db, "o_ego", "ego_domain_redirect:ego_cycle")
        await _ins(db, "o_retro", "retrospective")
        await _ins(db, "o_debrief", "cc_debrief")
        await db.commit()
        await m0085.up(db)
        assert await _origin(db, "o_fuw") == "first_party"
        assert await _origin(db, "o_ego") == "first_party"
        assert await _origin(db, "o_retro") is None  # channel-stamped, not source-derived
        assert await _origin(db, "o_debrief") is None
    finally:
        await db.close()


async def test_backfill_grandfathers_gateway_sessions():
    """Step 0: pre-deploy gateway/voice cc_sessions rows with NULL origin are
    stamped external_untrusted BEFORE the session-JOIN, so their observations
    inherit it. NULL-channel adoptee (owner CLI) and telegram stay untouched."""
    db = await _db()
    try:
        # gateway/voice foreground rows that predate the create-time stamp
        await db.execute(
            "INSERT INTO cc_sessions (id, channel, source_tag) VALUES "
            "('W','web','foreground'),('H','whatsapp','foreground'),"
            "('V','voice_s2s','voice'),('A',NULL,'foreground'),('T','telegram','foreground')"
        )
        await _ins(db, "o_web", "session:W")
        await _ins(db, "o_voice", "session:V")
        await _ins(db, "o_adopt", "session:A")  # NULL-channel owner CLI → stays NULL
        await _ins(db, "o_tg", "session:T")  # telegram (owner) → stays NULL
        await db.commit()
        await m0085.up(db)

        # cc_sessions themselves were grandfathered
        cur = await db.execute("SELECT id, origin_class FROM cc_sessions ORDER BY id")
        got = {r[0]: r[1] for r in await cur.fetchall()}
        assert got == {
            "A": None,
            "H": "external_untrusted",
            "T": None,
            "V": "external_untrusted",
            "W": "external_untrusted",
        }
        # and their observations inherited via the session-JOIN
        assert await _origin(db, "o_web") == "external_untrusted"
        assert await _origin(db, "o_voice") == "external_untrusted"
        assert await _origin(db, "o_adopt") is None
        assert await _origin(db, "o_tg") is None
    finally:
        await db.close()


async def test_backfill_idempotent():
    db = await _db()
    try:
        await _ins(db, "o1", "recon")
        await db.commit()
        await m0085.up(db)
        await m0085.up(db)  # second run must be a no-op, not raise
        assert await _origin(db, "o1") == "external_untrusted"
    finally:
        await db.close()


async def test_backfill_no_observations_table_is_safe():
    db = await aiosqlite.connect(":memory:")
    try:
        await m0085.up(db)  # missing table → guarded no-op
    finally:
        await db.close()


async def test_backfill_session_edge_cases():
    """Absent session row → NULL; a pre-stamped session row is never overwritten."""
    db = await _db()
    try:
        await db.execute("INSERT INTO cc_sessions (id, origin_class) VALUES ('S1','owner')")
        await _ins(db, "o_sess_absent", "session:NO_SUCH_SESSION")  # no matching row
        await _ins(db, "o_sess_pre", "session:S1", origin="external_untrusted")  # already set
        await db.commit()
        await m0085.up(db)
        assert await _origin(db, "o_sess_absent") is None  # fail-closed, no session row
        assert await _origin(db, "o_sess_pre") == "external_untrusted"  # guard: not overwritten
    finally:
        await db.close()


async def test_backfill_no_cc_sessions_table():
    """observations present but cc_sessions absent → path-1 skipped, path-2 still
    classifies source-derivable rows; session rows stay NULL."""
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE observations (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, type TEXT NOT NULL,
            content TEXT NOT NULL, priority TEXT NOT NULL, created_at TEXT NOT NULL,
            origin_class TEXT)"""
    )
    try:
        await _ins(db, "o_recon", "recon")
        await _ins(db, "o_sess", "session:S1")
        await db.commit()
        await m0085.up(db)  # no cc_sessions table
        assert await _origin(db, "o_recon") == "external_untrusted"  # path-2 works
        assert await _origin(db, "o_sess") is None  # path-1 skipped → stays NULL
    finally:
        await db.close()
