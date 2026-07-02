"""Sink adapters for ``AttentionEvent``. ``ShadowStoreConsumer`` persists refs + derived
features to genesis.db ``attention_events`` via the crud layer (``genesis.db.crud.attention``)
— NEVER transcript text (firewall). Events are buffered and written in ONE transaction: the
offline runner is a 2nd writer to the WAL DB while the server writes on its ticks, so the
connection's ``timeout`` + ``busy_timeout`` + a single write window absorb lock contention.
A ``JSONLConsumer`` (the edge sink) slots in beside this later (PR3).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import aiosqlite

from genesis.attention.types import AttentionEvent
from genesis.db.crud import attention as attention_crud

logger = logging.getLogger(__name__)


@runtime_checkable
class ShadowConsumer(Protocol):
    """Structural sink for the ``AttentionEvent``s ``run_shadow`` emits.

    Buffer on ``add`` (sync), commit on ``flush`` (async, returns the count written).
    ``ShadowStoreConsumer`` persists to genesis.db; an in-memory collector (the PR3c-1
    differ) captures the full fire-set; a ``JSONLConsumer`` edge sink slots in later — all
    satisfy this contract, so ``run_shadow`` need not know which concrete sink it holds."""

    def add(self, ev: AttentionEvent) -> None: ...

    async def flush(self) -> int: ...


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


class ShadowStoreConsumer:
    """Buffer AttentionEvents; flush them to ``attention_events`` (via crud) in one txn.

    Row id = ``snapshot_id:config_version:trigger_utt_id`` — idempotent per (snapshot,
    config) so re-running the same snapshot yields identical rows, while a NEW
    config_version writes NEW rows (never clobbers a labeled row's acceptance_signal).
    """

    def __init__(self, db_path, *, snapshot_id: str, config_version: str, timeout_s: float = 30.0):
        self.db_path = str(db_path)
        self.snapshot_id = snapshot_id
        self.config_version = config_version
        self.timeout_s = timeout_s
        self._rows: list[tuple] = []
        self._created_at = datetime.now(UTC).isoformat()

    def add(self, ev: AttentionEvent) -> None:
        self._rows.append(self._to_row(ev))

    def _to_row(self, ev: AttentionEvent) -> tuple:
        # Order MUST match genesis.db.crud.attention.COLUMNS.
        wr = ev.window_ref
        trig_id = wr.utt_ids[-1] if wr.utt_ids else "x"
        row_id = f"{self.snapshot_id}:{self.config_version}:{trig_id}"
        triggers = json.dumps([  # names + kinds + weights only — NO transcript text
            {"name": h.name, "kind": h.kind.value, "contribution": h.contribution}
            for h in ev.triggers_fired
        ])
        window_ref = json.dumps({  # ids + ts range only — NO transcript text
            "snapshot_id": self.snapshot_id,
            "session_id": wr.session_id,
            "utt_ids": list(wr.utt_ids),
            "ts_start": wr.ts_start,
            "ts_end": wr.ts_end,
        })
        return (
            row_id, _iso(ev.ts), ev.session_id, ev.activation.value, ev.score,
            triggers, json.dumps(list(ev.suppressors)), window_ref, ev.mode_state,
            ev.clarity, json.dumps(ev.l15_verdict) if ev.l15_verdict is not None else None,
            None,  # acceptance_signal — back-filled at review (PR2)
            self.snapshot_id, self.config_version, self._created_at,
        )

    async def flush(self) -> int:
        """Write all buffered rows via the crud layer in one transaction; returns count."""
        if not self._rows:
            return 0
        conn = await aiosqlite.connect(self.db_path, timeout=self.timeout_s)
        try:
            await conn.execute("PRAGMA busy_timeout=30000")
            n = await attention_crud.bulk_upsert_events(conn, self._rows)
        finally:
            await conn.close()
        logger.info("persisted %d attention_events (snapshot=%s config=%s)",
                    n, self.snapshot_id, self.config_version)
        self._rows = []
        return n
