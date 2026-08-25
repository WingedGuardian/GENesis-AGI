"""bootstrap_manifest, subsystem_heartbeats, and job_health tools."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from genesis.env import db_busy_timeout_ms
from genesis.mcp.health import mcp  # noqa: E402
from genesis.mcp.health.constants import JOB_STALE_GAP_DAYS

logger = logging.getLogger(__name__)

# Module-level DB path so tests can monkeypatch it without touching
# ``Path.home()``. Matches the pattern used in ``update_history.py``.
_DB_PATH = Path.home() / "genesis" / "data" / "genesis.db"

def _parse_ts_utc(raw: str) -> datetime:
    """Parse an ISO timestamp, coercing to timezone-aware UTC.

    job_health timestamps are written as ``datetime.now(UTC).isoformat()``
    (offset-aware), but coerce naive strings to UTC too so a mixed naive/aware
    pair can still be subtracted without raising ``TypeError``.
    """
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _annotate_staleness(jobs: dict) -> dict:
    """Return a copy of ``jobs`` with derived ``days_since_success`` + ``stale``.

    ``days_since_success`` = the ``last_run − last_success`` gap in days — how
    long a job has been firing without completing. ``stale`` = that gap exceeds
    the alert threshold. Keying on this gap (rather than ``consecutive_failures``)
    surfaces silent failures that ``clear_stale_job_failures`` would otherwise
    hide by resetting the failure counter on every restart.

    Non-mutating: the runtime return path hands back ``rt.job_health`` by
    reference, so each entry is shallow-copied before the derived keys are added.
    """
    annotated: dict[str, dict] = {}
    for name, info in jobs.items():
        entry = dict(info)
        gap: float | None = None
        last_run = info.get("last_run")
        last_ok = info.get("last_success")
        if last_run and last_ok:
            try:
                gap = round(
                    (_parse_ts_utc(last_run) - _parse_ts_utc(last_ok)).total_seconds()
                    / 86400,
                    1,
                )
            except (ValueError, TypeError):
                gap = None
        entry["days_since_success"] = gap
        entry["stale"] = gap is not None and gap > JOB_STALE_GAP_DAYS
        annotated[name] = entry
    return annotated


# Module-level so tests can monkeypatch the location.
_MANIFEST_FILE = Path.home() / ".genesis" / "bootstrap_manifest.json"


def _read_persisted_manifest() -> dict | None:
    """Read the manifest genesis-server persisted at its last bootstrap.

    MCP servers run in a separate CC-child process that never bootstraps the
    runtime, so the in-process singleton has no manifest. genesis-server writes
    the verbatim manifest to ``~/.genesis/bootstrap_manifest.json`` at the tail
    of its own bootstrap (``runtime/_capabilities.py``); read it directly — no
    reverse mapping, exact fidelity. Returns None when the file is missing or
    unreadable so the caller can report an honest empty result.

    This is last-known state, NOT a liveness signal: the file survives a server
    crash, so it never asserts the server is currently up. ``bootstrapped`` is
    reported False (this process did not bootstrap); the populated manifest is
    labelled ``source='persisted_manifest'`` with ``persisted_at`` and a pointer
    to ``subsystem_heartbeats`` for current liveness.
    """
    try:
        raw = json.loads(_MANIFEST_FILE.read_text())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read %s: %s", _MANIFEST_FILE, exc)
        return None

    manifest = raw.get("manifest") if isinstance(raw, dict) else None
    if not isinstance(manifest, dict):
        logger.error("Malformed bootstrap manifest file: %s", _MANIFEST_FILE)
        return None

    return {
        "bootstrapped": False,  # THIS process didn't bootstrap; liveness ≠ file
        "manifest": manifest,
        "source": "persisted_manifest",
        "persisted_at": raw.get("persisted_at"),
        "note": (
            "Last-known manifest persisted by genesis-server at its last "
            "bootstrap (this MCP process does not bootstrap the runtime). "
            "Does NOT indicate current liveness — check subsystem_heartbeats."
        ),
    }


async def _impl_bootstrap_manifest() -> dict:
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        if rt.is_bootstrapped:
            return {
                "bootstrapped": True,
                "manifest": rt.bootstrap_manifest,
                "source": "runtime",
            }
        # Not bootstrapped in THIS process — the normal case for MCP
        # servers (CC child processes, separate from genesis-server).
        # Serve the manifest genesis-server persisted at its bootstrap.
        persisted = _read_persisted_manifest()
        if persisted is not None:
            return persisted
        return {
            "bootstrapped": False,
            "manifest": {},
            "source": "runtime",
        }
    except (ImportError, AttributeError, RuntimeError):
        # Narrow catch: runtime module genuinely unimportable, singleton
        # __init__ failed, or runtime attribute access failure. In the
        # common "standalone mode" path the runtime IS importable and the
        # happy path answers from the persisted capabilities file — this
        # except branch only fires on a real bug, which is why it logs at
        # ERROR. A broader catch would hide those bugs.
        logger.error(
            "bootstrap_manifest fallback fired — runtime unreachable",
            exc_info=True,
        )
        return {
            "status": "unavailable",
            "message": "Bootstrap manifest unavailable — runtime unreachable",
        }


# (expected_interval_s, overdue_threshold_s) per heartbeat-emitting subsystem.
# ONE source of truth for the subsystem_heartbeats display tool, the
# ``subsystem_stale:<name>`` pulse-staleness alert (errors.py), and the ego
# dashboard tile (routes/ego.py) — so those surfaces can never disagree.
# ``expected_interval_s`` is currently informational; ``overdue_s`` drives the
# verdict.
HEARTBEAT_EXPECTED = {
    "awareness": (300, 360),      # 5 min tick, error at 6 min
    "surplus": (300, 600),
    # inbox checks every 30 min (inbox/types.py check_interval_seconds=1800).
    # overdue=4× (7200s) so one slow/skipped check doesn't false-fire; a 2×
    # (3600s) threshold was too tight. (Assumes the default interval; a custom
    # check_interval_seconds should scale this — derive-from-config is a nicety.)
    "inbox": (1800, 7200),
    # Reflections only fire when signals warrant; calm periods can be quiet
    # for hours. An idle alive-pulse (awareness loop) keeps this fresh during
    # legitimate idle, so overdue=4h (matches ego) avoids false "dark" alarms.
    # A real reflection outage surfaces faster via the awareness heartbeat
    # (6 min), the cc resilience axis, and the deferred-work backlog.
    "reflection": (600, 14400),
    "outreach": (86400, 172800),
    "dashboard": (120, 600),      # 60s daemon-thread pulse; overdue at 10× (was 4×)
    "ego": (300, 14400),          # 5-min liveness pulse (ego_heartbeat job), overdue at 4h
}

# Subsystems whose heartbeat is emitted only AFTER their loop's
# ``if paused: return`` guard — so a deliberately-paused Genesis stops their
# pulse and they would false-read as dead. VERIFIED emit-after-pause sites:
# surplus (surplus/scheduler.py:823-853) and inbox (inbox/monitor.py:2120-2154).
# ego (dedicated ``ego_heartbeat`` job) and dashboard (dedicated daemon thread)
# pulse THROUGH pause, so they are NOT here — an overdue-while-paused for them is
# a genuine scheduler death that must still surface. See compute_heartbeat_staleness.
_PAUSE_GATED_HEARTBEATS = frozenset({"surplus", "inbox"})

# A heartbeat more than this far in the FUTURE (clock skew / corrupt row) cannot
# confirm liveness → verdict "unknown", never a false "alive" from a negative age.
# Mirrors observability/liveness.py FUTURE_SKEW_TOLERANCE_MINUTES (kept local to
# avoid an import cycle into that module from the health manifest).
_FUTURE_SKEW_TOLERANCE_S = 5 * 60


def _read_global_paused() -> bool:
    """Best-effort read of the global runtime pause flag (never constructs a
    zombie singleton; defaults to NOT-paused so a read failure surfaces the
    alert rather than silently masking a real death)."""
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.peek()
        return bool(rt.paused) if rt is not None else False
    except Exception:
        logger.debug("global pause read failed; treating as not-paused", exc_info=True)
        return False


def _freshest_timestamp(
    a: str | None, b: str | None, *, not_after: datetime | None = None
) -> str | None:
    """Return the freshest USABLE ISO timestamp (tolerant of None/unparseable).

    The durable ``events`` row and the in-memory event-bus ring can each hold the
    newest pulse — the ring updates synchronously while the DB persists
    fire-and-forget, so a fresh pulse can live only in the ring for a moment. Take
    the freshest so a DB-lagged read never false-REDs a live subsystem (P2-2).

    ``not_after`` (typically now + skew tolerance) marks a value as materially
    future/corrupt: such a candidate must NOT win over a valid non-future one, or a
    corrupt future DB row would mask a fresh ring pulse and yield a false "unknown".
    A future/unparseable value is returned ONLY as a last resort (sole candidate),
    so the caller still surfaces "unknown" rather than a spurious "alive"."""
    best: str | None = None  # freshest qualifying (parseable, not materially-future)
    best_dt: datetime | None = None
    fallback: str | None = None  # any value at all, if nothing qualifies
    for ts in (a, b):
        if not ts:
            continue
        if fallback is None:
            fallback = ts
        try:
            dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if not_after is not None and dt > not_after:
            continue  # materially-future/corrupt → never beats a valid non-future one
        if best_dt is None or dt > best_dt:
            best, best_dt = ts, dt
    return best if best is not None else fallback


async def compute_heartbeat_staleness(
    name: str, db=None, raise_on_error: bool = False, paused: bool | None = None
) -> dict:
    """Staleness verdict for ONE heartbeat-emitting subsystem.

    Reads the latest durable ``heartbeat`` event for *name* AND the in-memory
    event-bus ring, uses the FRESHEST of the two, and compares its age to the
    subsystem's overdue threshold in :data:`HEARTBEAT_EXPECTED`. Returns the same
    per-subsystem shape :func:`_impl_subsystem_heartbeats` yields:
    ``{status: alive|overdue|paused|no_heartbeat|unknown, last_seen, age_seconds?}``.

    *db* defaults to the health service's connection; a caller with its own
    runtime connection (e.g. the ego dashboard route) passes it explicitly.

    *raise_on_error*: by default a caught query error is ERROR-logged and the
    verdict degrades to ``no_heartbeat`` (best-effort, as the display/alert paths
    want). A caller that renders a POSITIVE health assertion (the ego tile) passes
    ``True`` so the read failure RE-RAISES and it can fail loud (surface unknown)
    rather than paint a healthy tile over a broken read. (Truly unexpected
    exception types always propagate — matching the original display behavior of
    surfacing ``is_error`` rather than silently dropping a real bug.)

    *paused*: for a :data:`_PAUSE_GATED_HEARTBEATS` subsystem (surplus/inbox,
    whose pulse stops behind their loop's ``if paused: return``), a would-be
    ``overdue`` verdict while Genesis is globally paused is downgraded to
    ``paused`` — a deliberately-paused subsystem is not a dead one. ``None`` (the
    default) reads the live global pause flag; callers may pass an explicit bool
    (tests, or a caller that already read it). Non-pause-gated subsystems
    (ego/dashboard) pulse THROUGH pause, so an ``overdue`` for them is a real
    death and is never downgraded.

    An unknown *name* (not in the threshold table) → ``no_heartbeat``; never raises
    for that reason.
    """
    import genesis.mcp.health_mcp as health_mcp_mod

    entry = HEARTBEAT_EXPECTED.get(name)
    if entry is None:
        return {"status": "no_heartbeat", "last_seen": None}
    _interval_s, overdue_s = entry

    if db is None:
        _service = health_mcp_mod._service
        db = _service._db if _service else None
    _event_bus = health_mcp_mod._event_bus

    db_ts = None
    if db is not None:
        try:
            from genesis.db.crud import events as events_crud

            rows = await events_crud.query(
                db, subsystem=name, event_type="heartbeat", limit=1
            )
            if rows:
                db_ts = rows[0].get("timestamp")
        except (aiosqlite.Error, ImportError, AttributeError, TimeoutError):
            # Narrow catch: DB query failure (aiosqlite.Error, TimeoutError),
            # broken import chain (ImportError), or malformed row shape
            # (AttributeError). Log at ERROR — a heartbeat probe failure against a
            # wired DB is an operational failure. A POSITIVE-assertion caller
            # (the ego tile) sets raise_on_error so this fails loud (unknown)
            # instead of a spurious healthy tile; the display/alert callers
            # degrade to no_heartbeat (absence, logged). Unexpected exception
            # types still bubble on purpose (FastMCP is_error over a silent drop).
            logger.error(
                "Heartbeat timestamp query failed for subsystem %s", name, exc_info=True
            )
            if raise_on_error:
                raise

    ring_ts = None
    if _event_bus is not None and hasattr(_event_bus, "_ring"):
        for event in reversed(_event_bus._ring):
            sub_val = (
                event.subsystem.value
                if hasattr(event.subsystem, "value")
                else str(event.subsystem)
            )
            if sub_val == name and event.event_type == "heartbeat":
                ring_ts = event.timestamp
                break

    # Use the freshest of the durable row and the in-memory ring — a DB-lagged
    # read must never false-RED a subsystem that just pulsed (P2-2) — but a corrupt
    # future row must not mask a valid fresh pulse, so exclude materially-future
    # candidates from winning (N-2).
    _now = datetime.now(UTC)
    last_ts = _freshest_timestamp(
        db_ts, ring_ts, not_after=_now + timedelta(seconds=_FUTURE_SKEW_TOLERANCE_S)
    )

    if last_ts is None:
        return {"status": "no_heartbeat", "last_seen": None}
    try:
        age_s = (_now - datetime.fromisoformat(last_ts)).total_seconds()
    except (ValueError, TypeError):
        return {"status": "unknown", "last_seen": last_ts}

    # A pulse materially in the future (clock skew / corrupt row) cannot confirm
    # liveness → unknown, never a false "alive" from a negative age (P2-3).
    if age_s < -_FUTURE_SKEW_TOLERANCE_S:
        return {"status": "unknown", "last_seen": last_ts}

    if age_s <= overdue_s:
        return {"status": "alive", "last_seen": last_ts, "age_seconds": round(age_s, 1)}

    # Overdue. For a pause-gated subsystem, a globally-paused Genesis is NOT a
    # dead one — its pulse legitimately stops on pause. Downgrade to a non-overdue
    # "paused" verdict so no consumer (alert / morning report / tile) reads it as
    # death. ego/dashboard pulse through pause, so they are never downgraded.
    if name in _PAUSE_GATED_HEARTBEATS:
        is_paused = paused if paused is not None else _read_global_paused()
        if is_paused:
            return {"status": "paused", "last_seen": last_ts, "age_seconds": round(age_s, 1)}
    return {"status": "overdue", "last_seen": last_ts, "age_seconds": round(age_s, 1)}


async def _impl_subsystem_heartbeats() -> dict:
    return {
        name: await compute_heartbeat_staleness(name) for name in HEARTBEAT_EXPECTED
    }


async def _impl_job_health() -> dict:
    """Return scheduled-job health under a single normalized envelope.

    Contract (all four return paths share this shape)::

        {
            "jobs": {job_name: {...}, ...},
            "note": None | str,
            "source": "runtime" | "sqlite" | "missing_db" | "query_failed",
        }

    Callers can always read ``result["jobs"]`` and ``result["source"]``
    without branching on shape. A non-None ``note`` is a human-readable
    explanation of a degraded state (missing DB, query failure).
    """
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        if rt.job_health:
            return {
                "jobs": _annotate_staleness(rt.job_health),
                "note": None,
                "source": "runtime",
            }
    except (ImportError, AttributeError, RuntimeError):
        # Probe of the runtime singleton. This is expected to miss in
        # standalone mode — we fall through to the sqlite fallback —
        # so DEBUG is the right level here. Narrow catch still lets
        # real bugs surface.
        logger.debug("Runtime job_health unavailable", exc_info=True)

    if not _DB_PATH.exists():
        return {
            "jobs": {},
            "note": (
                f"Genesis database not found at {_DB_PATH}; "
                "no job health data available."
            ),
            "source": "missing_db",
        }

    try:
        async with aiosqlite.connect(str(_DB_PATH)) as db:
            await db.execute(f"PRAGMA busy_timeout={db_busy_timeout_ms()}")
            cursor = await db.execute(
                "SELECT job_name, last_run, last_success, last_failure, "
                "last_error, consecutive_failures FROM job_health"
            )
            jobs: dict[str, dict] = {}
            for row in await cursor.fetchall():
                jobs[row[0]] = {
                    "last_run": row[1],
                    "last_success": row[2],
                    "last_failure": row[3],
                    "last_error": row[4],
                    "consecutive_failures": row[5],
                }
            return {"jobs": _annotate_staleness(jobs), "note": None, "source": "sqlite"}
    except (aiosqlite.Error, OSError):
        # aiosqlite.Error covers DB-level failures (corrupt file, busy
        # timeout, schema mismatch). OSError covers filesystem issues
        # between the exists() check and connect() (race, permission).
        # Log at ERROR — a DB probe failure is the kind of operational
        # failure the rules say to surface. Return a structured envelope
        # with an explicit ``source`` so callers can distinguish "no
        # jobs" from "check failed".
        logger.error(
            "job_health sqlite fallback failed at %s",
            _DB_PATH, exc_info=True,
        )
        return {
            "jobs": {},
            "note": "job_health check failed — see logs for details.",
            "source": "query_failed",
        }


@mcp.tool()
async def bootstrap_manifest() -> dict:
    """Which subsystems initialized successfully, failed, or degraded at startup."""
    return await _impl_bootstrap_manifest()


@mcp.tool()
async def subsystem_heartbeats() -> dict:
    """Last heartbeat time for each background subsystem. Detects silent deaths."""
    return await _impl_subsystem_heartbeats()


@mcp.tool()
async def job_health() -> dict:
    """Scheduled job health: last run, last success, consecutive failures per job.

    Returns a normalized envelope::

        {
          "jobs": {job_name: {last_run, last_success, last_failure,
                              last_error, consecutive_failures}},
          "note": null | "human-readable explanation",
          "source": "runtime" | "sqlite" | "missing_db" | "query_failed"
        }

    ``note`` is null on the happy path; non-null when the check
    degraded (missing DB or query failure). ``source`` identifies
    which path produced the result.
    """
    return await _impl_job_health()
