"""CRUD for attention_events — the passive-listening attention engine's SHADOW store.

Rows are attention DECISIONS + REFERENCES + labels ONLY (activation/score/
triggers_fired/window_ref/clarity); NEVER ambient transcript text (firewall). The
offline shadow runner writes; the dashboard "Attention" tab (PR2) reads for the
should/shouldn't review + offline calibration, and writes back ``acceptance_signal``.

All reads assume ``db.row_factory = aiosqlite.Row`` (the runtime's ``rt.db`` sets it);
they return plain dicts. Filter/aggregate queries key off ``json_each``/``json_extract``
on the JSON columns (exact name match — NEVER a ``LIKE`` substring, which would false-match
the ``kind`` values and JSON keys).
"""
from __future__ import annotations

import aiosqlite

# Column order — the offline runner's row tuples (ShadowStoreConsumer._to_row) MUST match.
COLUMNS = (
    "id", "ts", "session_id", "activation", "score", "triggers_fired", "suppressors",
    "window_ref", "mode_state", "clarity", "l15_verdict", "acceptance_signal",
    "snapshot_id", "config_version", "created_at",
)

# On a re-run over the same snapshot+config the row id collides. Refresh the derived
# columns, but NEVER touch a human label (``acceptance_signal``) or the first-seen time
# (``created_at``) — the WHERE guard below makes the whole UPDATE a no-op for labeled rows.
_UPSERT_SET_COLS = tuple(c for c in COLUMNS if c not in ("id", "acceptance_signal", "created_at"))

# The only valid review labels (mirrors the plan's should/shouldn't/skip).
LABELS = ("should", "shouldnt", "skip")


async def bulk_upsert_events(db: aiosqlite.Connection, rows: list[tuple]) -> int:
    """Idempotent, label-preserving bulk upsert of shadow AttentionEvents.

    ``rows`` are tuples in ``COLUMNS`` order. Re-running the same snapshot+config collides
    on the row id; we refresh the derived columns but preserve any human ``acceptance_signal``
    label (and ``created_at``) via ``ON CONFLICT ... WHERE acceptance_signal IS NULL``. One
    transaction; returns the count of rows submitted (matching prior behaviour)."""
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(COLUMNS))
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in _UPSERT_SET_COLS)
    await db.executemany(
        f"INSERT INTO attention_events ({', '.join(COLUMNS)}) VALUES ({placeholders}) "  # noqa: S608
        f"ON CONFLICT(id) DO UPDATE SET {set_clause} "
        f"WHERE attention_events.acceptance_signal IS NULL",
        rows,
    )
    await db.commit()
    return len(rows)


async def count(db: aiosqlite.Connection) -> int:
    cursor = await db.execute("SELECT COUNT(*) AS n FROM attention_events")
    row = await cursor.fetchone()
    return row["n"] if row else 0


async def get_event(db: aiosqlite.Connection, event_id: str) -> dict | None:
    """One event row as a dict (refs + features + label — never transcript text), or None."""
    cursor = await db.execute("SELECT * FROM attention_events WHERE id = ?", (event_id,))
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def list_events(
    db: aiosqlite.Connection,
    *,
    activation: str | None = None,
    trigger: str | None = None,
    is_user: bool = False,
    unlabeled: bool = False,
    session_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """List events (newest first), value-free (refs + features + label, NO text).

    ``trigger`` filters to events whose ``triggers_fired`` contains a hit with that exact
    ``name`` (via ``json_each``, not ``LIKE``). ``is_user=True`` additionally requires the
    ``is_user`` trigger — i.e. the *trigger utterance* (window end) had ``is_user=1``.
    ``unlabeled=True`` restricts to ``acceptance_signal IS NULL`` (the review queue)."""
    where: list[str] = []
    params: list = []
    if activation is not None:
        where.append("activation = ?")
        params.append(activation)
    if session_id is not None:
        where.append("session_id = ?")
        params.append(session_id)
    if unlabeled:
        where.append("acceptance_signal IS NULL")
    # Exact name match on a triggers_fired[] element. EXISTS composes for AND without a
    # DISTINCT-producing join, and lets trigger + is_user filter independently.
    if trigger is not None:
        where.append(
            "EXISTS (SELECT 1 FROM json_each(triggers_fired) je "
            "WHERE json_extract(je.value, '$.name') = ?)"
        )
        params.append(trigger)
    if is_user:
        where.append(
            "EXISTS (SELECT 1 FROM json_each(triggers_fired) je "
            "WHERE json_extract(je.value, '$.name') = 'is_user')"
        )
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    lim = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    params.extend([lim, off])
    cursor = await db.execute(
        f"SELECT * FROM attention_events{where_sql} "  # noqa: S608
        f"ORDER BY ts DESC LIMIT ? OFFSET ?",
        params,
    )
    return [dict(r) for r in await cursor.fetchall()]


async def update_acceptance_signal(
    db: aiosqlite.Connection, event_id: str, signal: str,
) -> tuple[bool, str | None]:
    """Write a review label. Returns ``(found, prior_signal)`` so the UI can show X->Y.

    Raises ``ValueError`` for a signal outside ``LABELS`` (route maps it to 400)."""
    if signal not in LABELS:
        raise ValueError(f"invalid acceptance_signal {signal!r}; expected one of {LABELS}")
    cursor = await db.execute(
        "SELECT acceptance_signal FROM attention_events WHERE id = ?", (event_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return (False, None)
    prior = row[0]
    await db.execute(
        "UPDATE attention_events SET acceptance_signal = ? WHERE id = ?", (signal, event_id)
    )
    await db.commit()
    return (True, prior)


# ── aggregates for the cockpit stats panel (all counts — never text) ──────────────

async def activation_stats(db: aiosqlite.Connection) -> dict:
    cursor = await db.execute(
        "SELECT activation, COUNT(*) AS n FROM attention_events GROUP BY activation"
    )
    return {r["activation"]: r["n"] for r in await cursor.fetchall()}


async def trigger_stats(db: aiosqlite.Connection) -> dict:
    """Per-trigger fire counts (the dominance bar) — exact name via json_each."""
    cursor = await db.execute(
        "SELECT json_extract(je.value, '$.name') AS name, COUNT(*) AS n "
        "FROM attention_events ae, json_each(ae.triggers_fired) je "
        "GROUP BY name ORDER BY n DESC"
    )
    return {r["name"]: r["n"] for r in await cursor.fetchall()}


async def suppressor_stats(db: aiosqlite.Connection) -> dict:
    """Per-suppressor hit counts (``suppressors`` is a JSON array of names)."""
    cursor = await db.execute(
        "SELECT je.value AS name, COUNT(*) AS n "
        "FROM attention_events ae, json_each(ae.suppressors) je "
        "GROUP BY name ORDER BY n DESC"
    )
    return {r["name"]: r["n"] for r in await cursor.fetchall()}


async def label_counts(db: aiosqlite.Connection) -> dict:
    """total / labeled / unlabeled + a by-signal breakdown."""
    cursor = await db.execute(
        "SELECT acceptance_signal, COUNT(*) AS n FROM attention_events GROUP BY acceptance_signal"
    )
    by_signal: dict[str, int] = {}
    unlabeled = 0
    total = 0
    for r in await cursor.fetchall():
        n = r["n"]
        total += n
        if r["acceptance_signal"] is None:
            unlabeled += n
        else:
            by_signal[r["acceptance_signal"]] = n
    return {
        "total": total,
        "labeled": total - unlabeled,
        "unlabeled": unlabeled,
        "by_signal": by_signal,
    }
