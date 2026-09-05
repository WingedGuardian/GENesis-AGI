"""The CC-sessions snapshot must query columns that EXIST.

Origin (measured 2026-09-04): the avg-duration and failed-24h queries
referenced ``ended_at`` and ``duration_ms`` — neither is a cc_sessions
column — so every snapshot raised sqlite3.Error, the except swallowed it,
``failed_24h`` was hardcoded 0 forever, and the dashboard's "N failed
(24h)" line plus its degraded branch were unreachable despite 400+ failed
rows in a live DB. The schema-pinning test locks the class: a dead column
can never silently zero a stat again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import cc_sessions as crud
from genesis.observability.snapshots.cc_sessions import cc_sessions as snapshot

pytestmark = pytest.mark.asyncio


async def _seed(db, *, sid, status, started_delta_h, end_delta_h):
    now = datetime.now(UTC)
    started = (now - timedelta(hours=started_delta_h)).isoformat()
    await crud.create(
        db,
        id=sid,
        session_type="foreground",
        model="sonnet",
        effort="medium",
        status="active",
        started_at=started,
        last_activity_at=started,
    )
    end = (now - timedelta(hours=end_delta_h)).isoformat()
    await crud.update_status(db, sid, status=status, ts=end)


async def test_failed_24h_counts_real_failures(db):
    await _seed(db, sid="f1", status="failed", started_delta_h=3, end_delta_h=2)
    await _seed(db, sid="f2", status="failed", started_delta_h=60, end_delta_h=50)  # old
    snap = await snapshot(db, None, None)
    assert snap["failed_24h"] == 1


async def test_avg_duration_uses_real_columns(db):
    await _seed(db, sid="c1", status="completed", started_delta_h=3, end_delta_h=2)
    snap = await snapshot(db, None, None)
    avg = snap["avg_duration_ms_24h"]
    assert avg.get("foreground") == pytest.approx(3600 * 1000, rel=0.01)


async def test_legacy_rows_without_completed_at_still_count(db):
    """Historical failed rows (pre-discipline) have completed_at NULL —
    COALESCE(completed_at, last_activity_at) keeps them countable with no
    backfill migration. The legacy shape is simulated with the OLD
    status-only writer semantics (a status change that stamps nothing),
    which crud.update_status no longer produces — so build it by hand via
    a status value outside the terminal set, then flip the column with the
    plain writer the schema still allows."""
    now = datetime.now(UTC)
    recent = (now - timedelta(hours=2)).isoformat()
    await crud.create(
        db,
        id="legacy",
        session_type="foreground",
        model="sonnet",
        effort="medium",
        status="failed",
        started_at=recent,
        last_activity_at=recent,
    )
    row = await crud.get_by_id(db, "legacy")
    assert row["completed_at"] is None  # legacy shape: terminal, unstamped
    snap = await snapshot(db, None, None)
    assert snap["failed_24h"] == 1


async def test_stat_queries_reference_only_real_columns(db):
    """Schema pin: every column named by the module's SQL exists in the
    table — the exact class that shipped dead (ended_at, duration_ms)."""
    import importlib
    import re
    from pathlib import Path

    # importlib, not `import ... as`: the snapshots package __init__ re-exports
    # a FUNCTION named cc_sessions that shadows the module on attribute lookup.
    mod = importlib.import_module("genesis.observability.snapshots.cc_sessions")
    src = Path(mod.__file__).read_text()
    cur = await db.execute("SELECT name FROM pragma_table_info('cc_sessions')")
    real = {r[0] for r in await cur.fetchall()}
    # Conservative extraction: identifiers inside the module's SQL strings.
    sql_chunks = re.findall(r'"""(SELECT[^"]+?)"""', src, re.DOTALL)
    assert sql_chunks, "no SQL found — extraction broken"
    for chunk in sql_chunks:
        if "cc_sessions" not in chunk:
            continue
        idents = set(re.findall(r"\b([a-z_]+)\b", chunk))
        # SELECT aliases are names the query CREATES, not columns it reads.
        idents -= set(re.findall(r"\bas\s+([a-z_]+)", chunk))
        fake = (
            {
                i
                for i in idents
                if i.endswith("_at") or i.endswith("_ms") or i in ("duration", "ended")
            }
            - real
            - {"datetime"}
        )
        assert not fake, f"SQL references non-existent columns: {sorted(fake)}"
