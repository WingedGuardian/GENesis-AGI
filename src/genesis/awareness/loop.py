"""AwarenessLoop — the system's heartbeat.

Orchestrates the tick pipeline: collect signals → score → classify → store.
APScheduler drives the 5-minute interval. perform_tick() is the testable core.

When running inside Agent Zero (later phases), the scheduler will be started
via DeferredTask, matching AZ's job_loop.py pattern. Phase 1 tests run standalone.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from genesis.awareness.classifier import classify_depth
from genesis.awareness.scorer import compute_scores, get_staleness_context
from genesis.awareness.signals import SignalCollector, collect_all
from genesis.awareness.types import Depth, TickResult
from genesis.cc.constants import RATE_LIMIT_DEFERRAL_TTL_S
from genesis.db.crud import awareness_ticks, observations
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.resilience.state import CloudStatus
from genesis.routing.types import DegradationLevel

if TYPE_CHECKING:
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry

logger = logging.getLogger(__name__)


async def _sqlite_wal_checkpoint(db) -> None:
    """Run a non-blocking PASSIVE WAL checkpoint via the async API.

    MUST go through ``db.execute`` (the aiosqlite worker thread). Reaching into
    ``db._conn._conn`` and calling it synchronously from the event-loop thread
    raises ``ProgrammingError`` (sqlite3 connections are thread-bound) — which a
    bare ``except`` silently swallows, making the checkpoint a no-op (this was a
    latent bug). ``execute_fetchall`` consumes+closes the cursor (cancellation-
    safe). SQLite-specific; best-effort."""
    with contextlib.suppress(Exception):  # best-effort; failure is harmless
        await db.execute_fetchall("PRAGMA wal_checkpoint(PASSIVE)")


async def _sqlite_wal_truncate(db) -> None:
    """Run a TRUNCATE WAL checkpoint via the async API to reclaim WAL *file* space.

    PASSIVE checkpoints recycle WAL frames in place but never shrink the file;
    TRUNCATE resets it to zero bytes once all readers have caught up. No-op
    (busy) if a reader still holds a snapshot — that case is surfaced by
    :func:`_check_wal_health`. Run on a slow cadence (not every tick) so it
    doesn't needlessly contend with active readers. Goes through ``db.execute``
    (worker thread); ``execute_fetchall`` is cancellation-safe. SQLite-specific;
    best-effort."""
    try:
        rows = await db.execute_fetchall("PRAGMA wal_checkpoint(TRUNCATE)")
        if rows and rows[0][0] == 1:
            logger.debug("WAL TRUNCATE checkpoint blocked by an active reader")
    except Exception:
        pass  # Best-effort; failure is harmless


# WAL-health detection: a pinned checkpoint (e.g. a long-lived connection holding
# a read snapshot from an unclosed/cancelled cursor) makes the WAL file grow
# unbounded. Alert on abnormal WAL size so a stuck reader is caught in minutes,
# not days. Surfaces via the critical-observations job (Telegram) + morning report.
_WAL_SIZE_WARN_BYTES = 100 * 1024 * 1024   # 100 MB → "high" (morning report)
_WAL_SIZE_CRIT_BYTES = 500 * 1024 * 1024   # 500 MB → "critical" (Telegram now)
_WAL_ALERT_COOLDOWN_S = 3600               # one alert per hour max
_WAL_TRUNCATE_EVERY_N_TICKS = 12           # hourly TRUNCATE (tick ≈ 5 min)
# None = "never alerted". Must NOT be 0.0: time.monotonic() is since boot, so on a
# freshly-booted host `now - 0.0` is small and would wrongly suppress the first alert.
_last_wal_alert_at: float | None = None


async def _check_wal_health(db) -> None:
    """Create a high/critical observation when the SQLite WAL file is abnormally
    large — the direct symptom of a pinned checkpoint or chronic under-checkpointing.
    Best-effort; never raises into the tick."""
    global _last_wal_alert_at
    try:
        from pathlib import Path

        from genesis.env import genesis_db_path
        wal_path = Path(f"{genesis_db_path()}-wal")
        if not wal_path.exists():
            return
        size = wal_path.stat().st_size
    except Exception:
        return  # can't stat — nothing to alert on

    if size < _WAL_SIZE_WARN_BYTES or db is None:
        return
    now = time.monotonic()
    if _last_wal_alert_at is not None and now - _last_wal_alert_at < _WAL_ALERT_COOLDOWN_S:
        return

    mb = size / (1024 * 1024)
    priority = "critical" if size >= _WAL_SIZE_CRIT_BYTES else "high"
    # Set the cooldown BEFORE the write so a failed create (e.g. DB locked — the
    # very scenario this alerts on) still suppresses per-tick retries for an hour.
    _last_wal_alert_at = now
    try:
        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="wal_health_monitor",
            type="infrastructure_alert",
            content=(
                f"SQLite WAL file is {mb:.0f} MB (warn at "
                f"{_WAL_SIZE_WARN_BYTES // 1024 // 1024} MB). Likely cause: a long-lived "
                f"connection holding a read snapshot (unclosed/cancelled read cursor) "
                f"pinning the WAL checkpoint, or chronic under-checkpointing. Find the "
                f"stuck reader/MCP; a wal_checkpoint(TRUNCATE) reclaims the space once "
                f"it is gone."
            ),
            priority=priority,
            created_at=datetime.now(UTC).isoformat(),
        )
        logger.warning("WAL health alert: %.0f MB (%s)", mb, priority)
    except Exception:
        logger.debug("Failed to create WAL health alert observation", exc_info=True)


# Embedding-backlog degradation: memories stuck at embedding_status='failed' —
# the embedding recovery worker gave up, so they are permanently keyword-only
# (no vector/semantic search) and invisible to every rate/per-run embedding
# alert (the outage that created them is over). Baseline is 0 (verified live).
# HYBRID surfacing: a real-but-modest pile records a NON-paging 'high'
# observation (dashboard / morning report only); only a large pile — a serious
# permanent-loss backlog (HIGH ~= 1.8% of a ~55k store) — escalates to
# 'critical', which the critical-observations job pages to Telegram. The
# always-on count also feeds the neural-monitor via memory_health(). The metric
# fluctuates and partially self-heals, so band + cooldown + auto-resolve is the
# right shape (mirrors the dead-letter accumulation alert). Thresholds are
# tunable module constants. NOTE: 'pending' (self-healing) is context in the
# alert text only; a sustained-pending stuck-worker signal is a separate
# recovery-worker health concern, tracked as a follow-up, not alerted here.
_EMBED_BACKLOG_LOW = 50            # below this: quiet (+ resolve any prior alert)
_EMBED_BACKLOG_HIGH = 1000         # at/above this: 'critical' (pages); else 'high'
_EMBED_BACKLOG_COOLDOWN_S = 3600   # one alert per band per hour max
# Safe as 0.0/"" (unlike _check_wal_health): the band guard below means a fresh
# boot never matches the empty band, so the first real backlog always alerts.
_last_embed_backlog_alert_at: float = 0.0
_last_embed_backlog_band: str = ""


def _embed_backlog_band(failed: int) -> str:
    """Bucket the failed-embedding count into a stable band (only meaningful at
    or above ``_EMBED_BACKLOG_LOW``). The alert's content_hash keys on the band,
    not the raw count, so per-tick count drift does not defeat dedup; the exact
    count still appears in the content. A band change is escalation-worthy and
    bypasses the cooldown."""
    if failed < 200:
        return "50-199"
    if failed < _EMBED_BACKLOG_HIGH:
        return "200-999"
    if failed < 5000:
        return "1000-4999"
    return "5000+"


async def _check_embedding_backlog(db) -> None:
    """Alert when embedding_status='failed' memories accumulate.

    Hybrid: a modest pile records a non-paging 'high' observation (dashboard
    only); a large pile (>= HIGH) records a 'critical' one that pages Telegram
    via the critical-observations job. Best-effort — the whole body is guarded
    and never raises into the tick."""
    global _last_embed_backlog_alert_at, _last_embed_backlog_band
    if db is None:
        return
    try:
        from genesis.db.crud.memory import embedding_status_counts

        counts = await embedding_status_counts(db)
        failed = counts.get("failed", 0)

        if failed < _EMBED_BACKLOG_LOW:
            # Under threshold — clear any standing alert and stop.
            await _resolve_embedding_backlog(db)
            return

        band = _embed_backlog_band(failed)
        now = time.monotonic()
        # Same-band re-alerts respect the cooldown; a band change (worsening or
        # improving transition) is escalation-worthy and bypasses it.
        if (
            now - _last_embed_backlog_alert_at < _EMBED_BACKLOG_COOLDOWN_S
            and band == _last_embed_backlog_band
        ):
            return

        priority = "critical" if failed >= _EMBED_BACKLOG_HIGH else "high"
        pending = counts.get("pending", 0)
        content_hash = hashlib.sha256(
            f"embedding_backlog:{band}".encode()
        ).hexdigest()
        # Keep exactly ONE active alert = the current band. Resolve any
        # stale other-band rows so a worsening (high->critical) OR a partial
        # recovery (critical->high) transition leaves only the current-band
        # row active, instead of a lingering peak-severity row until the
        # backlog fully clears (< LOW). DB-based (not the in-memory band), so
        # it is restart-safe; a no-op in steady state at a fixed band.
        await db.execute(
            "UPDATE observations SET resolved=1, resolved_at=?, "
            "resolution_notes='superseded by a new embedding-backlog band' "
            "WHERE source='embedding_backlog_monitor' "
            "AND type='infrastructure_alert' AND resolved=0 "
            "AND content_hash != ?",
            (datetime.now(UTC).isoformat(), content_hash),
        )
        await db.commit()
        created = await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="embedding_backlog_monitor",
            type="infrastructure_alert",
            content=(
                f"{failed} memories are stuck at embedding_status='failed' — the "
                f"embedding recovery worker gave up on them, so they are "
                f"permanently keyword-only (no vector/semantic search) and "
                f"invisible to the rate-based embedding-failure alert (the outage "
                f"that created them is over). {pending} more are 'pending' and "
                f"still self-healing. Recovery: these failed rows have no "
                f"live pending_embeddings queue entry (it was reaped), so a "
                f"plain failed->pending reset will NOT retry them (nothing "
                f"auto-recovers a reaped failure) — re-enqueue the affected "
                f"memories for embedding (a fresh pending_embeddings row "
                f"each) after checking embedding-provider health."
            ),
            priority=priority,
            created_at=datetime.now(UTC).isoformat(),
            content_hash=content_hash,
            skip_if_duplicate=True,
        )
        if created is None:
            return  # An unresolved alert for this band already exists.
        _last_embed_backlog_alert_at = now
        _last_embed_backlog_band = band
        logger.warning(
            "Embedding backlog alert: %d failed memories (%s observation created)",
            failed,
            priority,
        )
    except Exception:
        logger.debug("Failed embedding backlog check", exc_info=True)


async def _resolve_embedding_backlog(db) -> None:
    """Resolve outstanding embedding-backlog alerts once the failed count drops
    back under ``_EMBED_BACKLOG_LOW``.

    Unconditional (no in-memory "is an alert active?" guard) so it survives a
    restart; the UPDATE is a cheap no-op when nothing matches. Resolving on a
    non-zero count clears the cooldown globals so a genuine recovery -> re-spike
    re-alerts cleanly."""
    global _last_embed_backlog_alert_at, _last_embed_backlog_band
    if db is None:
        return
    try:
        resolved = await observations.resolve_by_source_and_type(
            db,
            source="embedding_backlog_monitor",
            type="infrastructure_alert",
            resolved_at=datetime.now(UTC).isoformat(),
            resolution_notes=(
                f"auto-resolved: failed-embedding backlog back under "
                f"{_EMBED_BACKLOG_LOW}"
            ),
        )
        if resolved:
            _last_embed_backlog_alert_at = 0.0
            _last_embed_backlog_band = ""
            logger.info(
                "Auto-resolved %d embedding-backlog alert observation(s) on recovery",
                resolved,
            )
    except Exception:
        logger.debug("Failed to resolve embedding backlog alerts", exc_info=True)


# nodatacow (chattr +C) drift detection: on btrfs, a CoW SQLite DB suffers WAL
# write-amplification + chronic fragmentation. The install sets +C on data/;
# this catches regressions (a restore/recreate that dropped the flag). Static
# condition → probe on the slow WAL cadence, alert at most once per day.
_NOCOW_ALERT_COOLDOWN_S = 24 * 3600
# None = "never alerted" (same monotonic-since-boot caveat as the WAL alert).
_last_nocow_alert_at: float | None = None
_FS_IOC_GETFLAGS = 0x80086601
_FS_NOCOW_FL = 0x00800000


def _fs_type_for(path) -> str | None:
    """Filesystem type of the mount containing ``path`` (longest-prefix match
    over /proc/mounts). None if it can't be determined."""
    try:
        target = str(path)
        best, fstype = "", None
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt, typ = parts[1], parts[2]
                if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                    best, fstype = mnt, typ
        return fstype
    except OSError:
        return None


async def _check_db_nodatacow(db) -> None:
    """Create a 'high' observation (morning-report tier) when the SQLite DB
    sits on btrfs WITHOUT the nodatacow attribute. Non-btrfs filesystems are
    exempt (the flag is meaningless there). Best-effort; never raises into the
    tick, and never alerts on a probe failure."""
    global _last_nocow_alert_at
    try:
        import fcntl
        import struct

        from genesis.env import genesis_db_path
        db_path = genesis_db_path()
        if not db_path.exists() or _fs_type_for(db_path) != "btrfs":
            return
        with open(db_path, "rb") as fh:
            raw = fcntl.ioctl(fh.fileno(), _FS_IOC_GETFLAGS, struct.pack("l", 0))
        if struct.unpack("l", raw)[0] & _FS_NOCOW_FL:
            return  # +C set — healthy
    except Exception:
        return  # can't determine — nothing to alert on

    if db is None:
        return
    now = time.monotonic()
    if _last_nocow_alert_at is not None and now - _last_nocow_alert_at < _NOCOW_ALERT_COOLDOWN_S:
        return
    _last_nocow_alert_at = now
    try:
        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="nodatacow_monitor",
            type="infrastructure_alert",
            content=(
                "genesis.db is on btrfs WITHOUT nodatacow (+C): CoW + SQLite WAL "
                "means write-amplification and chronic fragmentation. Restore the "
                "attribute: stop the server, `chattr +C` the data/ directory, "
                "recreate the DB files inside it (cp, not mv — the flag only "
                "applies to freshly-created files), verify with lsattr, restart."
            ),
            priority="high",
            created_at=datetime.now(UTC).isoformat(),
        )
        logger.warning("nodatacow drift alert: genesis.db is CoW on btrfs")
    except Exception:
        logger.debug("Failed to create nodatacow alert observation", exc_info=True)


# Git-repository health (F.1): the thin-pool outage zeroed .git/config,
# packed-refs, and loose objects with ZERO detection, silently disabling the
# guardian's REVERT_CODE recovery lever. A cheap per-tick structural probe (plus
# a rootfs-RO write-probe) catches that class within one tick and writes a
# verdict to the shared mount so the host guardian can enrich its own alert.
# Same monotonic-since-boot caveat as the WAL alert: None = "never alerted".
_GIT_ALERT_COOLDOWN_S = 6 * 3600  # one critical observation per 6h max
_last_git_alert_at: float | None = None


async def _check_git_health(db) -> None:
    """Probe local git integrity + rootfs writability; write a shared-mount
    verdict and, on failure, create a critical observation pointing at the
    local-git repair procedure (recovery runbook).

    Deliberately NOT gated on ``db_available`` — git health matters MOST when the
    DB is broken, and the observation write is guarded on ``db`` internally.
    Best-effort; never raises into the tick."""
    global _last_git_alert_at
    try:
        from genesis.observability import git_health

        report = await git_health.check_git_cheap()
        # Always publish the verdict (best-effort) so the guardian can read it.
        git_health.write_git_health_verdict(report)
    except Exception:
        logger.debug("git health probe failed", exc_info=True)
        return

    if report.ok or db is None:
        return
    now = time.monotonic()
    if _last_git_alert_at is not None and now - _last_git_alert_at < _GIT_ALERT_COOLDOWN_S:
        return
    # Set the cooldown BEFORE the write so a failed create still suppresses
    # per-tick retries (the DB may be on the same wedged fs this is detecting).
    _last_git_alert_at = now
    failures = ", ".join(report.failures)
    try:
        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="git_health_monitor",
            type="infrastructure_alert",
            content=(
                f"Local git repository is UNHEALTHY ({failures}). This disables the "
                f"guardian's REVERT_CODE recovery lever, which needs a healthy local "
                f"git. Diagnose and repair the local git repository in ~/genesis — see "
                f"the recovery runbook (docs/reference/recovery-and-portability-workflow.md). "
                f"If 'rootfs_readonly', the container filesystem "
                f"has gone read-only (thin-pool exhaustion) — check host storage first."
            ),
            priority="critical",
            created_at=datetime.now(UTC).isoformat(),
        )
        logger.error("git health alert: %s", failures)
    except Exception:
        logger.debug("Failed to create git health alert observation", exc_info=True)


# Daily deep git-integrity scan (F.1). `git fsck --full` catches deep-only
# corruption — a zeroed-but-present reachable blob — that the cheap per-tick
# probe cannot see. Driven from the awareness loop (NOT the learning scheduler)
# so it still runs in a router-degraded startup, the exact window a
# belt-and-suspenders integrity check matters. A monotonic >=24h guard gives a
# daily cadence that also fires once on the first tick after any restart (no
# interval-reset starvation). The fsck runs in a thread (check_git_deep ->
# to_thread) so it never blocks the tick. None = "never run this boot".
_GIT_DEEP_INTERVAL_S = 24 * 3600
_last_git_deep_run_at: float | None = None


async def _check_git_health_deep(db) -> None:
    """Daily `git fsck --full` content-verifying scan: writes the deep verdict
    slot and, on failure, a critical observation. Best-effort; never raises.

    Runs at most once per ``_GIT_DEEP_INTERVAL_S`` (and once on the first tick
    after a restart). NOT gated on ``db``: git integrity matters most when the DB
    is broken; the observation write is guarded on ``db`` internally."""
    global _last_git_deep_run_at
    now = time.monotonic()
    if _last_git_deep_run_at is not None and now - _last_git_deep_run_at < _GIT_DEEP_INTERVAL_S:
        return
    # Claim the daily slot BEFORE running so an error can't retry every tick.
    _last_git_deep_run_at = now
    try:
        from genesis.observability import git_health

        report = await git_health.check_git_deep()
        git_health.write_git_health_verdict(report)
    except Exception:
        logger.debug("git deep-health scan failed", exc_info=True)
        return

    if report.ok or db is None:
        return
    failures = ", ".join(report.failures)
    try:
        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="git_health_monitor",
            type="infrastructure_alert",
            content=(
                f"`git fsck --full` reported problems ({failures}) — objects are "
                "missing or corrupt (incl. zeroed-but-present blobs), which disables "
                "the guardian's REVERT_CODE lever. Diagnose and repair the local git "
                "in ~/genesis — see docs/reference/recovery-and-portability-workflow.md."
            ),
            priority="critical",
            created_at=datetime.now(UTC).isoformat(),
        )
        logger.error("git deep-health alert: %s", failures)
    except Exception:
        logger.debug("Failed to create git deep-health observation", exc_info=True)


# Per-CC-slot RSS alerting. Same monotonic-since-boot caveat as WAL above: use a
# key-existence check (a missing key means "never alerted"), NEVER a default of
# 0.0 — on a host booted <cooldown ago, `now - 0.0` is small and would wrongly
# suppress the first alert for a slot.
_last_slot_alert_at: dict[str, float] = {}
_SLOT_ALERT_COOLDOWN_S = 3600  # one alert per slot per hour


async def _check_cc_slot_memory(db, slots: list[dict] | None = None) -> None:
    """Alert when a single CC slot's RSS is abnormally high (a session leak).

    Reads /proc (no DB dependency — runs even during a DB hiccup); only the
    observation write needs `db`. WARN → priority 'high' (morning report); CRIT
    → 'critical' (rides the critical-observations job to Telegram). Best-effort;
    never raises into the tick. `slots` may be passed pre-collected (tests /
    future sharing); otherwise it enumerates."""
    try:
        from genesis.observability.cc_slots import (
            SLOT_RSS_CRIT_MB,
            SLOT_RSS_WARN_MB,
            enumerate_cc_slots,
        )
        if slots is None:
            slots = enumerate_cc_slots()
    except Exception:
        logger.debug("cc_slot memory check: enumeration failed", exc_info=True)
        return

    now = time.monotonic()
    for slot in slots:
        rss = slot.get("rss_mb", 0.0)
        if rss < SLOT_RSS_WARN_MB:
            continue
        key = str(slot.get("slot", "?"))
        last = _last_slot_alert_at.get(key)
        if last is not None and (now - last) < _SLOT_ALERT_COOLDOWN_S:
            continue
        if db is None:
            continue  # can't write the observation now; retry next tick
        priority = "critical" if rss >= SLOT_RSS_CRIT_MB else "high"
        # Consumed only once we can actually write (after the db-None guard), and
        # set before the await so a failed create still suppresses per-tick retries.
        _last_slot_alert_at[key] = now
        try:
            await observations.create(
                db,
                id=str(uuid.uuid4()),
                source="cc_slot_monitor",
                type="infrastructure_alert",
                content=(
                    f"CC slot cc-{key} (pid {slot.get('pid')}) is using "
                    f"{rss / 1024:.1f} GB RAM (warn {SLOT_RSS_WARN_MB // 1024} GB, "
                    f"crit {SLOT_RSS_CRIT_MB // 1024} GB). A single Claude Code "
                    f"session may be leaking — consider restarting slot cc-{key}."
                ),
                priority=priority,
                created_at=datetime.now(UTC).isoformat(),
            )
            logger.warning("CC slot memory alert: cc-%s %.1f GB (%s)", key, rss / 1024, priority)
        except Exception:
            logger.debug("Failed to create cc_slot alert observation", exc_info=True)


# CC silent-cap detection. A capped Anthropic subscription makes `claude -p`
# return empty output (no text, no error, no rate-limit signal) on OUTPUT-EXPECTING
# cognitive invocations — it reads as a successful completion, so nothing alerts
# (this happened for ~2 days in late June). The invoker records one
# `cc_cap_empty_event` observation per such empty (opt-in via
# CCInvocation.expect_output; see runtime/init/cc_relay._on_cc_empty_output). This
# check aggregates a run of them into a single critical alert. Same monotonic-
# since-boot caveat as WAL/slot: None = "never alerted", never 0.0.
_CAP_EMPTY_WINDOW_MIN = 60          # look back this many minutes for empties
_CAP_EMPTY_THRESHOLD = 3           # ≥ this many empties in the window → alert
_CAP_ALERT_COOLDOWN_S = 3600       # one alert per hour max
_last_cap_alert_at: float | None = None


async def _check_cc_cap_detection(db) -> None:
    """Alert when output-expecting cognitive CC invocations return empty in a run.

    Counts recent `cc_cap_empty_event` observations (written by the invoker's
    empty-output callback) and raises ONE critical infrastructure_alert when
    ``>=_CAP_EMPTY_THRESHOLD`` land inside ``_CAP_EMPTY_WINDOW_MIN`` — the silent-
    cap signature. Rides the critical-observations job to Telegram. Detection only;
    the invoker never altered control flow to produce these. Best-effort; never
    raises into the tick. The cooldown + the window rolling forward mean a
    persisting cap re-alerts at most hourly (correct), and a transient blip that
    ages out of the window stops re-firing."""
    global _last_cap_alert_at
    if db is None:
        return
    # ONE best-effort guard around the whole thing: this runs on every tick, so any
    # failure (a DB hiccup, an unexpected row shape) must skip the check, never break
    # the tick. Query first (always) so we can BOTH alert on a run AND resolve on
    # recovery.
    try:
        cutoff = (datetime.now(UTC) - timedelta(minutes=_CAP_EMPTY_WINDOW_MIN)).isoformat()
        count = await observations.count_recent_unresolved_by_type_and_source(
            db, type="cc_cap_empty_event", source="cc_cap_monitor", since=cutoff,
        )

        if count < _CAP_EMPTY_THRESHOLD:
            # Recovery: clear any outstanding cap alert so it doesn't linger for its
            # 3-day TTL after the cap lifts, and reset the cooldown on a genuine
            # resolve so a fresh cap re-alerts immediately (mirrors the DLQ path).
            resolved = await observations.resolve_by_source_and_type(
                db,
                source="cc_cap_monitor",
                type="infrastructure_alert",
                resolved_at=datetime.now(UTC).isoformat(),
                resolution_notes=(
                    f"auto-resolved: {count} empty cognitive completions in the last "
                    f"{_CAP_EMPTY_WINDOW_MIN} min (< {_CAP_EMPTY_THRESHOLD})"
                ),
            )
            if resolved:
                _last_cap_alert_at = None
            return

        now = time.monotonic()
        if _last_cap_alert_at is not None and now - _last_cap_alert_at < _CAP_ALERT_COOLDOWN_S:
            return
        # Set the cooldown BEFORE the write so a failed create still suppresses retries.
        _last_cap_alert_at = now
        # DB-backed dedup: a stable content_hash + skip_if_duplicate means a cap
        # persisting for hours produces ONE unresolved alert, not one per hour (the
        # same discipline as the DLQ accumulation alert). It clears via the resolve
        # path above and re-alerts on a fresh cap.
        content_hash = hashlib.sha256(b"cc_cap_alert").hexdigest()
        created = await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="cc_cap_monitor",
            type="infrastructure_alert",
            content=(
                f"CC subscription likely capped: {count} output-expecting cognitive "
                f"sessions returned EMPTY in the last {_CAP_EMPTY_WINDOW_MIN} min "
                f"(no text, no error, no rate-limit signal — the silent-cap signature). "
                f"CC cognitive work (ego, reflections, weekly jobs) is degraded until "
                f"the Anthropic usage limit resets."
            ),
            priority="critical",
            created_at=datetime.now(UTC).isoformat(),
            content_hash=content_hash,
            skip_if_duplicate=True,
        )
        if created is None:
            return  # an unresolved cap alert already exists — don't duplicate
        logger.warning(
            "CC cap detection alert: %d empty cognitive completions in %d min",
            count, _CAP_EMPTY_WINDOW_MIN,
        )
    except Exception:
        logger.debug("cc_cap detection failed — skipping this tick", exc_info=True)


# Micro ticks are silent by default (counted for cascade, no LLM call).
# LLM fires only when these critical operational signals are active.
_MICRO_CRITICAL_SIGNALS = frozenset({"software_error_spike", "critical_failure"})
_SENTINEL_ANOMALY_THRESHOLD = 0.7

# Maps circuit-breaker degradation levels to resilience cloud axis states.
_DEGRADATION_TO_CLOUD: dict[DegradationLevel, CloudStatus] = {
    DegradationLevel.NORMAL: CloudStatus.NORMAL,
    DegradationLevel.FALLBACK: CloudStatus.FALLBACK,
    DegradationLevel.REDUCED: CloudStatus.REDUCED,
    DegradationLevel.ESSENTIAL: CloudStatus.ESSENTIAL,
    DegradationLevel.MEMORY_IMPAIRED: CloudStatus.REDUCED,
    DegradationLevel.LOCAL_COMPUTE_DOWN: CloudStatus.OFFLINE,
}


async def perform_tick(
    db: aiosqlite.Connection,
    collectors: list,
    *,
    source: str = "scheduled",
    reason: str | None = None,
    reflection_engine=None,
    cc_reflection_bridge=None,
    deferred_queue=None,
    dispatch_reflection: bool = True,
) -> TickResult:
    """Execute one awareness tick. Testable without the scheduler."""
    now = datetime.now(UTC).isoformat()
    tick_id = str(uuid.uuid4())

    # 1. Collect signals (DB-independent — always succeeds)
    signals = await collect_all(collectors)

    # 2-5. DB-dependent operations — wrapped for fault tolerance.
    # If the DB is locked/unavailable, the tick still "succeeds" as degraded:
    # signals are collected, _last_tick_at updates, but scoring/classification
    # are skipped and the resilience memory axis is set to DOWN.
    scores: list = []
    decision = None
    classified_depth = None
    trigger_reason = reason
    escalation_source: str | None = None
    escalation_pending_id: str | None = None
    db_available = True

    try:
        # 2. Score urgency per depth
        scores = await compute_scores(db, signals, now=now)

        # 3. Classify depth
        bypass = source == "critical_bypass"
        decision = await classify_depth(db, scores, bypass_ceiling=bypass)

        classified_depth = decision.depth if decision else None
        trigger_reason = decision.reason if decision else reason

        # 3b. Check for pending light->deep escalation
        if cc_reflection_bridge is not None:
            try:
                # Fix 3A: expire stale escalations (>8h) before checking
                _STALE_ESCALATION_HOURS = 8
                all_pending = await observations.query(
                    db, type="light_escalation_pending", resolved=False, limit=10,
                )
                for stale in all_pending:
                    stale_created = stale.get("created_at", "")
                    try:
                        stale_age = (
                            datetime.now(UTC) - datetime.fromisoformat(stale_created)
                        ).total_seconds() / 3600
                    except (ValueError, TypeError):
                        stale_age = 999
                    if stale_age >= _STALE_ESCALATION_HOURS:
                        await observations.resolve(
                            db, stale["id"],
                            resolved_at=now,
                            resolution_notes=f"Expired (age {stale_age:.1f}h > {_STALE_ESCALATION_HOURS}h TTL)",
                        )
                        logger.info("Auto-resolved stale escalation %s (%.1fh old)", stale["id"], stale_age)

                # Re-query after cleanup
                pending_escalations = await observations.query(
                    db, type="light_escalation_pending", resolved=False, limit=1,
                )
                if pending_escalations:
                    esc_created = pending_escalations[0].get("created_at", "")
                    try:
                        esc_age_hours = (
                            datetime.now(UTC) - datetime.fromisoformat(esc_created)
                        ).total_seconds() / 3600
                    except (ValueError, TypeError):
                        esc_age_hours = 999  # treat unparseable as expired

                    if esc_age_hours < _STALE_ESCALATION_HOURS:
                        # Fix 2A: daily escalation budget (max 2 per 24h)
                        _ESCALATION_BUDGET_PER_DAY = 2
                        resolved_recent = await observations.query(
                            db, type="light_escalation_resolved", limit=20,
                        )
                        resolved_24h_count = 0
                        resolved_2h_count = 0
                        for r in resolved_recent:
                            r_created = r.get("created_at", "")
                            try:
                                r_age = (
                                    datetime.now(UTC) - datetime.fromisoformat(r_created)
                                ).total_seconds() / 3600
                                if r_age < 2:
                                    resolved_2h_count += 1
                                if r_age < 24:
                                    resolved_24h_count += 1
                            except (ValueError, TypeError):
                                pass

                        # Check emergency bypass -- critical signals override budget
                        esc_content = pending_escalations[0].get("content", "").lower()
                        is_emergency = any(kw in esc_content for kw in (
                            "critical_failure", "data_loss", "security_breach",
                            "all providers", "container memory critical",
                        ))

                        if resolved_2h_count >= 1 and not is_emergency:
                            logger.info("Light escalation cooldown active (2h), skipping")
                        elif resolved_24h_count >= _ESCALATION_BUDGET_PER_DAY and not is_emergency:
                            logger.info(
                                "Escalation budget exhausted (%d/%d in 24h), skipping",
                                resolved_24h_count, _ESCALATION_BUDGET_PER_DAY,
                            )
                        else:
                            if is_emergency:
                                logger.warning("Emergency escalation bypassing budget: %s", esc_content[:100])
                            classified_depth = Depth.DEEP
                            escalation_source = "light_escalation"
                            trigger_reason = f"light escalation: {pending_escalations[0].get('content', 'unknown')}"
                            logger.info("Forcing DEEP reflection due to light escalation")

                            # Fix 3B: defer resolution until after successful dispatch
                            escalation_pending_id = pending_escalations[0]["id"]
            except Exception:
                logger.warning("Failed to check light escalation state", exc_info=True)

        # 4. Store tick result
        await awareness_ticks.create(
            db,
            id=tick_id,
            source=source,
            signals_json=json.dumps([
                {"name": s.name, "value": s.value, "source": s.source,
                 "collected_at": s.collected_at}
                for s in signals
            ]),
            scores_json=json.dumps([
                {"depth": s.depth.value, "raw_score": s.raw_score,
                 "time_multiplier": s.time_multiplier, "final_score": s.final_score,
                 "threshold": s.threshold, "triggered": s.triggered}
                for s in scores
            ]),
            classified_depth=classified_depth.value if classified_depth else None,
            trigger_reason=trigger_reason,
            created_at=now,
        )

        # 5. If triggered, also create an observation (with content-hash dedup)
        if decision is not None:
            obs_content = json.dumps({
                "tick_id": tick_id,
                "depth": classified_depth.value,
                "reason": trigger_reason,
                "scores": {s.depth.value: s.final_score for s in scores},
            }, sort_keys=True)
            content_hash = hashlib.sha256(obs_content.encode()).hexdigest()
            is_dup = await observations.exists_by_hash(
                db, source="awareness_loop", content_hash=content_hash, unresolved_only=True,
            )
            if not is_dup:
                obs_id = str(uuid.uuid4())
                await observations.create(
                    db,
                    id=obs_id,
                    source="awareness_loop",
                    type="awareness_tick",
                    content=obs_content,
                    priority="high" if classified_depth in (Depth.DEEP, Depth.STRATEGIC) else "medium",
                    created_at=now,
                    content_hash=content_hash,
                    skip_if_duplicate=True,
                )

    except Exception as db_exc:
        db_available = False
        logger.warning(
            "Tick DB operations failed — degraded tick (signals collected, "
            "scoring/persistence skipped): %s", db_exc,
        )

    result = TickResult(
        tick_id=tick_id,
        timestamp=now,
        source=source,
        signals=signals,
        scores=scores,
        classified_depth=classified_depth,
        trigger_reason=trigger_reason,
        escalation_source=escalation_source if db_available else None,
        escalation_pending_id=escalation_pending_id if db_available else None,
        signal_staleness=get_staleness_context(),
        db_available=db_available,
    )

    if not dispatch_reflection:
        return result

    if reflection_engine is not None and classified_depth == Depth.MICRO:
        ref_result = None
        try:
            ref_result = await reflection_engine.reflect(classified_depth, result, db=db)
        except Exception:
            logger.exception("Reflection crashed for tick %s", tick_id)

        if (ref_result is None or not ref_result.success) and deferred_queue:
            try:
                await deferred_queue.enqueue(
                    work_type="reflection",
                    call_site_id="reflection_micro",
                    priority=30,
                    payload=json.dumps({"tick_id": tick_id, "depth": "Micro"}),
                    reason="reflection_failed",
                    staleness_policy="ttl",
                    staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                )
            except Exception:
                logger.warning("Failed to enqueue deferred reflection")

    if classified_depth == Depth.LIGHT and cc_reflection_bridge is None and reflection_engine is not None:
        try:
            await reflection_engine.reflect(classified_depth, result, db=db)
        except Exception:
            logger.exception("Light reflection fallback (API) failed for tick %s", tick_id)
            if deferred_queue:
                try:
                    await deferred_queue.enqueue(
                        work_type="reflection",
                        call_site_id="reflection_light",
                        priority=30,
                        payload=json.dumps({"tick_id": tick_id, "depth": "Light"}),
                        reason="reflection_failed",
                        staleness_policy="ttl",
                        staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                    )
                except Exception:
                    logger.warning("Failed to enqueue deferred reflection")
    elif cc_reflection_bridge is not None and classified_depth in (Depth.LIGHT, Depth.DEEP, Depth.STRATEGIC):
        try:
            ref_result = await cc_reflection_bridge.reflect(
                classified_depth,
                result,
                db=db,
                escalation_source=escalation_source if classified_depth == Depth.DEEP else None,
            )
            # A non-success result here is normally a gated CC fallback awaiting
            # approval (not a crash, so no exception fires). Log it so the
            # deferral is observable rather than a silent no-op; the approved
            # request is picked up later by _resume_approved_reflections.
            if ref_result is not None and not ref_result.success:
                logger.info(
                    "%s reflection deferred for tick %s: %s",
                    classified_depth.value, tick_id,
                    ref_result.reason or "unknown",
                )
            # Resolve escalation after successful dispatch
            if escalation_pending_id and classified_depth == Depth.DEEP:
                try:
                    await observations.resolve(
                        db, escalation_pending_id,
                        resolved_at=now,
                        resolution_notes="Escalation consumed by deep reflection",
                    )
                    await observations.create(
                        db,
                        id=str(uuid.uuid4()),
                        source="awareness_loop",
                        type="light_escalation_resolved",
                        content=f"Escalation {escalation_pending_id} consumed",
                        priority="low",
                        created_at=now,
                    )
                except Exception:
                    logger.warning("Failed to resolve escalation %s", escalation_pending_id, exc_info=True)
        except Exception:
            logger.exception("CC reflection failed for tick %s", tick_id)
            if deferred_queue and classified_depth:
                try:
                    await deferred_queue.enqueue(
                        work_type="reflection",
                        call_site_id=f"reflection_{classified_depth.value.lower()}",
                        priority=30,
                        payload=json.dumps({"tick_id": tick_id, "depth": classified_depth.value}),
                        reason="reflection_failed",
                        staleness_policy="ttl",
                        staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                    )
                except Exception:
                    logger.warning("Failed to enqueue deferred reflection")

    return result


class AwarenessLoop:
    """The metronome — drives the 5-minute awareness tick via APScheduler."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        collectors: list[SignalCollector],
        *,
        interval_minutes: int = 5,
        event_bus: GenesisEventBus | None = None,
        reflection_engine=None,
        cc_reflection_bridge=None,
        resilience_state_machine=None,
        deferred_queue=None,
    ):
        self._db = db
        self._collectors = list(collectors)
        self._interval = interval_minutes
        self._scheduler = AsyncIOScheduler()
        self._tick_lock = asyncio.Lock()
        # Single-flight guard for the session observer, which now runs
        # out-of-band (no longer serialized by _tick_lock). Prevents
        # overlapping runs from racing the .jsonl→.processing file renames.
        self._session_observer_lock = asyncio.Lock()
        self._event_bus = event_bus
        self._reflection_engine = reflection_engine
        self._cc_reflection_bridge = cc_reflection_bridge
        self._resilience_state_machine = resilience_state_machine
        self._deferred_queue = deferred_queue
        self._circuit_breakers: CircuitBreakerRegistry | None = None
        self._tick_event_loop: asyncio.AbstractEventLoop | None = None
        self._topic_manager = None
        self._guardian_watchdog = None
        self._remediation_registry = None
        self._sentinel = None
        self._credential_bridge_fn = None
        self._cred_integrity_fn = None
        self._alert_queue_drainer_fn = None
        self._autonomous_cli_policy_export_fn = None
        self._briefing_writer_fn = None
        self._findings_ingest_fn = None
        self._session_observer_fn = None
        self._stopping: bool = False
        self._tick_count: int = 0
        self._last_tick_at: str | None = None
        self._last_tick_result: TickResult | None = None
        self._last_degradation_level: DegradationLevel | None = None

    def request_stop(self) -> None:
        """Signal that shutdown is imminent — skip deferred retries.

        Called from the bridge signal handler to prevent the ~650ms race
        between SIGTERM receipt and runtime.shutdown() reaching stop().
        Does NOT stop the scheduler — that happens in stop().
        """
        self._stopping = True

    @property
    def tick_count(self) -> int:
        """Total ticks since this loop instance started."""
        return self._tick_count

    @property
    def last_tick_at(self) -> str | None:
        """ISO timestamp of the most recent tick completion."""
        return self._last_tick_at

    def set_circuit_breakers(self, breakers: CircuitBreakerRegistry) -> None:
        """Inject circuit breaker registry for resilience state updates."""
        self._circuit_breakers = breakers

    async def _update_resilience_cognitive_state(self, level: DegradationLevel) -> None:
        """Write or clear cognitive state when resilience level changes."""
        try:
            from genesis.db.crud import cognitive_state

            now = datetime.now(UTC).isoformat()
            if level == DegradationLevel.NORMAL:
                content = "All providers normal — no degradation."
            else:
                # Identify which providers are down
                down = []
                if self._circuit_breakers:
                    down = [
                        name for name, cb in self._circuit_breakers._breakers.items()
                        if not cb.is_available()
                    ]
                detail = f"Providers down: {', '.join(sorted(down))}" if down else ""
                content = f"Resilience {level.value}: {detail}"

            await cognitive_state.replace_section(
                self._db,
                section="resilience_degradation",
                id=str(uuid.uuid4()),
                content=content,
                generated_by="awareness_loop",
                created_at=now,
            )
            logger.info("Resilience cognitive state updated: %s → %s", self._last_degradation_level, level)
        except Exception:
            logger.warning("Failed to update resilience cognitive state", exc_info=True)

    async def start(self) -> None:
        """Start the scheduler with the tick job.

        Uses next_run_time=now so the first tick fires immediately rather than
        waiting one full interval.  This keeps status.json fresh from the
        moment the bridge starts, preventing watchdog false-positives.
        """
        self._scheduler.add_job(
            self._on_tick,
            IntervalTrigger(minutes=self._interval),
            id="awareness_tick",
            max_instances=1,
            misfire_grace_time=60,
            next_run_time=datetime.now(UTC),
        )
        # Surface dropped-tick events. APScheduler emits these synchronously
        # on its own thread; bounce to our event loop via call_soon_threadsafe
        # so we can await event_bus.emit safely.
        try:
            self._tick_event_loop = asyncio.get_running_loop()
            self._scheduler.add_listener(
                self._on_scheduler_job_event,
                EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES,
            )
        except Exception:
            logger.warning(
                "Failed to register scheduler job-event listener", exc_info=True,
            )
        self._scheduler.start()
        logger.info("Awareness Loop started (interval=%dm, immediate first tick)", self._interval)

    def _on_scheduler_job_event(self, event) -> None:
        """APScheduler listener — runs in scheduler thread.

        Hand the event off to the asyncio loop so async emit can run safely.
        """
        if getattr(event, "job_id", None) != "awareness_tick":
            return
        event_code = getattr(event, "code", None)
        try:
            loop = self._tick_event_loop
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._emit_tick_drop_event(event_code)),
            )
        except Exception:
            logger.warning("Failed to hand off scheduler event", exc_info=True)

    async def _emit_tick_drop_event(self, event_code: int | None) -> None:
        """Emit an observability event for a dropped / missed tick."""
        if self._event_bus is None:
            return
        if event_code == EVENT_JOB_MAX_INSTANCES:
            event_type = "tick.max_instances"
            message = (
                "Awareness tick dropped: previous tick still running "
                "(max_instances=1)"
            )
        elif event_code == EVENT_JOB_MISSED:
            event_type = "tick.missed"
            message = "Awareness tick missed (past misfire grace time)"
        else:
            event_type = "tick.dropped"
            message = f"Awareness tick dropped (code={event_code})"
        try:
            await self._event_bus.emit(
                Subsystem.AWARENESS,
                Severity.ERROR,
                event_type,
                message,
            )
        except Exception:
            logger.warning("Failed to emit tick drop event", exc_info=True)

    async def stop(self) -> None:
        """Stop the scheduler, waiting for any running tick to finish."""
        self._stopping = True
        self._scheduler.shutdown(wait=True)
        logger.info("Awareness Loop stopped")

    async def force_tick(self, reason: str) -> TickResult:
        """Critical event bypass — immediate out-of-cycle tick."""
        async with self._tick_lock:
            logger.info("Force tick triggered: %s", reason)
            result = await perform_tick(
                self._db, self._collectors,
                source="critical_bypass", reason=reason,
                reflection_engine=self._reflection_engine,
                cc_reflection_bridge=self._cc_reflection_bridge,
                deferred_queue=self._deferred_queue,
                dispatch_reflection=False,
            )

        if result.classified_depth is not None:
            from genesis.util.tasks import tracked_task

            tracked_task(
                self._dispatch_reflection(result),
                name=f"reflection-force-{result.tick_id[:8]}",
                subsystem=Subsystem.AWARENESS,
            )

        return result

    async def _on_tick(self) -> None:
        """Scheduled tick callback."""
        result = None
        async with self._tick_lock:
            try:
                result = await perform_tick(
                    self._db, self._collectors, source="scheduled",
                    reflection_engine=self._reflection_engine,
                    cc_reflection_bridge=self._cc_reflection_bridge,
                    deferred_queue=self._deferred_queue,
                    dispatch_reflection=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # perform_tick itself shouldn't raise (it has internal
                # try/except for DB ops), but guard against unexpected
                # failures in signal collection or other non-DB code.
                logger.exception("Awareness tick failed unexpectedly")
                if self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.AWARENESS, Severity.ERROR,
                        "tick.failed",
                        "Awareness tick failed with exception",
                    )
                try:
                    from genesis.runtime import GenesisRuntime
                    GenesisRuntime.instance().record_job_failure("awareness_tick", str(exc))
                except Exception:
                    pass
                # Even on unexpected failure, don't leave _last_tick_at stale
                self._last_tick_at = datetime.now(UTC).isoformat()

            if result is not None:
                # Always update tick tracking — even degraded ticks count
                # as "alive" to prevent false overdue alerts.
                self._tick_count += 1
                self._last_tick_at = datetime.now(UTC).isoformat()
                self._last_tick_result = result

                if result.classified_depth:
                    logger.info(
                        "Tick triggered %s: %s",
                        result.classified_depth.value, result.trigger_reason,
                    )

                if not result.db_available:
                    logger.warning(
                        "Tick %d completed DEGRADED (DB unavailable)",
                        self._tick_count,
                    )

                # Heartbeat — lets health MCP detect silent death
                if self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.AWARENESS, Severity.DEBUG,
                        "heartbeat",
                        "awareness_loop tick completed"
                        + (" (degraded)" if not result.db_available else ""),
                    )
                try:
                    from genesis.runtime import GenesisRuntime
                    if result.db_available:
                        GenesisRuntime.instance().record_job_success("awareness_tick")
                    else:
                        GenesisRuntime.instance().record_job_failure(
                            "awareness_tick", "DB unavailable (degraded tick)")
                except Exception:
                    pass  # Runtime may not be available in tests

            # Update resilience memory axis based on DB availability
            if self._resilience_state_machine and result is not None:
                from genesis.resilience.state import MemoryStatus
                if result.db_available:
                    self._resilience_state_machine.update_memory(MemoryStatus.NORMAL)
                else:
                    self._resilience_state_machine.update_memory(MemoryStatus.DOWN)

            # Update resilience cloud axis from circuit breaker state
            if self._resilience_state_machine and self._circuit_breakers:
                try:
                    level = self._circuit_breakers.compute_degradation_level()
                    cloud = _DEGRADATION_TO_CLOUD.get(level)
                    if cloud is None:
                        logger.warning("Unknown degradation level %s, defaulting to OFFLINE", level)
                        cloud = CloudStatus.OFFLINE
                    self._resilience_state_machine.update_cloud(cloud)

                    # Track degradation transitions in cognitive state
                    if level != self._last_degradation_level:
                        await self._update_resilience_cognitive_state(level)
                        self._last_degradation_level = level
                except Exception:
                    logger.warning("Resilience state update failed", exc_info=True)

            # Update resilience tmp_pressure axis from watchgod state
            if self._resilience_state_machine:
                try:
                    from genesis.observability.service_status import collect_cc_tmp_usage
                    from genesis.resilience.state import TmpPressureStatus

                    _TIER_TO_TMP = {
                        "green": TmpPressureStatus.NORMAL,
                        "yellow": TmpPressureStatus.MODERATE,
                        "orange": TmpPressureStatus.HIGH,
                        "red": TmpPressureStatus.CRITICAL,
                    }
                    cc_tmp = collect_cc_tmp_usage()
                    tier = cc_tmp.get("cc_tier", "unknown")
                    tmp_status = _TIER_TO_TMP.get(tier)
                    if tmp_status is not None:
                        self._resilience_state_machine.update_tmp_pressure(tmp_status)
                except Exception:
                    logger.debug("tmp_pressure axis update failed", exc_info=True)

            # Per-CC-slot RSS leak check — reads /proc (no DB dependency, so it
            # still runs during a DB hiccup); the observation write is guarded on
            # db inside the function. Surfaces a single ballooning CC session.
            await _check_cc_slot_memory(self._db)

            # CC silent-cap detection — counts recent empty-output cognitive
            # completions (recorded by the invoker) and alerts on a run. Guarded
            # on db internally; a query failure no-ops (never breaks the tick).
            await _check_cc_cap_detection(self._db)
            # Git-repository health (F.1) — cheap structural probe + rootfs-RO
            # write-probe. NOT gated on db_available: git health matters most
            # when the DB is broken (the observation write is guarded on db
            # inside). Writes a verdict to the shared mount for the guardian.
            await _check_git_health(self._db)
            # Daily deep fsck (F.1) — content-verifying scan for zeroed-but-present
            # objects the cheap probe misses. Self-guards to ~daily and runs in a
            # thread. Loop-driven (not the learning scheduler) so it survives a
            # router-degraded startup.
            await _check_git_health_deep(self._db)

            # SQLite WAL checkpoint — prevent unbounded WAL growth from
            # external scripts or concurrent writers. PASSIVE is non-blocking.
            # (SQLite-specific; remove when migrating to PostgreSQL.)
            if result is not None and result.db_available:
                await _sqlite_wal_checkpoint(self._db)
                # Hourly TRUNCATE reclaims WAL *file* space (PASSIVE can't), and a
                # WAL-size check alerts if a stuck reader is pinning the checkpoint.
                if self._tick_count % _WAL_TRUNCATE_EVERY_N_TICKS == 0:
                    await _sqlite_wal_truncate(self._db)
                    # nodatacow drift check (btrfs-only, daily alert cooldown) —
                    # static condition, so the slow hourly cadence is plenty.
                    await _check_db_nodatacow(self._db)
                    # Embedding-backlog degradation — count memories permanently
                    # stuck at embedding_status='failed'. Slow-moving and self-
                    # healing, so the hourly cadence fits; self-resolves when the
                    # backlog clears. Best-effort (guarded internally).
                    await _check_embedding_backlog(self._db)
                await _check_wal_health(self._db)

            # Status file writes are handled by a dedicated loop in
            # runtime/init/memory.py (status-writer-loop). Decoupled from
            # the awareness tick so a slow tick (e.g. long Light reflection)
            # does not cause the watchdog to see a stale status.json.

            # Guardian bidirectional monitoring — check heartbeat, auto-recover
            if self._guardian_watchdog:
                try:
                    await self._guardian_watchdog.check_and_recover()
                except Exception:
                    logger.warning("Guardian watchdog check failed", exc_info=True)

            # Mechanical self-healing — run remediation registry against health probes
            if self._remediation_registry:
                try:
                    from genesis.observability.health import collect_probe_results
                    probe_results = await collect_probe_results(self._db)
                    outcomes = await self._remediation_registry.check_and_remediate(
                        probe_results,
                    )
                    acted = [o for o in outcomes if o.executed]
                    if acted:
                        logger.info(
                            "Remediation tick: %d actions executed (%s)",
                            len(acted),
                            ", ".join(o.action.name for o in acted),
                        )
                except Exception:
                    logger.warning("Remediation registry check failed", exc_info=True)

            # Propagate Telegram credentials to shared mount for Guardian
            if self._credential_bridge_fn:
                try:
                    self._credential_bridge_fn()
                except Exception:
                    logger.error("Credential bridge write failed", exc_info=True)

            # Credential-file integrity: detect corruption + self-heal from backup
            # (first responder; the host guardian steps in only if this doesn't).
            if self._cred_integrity_fn:
                try:
                    self._cred_integrity_fn()
                except Exception:
                    logger.error("Credential integrity self-heal failed", exc_info=True)

            # F.3: drain durable alert queue (shell/Python alerts → Telegram).
            # Async; never breaks the tick.
            if self._alert_queue_drainer_fn:
                try:
                    await self._alert_queue_drainer_fn()
                except Exception:
                    logger.error("Alert queue drain failed", exc_info=True)

            if self._autonomous_cli_policy_export_fn:
                try:
                    self._autonomous_cli_policy_export_fn()
                except Exception:
                    logger.error("Autonomous CLI policy export failed", exc_info=True)

            # Write dynamic Guardian briefing to shared mount
            if self._briefing_writer_fn:
                try:
                    await self._briefing_writer_fn(self._db)
                except Exception:
                    logger.error("Guardian briefing write failed", exc_info=True)

            # Ingest Guardian diagnosis results from shared mount
            if self._findings_ingest_fn:
                try:
                    count = await self._findings_ingest_fn(self._db)
                    if count:
                        logger.info("Ingested %d Guardian findings", count)
                except Exception:
                    logger.error("Guardian findings ingest failed", exc_info=True)

            # Sentinel fire alarm check — evaluate conditions and dispatch if needed
            if self._sentinel:
                try:
                    await self._sentinel.check_fire_alarms()
                except Exception:
                    logger.warning("Sentinel fire alarm check failed", exc_info=True)

            # NOTE: the session observer used to run here, awaited inside the
            # tick lock. It made an LLM call that, under provider exhaustion,
            # held the lock for 16+ min and starved the heartbeat. It is now
            # dispatched OUT-OF-BAND below (single-flight guarded).

        if result is None:
            return

        from genesis.util.tasks import tracked_task

        # Session observer: process tool observations into memories. Dispatched
        # OUT-OF-BAND (previously awaited inside the tick lock) so the heartbeat
        # fires on cadence even when its LLM call grinds through an exhausted
        # provider chain. Single-flight (_session_observer_lock) prevents
        # overlapping ticks from racing the observation-file renames. Runs
        # regardless of the pause kill-switch (internal memory work, no external
        # dispatch) — matching the prior in-lock behavior.
        if self._session_observer_fn:
            tracked_task(
                self._run_session_observer(),
                name=f"session-observer-{result.tick_id[:8]}",
                subsystem=Subsystem.AWARENESS,
            )

        # Kill switch — tick/heartbeats still run but no dispatches when paused
        try:
            from genesis.runtime import GenesisRuntime
            if GenesisRuntime.instance().paused:
                logger.debug("Skipping reflection dispatch (Genesis paused)")
                return
        except Exception:
            pass

        if result.classified_depth is not None:
            tracked_task(
                self._dispatch_reflection(result),
                name=f"reflection-{result.classified_depth.value.lower()}-{result.tick_id[:8]}",
                subsystem=Subsystem.AWARENESS,
            )
        else:
            # Idle alive-pulse: a quiet tick (depth=None) ran no reflection.
            # Refresh the reflection heartbeat so subsystem_heartbeats does not
            # falsely report reflection "dark" during calm periods. Degraded
            # ticks are filtered inside the helper so a real outage still alarms.
            await self._emit_reflection_idle_heartbeat(result)

        if not self._stopping:
            tracked_task(
                self._retry_deferred_if_pending(result),
                name=f"deferred-retry-{result.tick_id[:8]}",
                subsystem=Subsystem.AWARENESS,
            )
            tracked_task(
                self._resume_approved_reflections(),
                name=f"approval-resume-{result.tick_id[:8]}",
                subsystem=Subsystem.AWARENESS,
            )
            tracked_task(
                self._resume_approved_sentinel_dispatches(),
                name=f"sentinel-resume-{result.tick_id[:8]}",
                subsystem=Subsystem.AWARENESS,
            )

    async def _run_session_observer(self) -> None:
        """Run the session observer with single-flight (out-of-band of the tick).

        If a prior run is still in progress (e.g. its LLM call is grinding
        through an exhausted provider chain), skip this tick's run rather than
        overlap — overlapping runs would race the observer's atomic
        ``.jsonl→.processing`` file renames. The ``locked()`` check followed by
        ``async with`` is safe in asyncio: there is no ``await`` between them, so
        no other coroutine can acquire the lock in the gap.
        """
        if self._session_observer_fn is None:
            return
        if self._session_observer_lock.locked():
            logger.debug("Session observer still running from a prior tick — skipping")
            return
        async with self._session_observer_lock:
            try:
                obs_result = await self._session_observer_fn()
                if obs_result and obs_result.notes_stored > 0:
                    logger.info(
                        "Session observer: %d notes from %d observations",
                        obs_result.notes_stored, obs_result.observations_read,
                    )
            except Exception:
                logger.warning("Session observer processing failed", exc_info=True)

    async def _emit_reflection_idle_heartbeat(self, result: TickResult) -> None:
        """Emit a reflection heartbeat for a quiet tick that ran no reflection.

        A tick that classified to ``depth=None`` (nothing triggered, or a
        ceiling/floor throttle) correctly ran no reflection. Emitting a
        heartbeat keeps ``subsystem_heartbeats`` fresh during the quiet ticks
        that dominate calm periods (``depth=None`` is ~93% of ticks), so
        reflection is not falsely reported "dark" overnight while the loop is
        healthy. Called from ``_on_tick`` on the depth=None dispatch branch.

        Skipped for a DEGRADED tick (``db_available`` is False — the DB was
        unavailable so scoring/classification was skipped): a genuine
        reflection outage must still age out past the heartbeat threshold and
        alarm, rather than being masked by this pulse.
        """
        if not (result.db_available and self._event_bus):
            return
        with contextlib.suppress(Exception):
            await self._event_bus.emit(
                Subsystem.REFLECTION, Severity.DEBUG,
                "heartbeat", "reflection idle (no depth triggered)",
            )

    async def _dispatch_reflection(self, result: TickResult) -> None:
        depth = result.classified_depth
        if depth is None:
            return

        tick_id = result.tick_id
        db = self._db
        logger.info(
            "Dispatch reflection: depth=%s, tick=%s, bridge=%s, engine=%s",
            depth.value, tick_id[:8],
            self._cc_reflection_bridge is not None,
            self._reflection_engine is not None,
        )

        if self._reflection_engine is not None and depth == Depth.MICRO:
            # Check for critical operational signals that warrant LLM analysis.
            # Routine micro ticks are silent (counted for escalation cascade only).
            critical_active = any(
                s.value > 0 for s in result.signals
                if s.name in _MICRO_CRITICAL_SIGNALS
            ) or any(
                s.value >= _SENTINEL_ANOMALY_THRESHOLD for s in result.signals
                if s.name == "sentinel_activity"
            )

            if critical_active:
                # Anomaly path: full LLM reflection for genuine operational events
                ref_result = None
                try:
                    ref_result = await self._reflection_engine.reflect(depth, result, db=db)
                except Exception:
                    logger.exception("Micro anomaly reflection crashed for tick %s", tick_id)

                if ref_result and ref_result.success and self._event_bus:
                    try:
                        await self._event_bus.emit(
                            Subsystem.REFLECTION, Severity.DEBUG,
                            "heartbeat", "micro-reflection completed",
                        )
                    except Exception:
                        logger.warning("Failed to emit reflection heartbeat", exc_info=True)

                if ref_result and ref_result.success and ref_result.output and self._topic_manager:
                    micro = ref_result.output
                    try:
                        anomaly_flag = " [ANOMALY]" if micro.anomaly else ""
                        tags_str = ", ".join(micro.tags[:5]) if micro.tags else ""
                        text = (
                            f"<b>Micro Reflection</b>{anomaly_flag}\n\n"
                            f"{micro.summary}\n\n"
                            f"<i>Salience: {micro.salience:.2f}"
                            f"{f' | Tags: {tags_str}' if tags_str else ''}</i>"
                        )
                        await self._topic_manager.send_to_category("reflection_micro", text)
                        logger.info(
                            "Posted micro reflection to Telegram (tick=%s, salience=%.2f)",
                            tick_id[:8], micro.salience,
                        )
                    except Exception:
                        logger.warning("Failed to post micro reflection to topic", exc_info=True)

                if (ref_result is None or not ref_result.success) and self._deferred_queue:
                    try:
                        await self._deferred_queue.enqueue(
                            work_type="reflection",
                            call_site_id="reflection_micro",
                            priority=30,
                            payload=json.dumps({"tick_id": tick_id, "depth": "Micro"}),
                            reason="reflection_failed",
                            staleness_policy="ttl",
                            staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                        )
                    except Exception:
                        logger.warning("Failed to enqueue deferred reflection")
            else:
                logger.debug(
                    "Micro tick %s silent (no critical signals active)",
                    tick_id[:8],
                )
                # Idle alive-pulse: a calm tick correctly ran no reflection.
                # Emit a heartbeat so subsystem_heartbeats does not falsely
                # report reflection "dark" during legitimately quiet periods.
                # This fires ONLY on the silent path — a tick that ATTEMPTS and
                # fails a reflection does not pulse, so a real outage still ages
                # out and alarms.
                if self._event_bus:
                    with contextlib.suppress(Exception):
                        await self._event_bus.emit(
                            Subsystem.REFLECTION, Severity.DEBUG,
                            "heartbeat", "reflection idle (no critical signals)",
                        )

            # Always mark dispatched — cascade counting works on ticks
            try:
                await awareness_ticks.mark_dispatched(db, tick_id)
            except Exception:
                logger.warning("Failed to mark tick %s dispatched", tick_id[:8])
            return

        if depth == Depth.LIGHT and self._cc_reflection_bridge is None and self._reflection_engine is not None:
            try:
                await self._reflection_engine.reflect(depth, result, db=db)
                # Emit reflection heartbeat so subsystem_heartbeats doesn't
                # report overdue when only the API path fires.
                if self._event_bus:
                    with contextlib.suppress(Exception):
                        await self._event_bus.emit(
                            Subsystem.REFLECTION, Severity.DEBUG,
                            "heartbeat", "light-reflection completed (API)",
                        )
            except Exception:
                logger.exception("Light reflection fallback (API) failed for tick %s", tick_id)
                if self._deferred_queue:
                    try:
                        await self._deferred_queue.enqueue(
                            work_type="reflection",
                            call_site_id="reflection_light",
                            priority=30,
                            payload=json.dumps({"tick_id": tick_id, "depth": "Light"}),
                            reason="reflection_failed",
                            staleness_policy="ttl",
                            staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                        )
                    except Exception:
                        logger.warning("Failed to enqueue deferred reflection")
            return

        if self._cc_reflection_bridge is not None and depth in (Depth.LIGHT, Depth.DEEP, Depth.STRATEGIC):
            try:
                ref_result = await self._cc_reflection_bridge.reflect(
                    depth,
                    result,
                    db=db,
                    escalation_source=result.escalation_source if depth == Depth.DEEP else None,
                )
                # Mark tick as dispatched only when the bridge confirmed
                # success.  Throttled / gate-blocked attempts (success=False)
                # leave dispatched=0 so the floor/ceiling checks don't count
                # them — preventing rate-limit cascades that block future
                # reflections for 48h+.
                if ref_result and ref_result.success:
                    try:
                        await awareness_ticks.mark_dispatched(db, tick_id)
                    except Exception:
                        logger.warning("Failed to mark tick %s dispatched", tick_id[:8])
                    # Emit reflection heartbeat so subsystem_heartbeats
                    # tracks Light/Deep/Strategic dispatches, not just
                    # micro-reflection anomaly ticks and weekly jobs.
                    if self._event_bus:
                        with contextlib.suppress(Exception):
                            await self._event_bus.emit(
                                Subsystem.REFLECTION, Severity.DEBUG,
                                "heartbeat",
                                f"{depth.value.lower()}-reflection completed",
                            )
                # Fix 3B: resolve escalation AFTER successful dispatch
                if result.escalation_pending_id and depth == Depth.DEEP:
                    await self._resolve_escalation(result.escalation_pending_id, result.timestamp)
            except Exception:
                logger.exception("CC reflection failed for tick %s", tick_id)
                if result.escalation_pending_id:
                    logger.info(
                        "Escalation %s left pending (dispatch failed, will retry)",
                        result.escalation_pending_id,
                    )
                if self._deferred_queue:
                    try:
                        await self._deferred_queue.enqueue(
                            work_type="reflection",
                            call_site_id=f"reflection_{depth.value.lower()}",
                            priority=30,
                            payload=json.dumps({"tick_id": tick_id, "depth": depth.value}),
                            reason="reflection_failed",
                            staleness_policy="ttl",
                            staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                        )
                    except Exception:
                        logger.warning("Failed to enqueue deferred reflection")

    async def _retry_deferred_if_pending(self, current_tick: TickResult) -> None:
        try:
            await self._retry_deferred_reflection(current_tick)
        except Exception:
            logger.warning("Deferred reflection retry failed", exc_info=True)

    async def _resume_approved_reflections(self) -> None:
        """Resume light/deep/strategic reflections whose approvals were granted.

        When a user approves a reflection's CC fallback via Telegram or
        dashboard, the awareness loop's scoring may never independently reach
        that depth's threshold again. This method checks for approved-but-
        unconsumed reflection approvals and dispatches them immediately.

        Light is included because its free API chain (dispatch=dual) can
        exhaust during a provider outage and escalate to the gated CC
        fallback; without a resume path the approved request would never run.
        """
        if not self._cc_reflection_bridge:
            return
        # The autonomous dispatcher is set on the reflection bridge, not
        # directly on the awareness loop. Access the gate via the bridge.
        dispatcher = getattr(self._cc_reflection_bridge, "_autonomous_dispatcher", None)
        if dispatcher is None:
            return
        gate = getattr(dispatcher, "approval_gate", None)
        if gate is None:
            return

        tick = self._last_tick_result
        if tick is None:
            return  # No tick yet — can't build reflection prompt

        # Explicit name→depth map (NOT a binary): each policy_id must resolve
        # to its own depth, otherwise an added name would mis-dispatch (e.g.
        # light running as an expensive STRATEGIC reflection).
        resumable_depths = {
            "deep": Depth.DEEP,
            "strategic": Depth.STRATEGIC,
            "light": Depth.LIGHT,
        }
        for depth_name, depth in resumable_depths.items():
            try:
                approved = await gate.find_recently_approved(
                    subsystem="reflection",
                    policy_id=f"reflection_{depth_name}",
                )
                if not approved:
                    continue
                # Atomic consume — prevents double-dispatch across ticks.
                # Must happen HERE, not inside route(), because skip_approval
                # bypasses the approval gate (and its mark_consumed call).
                consumed = await gate.mark_consumed(approved["id"])
                if not consumed:
                    continue  # Another tick already consumed it
                logger.info(
                    "Resuming %s reflection from approved request %s",
                    depth_name, approved["id"][:8],
                )
                await self._cc_reflection_bridge.reflect(
                    depth, tick, db=self._db, skip_approval=True,
                )
            except Exception:
                logger.error(
                    "Failed to resume %s reflection", depth_name, exc_info=True,
                )

    async def _resume_approved_sentinel_dispatches(self) -> None:
        """Converge a parked sentinel dispatch on its approval's REAL status.

        State-keyed: when the sentinel is AWAITING_*, the dispatcher looks
        up the exact pending request row and applies whatever actually
        happened to it — approved → resume, rejected → apply the rejection
        (24h pattern suppression + HEALTHY), expired/cancelled/missing →
        clear the park, no-pending-id-recorded → clear the inconsistent
        park. The previous implementation only scanned for approved rows,
        so a rejection was never delivered and the sentinel stayed parked
        forever (Gate 2 blocks all other dispatches while parked — this
        blinded the Sentinel for 26 days in June/July 2026). The old
        approved-row scan fallback was removed outright: for a park with no
        recorded pending id it consumed an approval whose id could never
        match, eating the user's decision while staying parked.
        """
        if self._sentinel is None:
            return
        if getattr(self._sentinel, "_approval_gate", None) is None:
            return

        from genesis.sentinel.state import SentinelState as _SS

        # Only resume if sentinel is actually waiting for an approval
        state = self._sentinel.state
        if state.state not in (_SS.AWAITING_DISPATCH_APPROVAL, _SS.AWAITING_ACTION_APPROVAL):
            return

        try:
            await self._sentinel.converge_pending_approval()
        except Exception:
            logger.error(
                "Failed to converge sentinel on pending approval",
                exc_info=True,
            )

    async def _retry_deferred_reflection(self, current_tick: TickResult) -> None:
        """Retry ONE deferred reflection per tick using current tick's fresh data.

        Rate-limited: one item per 5-min tick. On failure, attempts increment
        and the item stays pending. After 3 failed attempts, escalate via
        WARNING event (not silently discarded).
        """
        if not self._deferred_queue:
            return

        if self._stopping:
            logger.debug("Skipping deferred reflection retry — loop is stopping")
            return

        item = await self._deferred_queue.next_pending(work_type="reflection", max_priority=40)
        if not item:
            return

        item_id = item["id"]
        payload = json.loads(item.get("payload_json", "{}"))
        depth_str = payload.get("depth", "")

        try:
            depth = Depth(depth_str)
        except ValueError:
            logger.warning("Deferred reflection has invalid depth=%s, discarding", depth_str)
            await self._deferred_queue.mark_discarded(item_id, f"invalid depth: {depth_str}")
            return

        attempts = item.get("attempts", 0)
        await self._deferred_queue.mark_processing(item_id)
        logger.info(
            "Retrying deferred reflection: id=%s depth=%s attempt=%d",
            item_id, depth.value, attempts + 1,
        )

        try:
            if depth in (Depth.LIGHT, Depth.DEEP, Depth.STRATEGIC) and self._cc_reflection_bridge:
                result = await self._cc_reflection_bridge.reflect(depth, current_tick, db=self._db)
            elif self._reflection_engine:
                result = await self._reflection_engine.reflect(depth, current_tick, db=self._db)
            else:
                logger.warning(
                    "No reflection handler for depth=%s — leaving pending", depth.value,
                )
                await self._deferred_queue.reset_to_pending(item_id)
                return

            if result.success:
                await self._deferred_queue.mark_completed(item_id)
                logger.info("Deferred reflection succeeded: id=%s depth=%s", item_id, depth.value)
            else:
                # Operational failure (rate limit, throttle, etc.) — reset for retry.
                # Don't count as a discard-worthy attempt; TTL handles expiry.
                await self._deferred_queue.reset_to_pending(item_id)
                logger.info(
                    "Deferred reflection not ready: id=%s depth=%s reason=%s — will retry",
                    item_id, depth.value, result.reason or "unknown",
                )
        except Exception:
            new_attempts = attempts + 1  # mark_processing already incremented in DB
            logger.warning(
                "Deferred reflection retry failed: id=%s depth=%s attempt=%d",
                item_id, depth.value, new_attempts, exc_info=True,
            )
            if new_attempts >= 3:
                await self._deferred_queue.mark_discarded(
                    item_id,
                    f"max attempts ({new_attempts}) exceeded — retry failed",
                )
                if self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.AWARENESS, Severity.WARNING,
                        "deferred.max_attempts",
                        f"Deferred {depth.value} reflection failed after {new_attempts} attempts",
                    )
            else:
                # Reset to pending so next tick can retry
                await self._deferred_queue.reset_to_pending(item_id)

    async def _resolve_escalation(self, pending_id: str, now: str) -> None:
        """Resolve a consumed escalation and record the cooldown marker."""
        from genesis.db.crud import observations

        try:
            await observations.resolve(
                self._db, pending_id,
                resolved_at=now,
                resolution_notes="Escalation consumed by deep reflection",
            )
            await observations.create(
                self._db,
                id=str(uuid.uuid4()),
                source="awareness_loop",
                type="light_escalation_resolved",
                content=f"Escalation {pending_id} consumed",
                priority="low",
                created_at=now,
            )
            logger.info("Escalation %s resolved after successful dispatch", pending_id)
        except Exception:
            logger.warning("Failed to resolve escalation %s", pending_id, exc_info=True)

    def set_resilience_state_machine(self, sm) -> None:
        """Inject resilience state machine after construction."""
        self._resilience_state_machine = sm

    def set_deferred_queue(self, dq) -> None:
        """Inject deferred queue after construction."""
        self._deferred_queue = dq

    def set_reflection_engine(self, engine) -> None:
        """Inject reflection engine after construction."""
        self._reflection_engine = engine

    def set_cc_reflection_bridge(self, bridge) -> None:
        """Inject CC reflection bridge after construction."""
        self._cc_reflection_bridge = bridge

    def set_topic_manager(self, manager) -> None:
        """Inject TopicManager for posting micro reflections to forum topics."""
        self._topic_manager = manager

    def set_guardian_watchdog(self, watchdog) -> None:
        """Inject Guardian watchdog for bidirectional host monitoring."""
        self._guardian_watchdog = watchdog

    def set_remediation_registry(self, registry) -> None:
        """Inject remediation registry for mechanical self-healing."""
        self._remediation_registry = registry

    def set_sentinel(self, sentinel) -> None:
        """Inject Sentinel dispatcher for autonomous fire alarm response."""
        self._sentinel = sentinel

    def set_credential_bridge(self, fn) -> None:
        """Inject credential bridge for Telegram credential propagation."""
        self._credential_bridge_fn = fn

    def set_cred_integrity_fn(self, fn) -> None:
        """Inject the credential-file integrity check + self-heal (per tick)."""
        self._cred_integrity_fn = fn

    def set_alert_queue_drainer(self, fn) -> None:
        """Inject the async container alert-queue drainer (per tick).

        ``fn`` is an awaitable that flushes ``~/.genesis/alerts/queue`` to
        Telegram via the outreach pipeline (F.3). Wired independently of the
        guardian, since shell-written alerts (watchgod/backup) are valuable
        guardian-or-not.
        """
        self._alert_queue_drainer_fn = fn

    def set_autonomous_cli_policy_exporter(self, fn) -> None:
        """Inject shared-mount exporter for effective autonomous CLI policy."""
        self._autonomous_cli_policy_export_fn = fn

    def set_briefing_writer(self, fn) -> None:
        """Inject dynamic briefing writer for Guardian context updates."""
        self._briefing_writer_fn = fn

    def set_findings_ingest(self, fn) -> None:
        """Inject Guardian findings ingest for reading diagnosis results."""
        self._findings_ingest_fn = fn

    def set_session_observer(self, fn) -> None:
        """Inject session observer processor for tool activity notes."""
        self._session_observer_fn = fn

    def replace_collectors(self, collectors: list) -> None:
        """Replace signal collectors (late-binding upgrade from stubs to real).

        WARNING: this is a **full replacement**, not a superset merge. Any
        collector registered by ``runtime/init/awareness.py`` that should
        survive the swap MUST be re-added to the new ``collectors`` list
        passed by ``runtime/init/learning.py``. Otherwise it is silently
        dropped from the awareness loop and its signal stops being measured.

        Currently both ``ContainerMemoryCollector`` and ``JobHealthCollector``
        are registered in awareness init but NOT re-listed in the learning
        swap, so their signals (`container_memory_pct`, `scheduled_job_health`)
        are dropped post-bootstrap. This is functionally OK today because
        neither has a corresponding ``signal_weights`` row, but adding such
        a row in the future will silently produce 0.0 readings unless the
        learning swap is updated to re-include them.
        """
        self._collectors = list(collectors)

    # GROUNDWORK(category-2-rhythms): add_rhythm(name, interval, callback)
    # GROUNDWORK(category-3-crons): add_cron(name, cron_expr, callback)
