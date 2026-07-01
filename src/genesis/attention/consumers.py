"""Sink adapters for ``AttentionEvent``. ``ShadowStoreConsumer`` persists refs + derived
features to genesis.db ``attention_events`` — NEVER transcript text (firewall). Events are
buffered and flushed in ONE short transaction: the offline runner is a 2nd writer to the
WAL DB while the server writes on its ticks, so ``timeout`` + ``busy_timeout`` + a single
write window absorb lock contention. A ``JSONLConsumer`` (the edge sink) slots in beside
this later (PR3).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime

from genesis.attention.types import AttentionEvent

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id", "ts", "session_id", "activation", "score", "triggers_fired", "suppressors",
    "window_ref", "mode_state", "clarity", "l15_verdict", "acceptance_signal",
    "snapshot_id", "config_version", "created_at",
)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


class ShadowStoreConsumer:
    """Buffer AttentionEvents; flush them to ``attention_events`` in one transaction.

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

    def flush(self) -> int:
        """Write all buffered rows in one transaction; returns the count written."""
        if not self._rows:
            return 0
        placeholders = ", ".join(["?"] * len(_COLUMNS))
        sql = (  # columns/placeholders are constants, not user input
            f"INSERT OR REPLACE INTO attention_events ({', '.join(_COLUMNS)}) "  # noqa: S608
            f"VALUES ({placeholders})"
        )
        conn = sqlite3.connect(self.db_path, timeout=self.timeout_s)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.executemany(sql, self._rows)
            conn.commit()
        finally:
            conn.close()
        n = len(self._rows)
        logger.info("persisted %d attention_events (snapshot=%s config=%s)",
                    n, self.snapshot_id, self.config_version)
        self._rows = []
        return n
