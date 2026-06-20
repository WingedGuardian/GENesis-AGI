"""SpanWriter — non-blocking, batched persistence for ``otel_spans``.

Transport cloned from ``ProviderActivityTracker``: ``record()`` is SYNCHRONOUS
(no await) — it appends a row tuple to an in-memory batch and schedules a
fire-and-forget flush via ``tracked_task``. Flushes are batched
(``executemany`` + a single ``commit``) so the hot path never blocks on the DB
and there is at most one short write per flush burst — the WAL-safety guarantee.
All errors are swallowed at DEBUG (never break the caller); ``INSERT OR IGNORE``
means a duplicate ``span_id`` can never raise inside a flush.

Unlike the tracker it's modeled on, this does NOT fall back to a bare
``loop.create_task`` when ``tracked_task`` is unavailable — it logs and skips,
so an untracked task can never leak.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.observability.spans import Span

logger = logging.getLogger(__name__)

# Column order for the INSERT (created_at is filled by its DEFAULT).
_COLS = (
    "span_id", "trace_id", "parent_span_id", "name", "kind", "status",
    "status_message", "start_unix_us", "end_unix_us", "duration_us",
    "session_id", "process", "call_site", "provider", "model_id",
    "input_tokens", "output_tokens", "cost_usd", "cost_known", "attributes_json",
)
_INSERT = (
    f"INSERT OR IGNORE INTO otel_spans ({', '.join(_COLS)}) "
    f"VALUES ({', '.join('?' * len(_COLS))})"
)


class SpanWriter:
    """Batched async writer for trace spans. Wired once at bootstrap."""

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._write_batch: list[tuple] = []
        self._flush_task: asyncio.Task | None = None
        self._process = "server"

    def set_db(self, db: aiosqlite.Connection, *, process: str = "server") -> None:
        """Inject the DB connection + this process's provenance label."""
        self._db = db
        self._process = process
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def _row(self, span: Span) -> tuple:
        return (
            span.span_id,
            span.trace_id,
            span.parent_span_id,
            span.name,
            span.kind,
            span.status,
            span.status_message,
            span.start_unix_us,
            span.end_unix_us,
            span.duration_us,
            span.session_id,
            self._process,
            span.call_site,
            span.provider,
            span.model_id,
            span.input_tokens,
            span.output_tokens,
            span.cost_usd,
            (None if span.cost_known is None else int(span.cost_known)),
            json.dumps(span.attributes) if span.attributes else None,
        )

    def record(self, span: Span) -> None:
        """Queue a finished span for persistence (sync, no await)."""
        if self._db is None:
            return
        try:
            self._write_batch.append(self._row(span))
        except Exception:
            logger.debug("span row build failed", exc_info=True)
            return
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            return
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        try:
            from genesis.observability.types import Subsystem
            from genesis.util.tasks import tracked_task

            self._flush_task = tracked_task(
                self._flush_to_db(),
                name="span-flush",
                subsystem=Subsystem.OBSERVABILITY,
            )
        except RuntimeError:
            logger.debug("Cannot schedule span flush — event loop closed")
        except ImportError:
            # No bare create_task fallback — skip rather than leak an untracked task.
            logger.debug("tracked_task unavailable — span flush skipped")

    async def _flush_to_db(self) -> None:
        """Write the batched span rows in one executemany + commit."""
        if not self._db or not self._write_batch:
            return
        batch = self._write_batch[:]
        self._write_batch.clear()
        try:
            await self._db.executemany(_INSERT, batch)
            await self._db.commit()
        except Exception:
            logger.debug("otel_spans flush failed", exc_info=True)

    async def prune(self, *, older_than_days: int) -> int:
        """Delete spans older than the retention horizon. Returns rows removed."""
        if not self._db:
            return 0
        cutoff_us = int((time.time() - older_than_days * 86400) * 1_000_000)
        try:
            cur = await self._db.execute(
                "DELETE FROM otel_spans WHERE start_unix_us < ?", (cutoff_us,)
            )
            await self._db.commit()
            return cur.rowcount
        except Exception:
            logger.debug("otel_spans prune failed", exc_info=True)
            return 0
