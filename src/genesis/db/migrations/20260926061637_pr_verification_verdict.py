"""Add the validator's verdict and attempt record to ``pr_verifications``.

WHY. Issue #1718 half B shipped the PRODUCER — the repo-pulse worker opens one
row per merged PR — and left the row with nowhere to say what a validator
CONCLUDED. MEASURED 2026-09-25 on a live install: 217 open / 40 closed, with
``evidence`` NULL on all 257, because nothing outside tests had ever called
``close_verification``.

THE FOUR VERDICTS (owner standing ruling, 2026-09-26). A validator session
returns exactly one of ``pass-mechanical``, ``pass-with-measured-gaps``,
``fail-intent``, ``cannot-verify``. The two PASS verdicts close the row. The
other two DO NOT: the obligation is not discharged, so the row stays OPEN and
instead records that an attempt was made and why it could not complete — which
is what ``last_attempt_note`` is for.

WHY A COLUMN AND NOT A ``closed_reason`` PREFIX. ``closed_reason`` must stay
NULL on a row that stays open, or ``closed_reason IS NOT NULL`` stops implying
``status='closed'`` and the store's one unambiguous axis is gone. It is also
free text written by an LLM caller — the module already had to defend that
surface with a raise rather than a no-op (``crud/pr_verifications.py`` on the
empty-reason case), and a prefix convention over LLM-written text is enforced
only by every reader remembering it. That is the "convention, not a chokepoint"
failure the store's own birth docstring rejects for ``follow_ups``.

``verdict`` IS THE LAST ATTEMPT'S VERDICT, not "the closing verdict" — one
column serves both states, disambiguated by ``status``:

  * ``status='closed'``                      -> terminal, one of the PASS pair
  * ``status='open'`` + ``verdict`` non-NULL -> the last non-closing attempt
  * ``status='open'`` + ``verdict`` NULL     -> never attempted

The invariant the CRUD enforces and the tests pin: a PASS verdict exists if and
only if the row is closed.

NO CHECK CONSTRAINT, deliberately. SQLite cannot ALTER a CHECK, so widening the
vocabulary later costs the full 12-step table rebuild — and this repo has the
receipt: ``20260904231054_session_ledger_ambient_extractor`` exists ONLY to
widen one CHECK and had to grow ``DROP TABLE`` against a live table to do it. A
four-value vocabulary invented before the validator has run even once is
precisely the one most likely to gain a fifth value. There is also no
``ADD COLUMN … CHECK`` precedent anywhere in ``src/``. The vocabulary is
enforced writer-side (``VERDICTS`` / ``PASS_VERDICTS`` + a raise), which also
gives an LLM caller a readable message naming the legal values instead of
``IntegrityError: CHECK constraint failed``.

(Correcting a claim made while designing this, so it is not repeated: an
ALTER-added CHECK is NOT exempt from existing rows. Per sqlite.org's ALTER TABLE
page, since SQLite 3.37.0 an added CHECK is tested against every preexisting row
and the ADD COLUMN FAILS if any row violates it. The decision above stands on
its other reasons; that one was wrong.)

NO BACKFILL. NULL ``verdict`` means "recorded before verdicts existed, or never
attempted" — never a judgement. In particular the 40 already-closed rows are
docs-only path exemptions, not validator conclusions: stamping them
``pass-mechanical`` would fabricate a verdict nobody reached, in the one ledger
whose entire purpose is to be trustworthy about what was verified.
``attempt_count = 0`` + ``verdict IS NULL`` on those rows states the truth.

APPENDED LAST, IN ALTER ORDER — load-bearing for the two-build-path parity test.
``ALTER TABLE … ADD COLUMN`` rewrites the stored schema text by appending the
column-definition text after the last existing column definition, so the only
column order the fresh path (``schema/_tables.py``) and the migrated path can
both produce is: these four, in this order, after ``created_at``. For the same
reason the appended declarations carry NO inline ``--`` comments — a comment
present in one path and absent in the other is a text difference the parity
test's whitespace-collapsing normalizer will not absorb.

Metadata-only and therefore instant: per sqlite.org, a column addition without
constraints makes no change to table content, so execution time is independent
of the 257 rows already present.

Self-contained + idempotent: the table guard returns early on a fresh install
(``create_all_tables`` builds the canonical shape), and each ADD is PRAGMA
guarded on ITS OWN column — never on a sibling's presence, which is how
migration 0007 guarded a constraint change on the wrong fact and silently never
applied it. No commit: the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite

#: (column, declaration) in the order the fresh-install DDL must also list them.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("verdict", "TEXT"),
    ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_attempt_at", "TEXT"),
    ("last_attempt_note", "TEXT"),
)


async def _add_column(db: aiosqlite.Connection, table: str, col: str, decl: str) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if await cursor.fetchone() is None:
        return  # fresh install: create_all_tables builds the full canonical shape
    cursor = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cursor.fetchall()}
    if col not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


async def up(db: aiosqlite.Connection) -> None:
    for col, decl in _COLUMNS:
        await _add_column(db, "pr_verifications", col, decl)


async def down(db: aiosqlite.Connection) -> None:
    """No-op, deliberately — and this one is not merely "harmless to leave".

    ``last_attempt_note`` holds the only record of why a validator could not
    finish, written by a session that has since ended. DROP COLUMN rewrites the
    table to purge that content, so a down-migration here destroys evidence to
    reclaim four nullable columns nothing is paying for.
    """
