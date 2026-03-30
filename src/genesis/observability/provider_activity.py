"""ProviderActivityTracker — rolling-window call counters per provider.

Tracks call counts, latency, error rates, and cache hit rates for any
provider (embedding, TTS, STT, etc.).  In-memory rolling window with
optional DB persistence for cross-restart survival.

Thread safety note: record() is synchronous with no awaits, so asyncio's
cooperative scheduling makes it effectively atomic within the event loop.
Do NOT call record() from ThreadPoolExecutor or outside the event loop thread.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


class CallRecord(NamedTuple):
    """Single provider call record."""

    timestamp: float  # time.monotonic()
    latency_ms: float
    success: bool
    cache_hit: bool


class ProviderActivityTracker:
    """Rolling-window call counters per provider with optional DB persistence."""

    def __init__(self, window_seconds: int = 3600) -> None:
        self._window = window_seconds
        self._calls: dict[str, deque[CallRecord]] = defaultdict(deque)
        self._db: aiosqlite.Connection | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._write_batch: list[tuple[str, float, bool, bool]] = []
        self._flush_task: asyncio.Task | None = None

    def set_db(self, db: aiosqlite.Connection) -> None:
        """Inject DB connection for persistence.  Called once during bootstrap."""
        self._db = db
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def record(
        self,
        provider: str,
        *,
        latency_ms: float,
        success: bool,
        cache_hit: bool = False,
    ) -> None:
        """Record a single call. Auto-evicts entries older than window."""
        self._calls[provider].append(
            CallRecord(
                timestamp=time.monotonic(),
                latency_ms=latency_ms,
                success=success,
                cache_hit=cache_hit,
            )
        )
        self._evict(provider)

        # Queue for DB persistence (fire-and-forget)
        if self._db is not None:
            self._write_batch.append((provider, latency_ms, success, cache_hit))
            self._schedule_flush()

    def _schedule_flush(self) -> None:
        """Schedule a batched DB flush if not already pending."""
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
                name="activity-log-flush",
                subsystem=Subsystem.OBSERVABILITY,
            )
        except RuntimeError:
            logger.debug("Cannot schedule activity flush — event loop closed")
        except ImportError:
            # tracked_task not available (e.g., minimal test env)
            try:
                self._flush_task = self._loop.create_task(self._flush_to_db())
            except RuntimeError:
                logger.debug("Cannot schedule activity flush — event loop closed")

    async def _flush_to_db(self) -> None:
        """Write batched records to activity_log table."""
        if not self._db or not self._write_batch:
            return
        batch = self._write_batch[:]
        self._write_batch.clear()
        now = datetime.now(UTC).isoformat()
        try:
            await self._db.executemany(
                "INSERT INTO activity_log (provider, latency_ms, success, cache_hit, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(p, lat, int(s), int(c), now) for p, lat, s, c in batch],
            )
            await self._db.commit()
        except Exception:
            logger.debug("activity_log flush failed", exc_info=True)

    async def warm_from_db(self) -> None:
        """Load recent activity from DB into in-memory tracker on bootstrap."""
        if not self._db:
            return
        try:
            cutoff = (datetime.now(UTC) - timedelta(seconds=self._window)).isoformat()
            cursor = await self._db.execute(
                "SELECT provider, latency_ms, success, cache_hit, created_at "
                "FROM activity_log "
                "WHERE created_at >= ? ORDER BY created_at ASC",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            now_mono = time.monotonic()
            now_wall = datetime.now(UTC)
            for row in rows:
                provider, latency_ms, success, cache_hit, created_at_str = row
                # Approximate monotonic timestamp from wall-clock age so older
                # records get evicted sooner (instead of all surviving the full window).
                try:
                    created_at = datetime.fromisoformat(created_at_str)
                    age_s = (now_wall - created_at).total_seconds()
                    mono_ts = now_mono - max(0, age_s)
                except (ValueError, TypeError):
                    mono_ts = now_mono  # fallback: treat as fresh
                self._calls[provider].append(
                    CallRecord(
                        timestamp=mono_ts,
                        latency_ms=latency_ms,
                        success=bool(success),
                        cache_hit=bool(cache_hit),
                    )
                )
            if rows:
                logger.info("Warmed activity tracker with %d records from DB", len(rows))
        except Exception:
            logger.debug("activity_log warm-up failed (table may not exist)", exc_info=True)

    async def reap_old_records(self) -> int:
        """Delete activity_log records older than 24 hours.  Returns count deleted."""
        if not self._db:
            return 0
        try:
            cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
            cursor = await self._db.execute(
                "DELETE FROM activity_log WHERE created_at < ?", (cutoff,),
            )
            await self._db.commit()
            return cursor.rowcount
        except Exception:
            logger.debug("activity_log reap failed", exc_info=True)
            return 0

    def summary(self, provider: str | None = None) -> dict | list[dict]:
        """Return activity summary for one or all providers.

        Single provider returns a dict.  None returns list of dicts for all.
        """
        if provider is not None:
            return self._provider_summary(provider)
        return [self._provider_summary(p) for p in sorted(self._calls)]

    async def summary_with_db_fallback(self, *, db_window_hours: int = 24) -> list[dict]:
        """Return summary, falling back to DB query if in-memory is empty."""
        mem = self.summary()
        if mem:
            return mem
        if not self._db:
            return []
        # In-memory empty — query DB directly using a wider visibility window.
        try:
            cutoff = (datetime.now(UTC) - timedelta(hours=db_window_hours)).isoformat()
            cursor = await self._db.execute(
                "SELECT provider, COUNT(*) as calls, "
                "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors, "
                "AVG(latency_ms) as avg_lat, "
                "SUM(cache_hit) as cache_hits "
                "FROM activity_log WHERE created_at >= ? "
                "GROUP BY provider ORDER BY provider",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                prov, calls, errors, avg_lat, cache_hits = row
                result.append({
                    "provider": prov,
                    "calls": calls,
                    "errors": errors,
                    "error_rate": round(errors / calls, 4) if calls else 0.0,
                    "avg_latency_ms": round(avg_lat or 0, 2),
                    "p95_latency_ms": 0.0,  # can't compute p95 from aggregate
                    "cache_hits": cache_hits or 0,
                    "cache_hit_rate": round((cache_hits or 0) / calls, 4) if calls else 0.0,
                    "window_hours": db_window_hours,
                })
            return result
        except Exception:
            logger.debug("activity_log DB fallback query failed", exc_info=True)
            return []

    def _provider_summary(self, provider: str) -> dict:
        self._evict(provider)
        records = self._calls.get(provider, deque())

        total = len(records)
        if total == 0:
            return {
                "provider": provider,
                "calls": 0,
                "errors": 0,
                "error_rate": 0.0,
                "avg_latency_ms": 0.0,
                "p95_latency_ms": 0.0,
                "cache_hits": 0,
                "cache_hit_rate": 0.0,
            }

        errors = sum(1 for r in records if not r.success)
        cache_hits = sum(1 for r in records if r.cache_hit)

        # Latency stats — exclude cache hits (latency=0) for meaningful numbers
        latencies = sorted(r.latency_ms for r in records if not r.cache_hit)
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            p95_idx = max(0, int(len(latencies) * 0.95) - 1)
            p95_latency = latencies[p95_idx]
        else:
            avg_latency = 0.0
            p95_latency = 0.0

        return {
            "provider": provider,
            "calls": total,
            "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "avg_latency_ms": round(avg_latency, 2),
            "p95_latency_ms": round(p95_latency, 2),
            "cache_hits": cache_hits,
            "cache_hit_rate": round(cache_hits / total, 4) if total else 0.0,
        }

    def _evict(self, provider: str) -> None:
        """Remove entries older than window_seconds."""
        cutoff = time.monotonic() - self._window
        q = self._calls.get(provider)
        if q is None:
            return
        while q and q[0].timestamp < cutoff:
            q.popleft()
        if not q:
            del self._calls[provider]


# ---------------------------------------------------------------------------
# Instrumentation helpers — wrap any operation with timing + error tracking.
# These NEVER propagate tracker errors to the caller.
# ---------------------------------------------------------------------------


@contextmanager
def track_operation(
    tracker: ProviderActivityTracker | None,
    provider: str,
):
    """Sync context manager that times an operation and records it.

    Safe to use even if tracker is None (no-op). If tracker.record() itself
    raises, the error is logged at WARNING and swallowed — the caller's
    operation is never affected by tracker bugs.
    """
    if tracker is None:
        yield
        return
    t0 = time.monotonic()
    success = True
    try:
        yield
    except Exception:
        success = False
        raise
    finally:
        try:
            tracker.record(
                provider,
                latency_ms=(time.monotonic() - t0) * 1000,
                success=success,
            )
        except Exception:
            logger.warning(
                "ProviderActivityTracker.record() failed for %s",
                provider,
                exc_info=True,
            )


# GROUNDWORK(async-instrumentation): async callers (e.g., httpx-based
# providers, async DB operations) will use this when instrumented.
@asynccontextmanager
async def async_track_operation(
    tracker: ProviderActivityTracker | None,
    provider: str,
):
    """Async context manager that times an operation and records it.

    Same safety guarantees as track_operation — tracker errors never
    propagate to the caller.
    """
    if tracker is None:
        yield
        return
    t0 = time.monotonic()
    success = True
    try:
        yield
    except Exception:
        success = False
        raise
    finally:
        try:
            tracker.record(
                provider,
                latency_ms=(time.monotonic() - t0) * 1000,
                success=success,
            )
        except Exception:
            logger.warning(
                "ProviderActivityTracker.record() failed for %s",
                provider,
                exc_info=True,
            )
