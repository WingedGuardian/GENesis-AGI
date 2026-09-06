"""Provider failure escalation — creates observations when providers fail persistently.

Subscribes to the event bus for breaker.tripped events. When a provider
trips its circuit breaker N times without recovery, creates a high-priority
observation so the ego picks it up in its next cycle.

The listener MUST be fast (event bus awaits listeners sequentially).
Observation creation is deferred via tracked_task (failures land on the
event bus as task.failed — the reflex nerve — as well as in the log).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime

from genesis.observability.types import GenesisEvent, Severity, Subsystem
from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)

# Trip threshold before creating an observation.
# 5 trips ≈ 10 minutes of cycling (120s open duration × 5 cycles).
_TRIP_THRESHOLD = 5

# Consecutive trips further apart than this belong to SEPARATE incidents.
# Genuine same-outage trip gaps are bounded by the breaker backoff (30 min
# cap, 4h for quota) plus idle-traffic stretches — the widest gap measured on
# a real multi-day outage was ~12h (sparse overnight traffic on a HALF_OPEN
# breaker). 24h clears that with margin, while bounding a stale in-memory
# anchor to a day instead of weeks.
_TRIP_WINDOW_S = 24 * 3600

# How long a provider must have been continuously failing before the user is
# told. The escalation observation above fires at ~10 minutes, which is the
# right threshold for a DASHBOARD row and far too eager for a notification —
# most breaker trips resolve themselves well inside an hour, so anything shorter
# pages on noise. An earlier version of this comment cited an
# `_ONGOING_FLOOR_S` constant in the health subsystem as precedent; no such
# symbol exists anywhere in the repo, so the hour stands on the reasoning above
# rather than on a borrowed threshold.
_NOTIFY_AFTER_S = 3600.0

# Ceiling on the SPAN a message may quote. Derived, not chosen: it is the
# observations TTL (`db.crud.observations._DEFAULT_TTL`, 14 days), so past this
# the row the span is measured from has expired and nothing a reader can look up
# corroborates the number. Beyond it the wording degrades to "more than N days"
# rather than quoting a precise total no record supports. Kept as a literal
# rather than imported so this module does not take a CRUD dependency for a
# constant; the tie is asserted in the tests.
_EVIDENCE_SPAN_CAP_S = 14 * 24 * 3600


class ProviderEscalation:
    """Track per-provider failures and escalate to observations."""

    def __init__(self, db, event_bus, *, clock=None):
        self._db = db
        self._event_bus = event_bus
        self._clock = clock or (lambda: datetime.now(UTC))
        # Per-provider tracking state:
        # {name: {"trip_count": int, "first_trip_at": str, "escalated": bool}}
        self._state: dict[str, dict] = {}

    def attach(self) -> None:
        """Subscribe to routing events on the event bus."""
        self._event_bus.subscribe(self._on_event, min_severity=Severity.WARNING)
        logger.info("Provider escalation listener attached to event bus")

    async def _on_event(self, event: GenesisEvent) -> None:
        """Handle breaker.tripped events. Must be fast — runs in emit() path."""
        if event.event_type != "breaker.tripped":
            return

        provider = event.details.get("provider", "unknown")
        state = self._state.setdefault(
            provider,
            {
                "trip_count": 0,
                "first_trip_at": None,
                "escalated": False,
                "last_trip_at": None,
            },
        )

        # A wide gap is NOTED, not acted on. The state entry survives until
        # `record_recovery` clears it, so `first_trip_at` can be weeks old on a
        # provider that never fully recovers — and the escalation used to render
        # that as a weeks-long outage that never happened.
        #
        # The fix for that lives in `_describe_evidence`, not here. Trips are
        # DISCRETE events and recovery is a DISCRETE event; between two of them
        # this class has no signal about the provider's state at all, so it must
        # not infer one in either direction. Zeroing the count inferred a
        # recovery from silence (and blinded the threshold to sparse outages);
        # rendering "failing every call for N days" inferred a continuous outage
        # from the same silence. Both were the same mistake, and only the second
        # one is a real defect.
        now = self._clock()
        last_trip = state.get("last_trip_at")
        if last_trip is not None:
            try:
                gap_s = (now - datetime.fromisoformat(last_trip)).total_seconds()
            except ValueError:
                gap_s = None
            if gap_s is not None and gap_s > _TRIP_WINDOW_S:
                # OBSERVED ONLY — nothing is discarded here any more.
                #
                # This used to zero `trip_count` and clear the anchor, on the
                # theory that a long gap meant the old incident was stale. That
                # was the wrong diagnosis of the right symptom, and it cost more
                # than it bought. MEASURED: a provider failing once a week and
                # never recovering sat at trip_count=1 for 8 consecutive weeks —
                # it could never reach `_TRIP_THRESHOLD`, so no observation was
                # ever written and the user was never told it was dead
                # (Codex P1, #1632).
                #
                # Elapsed silence is not recovery evidence. The breaker can sit
                # OPEN through an idle stretch, and only `record_recovery` —
                # which requires actual successful calls — clears an incident.
                # Resetting on the clock asserted a recovery nothing witnessed.
                #
                # The symptom that motivated the reset was real, but it lives in
                # the REPORTING: the messages inferred a continuous outage from
                # sparse trip evidence. That is fixed where it happens, in
                # `_describe_evidence`, so the state can stay true.
                logger.info(
                    "Provider '%s': %.0fh since its last trip — the incident "
                    "continues (no recovery observed); evidence now reaches "
                    "back to %s",
                    provider, gap_s / 3600, state.get("first_trip_at"),
                )
        state["last_trip_at"] = now.isoformat()

        state["trip_count"] += 1
        if state["first_trip_at"] is None:
            state["first_trip_at"] = self._clock().isoformat()

        if state["trip_count"] >= _TRIP_THRESHOLD and not state["escalated"]:
            # `escalated` is set by `_create_observation` on a SUCCESSFUL write,
            # NOT here. Setting it before the deferred write meant a transient DB
            # error — which that method swallows — permanently convinced this
            # process the row existed, so no later trip ever retried and the
            # outage never became visible. The flag means "the row exists", so
            # only the code that knows it exists may set it. Several trips racing
            # here can spawn several creates; that is harmless because
            # `skip_if_duplicate` is a single guarded statement and yields at
            # most one row.
            tracked_task(
                self._create_observation(provider, state),
                name=f"escalation-obs-{provider}",
                event_bus=self._event_bus,
                subsystem=Subsystem.ROUTING,
            )

        # NO notification decision here, deliberately. "Has this outage lasted an
        # hour" is a CLOCK question, and answering it from trip events made the
        # answer depend on traffic: the check at the 5th trip is below the floor
        # BY CONSTRUCTION, so delivery relied on a later trip that may never
        # arrive. That single mismatch generated four separate defects across
        # four review rounds (starvation; a replacement outage marked notified by
        # an in-flight task; a restart-reset gate; an unbounded in-memory
        # anchor). The decision now lives in `sweep_due_notifications()`, driven
        # by the awareness tick and reading the durable row. See its docstring.

    def record_recovery(self, provider: str) -> None:
        """Called when a provider recovers (breaker → CLOSED).

        Clears in-memory tracking AND resolves the provider's lingering
        ``provider_failure`` observation. Without the resolve the pipeline is
        write-only on recovery: the row created on trip survives until its TTL
        and keeps reporting the provider as failing after it has recovered.
        The resolve is unconditional (not gated on in-memory state) so a row
        created before a restart still clears — mirrors the dead-letter
        resolve-on-drain pattern.
        """
        if provider in self._state:
            logger.info(
                "Provider '%s' recovered after %d trips — clearing escalation state",
                provider,
                self._state[provider].get("trip_count", 0),
            )
            del self._state[provider]

        # record_recovery() is called from the SYNC CircuitBreaker.record_success()
        # path. In production that runs inside the async routing call (a loop is
        # present); a sync caller (e.g. a unit test) may have none — guard so we
        # never raise there, and defer the DB resolve like _create_observation does.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "record_recovery('%s'): no running loop; skipping observation resolve",
                provider,
            )
            return
        tracked_task(
            self._resolve_observation(provider),
            name=f"escalation-resolve-{provider}",
            event_bus=self._event_bus,
            subsystem=Subsystem.ROUTING,
        )

    async def _create_observation(self, provider: str, state: dict) -> None:
        """Create a high-priority observation for a persistently failing provider."""
        from genesis.db.crud import observations

        content = json.dumps(
            {
                "provider": provider,
                "trip_count": state["trip_count"],
                "first_trip_at": state["first_trip_at"],
                # Says what the trip record SHOWS. "N times since <date>" read as
                # a continuous N-day outage on a provider that had merely tripped
                # sparsely over that span — the claim the re-anchor was added to
                # suppress by discarding state. Naming both ends of the evidence
                # and saying "observed" makes the same row honest for a burst and
                # for a once-a-week failure, so the state does not have to lie.
                "last_trip_at": state["last_trip_at"],
                "message": (
                    f"Provider '{provider}' tripped its circuit breaker "
                    f"{state['trip_count']} times between {state['first_trip_at']} "
                    f"and {state['last_trip_at']}; no recovery has been observed in "
                    f"between. Calls are falling back to other providers."
                ),
            }
        )
        # Hash on provider name — one unresolved observation per provider.
        # Shared helper so the resolve-on-recovery path computes the SAME hash.
        content_hash = self._provider_content_hash(provider)

        try:
            obs_id = await observations.create(
                self._db,
                id=str(uuid.uuid4()),
                source="routing",
                type="provider_failure",
                content=content,
                priority="high",
                category="system_health",
                # KNOWN GAP, deliberately not fixed here: this stamps the row
                # when the 5th trip lands, so the duration every consumer reads
                # is short by the escalation ramp (~15-30 min, more under
                # backoff). `first_trip_at` — the true start — is carried in the
                # content JSON below but read by nothing. Backdating this column
                # to it was tried and REVERTED: the anchor is written once per
                # state entry (:72) and cleared only by `record_recovery`, so a
                # flapping provider (success_threshold=2 means one clean call
                # does not clear a trip) keeps a weeks-old anchor and the row is
                # then born already past the 1h notification floor — a critical
                # page with a fabricated multi-day duration. Fixing this needs
                # the anchor bounded first; see the follow-up.
                created_at=self._clock().isoformat(),
                content_hash=content_hash,
                skip_if_duplicate=True,
            )
            # The row now provably exists — either we wrote it, or
            # `skip_if_duplicate` found an unresolved one already there. BOTH
            # are "escalated"; only an EXCEPTION leaves it unwritten, and that
            # path deliberately does not set the flag so a later trip retries.
            state["escalated"] = True
            if obs_id:
                logger.warning(
                    "Created observation %s for provider '%s' failure (%d trips since %s)",
                    obs_id,
                    provider,
                    state["trip_count"],
                    state["first_trip_at"],
                )
            else:
                logger.debug(
                    "Skipped duplicate observation for provider '%s'",
                    provider,
                )
        except Exception:
            logger.error(
                "Failed to create observation for provider '%s'",
                provider,
                exc_info=True,
            )

    async def _resolve_observation(self, provider: str) -> None:
        """Resolve the lingering provider_failure observation on recovery.

        Keyed on the deterministic per-provider content_hash, so only THIS
        provider's row resolves — a different, still-down provider's row is
        untouched. Idempotent (no-op when nothing matches) and non-fatal.
        """
        from genesis.db.crud import observations

        # BOTH rows: the escalation record and the user-notification record.
        # Leaving the notify row open would make the first outage in a
        # provider's life the only one the user ever hears about, because
        # `skip_if_duplicate` suppresses on an unresolved row of the same hash.
        # KNOWN, ACCEPTED (raised twice at review): if the SECOND resolve
        # fails, the stale failure row survives and will deduplicate the NEXT
        # outage's record, so that outage reports a duration measured from the
        # older row. Accepted because the earliest-row-wins clock semantics
        # knowingly tolerate stale-row persistence, the liveness gate bounds
        # the damage (no page unless the breaker really is non-closed), and
        # the row self-heals on the next successful recovery resolve. The
        # single-statement plural resolve that would close it was rejected on
        # measured probability — a failure must land between two adjacent
        # retried UPDATEs.
        # NOTIFY FIRST, deliberately. These are two separately committed
        # statements — there is no transaction facility on this connection
        # (`SerializedConnection.__aenter__` is a no-op) — so one can succeed
        # and the next fail. Order decides which row survives that. Notify-row
        # open = `skip_if_duplicate` (which keys on an UNRESOLVED row of the
        # same hash) silences this provider until some later recovery happens
        # to succeed: silent. Failure-row open = the dashboard shows a provider
        # as failing when it is not, and the next recovery clears it: visible.
        # Visible beats silent at identical cost.
        content_hashes = (
            self._notify_content_hash(provider),
            self._provider_content_hash(provider),
        )
        try:
            resolved = 0
            for content_hash in content_hashes:
                resolved += await observations.resolve_by_content_hash(
                    self._db,
                    source="routing",
                    content_hash=content_hash,
                    resolved_at=self._clock().isoformat(),
                    resolution_notes=(
                        f"auto-resolved: provider '{provider}' recovered (circuit breaker closed)"
                    ),
                )
            if resolved:
                logger.info(
                    "Auto-resolved %d provider_failure observation(s) for recovered provider '%s'",
                    resolved,
                    provider,
                )
        except Exception:
            logger.error(
                "Failed to resolve provider_failure observation for '%s'",
                provider,
                exc_info=True,
            )

    @staticmethod
    def _outage_started_at(row: dict) -> datetime | None:
        """Parse an observation's ``created_at`` into an aware UTC datetime.

        Handles both shapes that reach this table: ``create()`` writes an
        ISO string with an offset, while SQLite's ``datetime('now')`` DEFAULT
        writes a naive UTC string. A naive value is therefore UTC, not local.
        """
        raw = row.get("created_at")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    async def _maybe_notify(self, provider: str) -> None:
        """Thin delegate kept for the existing suite ONLY — no runtime caller.

        The event path no longer notifies (see `sweep_due_notifications`), and
        this shim bypasses the operator lever (it defaults priority="critical"
        and consults no config). Do not add production callers.

        The decision itself is module-level and stateless — see
        `notify_provider_if_due`. Nothing here consults `self._state`: the
        durable row is the state machine now.
        """
        await notify_provider_if_due(self._db, provider, clock=self._clock)

    @staticmethod
    def _notify_content_hash(provider: str) -> str:
        return hashlib.sha256(f"provider_dead_notify:{provider}".encode()).hexdigest()

    @staticmethod
    def _provider_content_hash(provider: str) -> str:
        """Deterministic per-provider hash — MUST match between create + resolve."""
        return hashlib.sha256(f"provider_failure:{provider}".encode()).hexdigest()


async def notify_provider_if_due(
    db,
    provider: str,
    *,
    clock=None,
    priority: str = "critical",
    provider_still_failing=None,
) -> bool:
    """Tell the user ONCE that a provider has been dead long enough to matter.

    Reads the age off the unresolved ``provider_failure`` row rather than
    this object's in-memory state, for two reasons: that row IS the outage
    clock (the same one `_resolve_observation` clears on recovery), and it
    survives a restart, which the in-memory dict does not.

    Delivery reuses the existing fire-once path rather than adding one — a
    ``priority="critical"`` observation is picked up by
    `outreach/scheduler.py::_critical_observations_job`, batched into a
    single message, and `mark_surfaced` guarantees it is never re-sent.
    `skip_if_duplicate` keys on an UNRESOLVED row with the same hash, so a
    second notification cannot be written while the first is still open —
    including from a freshly restarted process.
    """
    from genesis.db.crud import observations

    clock = clock or (lambda: datetime.now(UTC))
    failure_hash = ProviderEscalation._provider_content_hash(provider)
    try:
        # Hash-scoped read, NOT `query(resolved=False, limit=N)` + a Python
        # filter: that shape silently starves this provider once the TOTAL
        # unresolved routing population exceeds the fetch window — its clock
        # then reads as absent, indistinguishable from "provider is fine".
        # `unresolved_by_hash` is index-covered (idx_observations_content_hash)
        # and returns oldest-first, so even its own (per-hash) bound keeps
        # the earliest rows — the direction the outage clock needs.
        rows = await observations.unresolved_by_hash(
            db,
            source="routing",
            content_hash=failure_hash,
        )
    except Exception:
        logger.error(
            "could not read the outage clock for provider '%s'", provider, exc_info=True
        )
        return False

    # The outage started at the EARLIEST unresolved record, not an arbitrary
    # one. More than one can exist despite `skip_if_duplicate`: that check
    # matches on (source, content_hash, resolved, origin_class), so a row
    # written through a different origin_class — or present before this
    # provider was first seen by THIS process — does not suppress a second.
    # Taking the newest would reset the outage clock on every duplicate,
    # which is the exact failure this whole change exists to remove. The
    # min() stays in Python (not LIMIT 1 in SQL) deliberately: the oldest
    # row's content can be unparseable, and the clock should then fall to
    # the next parseable row rather than to silence.
    starts = [
        s
        for r in rows
        if (s := ProviderEscalation._outage_started_at(r)) is not None
    ]
    if not starts:
        # No unresolved failure record → nothing to measure an outage from.
        # Absence of evidence is not an outage; say nothing.
        return False
    started = min(starts)
    elapsed = (clock() - started).total_seconds()
    if elapsed < _NOTIFY_AFTER_S:
        return False

    # Re-read the failure row's state immediately before writing. The query
    # above and the create below are separated by awaits, so a recovery can
    # land between them: `_resolve_observation` clears both hashes, and the
    # create then fires anyway because `skip_if_duplicate` sees no UNRESOLVED
    # notify row — reporting a multi-day outage for a provider that just came
    # back. This does NOT close the window (there is no transaction facility
    # on this connection), it narrows it to the gap between this check and
    # the insert. Raised independently by two reviewers and reproduced by a
    # test that resolves inside the query call.
    try:
        still_failing = await observations.exists_by_hash(
            db,
            source="routing",
            content_hash=failure_hash,
            unresolved_only=True,
        )
    except Exception:
        # Fail CLOSED: an unreadable check must not authorise a user-facing
        # alert. A missed notification is recoverable on the next trip; a
        # false "your provider is dead for 5 days" is not.
        logger.error(
            "could not confirm provider '%s' is still failing; not notifying",
            provider,
            exc_info=True,
        )
        return False
    if not still_failing:
        return False

    # LIVENESS GATE — the fence the deleted `escalated` conjunct used to be.
    # The durable row proves the outage HAPPENED; it cannot prove the outage is
    # still happening, and a row STRANDED by a half-completed recovery resolve
    # (the failure hash is deliberately the survivor there — "visible beats
    # silent") would otherwise become a false critical page claiming a
    # multi-day outage. When the caller can consult live evidence — the breaker
    # registry — a provider whose breaker reads CLOSED (real successes proved
    # recovery) is never paged from a stale row. A provider whose breaker is
    # OPEN or HALF_OPEN still pages: by this system's own evidence model
    # (PR #1563), a recovery nothing has measured is not a recovery. A provider
    # the callback cannot answer for (dropped from config, callback error) is
    # SKIPPED — fail toward silence, since every defect class here fails that
    # direction and a wrong page is the unrecoverable one.
    if provider_still_failing is not None:
        try:
            if not provider_still_failing(provider):
                logger.info(
                    "outage row for '%s' is past the floor but the breaker "
                    "reads recovered — stale row, not paging",
                    provider,
                )
                return False
        except Exception:
            logger.warning(
                "could not confirm provider '%s' is still failing (liveness "
                "callback errored); not notifying",
                provider,
                exc_info=True,
            )
            return False

    # ACK SUPPRESSION — "once per outage" must survive the user resolving the
    # delivered notification. `skip_if_duplicate` keys on UNRESOLVED rows only,
    # so a dashboard ack would otherwise re-arm the sweep and re-page five
    # minutes later — alert fatigue, the disease this feature treats. A RESOLVED
    # notify row younger than the outage start means this outage was already
    # reported: suppress, UNLESS the resolution was the operator LEVER (its
    # resolution notes are machine-written and prefixed detectably), because the
    # off→on re-notify is a user-decided contract. A resolved notify row OLDER
    # than the outage start belongs to a previous outage and blocks nothing.
    try:
        from genesis.db.crud import observations as _obs

        prior = await _obs.query(
            db,
            source="routing",
            type="provider_failure",
            resolved=True,
            limit=_SWEEP_ROW_LIMIT,
        )
        notify_hash = ProviderEscalation._notify_content_hash(provider)
        for r in prior:
            if r.get("content_hash") != notify_hash:
                continue
            r_at = ProviderEscalation._outage_started_at(r)
            if r_at is None or r_at < started:
                continue  # a previous outage's notification — irrelevant
            notes = str(r.get("resolution_notes") or "")
            # MACHINE resolutions permit a re-notify; only a USER ack
            # suppresses. Three machine classes exist, each with a
            # machine-written prefix: the lever (off→on re-notify is a
            # user-decided contract), the lever upgrade, and RECOVERY's
            # auto-resolve — a genuine recovery then re-death is a NEW
            # outage and must be reported again (the original design
            # decision, pinned by test_recovery_then_re_death_notifies_again).
            if notes.startswith(
                (
                    "provider-outage notify lever",
                    "superseded: lever",
                    "auto-resolved:",
                )
            ):
                continue
            logger.debug(
                "outage for '%s' already notified and acknowledged; not re-paging",
                provider,
            )
            return False
    except Exception:
        # An unreadable ack-history must not BLOCK a due notification: the
        # failure direction here is the opposite of the liveness gate's. A
        # missed suppression re-sends a message the user has seen — annoying,
        # recoverable. A false suppression silences a real outage — the
        # unrecoverable direction for THIS check.
        logger.warning(
            "could not read ack history for '%s'; notifying anyway",
            provider,
            exc_info=True,
        )

    hours = elapsed / 3600.0
    # Bounded so the reported span cannot grow without limit. The cap is the
    # observations TTL (`_DEFAULT_TTL`, 14 days) rather than a chosen number:
    # past it the row this span is measured from has itself expired, so nothing
    # a reader can look up corroborates the figure. Beyond the cap the message
    # says "more than N days" instead of quoting a precise, uncheckable total.
    if elapsed > _EVIDENCE_SPAN_CAP_S:
        human = f"more than {_EVIDENCE_SPAN_CAP_S / 86400:.0f} days"
    else:
        human = f"{hours / 24:.1f} days" if hours >= 24 else f"{hours:.1f} hours"
    try:
        obs_id = await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="routing",
            type="provider_failure",
            content=json.dumps(
                {
                    "provider": provider,
                    "outage_started_at": started.isoformat(),
                    # "has been failing EVERY CALL for N days" was the false
                    # claim at the centre of this PR. Between two trips a week
                    # apart nothing here observed the provider at all — success
                    # only clears state after two consecutive wins, and that
                    # clears everything — so continuous total failure was an
                    # inference the evidence never supported. What IS known is
                    # that no recovery has been recorded since the row opened.
                    "message": (
                        f"Provider '{provider}' has not recovered since {human} ago — "
                        f"no successful recovery has been recorded in that time. Calls "
                        f"are falling back to other providers in each chain. If this "
                        f"provider is a paid or entitlement-gated model, the account "
                        f"may need attention."
                    ),
                }
            ),
            priority=priority,
            category="system_health",
            created_at=clock().isoformat(),
            content_hash=ProviderEscalation._notify_content_hash(provider),
            skip_if_duplicate=True,
        )
    except Exception:
        logger.error(
            "failed to write dead-provider notification for '%s'", provider, exc_info=True
        )
        return False

    if obs_id:
        logger.warning(
            "Notified user: provider '%s' has been failing for %s", provider, human
        )
    return bool(obs_id)


# Bounded per sweep. HONEST LIMIT: `observations.query` orders created_at DESC
# with no offset, so rows beyond the bound are the OLDEST — and they stay
# excluded for as long as the unresolved population exceeds the bound, which is
# starvation, not mere delay. Accepted because the population CANNOT approach
# it: `skip_if_duplicate` caps open rows at ~2 per provider (failure + notify),
# the type carries a 14-day TTL, and the measured all-time population is 27
# rows across 8 providers. The warning below exists so that if some future
# writer floods this type, the cap reads as a named condition instead of
# silence.
_SWEEP_ROW_LIMIT = 200


async def sweep_due_notifications(
    db, *, clock=None, priority: str = "critical", provider_still_failing=None
) -> int:
    """Notify for every provider whose unresolved outage has passed the floor.

    THIS REPLACES the trip-driven notification path, and the replacement is the
    point rather than an implementation detail.

    "Has this outage lasted an hour" is a CLOCK question. Answering it from
    `breaker.tripped` events made the answer depend on TRAFFIC: the check fired
    at the 5th trip is below `_NOTIFY_AFTER_S` by construction, so delivery
    relied on a later trip arriving — and if traffic to that provider stopped,
    the user was never told at all. That one mismatch generated four defects
    across four review rounds, each fixed in isolation and each replaced by the
    next: starvation when trips stop; a replacement outage marked notified by an
    in-flight task; a gate that a restart reset; an in-memory anchor with no
    decay that, once backdated, fabricated multi-day durations.

    None of those are reachable here, BY CONSTRUCTION rather than by guarding:
    this function holds no state between calls, consults no in-memory flag, and
    is driven by a clock instead of by traffic. Dedup is `skip_if_duplicate`,
    which is a single ``INSERT … WHERE NOT EXISTS`` statement (see
    `db/crud/observations.py`) and therefore race-free ACROSS PROCESSES — a
    strictly stronger guarantee than the per-process flag it replaces, and one
    that survives a restart, two overlapping sweeps, and a process dying
    mid-write.

    The provider name comes from the row's own content JSON. A row whose content
    is not JSON, or carries no provider, is SKIPPED rather than guessed at: the
    notification hash derives from the provider name, so a row we cannot
    attribute is one we cannot notify about without risking naming the wrong
    provider to the user.

    Returns the number of notifications actually written.
    """
    from genesis.db.crud import observations

    clock = clock or (lambda: datetime.now(UTC))
    try:
        rows = await observations.query(
            db,
            source="routing",
            type="provider_failure",
            resolved=False,
            limit=_SWEEP_ROW_LIMIT,
        )
    except Exception:
        logger.error("dead-provider sweep could not read outage rows", exc_info=True)
        return 0

    if len(rows) >= _SWEEP_ROW_LIMIT:
        logger.warning(
            "dead-provider sweep hit its %d-row bound; remaining rows are deferred "
            "to the next tick, not dropped",
            _SWEEP_ROW_LIMIT,
        )

    providers: set[str] = set()
    for row in rows:
        raw = row.get("content")
        try:
            blob = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            continue
        if isinstance(blob, dict):
            name = blob.get("provider")
            if isinstance(name, str) and name:
                providers.add(name)

    written = 0
    for provider in sorted(providers):
        try:
            if await notify_provider_if_due(
                db,
                provider,
                clock=clock,
                priority=priority,
                provider_still_failing=provider_still_failing,
            ):
                written += 1
        except Exception:
            # One provider's failure must not strand the rest of the sweep.
            logger.error(
                "dead-provider sweep failed for provider '%s'", provider, exc_info=True
            )
    return written
