"""WS-2 M11 injected-failure E2E — message_queue findings are closable.

Before #956, findings rendered into the morning report were never marked
responded, so query_pending re-listed them every day until the 7-day expiry (it
conflated "seen in a delivered report" with "never seen"). This pins the fix:
after confirm_delivery, a rendered finding is marked responded and drops out of
query_pending — it is not re-listed on day 2.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import message_queue as mq_crud
from genesis.outreach.morning_report import MorningReportGenerator

_CREATE_MESSAGE_QUEUE = """
    CREATE TABLE IF NOT EXISTS message_queue (
        id             TEXT PRIMARY KEY,
        task_id        TEXT,
        source         TEXT NOT NULL,
        target         TEXT NOT NULL,
        message_type   TEXT NOT NULL,
        priority       TEXT NOT NULL DEFAULT 'medium',
        content        TEXT NOT NULL,
        response       TEXT,
        session_id     TEXT,
        created_at     TEXT NOT NULL,
        responded_at   TEXT,
        expired_at     TEXT
    )
"""


async def test_delivered_finding_is_closed_not_relisted():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    try:
        await db.execute(_CREATE_MESSAGE_QUEUE)
        await db.commit()

        # Day 1: a finding lands in the queue and is pending.
        await mq_crud.create(
            db,
            id="mq-1",
            source="autonomy",
            target="owner",
            message_type="finding",
            content="synthetic finding",
            created_at="2026-07-15T06:00:00",
        )
        pending = await mq_crud.query_pending(db)
        assert any(r["id"] == "mq-1" for r in pending), "finding should be pending pre-delivery"

        # The report renders it, then confirm_delivery closes it.
        gen = MorningReportGenerator.__new__(MorningReportGenerator)
        gen._db = db
        gen._pending_surface_ids = []
        gen._pending_mq_ids = ["mq-1"]
        await gen.confirm_delivery()

        # Day 2: it must be marked responded and NOT re-listed.
        row = await mq_crud.get_by_id(db, "mq-1")
        assert row["response"] == "surfaced_in_morning_report"
        assert row["responded_at"] is not None
        pending_after = await mq_crud.query_pending(db)
        assert not any(r["id"] == "mq-1" for r in pending_after), (
            "delivered finding must not re-list"
        )
    finally:
        await db.close()


pytestmark = pytest.mark.asyncio
