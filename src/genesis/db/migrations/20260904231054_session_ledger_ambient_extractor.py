"""Widen session_ledger.added_by to admit the ambient ledger extractor.

The extractor (Haiku, run detached at each PreCompact boundary) has produced
shadow proposals for weeks but had no way to write a live ledger row: the write
path did not exist, and `added_by` is constrained at the SCHEMA level, not just
in Python.

WHY A NEW VALUE RATHER THAN REUSING 'ambient'. `_default_added_by()` already
returns 'ambient' for any DISPATCHED Claude Code session
(``GENESIS_CC_SESSION=1``), and the shadow report's leak invariant keys on
exactly that value to assert the extractor has written nothing live. Reusing it
would make that invariant unable to tell the extractor from a dispatched
session -- it would stop meaning anything on the very day it starts mattering.
A distinct value keeps the invariant a typed check, and makes extractor rows
filterable and bulk-revertible.

WHY A SEPARATE `source_quote` COLUMN. The extractor's verified transcript quote
and a resolver's attribution are two different facts, and `evidence` was
carrying both: `repo_pulse_worker` REPLACES `evidence` with PR attribution when
it absorbs a row, so a promoted extractor row lost its quote the moment
repo-pulse touched it — and the shadow report's live invariant ("every extractor
row carries its source quote") then flagged that row as a leak. One column with
two writers and two meanings cannot answer either question reliably; the quote
now has a column no resolver writes.

SQLite cannot ALTER a CHECK constraint, so this is the documented 12-step table
rebuild (precedent: 0012_ego_proposals_status_check).

IDEMPOTENCY IS KEYED ON THE CHECK DDL ITSELF, deliberately. Migration 0007
guarded its own rebuild on a COLUMN's existence, an earlier ALTER added that
column first, and 0007 then short-circuited and silently never applied its
constraint change -- which is why 0012 had to exist at all. Guarding on the
thing this migration actually changes is the only check that cannot lie.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)

_NEW_VALUE = "ambient_ledger_extractor"

# The columns this rebuild KNOWS. Anything else on the live table is a
# fork-added column (the migration runner intentionally executes fork
# migrations too), and a hardcoded copy list would silently destroy it —
# the DROP TABLE below is irreversible. Unknown columns are re-added to the
# new table from their PRAGMA declaration and their data copied.
_KNOWN_COLUMNS = (
    "id",
    "session_id",
    "text",
    "status",
    "source_ref",
    "added_by",
    "evidence",
    "source_quote",
    "created_at",
    "updated_at",
)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _add_promoted_item_id(db: aiosqlite.Connection) -> None:
    """Additive: promotion state on shadow events (retryable-promotion sweep).

    ``promoted_item_id`` is the ``session_ledger`` row a proposal became in
    live mode; NULL means unpromoted and therefore still retryable. Guarded on
    the column's own presence — pragma, not a version flag — so a partial
    prior attempt cannot make this silently skip.
    """
    cursor = await db.execute("PRAGMA table_info(session_ledger_shadow_events)")
    cols = {row[1] for row in await cursor.fetchall()}
    if not cols:
        return  # table absent: fresh install creates it from _tables.py
    if "promoted_item_id" in cols:
        return
    await db.execute("ALTER TABLE session_ledger_shadow_events ADD COLUMN promoted_item_id TEXT")


async def _add_source_quote(db: aiosqlite.Connection) -> None:
    """Additive: durable provenance, guarded on ITS OWN column.

    Deliberately NOT folded into the rebuild below. That rebuild is idempotent
    on the added_by CHECK DDL, so on an install where the constraint was already
    widened it returns early — and any second change riding along behind that
    guard would silently never apply. This migration's own docstring records
    that exact failure (0007 guarded a constraint change on a column's presence
    and never applied it); guarding each change on the thing IT changes is the
    only check that cannot lie, and that rule cuts both ways.
    """
    cursor = await db.execute("PRAGMA table_info(session_ledger)")
    cols = {row[1] for row in await cursor.fetchall()}
    if not cols:
        return  # table absent: a fresh install creates it from _tables.py
    if "source_quote" in cols:
        return
    await db.execute("ALTER TABLE session_ledger ADD COLUMN source_quote TEXT")


async def up(db: aiosqlite.Connection) -> None:
    await _add_promoted_item_id(db)

    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_ledger'"
    )
    row = await cursor.fetchone()
    if not row:
        # Table absent: a fresh install creates it from _tables.py, which
        # already carries the widened constraint.
        return

    ddl = row[0] or ""
    if _NEW_VALUE in ddl:
        # Constraint already widened by a prior run — but the column may still
        # be missing, which is the case the separate guard exists for.
        await _add_source_quote(db)
        return

    # FORK-COLUMN PRESERVATION. Read the LIVE table's columns before the
    # rebuild: a supported private fork may have added columns here, and the
    # DROP TABLE below destroys anything the copy list omits — irreversibly.
    # Each unknown column is re-added to the new table from its own PRAGMA
    # declaration (name/type/notnull/default) and its data copied. The one
    # shape ADD COLUMN cannot carry — NOT NULL with no default — aborts the
    # migration loudly instead of silently dropping the data: the fork
    # reconciles first, nothing is lost.
    cursor = await db.execute("PRAGMA table_info(session_ledger)")
    live_info = await cursor.fetchall()  # (cid, name, type, notnull, dflt_value, pk)
    live_names = {row[1] for row in live_info}
    extras = [row for row in live_info if row[1] not in _KNOWN_COLUMNS]
    for row in extras:
        if row[3] and row[4] is None:
            raise RuntimeError(
                "session_ledger rebuild: fork-added column "
                f"{row[1]!r} is NOT NULL without a default, which ADD COLUMN "
                "cannot recreate — reconcile the fork migration (add a "
                "default or drop the constraint) before this rebuild runs"
            )
    if extras:
        logger.info(
            "session_ledger rebuild: preserving %d fork-added column(s): %s",
            len(extras),
            ", ".join(row[1] for row in extras),
        )

    # A prior attempt may have died between CREATE and RENAME.
    await db.execute("DROP TABLE IF EXISTS session_ledger_new")

    await db.execute(
        """
        CREATE TABLE session_ledger_new (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            text        TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','in_progress','done','absorbed','dropped')),
            source_ref  TEXT,
            added_by    TEXT NOT NULL DEFAULT 'foreground'
                        CHECK(added_by IN ('foreground','ambient','pulse',
                                           'ambient_ledger_extractor')),
            evidence    TEXT,
            source_quote TEXT,   -- extractor's verified transcript quote; never overwritten by a resolver
            created_at  TEXT NOT NULL,
            updated_at  TEXT
        )
        """
    )
    for row in extras:
        decl = _quote_ident(row[1])
        if row[2]:
            decl += f" {row[2]}"
        if row[3]:
            decl += f" NOT NULL DEFAULT {row[4]}"
        elif row[4] is not None:
            decl += f" DEFAULT {row[4]}"
        await db.execute(f"ALTER TABLE session_ledger_new ADD COLUMN {decl}")

    base = [
        "id",
        "session_id",
        "text",
        "status",
        "source_ref",
        "added_by",
        "evidence",
        "created_at",
        "updated_at",
    ]
    extra_names = [row[1] for row in extras]
    target_cols = [*base, "source_quote", *extra_names]
    select_cols = [
        *base,
        # A partial prior attempt (or a fork) may already carry the column —
        # copy it rather than resetting provenance to NULL.
        "source_quote" if "source_quote" in live_names else "NULL",
        *extra_names,
    ]
    await db.execute(
        f"INSERT INTO session_ledger_new ({', '.join(_quote_ident(c) for c in target_cols)}) "  # noqa: S608 — identifiers from PRAGMA, values never interpolated
        f"SELECT {', '.join(c if c == 'NULL' else _quote_ident(c) for c in select_cols)} "
        "FROM session_ledger"
    )

    await db.execute("DROP TABLE session_ledger")
    await db.execute("ALTER TABLE session_ledger_new RENAME TO session_ledger")

    # The rebuild drops the table's indexes with it.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_ledger_session "
        "ON session_ledger(session_id, status)"
    )

    # After the rebuild too: the rebuild's DDL already carries the column, so
    # this is a no-op there. It matters on the OTHER path — the early return
    # above, where the constraint was already widened and only the column is
    # missing. Both paths converge on the same shape.
    await _add_source_quote(db)
