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
from genesis.observability.liveness import FUTURE_SKEW_TOLERANCE_MINUTES, parse_iso_utc

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

# The complete set of statuses ``compute_heartbeat_staleness`` can return. Every
# consumer that switches on the status string (the alert loop in errors.py, the ego
# dashboard tile/route, morning_report, the subsystem_heartbeats MCP tool) must handle
# ALL of these. Locked by test_heartbeat_status_set_is_closed: the test pins this set to
# a literal, so adding a status forces a deliberate edit here + in the test — a prompt to
# audit the consumers. (It is a forcing function, not a proof that every consumer branches
# on each member.)
_HEARTBEAT_STATUSES = frozenset(
    {"alive", "overdue", "paused", "resuming", "no_heartbeat", "unknown", "never_started"}
)

# How many recent heartbeat rows to scan for the newest USABLE pulse. >1 so a few
# unparseable / materially-future rows (which sort FIRST under the events table's
# TEXT ``ORDER BY timestamp DESC`` — a "2099-.." or "not-a-date" value) can't hide
# the valid pulses beneath them and pin a LIMIT-1 read on a bad row forever (#9). A
# window with NO valid non-future row → ``unknown`` (corrupt) or ``no_heartbeat``
# (empty), never a spurious verdict off the bad row. Residual (bounded, surfaced,
# not silent): if MORE than this many future/unparseable rows sort ahead of the
# newest valid pulse it reads ``unknown`` until they age out — needs sustained
# future-timestamp corruption, which the keep-latest GC otherwise bounds.
_HEARTBEAT_SCAN_LIMIT = 25

# Path of the cross-process pause file (single source of truth for pause; the
# standalone health MCP has no bootstrapped GenesisRuntime, so peek() is None there).
_PAUSE_FILE = Path.home() / ".genesis" / "paused.json"


def _read_global_paused() -> bool:
    """Global pause state, read cross-process-safe from the persisted pause file.

    Pause is persisted to ``~/.genesis/paused.json`` (present with ``paused:true``
    when paused, ABSENT when not — see runtime/_pause_state.py) — the source of
    truth EVERY process sees, including the standalone health MCP, whose
    ``StandaloneHealthDataService`` has no bootstrapped GenesisRuntime (so a
    runtime-singleton read would always return not-paused there and false-alarm a
    paused subsystem). Absent file / read error → not-paused, so a read failure
    surfaces the alert (fail-loud) rather than masking a real death."""
    try:
        if not _PAUSE_FILE.exists():
            return False
        import json

        return bool(json.loads(_PAUSE_FILE.read_text()).get("paused", False))
    except Exception:
        logger.debug("global pause read failed; treating as not-paused", exc_info=True)
        return False


def _inbox_enabled() -> bool:
    """Is the inbox monitor configured AND enabled? Mirrors ``runtime/init/inbox.py``:
    no ``config/inbox_monitor.yaml`` → unconfigured → not enabled; else ``config.enabled``.

    Load-bearing for the never_started verdict: inbox init SWALLOWS its own exceptions
    (``runtime/init/inbox.py:68``) and returns early when disabled/unconfigured, so a
    disabled, an unconfigured, AND a genuinely-crashed inbox ALL record the same
    ``degraded`` manifest value. Gating the never_started alarm on this lets a
    configured+enabled inbox that failed to start surface, while a deliberately-off or
    unconfigured inbox (also ``degraded``) stays benign."""
    from genesis.env import repo_root

    config_path = repo_root() / "config" / "inbox_monitor.yaml"
    if not config_path.exists():
        return False
    from genesis.inbox.config import load_inbox_config

    return bool(load_inbox_config(config_path).enabled)


def _subsystem_enabled(name: str) -> bool:
    """Best-effort: is this subsystem configured to run?

    A subsystem DISABLED (or unconfigured) must not raise a cessation/never-started
    alert: a disabled subsystem's stopped or absent pulse is intentional, not a death.
    Covers ego (``ego`` config ``.enabled``) and inbox (``inbox_monitor.yaml`` present +
    ``.enabled``); every other name defaults True (fail toward surfacing — only
    subsystems with a real disable switch are checked)."""
    try:
        if name == "ego":
            from genesis.ego.config import load_ego_config

            return bool(load_ego_config().enabled)
        if name == "inbox":
            return _inbox_enabled()
    except Exception:
        logger.debug("subsystem-enabled read failed for %s", name, exc_info=True)
    return True


def _newest_valid_ts(candidates_newest_first, *, now: datetime) -> tuple[str | None, bool]:
    """Pick the newest USABLE heartbeat ISO from an ordered (newest-first) iterable.

    Returns ``(iso, saw_any)``: *iso* is the candidate with the newest PARSED UTC
    instant that is no more than the skew tolerance in the future — compared BY
    INSTANT (via the shared ``parse_iso_utc``, which normalizes naive values), NOT by
    the caller's textual order, since a textual ``ORDER BY`` is not chronological when
    rows carry different UTC offsets (e.g. ``15:00+00:00`` sorts ahead of the actually-
    later ``11:30-04:00``). *saw_any* is True iff any candidate string was seen at all
    (used to tell a corrupt-but-present pulse — ``unknown`` — from a fresh install —
    ``no_heartbeat``). A materially-future or unparseable candidate is never chosen."""
    bound = now + timedelta(minutes=FUTURE_SKEW_TOLERANCE_MINUTES)
    saw_any = False
    best_iso: str | None = None
    best_dt: datetime | None = None
    for iso in candidates_newest_first:
        if not iso:
            continue
        saw_any = True
        dt = parse_iso_utc(iso)
        if dt is not None and dt <= bound and (best_dt is None or dt > best_dt):
            best_iso, best_dt = iso, dt
    return best_iso, saw_any


async def _post_resume_grace_active(db, *, name: str, now: datetime, interval_s: int) -> bool:
    """True if Genesis resumed from a pause within this subsystem's expected interval
    (+ a scheduler-timing buffer).

    A pause-gated subsystem's pulse legitimately stops during a pause and does not
    refresh until its next cycle AFTER resume — so an overdue pulse in that window is a
    bounded post-resume transient, NOT a death (``observability/liveness.py`` documents
    that any PUSH alert built on a pulse must anchor to a resume/boot grace). Reads the
    latest durable ``runtime``/``resume`` event, so it works cross-process (the standalone
    health MCP sees the same events table). No resume on record → no grace."""
    if db is None:
        return False
    try:
        from genesis.db.crud import events as events_crud

        rows = await events_crud.query(
            db, subsystem="runtime", event_type="resume", limit=_HEARTBEAT_SCAN_LIMIT
        )
        # Same textual-ORDER-BY trap #240 fixes for heartbeats: pick the newest resume
        # by PARSED instant, not textual order (resume ts are UTC today, but don't rely
        # on that invariant here either).
        resume_iso, _ = _newest_valid_ts([r.get("timestamp") for r in rows], now=now)
        resume_dt = parse_iso_utc(resume_iso) if resume_iso else None
        if resume_dt is None:
            return False
        grace_s = interval_s + 120  # one expected interval + scheduler-timing slack
        return 0 <= (now - resume_dt).total_seconds() < grace_s
    except (aiosqlite.Error, ImportError, AttributeError, TimeoutError):
        logger.debug("post-resume grace check failed for %s", name, exc_info=True)
        return False


# Subsystems that emit NO bootstrap "start" pulse — their first heartbeat lands a
# full check-interval after boot, so "manifest ok + no pulse yet" is the HONEST state
# until then. Use the overdue threshold (not interval+60) as the never_started grace
# for these to avoid a boot-time flap. ego/awareness/surplus emit a start pulse at
# bootstrap, so the tighter interval+60 grace is safe for them.
_NO_BOOT_PULSE_SUBSYSTEMS = frozenset({"inbox"})


def _never_started_grace_s(name: str) -> float:
    """Grace before a manifest-``ok`` but never-pulsed subsystem reads never_started.

    Mirrors the ego dashboard tile (``interval_s + 60``) for start-pulse subsystems;
    for a no-boot-pulse subsystem (inbox) uses the overdue threshold so a healthy one
    that simply hasn't reached its first check interval is not falsely flagged."""
    interval_s, overdue_s = HEARTBEAT_EXPECTED[name]
    if name in _NO_BOOT_PULSE_SUBSYSTEMS:
        return float(overdue_s)
    return float(interval_s + 60)


def _never_started_verdict(
    name: str, *, now: datetime, paused: bool | None = None
) -> dict:
    """No usable pulse exists for *name*: decide fresh-install empty-state (benign
    ``no_heartbeat``) vs a genuine failed / never-pulsed start (``never_started``),
    using the persisted bootstrap manifest (``_read_persisted_manifest``).

    Fails BENIGN (``no_heartbeat``) whenever never_started cannot be confidently
    established — a deliberately-disabled/unconfigured subsystem, an absent/unreadable
    manifest, a subsystem absent from the manifest, an unparseable boot timestamp, a
    still-within-grace ``ok``, or a paused pause-gated subsystem whose ONLY silence is a
    stopped pulse (``ok``) — so a fresh install is never spuriously alarmed. A genuine
    init fault (``failed:``/``degraded``) alarms even while paused, since pause cannot
    cause it. The boot manifest is written cross-process by ``runtime/_capabilities.py``
    (this MCP process does not bootstrap), so this is the only liveness-independent
    signal that tells "failed to start" from "freshly installed, never run"."""
    benign = {"status": "no_heartbeat", "last_seen": None}
    # Deliberately-off / unconfigured (ego config, or inbox with no yaml / disabled) —
    # its absent pulse is intentional, not a death.
    if not _subsystem_enabled(name):
        return benign
    mf = _read_persisted_manifest()
    if not mf:
        return benign  # no manifest at all (missing/unreadable) → protect fresh installs
    st = (mf.get("manifest") or {}).get(name)
    if not isinstance(st, str):
        return benign  # subsystem absent from the manifest → fresh / unknown
    # init raised or short-circuited: recorded as "failed: <msg>" (prefix) or "degraded".
    # A real init fault is INDEPENDENT of pause — pause is a runtime toggle applied AFTER
    # bootstrap, so it cannot cause an init failure — so surface it even while paused
    # (suppressing it would silence a genuinely-broken start behind an operator pause).
    if st.startswith("failed") or st == "degraded":
        return {"status": "never_started", "last_seen": None, "reason": "init-failed"}
    # Registered ok but has never pulsed: a death only once past a boot grace (its first
    # pulse may legitimately still be pending just after boot).
    if st == "ok":
        # ONLY the ok-but-silent pulse is excused by pause: a pause-gated subsystem
        # (surplus/inbox) emits its pulse after its loop's ``if paused: return`` guard,
        # so a globally-paused Genesis legitimately has zero pulses for it — intentional
        # silence, not a never-started death. Mirrors the overdue-branch pause downgrade.
        # (ego/dashboard pulse THROUGH pause, so they are never suppressed.)
        if name in _PAUSE_GATED_HEARTBEATS:
            is_paused = paused if paused is not None else _read_global_paused()
            if is_paused:
                return benign
        persisted_at = parse_iso_utc(mf.get("persisted_at"))
        if persisted_at is None:
            return benign  # can't establish the grace clock → benign
        if (now - persisted_at).total_seconds() > _never_started_grace_s(name):
            return {
                "status": "never_started",
                "last_seen": None,
                "reason": "started-silent",
            }
    return benign  # within grace, or an unrecognized manifest status → benign


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

    _now = datetime.now(UTC)

    # Durable candidates: a WINDOW of the newest heartbeat rows (not just LIMIT 1).
    # A corrupt / materially-future timestamp sorts FIRST under the events table's
    # TEXT ``ORDER BY timestamp DESC``, so a single-row read could pin the verdict on
    # a bad row forever; scanning a window lets ``_newest_valid_ts`` skip past the bad
    # rows to the newest genuinely-usable pulse (#9). A window with only future /
    # unparseable rows → unknown (surfaced, per the liveness contract), never silent.
    db_candidates: list[str | None] = []
    if db is not None:
        try:
            from genesis.db.crud import events as events_crud

            rows = await events_crud.query(
                db,
                subsystem=name,
                event_type="heartbeat",
                limit=_HEARTBEAT_SCAN_LIMIT,
            )
            db_candidates = [r.get("timestamp") for r in rows]
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

    # In-memory ring: a fresher not-yet-persisted pulse (the DB write is async),
    # newest-first.
    ring_candidates: list[str | None] = []
    if _event_bus is not None and hasattr(_event_bus, "_ring"):
        for event in reversed(_event_bus._ring):
            sub_val = (
                event.subsystem.value
                if hasattr(event.subsystem, "value")
                else str(event.subsystem)
            )
            if sub_val == name and event.event_type == "heartbeat":
                ring_candidates.append(event.timestamp)

    # Newest USABLE pulse across both sources — freshest wins (P2-2), naive values
    # normalized (fixes mixed-awareness TypeError), materially-future / unparseable
    # skipped. saw_* records whether a candidate string existed at all, so a
    # corrupt-but-present pulse (unknown) is told from a fresh install (no_heartbeat).
    db_ts, saw_db = _newest_valid_ts(db_candidates, now=_now)
    ring_ts, saw_ring = _newest_valid_ts(ring_candidates, now=_now)
    last_ts = None
    last_dt: datetime | None = None
    for cand in (db_ts, ring_ts):
        if cand is None:
            continue
        cand_dt = parse_iso_utc(cand)
        if cand_dt is not None and (last_dt is None or cand_dt > last_dt):
            last_ts, last_dt = cand, cand_dt

    if last_ts is None or last_dt is None:
        # No usable pulse. A candidate WAS present but unusable → unknown (can't
        # confirm liveness). Nothing at all → either a fresh-install empty-state
        # (no_heartbeat, benign) OR a subsystem that failed to start / registered
        # but never pulsed (never_started) — distinguished via the persisted boot
        # manifest (#10). Fail benign whenever never_started can't be established.
        if saw_db or saw_ring:
            return {"status": "unknown", "last_seen": None}
        return _never_started_verdict(name, now=_now, paused=paused)

    age_s = (_now - last_dt).total_seconds()

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
        # Just resumed from a >threshold pause: the pulse hasn't had a cycle to refresh
        # yet → bounded transient, not a death. Non-alerting "resuming" verdict.
        if await _post_resume_grace_active(db, name=name, now=_now, interval_s=_interval_s):
            return {"status": "resuming", "last_seen": last_ts, "age_seconds": round(age_s, 1)}
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
