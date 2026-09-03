"""Tests for provider failure escalation — breaker trip → observation creation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.observability.events import GenesisEventBus
from genesis.observability.types import GenesisEvent, Severity, Subsystem
from genesis.routing.circuit_breaker import CircuitBreaker
from genesis.routing.escalation import _TRIP_THRESHOLD, ProviderEscalation
from genesis.routing.types import ErrorCategory, ProviderConfig


def _provider(name: str = "test-provider") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        provider_type="openrouter",
        model_id="test/model",
        is_free=False,
        rpm_limit=10,
        open_duration_s=120,
    )


def _make_event(provider: str = "test-provider") -> GenesisEvent:
    return GenesisEvent(
        subsystem=Subsystem.ROUTING,
        severity=Severity.WARNING,
        event_type="breaker.tripped",
        message=f"Circuit breaker tripped for {provider}",
        timestamp=datetime.now(UTC).isoformat(),
        details={"provider": provider, "call_site": "test"},
    )


@pytest.fixture
def event_bus():
    return GenesisEventBus()


@pytest.fixture
def escalation(empty_db, event_bus):
    return ProviderEscalation(db=empty_db, event_bus=event_bus)


class TestTripTracking:
    """Test the state tracking logic (_on_event)."""

    async def test_no_escalation_below_threshold(self, escalation):
        """Fewer than _TRIP_THRESHOLD trips should not set escalated flag."""
        for _ in range(_TRIP_THRESHOLD - 1):
            await escalation._on_event(_make_event())

        state = escalation._state.get("test-provider")
        assert state is not None
        assert state["trip_count"] == _TRIP_THRESHOLD - 1
        assert state["escalated"] is False

    async def test_escalation_at_threshold(self, escalation):
        """_TRIP_THRESHOLD trips escalate — once the observation WRITE lands.

        SUPERSEDED EXPECTATION, recorded: this used to assert `escalated` was
        True synchronously after the 5th trip. The flag was set BEFORE the
        deferred DB write, whose errors are swallowed — so a transient failure
        permanently convinced the process the row existed and no later trip ever
        retried (#1573 Codex P2). The flag now means "the row exists" and is set
        by `_create_observation` on success, so the test awaits the write like
        the runtime does.
        """
        import asyncio

        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event())

        state = escalation._state["test-provider"]
        assert state["trip_count"] == _TRIP_THRESHOLD
        assert state["first_trip_at"] is not None
        pending = [t for t in asyncio.all_tasks()
                   if t.get_name().startswith("escalation-obs-")]
        assert pending, "the 5th trip did not schedule the observation write"
        await asyncio.gather(*pending)
        assert state["escalated"] is True, (
            "a successful observation write did not mark the provider escalated"
        )

    async def test_a_failed_observation_write_is_retried_on_the_next_trip(
        self, escalation, empty_db, monkeypatch
    ):
        """A transient DB error at the 5th trip must not silence the outage forever.

        REGRESSION (#1573 Codex P2, `:326`). `escalated` was set BEFORE the
        deferred write, whose exceptions `_create_observation` swallows — so one
        transient failure permanently convinced the process the row existed, no
        later trip retried, and the outage never became visible to any surface.
        The flag now means "the row exists"; only the create's success path sets
        it, so the next trip finds it False and retries.
        """
        import asyncio

        import genesis.db.crud.observations as obs_crud

        calls = {"n": 0}
        real_create = obs_crud.create

        async def flaky_create(db, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated transient DB failure")
            return await real_create(db, **kwargs)

        monkeypatch.setattr(obs_crud, "create", flaky_create)

        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event("prov-retry"))
        await asyncio.gather(
            *(t for t in asyncio.all_tasks()
              if t.get_name() == "escalation-obs-prov-retry"),
            return_exceptions=True,
        )
        assert escalation._state["prov-retry"]["escalated"] is False, (
            "a swallowed create failure still marked the provider escalated — "
            "no later trip will ever retry and the outage stays invisible"
        )

        # The 6th trip retries, and this time the write lands.
        await escalation._on_event(_make_event("prov-retry"))
        await asyncio.gather(
            *(t for t in asyncio.all_tasks()
              if t.get_name() == "escalation-obs-prov-retry"),
            return_exceptions=True,
        )
        assert escalation._state["prov-retry"]["escalated"] is True
        cur = await empty_db.execute(
            "SELECT COUNT(*) FROM observations WHERE content_hash = ?",
            (escalation._provider_content_hash("prov-retry"),),
        )
        assert (await cur.fetchone())[0] == 1, "the retried write did not land"

    async def test_separate_providers_tracked_independently(self, escalation):
        """Different providers each have their own trip counter."""
        import asyncio

        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event("provider-a"))
        for _ in range(_TRIP_THRESHOLD - 1):
            await escalation._on_event(_make_event("provider-b"))

        # `escalated` is set by the deferred write on success (see the
        # threshold test above) — settle provider-a's before asserting.
        pending = [t for t in asyncio.all_tasks()
                   if t.get_name() == "escalation-obs-provider-a"]
        await asyncio.gather(*pending)
        assert escalation._state["provider-a"]["escalated"] is True
        assert escalation._state["provider-b"]["escalated"] is False


class TestRecovery:
    async def test_recovery_clears_state(self, escalation):
        """record_recovery should clear the provider's tracking state."""
        for _ in range(3):
            await escalation._on_event(_make_event())

        assert "test-provider" in escalation._state
        escalation.record_recovery("test-provider")
        assert "test-provider" not in escalation._state

    async def test_recovery_unknown_provider_no_error(self, escalation):
        """Recovering an untracked provider should not raise."""
        escalation.record_recovery("nonexistent")

    async def _make_pf(self, escalation, empty_db, provider, oid):
        # Raw INSERT (the repo's obs-test convention) so setup does not depend on
        # obs_crud.create being unmocked under the full suite. The content_hash
        # matches what _resolve_observation resolves on.
        await empty_db.execute(
            "INSERT INTO observations "
            "(id, source, type, content, priority, resolved, content_hash, created_at) "
            "VALUES (?, 'routing', 'provider_failure', ?, 'high', 0, ?, datetime('now'))",
            (oid, f"{provider} failing", escalation._provider_content_hash(provider)),
        )
        await empty_db.commit()

    async def _pf_state(self, empty_db, oid):
        # Raw read of (resolved, resolution_notes) — independent of obs_crud so the
        # assertion can't be defeated by a leaked CRUD mock elsewhere in the suite.
        cur = await empty_db.execute(
            "SELECT resolved, resolution_notes FROM observations WHERE id = ?",
            (oid,),
        )
        return await cur.fetchone()

    async def test_recovery_resolves_provider_observation(self, escalation, empty_db):
        """record_recovery resolves THIS provider's unresolved provider_failure obs."""
        await self._make_pf(escalation, empty_db, "prov-x", "pf-x")
        await escalation._resolve_observation("prov-x")
        row = await self._pf_state(empty_db, "pf-x")
        assert row["resolved"] == 1
        assert "recovered" in (row["resolution_notes"] or "")

    async def test_recovery_does_not_resolve_other_providers(self, escalation, empty_db):
        """A recovered provider must NOT resolve a different (still-down) provider."""
        await self._make_pf(escalation, empty_db, "prov-a", "pf-a")
        await self._make_pf(escalation, empty_db, "prov-b", "pf-b")
        await escalation._resolve_observation("prov-a")
        assert (await self._pf_state(empty_db, "pf-a"))["resolved"] == 1
        assert (await self._pf_state(empty_db, "pf-b"))["resolved"] == 0

    async def test_record_recovery_schedules_resolve_task(self, escalation, empty_db):
        """record_recovery (running loop) schedules + completes the resolve task."""
        import asyncio

        await self._make_pf(escalation, empty_db, "prov-y", "pf-y")
        escalation.record_recovery("prov-y")
        pending = [t for t in asyncio.all_tasks() if t.get_name() == "escalation-resolve-prov-y"]
        assert pending, "record_recovery did not schedule the resolve task"
        await asyncio.gather(*pending)
        assert (await self._pf_state(empty_db, "pf-y"))["resolved"] == 1

    async def test_record_recovery_no_running_loop_no_raise(self, escalation):
        """record_recovery from a sync/no-loop context (worker thread) must not raise."""
        import asyncio

        # Runs the sync method in a thread with no running loop → guard returns.
        await asyncio.to_thread(escalation.record_recovery, "test-provider")


class TestEventFiltering:
    async def test_ignores_non_breaker_events(self, escalation):
        """Events other than breaker.tripped should be ignored."""
        event = GenesisEvent(
            subsystem=Subsystem.ROUTING,
            severity=Severity.WARNING,
            event_type="provider.fallback",
            message="fallback happened",
            timestamp=datetime.now(UTC).isoformat(),
            details={"provider": "test"},
        )
        await escalation._on_event(event)
        assert len(escalation._state) == 0


class TestEventBusIntegration:
    async def test_attach_subscribes_to_bus(self, escalation, event_bus):
        """attach() should register as a listener on the event bus."""
        initial_count = len(event_bus._listeners)
        escalation.attach()
        assert len(event_bus._listeners) == initial_count + 1


class TestTaskFailureReporting:
    """The tracked_task swap (reflex A4): a crash in a deferred escalation
    task must land on the event bus as task.failed — the log-only
    _on_task_done callback it replaced reported to nobody. The inner DB
    helpers swallow their own errors, so these tests inject the failure at
    the coroutine boundary (the escape path the wrapper exists for)."""

    async def _settle(self):
        import asyncio

        for _ in range(10):
            await asyncio.sleep(0)

    def _capture(self, event_bus):
        captured: list = []

        async def listener(event):
            captured.append(event)

        event_bus.subscribe(listener, min_severity=Severity.ERROR)
        return captured

    async def test_create_observation_crash_emits_task_failed(
        self, escalation, event_bus, monkeypatch
    ):
        captured = self._capture(event_bus)

        async def _boom(provider, state):
            raise RuntimeError("obs write exploded")

        monkeypatch.setattr(escalation, "_create_observation", _boom)
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event())
        await self._settle()

        failed = [e for e in captured if e.event_type == "task.failed"]
        assert len(failed) == 1
        assert failed[0].details["task_name"] == "escalation-obs-test-provider"
        assert failed[0].details["error_type"] == "RuntimeError"
        assert failed[0].subsystem == Subsystem.ROUTING

    async def test_resolve_observation_crash_emits_task_failed(
        self, escalation, event_bus, monkeypatch
    ):
        captured = self._capture(event_bus)

        async def _boom(provider):
            raise RuntimeError("resolve exploded")

        monkeypatch.setattr(escalation, "_resolve_observation", _boom)
        # Seed state so record_recovery has something to clear, then recover.
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event())
        escalation.record_recovery("test-provider")
        await self._settle()

        failed = [
            e
            for e in captured
            if e.event_type == "task.failed"
            and e.details.get("task_name") == "escalation-resolve-test-provider"
        ]
        assert len(failed) == 1
        assert failed[0].details["error_type"] == "RuntimeError"

    async def test_successful_task_emits_nothing(self, escalation, event_bus):
        captured = self._capture(event_bus)
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event())
        await self._settle()
        assert [e for e in captured if e.event_type == "task.failed"] == []


class TestCircuitBreakerRecoveryCallback:
    def test_on_recovery_called_on_full_recovery(self):
        """on_recovery fires when breaker transitions to CLOSED with trip_count reset."""
        from genesis.routing.types import ProviderState

        recoveries = []
        cb = CircuitBreaker(
            _provider("test"),
            failure_threshold=1,
            success_threshold=1,
            on_recovery=lambda name: recoveries.append(name),
        )
        cb.record_failure(ErrorCategory.TRANSIENT)
        assert cb._trip_count == 1

        cb._state = ProviderState.HALF_OPEN
        cb.record_success()
        assert cb._trip_count == 0
        assert recoveries == ["test"]

    def test_on_recovery_not_called_without_prior_trips(self):
        """on_recovery should NOT fire on a normal success (no prior trips)."""
        recoveries = []
        cb = CircuitBreaker(
            _provider("test"),
            on_recovery=lambda name: recoveries.append(name),
        )
        cb.record_success()
        assert recoveries == []


class TestDeadProviderNotification:
    """One Telegram when a provider has been down long enough to matter, then quiet.

    The gap this closes: a provider dead for days produced NO user-facing ping.
    `_create_observation` writes `priority="high"`, but the Telegram job
    (`outreach/scheduler.py::_critical_observations_job`) polls only for
    `priority="critical"`; the sibling `call_site:` alert is a WARNING, and
    `outreach/health_outreach.py` sends only whitelisted CRITICALs. So the
    dashboard knew and nothing reached the user.

    Deliberately NOT solved by raising the existing observation to critical: it
    fires at ~10 minutes (5 trips), which would page on every transient blip —
    the alert fatigue that made the original incident inaudible.
    """

    async def _seed_failure_obs(self, escalation, empty_db, provider, oid, *, age_s: int):
        """An unresolved provider_failure row aged `age_s` seconds.

        Its created_at is the outage clock — the same row `_resolve_observation`
        clears on recovery, so the age is real elapsed downtime rather than
        anything this class tracks in memory.
        """
        await empty_db.execute(
            "INSERT INTO observations "
            "(id, source, type, content, priority, resolved, content_hash, created_at) "
            "VALUES (?, 'routing', 'provider_failure', ?, 'high', 0, ?, "
            "datetime('now', ?))",
            (
                oid,
                f"{provider} failing",
                escalation._provider_content_hash(provider),
                f"-{age_s} seconds",
            ),
        )
        await empty_db.commit()

    async def _criticals(self, empty_db, provider, escalation):
        cur = await empty_db.execute(
            "SELECT id, priority, content, resolved FROM observations "
            "WHERE content_hash = ? ORDER BY created_at",
            (escalation._notify_content_hash(provider),),
        )
        return await cur.fetchall()

    async def test_no_notification_before_the_floor(self, escalation, empty_db):
        """Under an hour is a blip. Most breaker trips resolve themselves."""
        await self._seed_failure_obs(escalation, empty_db, "prov-y", "pf-y", age_s=600)
        await escalation._maybe_notify("prov-y")
        assert await self._criticals(empty_db, "prov-y", escalation) == []

    async def test_one_notification_past_the_floor(self, escalation, empty_db):
        await self._seed_failure_obs(escalation, empty_db, "prov-y", "pf-y", age_s=7200)
        await escalation._maybe_notify("prov-y")
        rows = await self._criticals(empty_db, "prov-y", escalation)
        assert len(rows) == 1
        assert rows[0]["priority"] == "critical"
        assert "prov-y" in rows[0]["content"]

    async def test_repeated_trips_do_not_re_notify(self, escalation, empty_db):
        """The whole point of 'then quiet'."""
        await self._seed_failure_obs(escalation, empty_db, "prov-y", "pf-y", age_s=7200)
        for _ in range(5):
            await escalation._maybe_notify("prov-y")
        assert len(await self._criticals(empty_db, "prov-y", escalation)) == 1

    async def test_a_restart_does_not_re_notify(self, escalation, event_bus, empty_db):
        """In-memory state is lost on restart; the DEDUP must not be.

        `skip_if_duplicate` keys on an UNRESOLVED row, so the still-open
        notification suppresses a second one even from a process that has never
        seen this provider before.
        """
        await self._seed_failure_obs(escalation, empty_db, "prov-y", "pf-y", age_s=7200)
        await escalation._maybe_notify("prov-y")
        fresh = ProviderEscalation(db=empty_db, event_bus=event_bus)
        await fresh._maybe_notify("prov-y")
        assert len(await self._criticals(empty_db, "prov-y", escalation)) == 1

    async def test_recovery_then_re_death_notifies_again(self, escalation, empty_db):
        """A genuine recovery must re-arm the notification.

        Otherwise the FIRST outage in a provider's life is the only one the user
        ever hears about.
        """
        await self._seed_failure_obs(escalation, empty_db, "prov-y", "pf-y", age_s=7200)
        await escalation._maybe_notify("prov-y")
        await escalation._resolve_observation("prov-y")
        rows = await self._criticals(empty_db, "prov-y", escalation)
        assert all(r["resolved"] == 1 for r in rows), "recovery left the notify row open"

        await self._seed_failure_obs(escalation, empty_db, "prov-y", "pf-y2", age_s=7200)
        await escalation._maybe_notify("prov-y")
        assert len(await self._criticals(empty_db, "prov-y", escalation)) == 2

    async def test_no_failure_record_means_no_notification(self, escalation, empty_db):
        """No outage clock to read → say nothing. Absence of evidence is not an outage."""
        await escalation._maybe_notify("prov-nothing")
        assert await self._criticals(empty_db, "prov-nothing", escalation) == []

    async def test_another_providers_outage_does_not_notify_for_this_one(
        self, escalation, empty_db
    ):
        """The age must come from THIS provider's row, not the oldest row present."""
        await self._seed_failure_obs(escalation, empty_db, "prov-old", "pf-old", age_s=99999)
        await self._seed_failure_obs(escalation, empty_db, "prov-new", "pf-new", age_s=60)
        await escalation._maybe_notify("prov-new")
        assert await self._criticals(empty_db, "prov-new", escalation) == []

    async def test_a_too_young_outage_does_not_suppress_the_later_notification(
        self, escalation, empty_db
    ):
        """A below-floor check must not prevent the later notification.

        SUPERSEDED EXPECTATION, recorded: this test used to pin that the
        in-memory `notified` flag was only set on a real WRITE — because setting
        it on a mere RUN silenced the provider forever. That flag no longer
        exists; the redesign deleted it, and with it the whole class of "the
        suppressor outlived what it described" defects (a replacement outage
        marked notified by an in-flight task was the fourth of them). The
        invariant this test protects is unchanged and now holds by construction:
        checking early writes nothing, and nothing an early check does can
        suppress the later one — the only dedup is `skip_if_duplicate` on an
        UNRESOLVED row, and an early check creates no row.
        """
        await self._seed_failure_obs(escalation, empty_db, "prov-z", "pf-z1", age_s=600)
        await escalation._maybe_notify("prov-z")
        assert await self._criticals(empty_db, "prov-z", escalation) == []

        # Same provider, now genuinely old.
        await empty_db.execute("DELETE FROM observations WHERE id = 'pf-z1'")
        await empty_db.commit()
        await self._seed_failure_obs(escalation, empty_db, "prov-z", "pf-z2", age_s=7200)
        await escalation._maybe_notify("prov-z")
        assert len(await self._criticals(empty_db, "prov-z", escalation)) == 1, (
            "an earlier below-floor check suppressed the real notification"
        )

    async def test_trip_events_never_drive_the_notification(
        self, escalation, empty_db
    ):
        """WIRING, inverted: a `breaker.tripped` event must NOT schedule a notify.

        SUPERSEDED EXPECTATION, recorded: this test used to assert the opposite
        — that a trip event reached `_maybe_notify`. That wiring was the root
        generator of four review defects: "has an hour passed" is a CLOCK
        question, and answering it from trip events made delivery depend on
        traffic (the 5th-trip check is below the floor BY CONSTRUCTION, so a
        provider whose traffic stopped was never reported at all — found
        independently by two reviewers). The notification is now driven by the
        awareness tick via `sweep_due_notifications`. This test pins the
        DELETION: if a trip ever schedules a notify task again, the starvation
        class is back.
        """
        import asyncio

        await self._seed_failure_obs(escalation, empty_db, "prov-w", "pf-w", age_s=7200)
        for _ in range(_TRIP_THRESHOLD + 1):
            await escalation._on_event(_make_event("prov-w"))

        names = {t.get_name() for t in asyncio.all_tasks()}
        assert not any(n.startswith("escalation-notify-") for n in names), (
            "a breaker-trip event scheduled a notification check — the "
            "trip-driven wiring the redesign deleted has been reintroduced"
        )
        # Settle the observation write so it cannot leak into a later test.
        await asyncio.gather(
            *(t for t in asyncio.all_tasks()
              if t.get_name().startswith("escalation-obs-"))
        )

    async def test_the_sweep_drives_the_notification_end_to_end(
        self, escalation, empty_db
    ):
        """WIRING: the sweep, given only the DB, finds the outage and notifies.

        The provider name comes from the row's own content JSON — the sweep has
        no in-memory state to consult. This is the replacement for the
        trip-driven end-to-end above, and JSON content mirrors what
        `_create_observation` actually writes (the plain-string seeds elsewhere
        in this class exercise `_maybe_notify`, which takes the name as an
        argument; the sweep cannot).
        """
        import json as _json

        from genesis.routing.escalation import sweep_due_notifications

        await empty_db.execute(
            "INSERT INTO observations "
            "(id, source, type, content, priority, resolved, content_hash, created_at) "
            "VALUES (?, 'routing', 'provider_failure', ?, 'high', 0, ?, "
            "datetime('now', '-7200 seconds'))",
            (
                "pf-sweep",
                _json.dumps({"provider": "prov-sw", "message": "prov-sw failing"}),
                escalation._provider_content_hash("prov-sw"),
            ),
        )
        await empty_db.commit()

        written = await sweep_due_notifications(empty_db)
        assert written == 1, "the sweep did not notify for a due outage"
        rows = await self._criticals(empty_db, "prov-sw", escalation)
        assert len(rows) == 1
        assert rows[0]["priority"] == "critical"

        # Idempotent: a second sweep writes nothing while the first row is open.
        assert await sweep_due_notifications(empty_db) == 0
        assert len(await self._criticals(empty_db, "prov-sw", escalation)) == 1

    async def test_the_sweep_skips_rows_it_cannot_attribute(
        self, escalation, empty_db
    ):
        """Non-JSON or provider-less content is SKIPPED, never guessed at.

        The notify hash derives from the provider name, so a row the sweep
        cannot attribute is one it cannot notify about without risking naming
        the wrong provider to the user. No production writer of plain-string
        content is known (every historical `_create_observation` wrote JSON
        with a `provider` key — checked at review), so this pins the CORRUPT-row
        posture rather than a legacy migration path; the test seeds in this
        class happen to use the same shape.
        """
        from genesis.routing.escalation import sweep_due_notifications

        await self._seed_failure_obs(escalation, empty_db, "prov-x", "pf-x", age_s=7200)
        assert await sweep_due_notifications(empty_db) == 0, (
            "the sweep notified from a row whose provider it could not know"
        )

    async def test_the_sweep_survives_one_provider_failing(
        self, escalation, empty_db, monkeypatch
    ):
        """One provider's DB failure must not strand the rest of the sweep."""
        import json as _json

        import genesis.db.crud.observations as obs_crud
        from genesis.routing.escalation import sweep_due_notifications

        for name, oid in (("prov-a1", "pf-a1"), ("prov-b2", "pf-b2")):
            await empty_db.execute(
                "INSERT INTO observations "
                "(id, source, type, content, priority, resolved, content_hash, created_at) "
                "VALUES (?, 'routing', 'provider_failure', ?, 'high', 0, ?, "
                "datetime('now', '-7200 seconds'))",
                (oid, _json.dumps({"provider": name}),
                 escalation._provider_content_hash(name)),
            )
        await empty_db.commit()

        real_create = obs_crud.create

        async def flaky_create(db, **kwargs):
            # Fail ONLY the first provider's notify write (sorted order: prov-a1).
            if kwargs.get("content_hash") == escalation._notify_content_hash("prov-a1"):
                raise RuntimeError("simulated transient failure")
            return await real_create(db, **kwargs)

        monkeypatch.setattr(obs_crud, "create", flaky_create)
        written = await sweep_due_notifications(empty_db)
        assert written == 1, "the surviving provider was not notified"
        assert len(await self._criticals(empty_db, "prov-b2", escalation)) == 1

    async def test_a_stale_row_for_a_recovered_provider_never_pages(
        self, escalation, empty_db
    ):
        """The liveness gate — the fence the deleted `escalated` conjunct was.

        REGRESSION GUARD (redesign review BLOCKER). The durable row proves an
        outage HAPPENED, not that it is still happening: a half-completed
        recovery resolve deliberately leaves the FAILURE row open ("visible
        beats silent"), and without a liveness source the clock sweep would turn
        that stranded row into a false critical page claiming a multi-day outage
        — the exact reverted failure the old in-memory gate prevented. When the
        caller supplies live breaker evidence, a provider that reads recovered
        is never paged; one that reads failing still is (an unmeasured recovery
        is not a recovery, per the evidence model); one the callback cannot
        answer for is skipped — every error fails toward silence, because the
        false page is the unrecoverable direction.
        """
        import json as _json

        from genesis.routing.escalation import sweep_due_notifications

        for name, oid in (("prov-dead", "pf-dead"), ("prov-fine", "pf-fine"),
                          ("prov-gone", "pf-gone")):
            await empty_db.execute(
                "INSERT INTO observations "
                "(id, source, type, content, priority, resolved, content_hash, created_at) "
                "VALUES (?, 'routing', 'provider_failure', ?, 'high', 0, ?, "
                "datetime('now', '-7200 seconds'))",
                (oid, _json.dumps({"provider": name}),
                 escalation._provider_content_hash(name)),
            )
        await empty_db.commit()

        def still_failing(name):
            if name == "prov-gone":
                raise KeyError(name)  # dropped from config — cannot verify
            return name == "prov-dead"

        written = await sweep_due_notifications(
            empty_db, provider_still_failing=still_failing
        )
        assert written == 1, "expected exactly the still-failing provider to page"
        assert len(await self._criticals(empty_db, "prov-dead", escalation)) == 1
        assert await self._criticals(empty_db, "prov-fine", escalation) == [], (
            "a stale row for a RECOVERED provider produced a critical page — "
            "the reverted false-page failure is back"
        )
        assert await self._criticals(empty_db, "prov-gone", escalation) == [], (
            "an unverifiable provider was paged — errors must fail toward silence"
        )

    async def test_a_user_ack_silences_the_outage_until_recovery(
        self, escalation, empty_db
    ):
        """Resolving the delivered notification must not re-page five minutes later.

        REGRESSION (#1573 Codex P2 on the redesign). `skip_if_duplicate` keys on
        UNRESOLVED rows, so a dashboard ack removed the only dedup and the next
        sweep tick re-created the critical — re-paging the user for an outage
        they had just acknowledged. A RESOLVED notify row younger than the
        outage start now suppresses re-creation.
        """
        await self._seed_failure_obs(escalation, empty_db, "prov-ack", "pf-ack", age_s=7200)
        await escalation._maybe_notify("prov-ack")
        rows = await self._criticals(empty_db, "prov-ack", escalation)
        assert len(rows) == 1

        # The user acks it from the dashboard (plain resolve, user notes).
        from genesis.db.crud import observations as obs_crud

        await obs_crud.resolve(
            empty_db, rows[0]["id"],
            resolved_at="2099-01-01T00:00:00+00:00",
            resolution_notes="seen it, working on the account",
        )
        await escalation._maybe_notify("prov-ack")
        rows = await self._criticals(empty_db, "prov-ack", escalation)
        assert len([r for r in rows if r["resolved"] == 0]) == 0, (
            "the sweep re-paged an outage the user had acknowledged"
        )

    async def test_a_lever_resolve_does_not_suppress_the_re_notify(
        self, escalation, empty_db
    ):
        """The discriminator's other direction — USER-DECIDED off→on re-notify.

        Lever resolutions carry machine-written notes; only those permit a
        re-page for the same outage. Without this arm, turning the lever off
        and on would go silent, undoing the recorded decision.
        """
        await self._seed_failure_obs(escalation, empty_db, "prov-lvr", "pf-lvr", age_s=7200)
        await escalation._maybe_notify("prov-lvr")
        rows = await self._criticals(empty_db, "prov-lvr", escalation)
        assert len(rows) == 1

        from genesis.db.crud import observations as obs_crud

        await obs_crud.resolve(
            empty_db, rows[0]["id"],
            resolved_at="2099-01-01T00:00:00+00:00",
            resolution_notes="provider-outage notify lever turned off",
        )
        await escalation._maybe_notify("prov-lvr")
        rows = await self._criticals(empty_db, "prov-lvr", escalation)
        assert len([r for r in rows if r["resolved"] == 0]) == 1, (
            "a lever resolve suppressed the off→on re-notify the user decided on"
        )

    async def test_a_duplicate_failure_row_does_not_reset_the_outage_clock(
        self, escalation, empty_db
    ):
        """Two unresolved records for one provider → the OLDEST is the clock.

        `skip_if_duplicate` matches on (source, content_hash, resolved,
        origin_class), so a row written under a different origin_class — or one
        that predates this process — does not suppress a second. Reading an
        arbitrary row then picks whichever the query happens to return first; if
        that is the newest, the outage reads as seconds old and the user is
        never told. That is the same clock-destruction this change exists to
        remove, one layer up.

        Found by the end-to-end wiring test rather than by design: driving the
        real event path produced exactly this two-row state, because
        `_create_observation` fires on the same trip.
        """
        await self._seed_failure_obs(escalation, empty_db, "prov-d", "pf-d-old", age_s=7200)
        await self._seed_failure_obs(escalation, empty_db, "prov-d", "pf-d-new", age_s=5)
        await escalation._maybe_notify("prov-d")
        rows = await self._criticals(empty_db, "prov-d", escalation)
        assert len(rows) == 1, (
            "a fresh duplicate failure row reset the outage clock and suppressed "
            "the notification"
        )

    async def test_a_failed_resolve_leaves_the_notification_re_armable(
        self, escalation, empty_db, monkeypatch
    ):
        """A partial resolve must fail VISIBLY, never silently.

        REGRESSION (#1573 Codex P2). Recovery resolves two hashes in two
        separately committed statements. Failure-first meant a failure on the
        second statement left the NOTIFY row unresolved — and `skip_if_duplicate`
        keys on an unresolved row of the same hash, so that provider could never
        notify again until some later recovery happened to succeed. Silent.

        Notify-first inverts which row survives: the FAILURE row stays open,
        which the dashboard shows as a provider still failing and the next
        recovery clears. Visible beats silent at identical cost.
        """
        import genesis.db.crud.observations as obs_crud

        await self._seed_failure_obs(escalation, empty_db, "prov-f", "pf-f", age_s=7200)
        await escalation._maybe_notify("prov-f")
        assert len(await self._criticals(empty_db, "prov-f", escalation)) == 1

        calls = {"n": 0}
        real = obs_crud.resolve_by_content_hash

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated transient failure on the second resolve")
            return await real(*args, **kwargs)

        monkeypatch.setattr(obs_crud, "resolve_by_content_hash", flaky)
        await escalation._resolve_observation("prov-f")
        assert calls["n"] == 2, "precondition: both resolve statements were attempted"

        # The provider is still dead. It must still be able to tell the user.
        await escalation._maybe_notify("prov-f")
        assert len(await self._criticals(empty_db, "prov-f", escalation)) == 2, (
            "a half-completed resolve permanently silenced this provider"
        )

    async def test_recovery_between_the_read_and_the_write_writes_no_alert(
        self, escalation, empty_db, monkeypatch
    ):
        """A provider that recovers mid-check must not be reported as dead.

        REGRESSION (#1573, raised independently by two reviewers). `_maybe_notify`
        reads the unresolved failure row, computes elapsed, and only then calls
        `observations.create`. Those are separated by awaits, so a recovery can
        land in between: `_resolve_observation` clears both hashes, then the
        create fires anyway — `skip_if_duplicate` sees no UNRESOLVED notify row
        and happily writes one. The result is a critical observation, and a
        Telegram, claiming a multi-day outage for a provider that just recovered.

        The race is driven deterministically by resolving inside the query call,
        which is exactly the window under test — rather than by sleeping and
        hoping the interleaving occurs.
        """
        import genesis.db.crud.observations as obs_crud

        await self._seed_failure_obs(escalation, empty_db, "prov-race", "pf-race", age_s=7200)

        # Patch the outage-clock read seam the notify path actually uses
        # (the hash-scoped unresolved_by_hash — NOT the generic query(),
        # which this path deliberately no longer calls).
        real_read = obs_crud.unresolved_by_hash

        async def racing_read(*args, **kwargs):
            rows = await real_read(*args, **kwargs)
            # Recovery lands HERE: after the read, before the write.
            await escalation._resolve_observation("prov-race")
            return rows

        monkeypatch.setattr(obs_crud, "unresolved_by_hash", racing_read)
        await escalation._maybe_notify("prov-race")

        rows = await self._criticals(empty_db, "prov-race", escalation)
        unresolved = [r for r in rows if r["resolved"] == 0]
        assert unresolved == [], (
            "a provider that recovered mid-check was still reported dead — "
            f"{len(unresolved)} unresolved critical row(s) written after recovery"
        )


class TestTripWindowReanchor:
    """A stale in-memory incident must not lend its anchor to a new one.

    The state entry dies only on FULL recovery (trip_count reaching 0, which
    a flapping provider never does with success_threshold=2), so without the
    24h trip window `first_trip_at` could be weeks old when a fresh incident
    escalates — writing an observation that claims a weeks-long outage.
    """

    async def test_a_daylong_gap_starts_a_fresh_incident(self, empty_db, event_bus):
        from datetime import timedelta

        t = {"now": datetime(2026, 9, 1, 12, 0, tzinfo=UTC)}
        esc = ProviderEscalation(
            db=empty_db, event_bus=event_bus, clock=lambda: t["now"],
        )
        await esc._on_event(_make_event("prov-gap"))
        first_anchor = esc._state["prov-gap"]["first_trip_at"]
        t["now"] += timedelta(hours=25)
        await esc._on_event(_make_event("prov-gap"))
        state = esc._state["prov-gap"]
        assert state["trip_count"] == 1, "the stale count must not carry over"
        assert state["first_trip_at"] == t["now"].isoformat()
        assert state["first_trip_at"] != first_anchor

    async def test_backoff_scale_gaps_never_reanchor(self, empty_db, event_bus):
        """Gaps a real continuing outage produces (backoff cap 30min/4h plus
        idle-traffic stretches, widest measured ~12h) must accumulate — the
        window exists for STALE incidents, not slow ones."""
        from datetime import timedelta

        t = {"now": datetime(2026, 9, 1, 12, 0, tzinfo=UTC)}
        esc = ProviderEscalation(
            db=empty_db, event_bus=event_bus, clock=lambda: t["now"],
        )
        for _ in range(5):
            await esc._on_event(_make_event("prov-slow"))
            t["now"] += timedelta(hours=4)
        state = esc._state["prov-slow"]
        assert state["trip_count"] == 5
        assert state["first_trip_at"] == "2026-09-01T12:00:00+00:00"


class TestHashScopedOutageClock:
    async def test_a_crowded_unresolved_table_cannot_starve_the_clock(self, empty_db):
        """ACCEPTANCE REPLAY of the truncated-read defect: with >100 unrelated
        unresolved routing rows, the old query(limit=100)+Python-filter shape
        dropped the target provider's row out of the fetch window and read the
        outage clock as ABSENT — no notification, indistinguishable from
        healthy. The hash-scoped read must find it regardless of crowd size."""
        from genesis.db.crud import observations as obs_crud
        from genesis.routing.escalation import notify_provider_if_due

        started = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        target_hash = ProviderEscalation._provider_content_hash("prov-crowded")
        await obs_crud.create(
            empty_db,
            id="target-row",
            person_id=None,
            type="provider_failure",
            content=json.dumps({"provider": "prov-crowded", "first_trip_at": started}),
            source="routing",
            priority="high",
            content_hash=target_hash,
            created_at=started,
        )
        # 120 NEWER unrelated unresolved rows — under a newest-first
        # limit-100 global read, the target falls outside the window.
        for i in range(120):
            await obs_crud.create(
                empty_db,
                id=f"crowd-{i}",
                person_id=None,
                type="provider_failure",
                content=json.dumps({"provider": f"crowd-{i}"}),
                source="routing",
                priority="high",
                content_hash=f"crowdhash-{i}",
                created_at=datetime.now(UTC).isoformat(),
            )
        notified = await notify_provider_if_due(
            empty_db, "prov-crowded",
            provider_still_failing=lambda p: True,
        )
        assert notified is True, (
            "the outage clock was starved by unrelated unresolved rows"
        )
