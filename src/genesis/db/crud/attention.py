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

import json

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
    config_version: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """List events (newest first), value-free (refs + features + label, NO text).

    ``trigger`` filters to events whose ``triggers_fired`` contains a hit with that exact
    ``name`` (via ``json_each``, not ``LIKE``). ``is_user=True`` additionally requires the
    ``is_user`` trigger — i.e. the *trigger utterance* (window end) had ``is_user=1``.
    ``unlabeled=True`` restricts to ``acceptance_signal IS NULL`` (the review queue).
    ``config_version`` scopes to one shadow-run config (the calibration A/B filter — PR3c-1)."""
    where: list[str] = []
    params: list = []
    if activation is not None:
        where.append("activation = ?")
        params.append(activation)
    if config_version is not None:
        where.append("config_version = ?")
        params.append(config_version)
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


_NOTE_UNSET = object()  # sentinel: distinguishes "note not provided" from "note set to None (clear)"


async def update_acceptance_signal(
    db: aiosqlite.Connection, event_id: str, signal: str, note=_NOTE_UNSET,
) -> tuple[bool, str | None]:
    """Write a review label (+ an optional reviewer note). Returns ``(found, prior_signal)``
    so the UI can show X->Y.

    ``note`` is the reviewer's own one-line WHY — the reasoning that the perk decision is an
    LLM judgment makes the point of the review (PR3d). It is SENTINEL-defaulted: omitted →
    the existing note is preserved; passed (a string, or ``None`` to clear) → it is written.

    Raises ``ValueError`` for a signal outside ``LABELS`` (route maps it to 400)."""
    if signal not in LABELS:
        raise ValueError(f"invalid acceptance_signal {signal!r}; expected one of {LABELS}")
    cursor = await db.execute(
        "SELECT acceptance_signal FROM attention_events WHERE id = ?", (event_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return (False, None)
    prior = row["acceptance_signal"]
    if note is _NOTE_UNSET:
        await db.execute(
            "UPDATE attention_events SET acceptance_signal = ? WHERE id = ?", (signal, event_id)
        )
    else:
        await db.execute(
            "UPDATE attention_events SET acceptance_signal = ?, acceptance_note = ? WHERE id = ?",
            (signal, note, event_id),
        )
    await db.commit()
    return (True, prior)


# ── aggregates for the cockpit stats panel (all counts — never text) ──────────────
#
# Each aggregate accepts an optional ``config_version`` so the stats panel can scope to
# ONE shadow-run config (PR3c-1's A/B filter) — otherwise, the moment a 2nd config_version
# is persisted, the dominance bar would mix versions while the list filters correctly.
# ``config_versions()`` itself stays UNFILTERED (the dropdown must list every version).

def _cv_clause(config_version: str | None, *, alias: str = "") -> tuple[str, tuple]:
    """WHERE fragment (leading space) + params for an optional config_version filter.

    ``alias`` prefixes the column for the json_each aggregates (``ae.config_version``)."""
    if config_version is None:
        return "", ()
    col = f"{alias}.config_version" if alias else "config_version"
    return f" WHERE {col} = ?", (config_version,)


async def activation_stats(db: aiosqlite.Connection, config_version: str | None = None) -> dict:
    """Per-activation event counts (hard / soft / suppressed) — the fire-rate breakdown."""
    where, params = _cv_clause(config_version)
    cursor = await db.execute(
        f"SELECT activation, COUNT(*) AS n FROM attention_events{where} GROUP BY activation",  # noqa: S608
        params,
    )
    return {r["activation"]: r["n"] for r in await cursor.fetchall()}


async def trigger_stats(db: aiosqlite.Connection, config_version: str | None = None) -> dict:
    """Per-trigger fire counts (the dominance bar) — exact name via json_each."""
    where, params = _cv_clause(config_version, alias="ae")
    cursor = await db.execute(
        "SELECT json_extract(je.value, '$.name') AS name, COUNT(*) AS n "
        f"FROM attention_events ae, json_each(ae.triggers_fired) je{where} "  # noqa: S608
        "GROUP BY name ORDER BY n DESC",
        params,
    )
    return {r["name"]: r["n"] for r in await cursor.fetchall()}


async def suppressor_stats(db: aiosqlite.Connection, config_version: str | None = None) -> dict:
    """Per-suppressor hit counts (``suppressors`` is a JSON array of names)."""
    where, params = _cv_clause(config_version, alias="ae")
    cursor = await db.execute(
        "SELECT je.value AS name, COUNT(*) AS n "
        f"FROM attention_events ae, json_each(ae.suppressors) je{where} "  # noqa: S608
        "GROUP BY name ORDER BY n DESC",
        params,
    )
    return {r["name"]: r["n"] for r in await cursor.fetchall()}


async def label_counts(db: aiosqlite.Connection, config_version: str | None = None) -> dict:
    """total / labeled / unlabeled + a by-signal breakdown."""
    where, params = _cv_clause(config_version)
    cursor = await db.execute(
        f"SELECT acceptance_signal, COUNT(*) AS n FROM attention_events{where} "  # noqa: S608
        "GROUP BY acceptance_signal",
        params,
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


async def config_versions(db: aiosqlite.Connection) -> list[str]:
    """Every distinct ``config_version`` present (UNFILTERED — populates the A/B dropdown)."""
    cursor = await db.execute(
        "SELECT DISTINCT config_version FROM attention_events "
        "WHERE config_version IS NOT NULL ORDER BY config_version"
    )
    return [r["config_version"] for r in await cursor.fetchall()]


# ── calibration tooling (PR3c-1) ──────────────────────────────────────────────────

async def load_fire_records(db: aiosqlite.Connection, config_version: str) -> list[dict]:
    """Value-free rows for the A/B fire-set differ: every event of ONE config_version.

    Returns raw dicts (``id``/``activation``/``triggers_fired``/``suppressors``/``window_ref``
    with the JSON columns still strings) — the differ parses + maps them to fire-records.
    ALL rows, UNPAGED (the differ needs the full set, not the 500-capped review page).
    Assumes ``db.row_factory = aiosqlite.Row`` (the caller sets it — see differ.load_from_db)."""
    cursor = await db.execute(
        "SELECT id, activation, triggers_fired, suppressors, window_ref "
        "FROM attention_events WHERE config_version = ? ORDER BY ts",
        (config_version,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def load_labeled_fires(db: aiosqlite.Connection, config_version: str) -> list[dict]:
    """Value-free rows for the LABEL-SCORED calibration report (PR3c-2a): every should/shouldnt
    -labeled event of ONE config_version. Extends ``load_fire_records``'s columns with ``score``
    + ``clarity`` + ``acceptance_signal`` (the label). ``skip`` and unlabeled rows are excluded
    in SQL — they are not a usable judgment and must never reach the metrics. UNPAGED.
    Assumes ``db.row_factory = aiosqlite.Row`` (the caller sets it — see calibrate.load_labeled)."""
    cursor = await db.execute(
        "SELECT id, activation, score, clarity, triggers_fired, suppressors, window_ref, "
        "acceptance_signal FROM attention_events "
        "WHERE config_version = ? AND acceptance_signal IS NOT NULL AND acceptance_signal != 'skip' "
        "ORDER BY ts",
        (config_version,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def update_l15_verdict(
    db: aiosqlite.Connection, event_id: str, verdict: dict | None,
) -> bool:
    """Backfill/refresh an event's L1.5 ``l15_verdict`` (a MACHINE field). Returns found.

    UNCONDITIONAL (no ``acceptance_signal IS NULL`` guard) — unlike ``bulk_upsert_events``,
    which guards the human label. ``l15_verdict`` carries no human input, so a later
    ``--l15`` run may safely refresh it even on an already-LABELED row (the label/verdict
    correlation workflow PR3c-2 needs). Stored as the judge's own JSON verdict (mirrors
    ``consumers._to_row``) — never raw transcript text (firewall)."""
    payload = json.dumps(verdict) if verdict is not None else None
    cursor = await db.execute(
        "UPDATE attention_events SET l15_verdict = ? WHERE id = ?", (payload, event_id)
    )
    await db.commit()
    return cursor.rowcount > 0


async def bulk_update_l15_verdicts(
    db: aiosqlite.Connection, rows: list[tuple[str, str]]
) -> int:
    """Unconditionally set ``l15_verdict`` for each ``(event_id, verdict_json)`` pair; returns
    the count submitted.

    The bulk sibling of ``update_l15_verdict`` for the shadow runner's backfill: ``l15_verdict``
    is a MACHINE field, so — unlike ``bulk_upsert_events``, whose ``WHERE acceptance_signal IS
    NULL`` guard FREEZES a labeled row's derived columns — this writes even on LABELED rows.
    That is exactly what a later ``--l15`` run needs: the verdict must attach to the same rows
    the human already labeled, or the human∩judge agreement view reads the judge as "missing"
    on precisely the rows under review. Callers pass already-serialized JSON (mirrors
    ``ShadowStoreConsumer``, which ``json.dumps`` once) and filter out None verdicts themselves
    (a no-verdict re-run must not null a stored one). One ``executemany`` in the caller's txn."""
    if not rows:
        return 0
    await db.executemany(
        "UPDATE attention_events SET l15_verdict = ? WHERE id = ?",
        [(verdict_json, event_id) for event_id, verdict_json in rows],
    )
    await db.commit()
    return len(rows)
