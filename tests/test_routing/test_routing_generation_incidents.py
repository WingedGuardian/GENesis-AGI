"""Durable routing incident ownership, retirement and health-view regressions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.types import CallSiteConfig, ErrorCategory, ProviderState
from tests.test_routing.generation_helpers import config, drain_escalation_tasks, make_router


@pytest.mark.asyncio
@pytest.mark.parametrize("already_escalated", [False, True])
async def test_retirement_replacement_starts_fresh_durable_incident(tmp_path, empty_db, already_escalated):
    from genesis.observability.events import GenesisEventBus
    from genesis.observability.types import Severity, Subsystem
    from genesis.routing.escalation import ProviderEscalation, sweep_due_notifications
    from genesis.routing.provider_identity import provider_identity

    bus = GenesisEventBus()
    escalation = ProviderEscalation(empty_db, bus,
        current_identity=lambda name: registry.current_identity(name),
        current_incident_identity=lambda name: registry.current_incident_identity(name),
        incident_binding=lambda name: registry.current_incident_binding(name),
        incident_owner=lambda name, incident: registry.incident_owner(name, incident))
    registry = CircuitBreakerRegistry(config().providers, state_file=tmp_path / "incidents.json",
        on_retirement=escalation.record_retirement)
    escalation.attach()

    async def trip():
        await bus.emit(Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "test",
                       provider="provider", health_identity=provider_identity(registry.get("provider")._provider),
                       incident_identity=registry.current_incident_identity("provider"))

    for _ in range(5 if already_escalated else 4):
        await trip()
    await drain_escalation_tasks()
    old = registry.get("provider")
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    registry.update_providers(config("new-model").providers)
    incident = registry.current_incident_identity("provider")
    assert incident is not None
    await drain_escalation_tasks()
    await trip()
    assert escalation._state[("provider", incident)]["trip_count"] == 1
    assert not escalation._state[("provider", incident)]["escalated"]
    assert escalation._state[("provider", incident)]["incident_identity"] == incident
    cursor = await empty_db.execute("SELECT resolved, resolution_notes FROM observations WHERE content_hash = ?",
                                   (escalation._provider_content_hash("provider"),))
    rows = await cursor.fetchall()
    if already_escalated:
        assert rows and all(row[0] == 1 and "retired" in row[1] and "recovered" not in row[1] for row in rows)
    else:
        assert not rows
    assert await sweep_due_notifications(empty_db, current_incident_identity=registry.current_incident_identity,
                                         provider_still_failing=lambda _: True) == 0
    for _ in range(4):
        await trip()
    await drain_escalation_tasks()
    cursor = await empty_db.execute("SELECT resolved FROM observations WHERE content_hash = ?",
                                   (escalation._provider_content_hash("provider", incident),))
    assert [row[0] for row in await cursor.fetchall()] == [0]
    restored = CircuitBreakerRegistry(config("new-model").providers, state_file=tmp_path / "incidents.json")
    assert restored.current_incident_identity("provider") == incident
    # A -> B -> A does not revive historical A hashes.
    new = registry.get("provider")
    new._failure_threshold = 1
    new.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    registry.update_providers(config().providers)
    assert registry.current_incident_identity("provider") not in (None, incident)
    await drain_escalation_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["operator", "auth", "quota", "ambiguous"])
async def test_repoint_preserves_account_escalation_history(tmp_path, empty_db, hold):
    from genesis.observability.events import GenesisEventBus
    from genesis.observability.types import Severity, Subsystem
    from genesis.routing.escalation import ProviderEscalation

    bus = GenesisEventBus()
    escalation = ProviderEscalation(empty_db, bus,
        current_identity=lambda name: registry.current_identity(name),
        current_incident_identity=lambda name: registry.current_incident_identity(name),
        incident_binding=lambda name: registry.current_incident_binding(name),
        incident_owner=lambda name, incident: registry.incident_owner(name, incident))
    registry = CircuitBreakerRegistry(config().providers, state_file=tmp_path / "account.json",
        on_retirement=escalation.record_retirement)
    escalation.attach()
    for _ in range(4):
        await bus.emit(Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "test", provider="provider")
    old = registry.get("provider")
    old._failure_threshold = 1
    if hold == "operator":
        old.force_open()
    else:
        old.record_failure({"auth": ErrorCategory.PERMANENT, "quota": ErrorCategory.QUOTA_EXHAUSTED,
                            "ambiguous": ErrorCategory.TRANSIENT}[hold])
    registry.update_providers(config("new-model").providers)
    assert registry.current_incident_identity("provider") is None
    await bus.emit(Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "test", provider="provider")
    await drain_escalation_tasks()
    assert escalation._state["provider"]["trip_count"] == 5
    assert escalation._state["provider"]["escalated"]


def test_restart_retires_model_with_fresh_persisted_incident(tmp_path):
    path = tmp_path / "restart-incident.json"
    registry = CircuitBreakerRegistry(config().providers, state_file=path)
    old = registry.get("provider")
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    retired = MagicMock()
    new = CircuitBreakerRegistry(config("new-model").providers, state_file=path, on_retirement=retired)
    incident = new.current_incident_identity("provider")
    assert incident is not None
    retired.assert_called_once_with("provider", None)
    assert new.get("provider").state == ProviderState.CLOSED
    again = CircuitBreakerRegistry(config("new-model").providers, state_file=path)
    assert again.current_incident_identity("provider") == incident


@pytest.mark.asyncio
async def test_retired_deferred_observation_is_not_left_actionable(empty_db, monkeypatch):
    from genesis.db.crud import observations
    from genesis.observability.events import GenesisEventBus
    from genesis.routing.escalation import ProviderEscalation

    current = [None]
    escalation = ProviderEscalation(empty_db, GenesisEventBus(), current_incident_identity=lambda _: current[0])
    state = {"incident_identity": None, "trip_count": 5, "first_trip_at": "2026-10-01T00:00:00+00:00",
             "last_trip_at": "2026-10-01T00:01:00+00:00", "escalated": False}
    entered, release = asyncio.Event(), asyncio.Event()
    actual_create = observations.create

    async def suspended_create(*args, **kwargs):
        entered.set()
        await release.wait()
        return await actual_create(*args, **kwargs)

    monkeypatch.setattr(observations, "create", suspended_create)
    pending = asyncio.create_task(escalation._create_observation("provider", state))
    await entered.wait()
    current[0] = "a" * 64
    escalation.record_retirement("provider", None)
    await drain_escalation_tasks()
    release.set()
    await pending
    cursor = await empty_db.execute("SELECT resolved, resolution_notes FROM observations WHERE content_hash = ?",
                                   (escalation._provider_content_hash("provider"),))
    rows = await cursor.fetchall()
    assert rows and all(row[0] == 1 and "retired" in row[1] for row in rows)
    assert not state["escalated"]


@pytest.mark.asyncio
async def test_retired_deferred_notification_is_not_left_actionable(empty_db, monkeypatch):
    import json
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud import observations
    from genesis.routing.escalation import ProviderEscalation, notify_provider_if_due

    now = datetime.now(UTC)
    incident = "a" * 64
    current = [incident]
    await observations.create(empty_db, id="old-failure", source="routing", type="provider_failure",
        content=json.dumps({"provider": "provider", "incident_identity": incident}), priority="high",
        created_at=(now - timedelta(hours=2)).isoformat(),
        content_hash=ProviderEscalation._provider_content_hash("provider", incident))
    entered, release = asyncio.Event(), asyncio.Event()
    actual_create = observations.create

    async def suspended_create(*args, **kwargs):
        entered.set()
        await release.wait()
        return await actual_create(*args, **kwargs)

    monkeypatch.setattr(observations, "create", suspended_create)
    pending = asyncio.create_task(notify_provider_if_due(empty_db, "provider", clock=lambda: now,
        incident_identity=incident, current_incident_identity=lambda _: current[0],
        provider_still_failing=lambda _: True))
    await entered.wait()
    current[0] = "b" * 64
    release.set()
    assert not await pending
    cursor = await empty_db.execute("SELECT resolved, resolution_notes FROM observations WHERE content_hash = ?",
                                   (ProviderEscalation._notify_content_hash("provider", incident),))
    rows = await cursor.fetchall()
    assert rows and all(row[0] == 1 and "retired" in row[1] for row in rows)


def test_health_view_never_restores_or_writes_disk_and_keeps_captured_coverage(tmp_path, monkeypatch):
    cfg = config()
    cfg.call_sites["3_micro_reflection"] = CallSiteConfig(id="3_micro_reflection", chain=["provider"])
    router = make_router(cfg, tmp_path)
    old = router.breakers.get("provider")
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    captured_cfg, bindings = router.health_snapshot()
    router.reload_config(config("new-model"))
    monkeypatch.setattr(CircuitBreakerRegistry, "load_state", MagicMock(side_effect=AssertionError("disk read")))
    view = CircuitBreakerRegistry.health_view(captured_cfg, bindings)
    assert view.get("provider") is old
    assert view.uncovered_essential_sites() == ["3_micro_reflection"]
    assert router.breakers.get("provider").state == ProviderState.CLOSED
    assert view._persist is False
    view.save_state()


@pytest.mark.asyncio
async def test_awareness_notification_uses_current_retirement_incident(tmp_path, empty_db, monkeypatch):
    import json
    from datetime import UTC, datetime, timedelta

    from genesis.awareness.loop import _check_provider_outage_notify
    from genesis.db.crud import observations
    from genesis.routing.escalation import ProviderEscalation
    from genesis.runtime import GenesisRuntime

    router = make_router(config(), tmp_path)
    old = router.breakers.get("provider")
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    router.reload_config(config("new-model"))
    router.breakers.get("provider").force_open()
    incident = router.breakers.current_incident_identity("provider")
    assert incident is not None
    await observations.create(empty_db, id="current-failure", source="routing", type="provider_failure",
        content=json.dumps({"provider": "provider", "incident_identity": incident}), priority="high",
        created_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        content_hash=ProviderEscalation._provider_content_hash("provider", incident))
    monkeypatch.setattr(GenesisRuntime, "instance", lambda: SimpleNamespace(_circuit_breakers=router.breakers))
    monkeypatch.setattr("genesis.awareness.provider_notify_config.effective_mode", lambda: "live")
    await _check_provider_outage_notify(empty_db)
    await _check_provider_outage_notify(empty_db)
    cursor = await empty_db.execute("SELECT content_hash, content FROM observations WHERE priority='critical' AND resolved=0")
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == ProviderEscalation._notify_content_hash("provider", incident)
    assert json.loads(rows[0][1])["incident_identity"] == incident


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["observation", "notification"])
@pytest.mark.parametrize("replacement", ["rename", "remove"])
@pytest.mark.parametrize("legacy_incident", [True, False])
async def test_missing_alias_retires_deferred_database_write(
        tmp_path, empty_db, monkeypatch, writer, replacement, legacy_incident):
    import json
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud import observations
    from genesis.observability.events import GenesisEventBus
    from genesis.routing.escalation import ProviderEscalation, notify_provider_if_due

    now = datetime.now(UTC)
    escalation = ProviderEscalation(empty_db, GenesisEventBus(),
        current_incident_identity=lambda name: registry.current_incident_identity(name),
        incident_binding=lambda name: registry.current_incident_binding(name),
        incident_owner=lambda name, incident: registry.incident_owner(name, incident))
    registry = CircuitBreakerRegistry(config("predecessor", "glm51").providers,
        state_file=tmp_path / "missing-alias.json", on_retirement=escalation.record_retirement)
    if not legacy_incident:
        predecessor = registry.get("glm51")
        predecessor._failure_threshold = 1
        predecessor.record_failure(ErrorCategory.TRANSIENT, retirement=True)
        registry.update_providers(config("old-model", "glm51").providers)
        await drain_escalation_tasks()
    incident = registry.current_incident_identity("glm51")
    assert (incident is None) == legacy_incident
    old = registry.get("glm51")
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    state = {"incident_identity": incident, "trip_count": 5, "first_trip_at": now.isoformat(),
             "last_trip_at": now.isoformat(), "escalated": False}
    if writer == "notification":
        await observations.create(empty_db, id="old-outage", source="routing", type="provider_failure",
            content=json.dumps({"provider": "glm51", "incident_identity": incident}), priority="high",
            created_at=(now - timedelta(hours=2)).isoformat(),
            content_hash=escalation._provider_content_hash("glm51", incident))
    entered, release = asyncio.Event(), asyncio.Event()
    actual_create = observations.create

    async def suspended_create(*args, **kwargs):
        entered.set()
        await release.wait()
        return await actual_create(*args, **kwargs)

    monkeypatch.setattr(observations, "create", suspended_create)
    if writer == "observation":
        pending = asyncio.create_task(escalation._create_observation("glm51", state))
        content_hash = escalation._provider_content_hash("glm51", incident)
    else:
        pending = asyncio.create_task(notify_provider_if_due(empty_db, "glm51", clock=lambda: now,
            incident_identity=incident, current_incident_identity=registry.current_incident_identity,
            incident_owner=registry.incident_owner,
            provider_still_failing=lambda name: registry.get(name).state != ProviderState.CLOSED))
        content_hash = escalation._notify_content_hash("glm51", incident)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        registry.update_providers(config("new-model", "glm").providers if replacement == "rename" else {})
        await drain_escalation_tasks()
        if replacement == "rename":
            assert registry.get("glm").state == ProviderState.CLOSED
            assert registry.current_incident_identity("glm") not in (None, incident)
    finally:
        release.set()
    result = await pending
    if writer == "notification":
        assert result is False
    cursor = await empty_db.execute("SELECT resolved, resolution_notes FROM observations WHERE content_hash = ?",
                                   (content_hash,))
    rows = await cursor.fetchall()
    assert rows and all(row[0] == 1 and "retired" in row[1] and "recovered" not in row[1] for row in rows)
    assert not state["escalated"]
    # Absence is stale before a write too; live None must remain a valid incident.
    assert not escalation._incident_is_current("glm51", incident)
    assert not escalation._incident_is_current("glm51", None)
    if replacement == "rename":
        assert escalation._incident_is_current("glm", registry.current_incident_identity("glm"))


def test_partial_recovery_persists_retirement_provenance_change(tmp_path):
    import json
    now = [0.0]
    path = tmp_path / "partial-recovery.json"
    recovered = MagicMock()
    registry = CircuitBreakerRegistry(config().providers, clock=lambda: now[0], state_file=path,
                                      on_recovery=recovered)
    cb = registry.get("provider")
    cb._failure_threshold = 1
    cb.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    now[0] = 61.0
    assert cb.state == ProviderState.HALF_OPEN
    cb.record_success()
    assert cb.state == ProviderState.HALF_OPEN
    recovered.assert_not_called()
    saved = json.loads(path.read_text())["provider"]
    assert saved["state"] == "half_open" and saved["failure_cause"] is None
    assert saved["failure_identity"] is None
    restarted = CircuitBreakerRegistry(config("replacement").providers, state_file=path)
    assert restarted.get("provider").state == ProviderState.HALF_OPEN
    assert restarted.current_incident_identity("provider") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("notified", [False, True])
@pytest.mark.parametrize("restart", [False, True])
async def test_continuing_alias_incident_keeps_clock_ack_and_owner(tmp_path, empty_db, fresh, notified, restart):
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud import observations
    from genesis.observability.events import GenesisEventBus
    from genesis.observability.types import Severity, Subsystem
    from genesis.routing.escalation import ProviderEscalation, sweep_due_notifications
    from genesis.routing.provider_identity import provider_identity

    now = [0.0]
    at = datetime.now(UTC)
    path = tmp_path / "continuing-owner.json"
    bus = GenesisEventBus()

    def listener():
        result = ProviderEscalation(empty_db, bus, current_identity=lambda name: registry.current_identity(name),
            current_incident_identity=lambda name: registry.current_incident_identity(name),
            incident_binding=lambda name: registry.current_incident_binding(name),
            incident_owner=lambda anchor, incident: registry.incident_owner(anchor, incident))
        result.attach()
        return result

    escalation = listener()
    registry = CircuitBreakerRegistry(config("original", "glm51").providers, clock=lambda: now[0], state_file=path,
        on_recovery=escalation.record_recovery, on_retirement=escalation.record_retirement)
    if fresh:
        cb = registry.get("glm51")
        cb._failure_threshold = 1
        cb.record_failure(ErrorCategory.TRANSIENT, retirement=True)
        registry.update_providers(config("active", "glm51").providers)
        await drain_escalation_tasks()
    cb = registry.get("glm51")
    cb._failure_threshold = 1
    cb.record_failure(ErrorCategory.TRANSIENT)
    anchor, incident = registry.current_incident_binding("glm51")
    for _ in range(5):
        await bus.emit(Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "test", provider="glm51",
            health_identity=provider_identity(cb._provider), incident_identity=incident)
    await drain_escalation_tasks()
    failure_hash = escalation._provider_content_hash(anchor, incident)
    notify_hash = escalation._notify_content_hash(anchor, incident)
    started = (at - timedelta(hours=2)).isoformat()
    await empty_db.execute("UPDATE observations SET created_at=? WHERE content_hash=?", (started, failure_hash))
    await empty_db.commit()

    async def sweep():
        return await sweep_due_notifications(empty_db, clock=lambda: at,
            current_incident_identity=registry.current_incident_identity, incident_owner=registry.incident_owner,
            provider_still_failing=lambda name: registry.get(name).state != ProviderState.CLOSED)

    if notified:
        assert await sweep() == 1
        await observations.resolve_by_content_hash(empty_db, source="routing", content_hash=notify_hash,
                                                   resolution_notes="user acknowledged", resolved_at=at.isoformat())
    # A plain registered rename is a continuing incident, not retirement/recovery.
    cfg = config(cb._provider.model_id, "glm")
    registry.update_providers(cfg.providers)
    assert registry.current_incident_binding("glm") == (anchor, incident)
    assert registry.incident_owner(anchor, incident) == "glm"
    # Adding the old alias back as an independent sibling must not steal history.
    cfg.providers["glm51"] = config("independent", "glm51").providers["glm51"]
    registry.update_providers(cfg.providers)
    assert registry.current_incident_binding("glm51") != (anchor, incident)
    assert registry.incident_owner(anchor, incident) == "glm"
    if restart:
        bus = GenesisEventBus()
        escalation = listener()
        registry = CircuitBreakerRegistry(cfg.providers, clock=lambda: now[0], state_file=path,
            on_recovery=escalation.record_recovery, on_retirement=escalation.record_retirement)
        assert registry.current_incident_binding("glm") == (anchor, incident)
        assert registry.incident_owner(anchor, incident) == "glm"
    assert await sweep() == (0 if notified else 1)
    # Recovery of the sibling cannot resolve the continuing incident.
    escalation.record_recovery("glm51")
    await drain_escalation_tasks()
    cursor = await empty_db.execute("SELECT created_at,resolved FROM observations WHERE content_hash=?", (failure_hash,))
    row = await cursor.fetchone()
    assert row[0] == started and row[1] == 0
    now[0] = 10000.0
    current = registry.get("glm")
    assert current.state == ProviderState.HALF_OPEN
    current.record_success()
    current.record_success()
    await drain_escalation_tasks()
    cursor = await empty_db.execute("SELECT content_hash,resolved,resolution_notes FROM observations WHERE content_hash IN (?,?)",
                                   (failure_hash, notify_hash))
    rows = await cursor.fetchall()
    assert rows and all(row[1] == 1 for row in rows)
    for row in rows:
        assert row[2] == "user acknowledged" if notified and row[0] == notify_hash else "recovered" in row[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True])
async def test_alias_rename_keeps_prethreshold_evidence(tmp_path, empty_db, fresh):
    from genesis.observability.events import GenesisEventBus
    from genesis.observability.types import Severity, Subsystem
    from genesis.routing.escalation import ProviderEscalation
    bus = GenesisEventBus()
    escalation = ProviderEscalation(empty_db, bus,
        incident_binding=lambda name: registry.current_incident_binding(name),
        incident_owner=lambda anchor, incident: registry.incident_owner(anchor, incident))
    registry = CircuitBreakerRegistry(config("first", "glm51").providers, state_file=tmp_path / "prethreshold.json")
    if fresh:
        cb = registry.get("glm51")
        cb._failure_threshold = 1
        cb.record_failure(ErrorCategory.TRANSIENT, retirement=True)
        registry.update_providers(config("second", "glm51").providers)
    escalation.attach()
    anchor, incident = registry.current_incident_binding("glm51")
    for _ in range(4):
        await bus.emit(Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "test",
                       provider="glm51", incident_identity=incident)
    old = registry.get("glm51")
    registry.update_providers(config(old._provider.model_id, "glm").providers)
    await bus.emit(Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "test",
                   provider="glm", incident_identity=incident)
    await drain_escalation_tasks()
    key = escalation._state_key(anchor, incident)
    assert escalation._state[key]["trip_count"] == 5 and escalation._state[key]["escalated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True])
async def test_removed_provider_retires_existing_incident(tmp_path, empty_db, fresh):
    from genesis.observability.events import GenesisEventBus
    from genesis.observability.types import Severity, Subsystem
    from genesis.routing.escalation import ProviderEscalation

    bus = GenesisEventBus()
    escalation = ProviderEscalation(empty_db, bus,
        incident_binding=lambda name: registry.current_incident_binding(name),
        incident_owner=lambda anchor, incident: registry.incident_owner(anchor, incident))
    registry = CircuitBreakerRegistry(config().providers, state_file=tmp_path / "removed.json",
        on_retirement=escalation.record_retirement)
    escalation.attach()
    cb = registry.get("provider")
    if fresh:
        cb._incident_identity = registry._new_incident_identity()
    incident = cb._incident_identity
    for _ in range(5):
        await bus.emit(Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "test",
            provider="provider", incident_identity=incident)
    await drain_escalation_tasks()
    key = escalation._provider_content_hash("provider", incident)
    cursor = await empty_db.execute("SELECT resolved FROM observations WHERE content_hash=?", (key,))
    assert (await cursor.fetchone())[0] == 0
    registry.update_providers({})
    await drain_escalation_tasks()
    cursor = await empty_db.execute("SELECT resolved,resolution_notes FROM observations WHERE content_hash=?", (key,))
    row = await cursor.fetchone()
    assert row[0] == 1 and "retired" in row[1]
    assert not escalation._state
    registry.update_providers(config().providers)
    await bus.emit(Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "test",
        provider="provider", incident_identity=registry.current_incident_identity("provider"))
    assert next(iter(escalation._state.values()))["trip_count"] == 1


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("retirement", [False, True])
def test_closed_pending_provenance_survives_repeated_restart(tmp_path, count, retirement):
    path = tmp_path / "pending.json"
    providers = config().providers
    registry = CircuitBreakerRegistry(providers, state_file=path)
    for _ in range(count):
        registry.get("provider").record_failure(ErrorCategory.TRANSIENT, retirement=retirement)
    registry.save_state()
    for _ in range(2):
        registry = CircuitBreakerRegistry(providers, state_file=path)
        cb = registry.get("provider")
        assert cb.state == ProviderState.CLOSED
        assert cb._consecutive_failures == count
        assert cb._failure_cause == ("retirement" if retirement else "other")
        assert not cb._opened_by_call
    for _ in range(3 - count):
        cb.record_failure(ErrorCategory.TRANSIENT, retirement=retirement)
    assert cb.state == ProviderState.OPEN
    registry.update_providers(config("replacement").providers)
    assert registry.get("provider").state == (ProviderState.CLOSED if retirement else ProviderState.OPEN)


def test_restore_never_persists_partial_bindings(tmp_path, monkeypatch):
    import json

    path = tmp_path / "restore.json"
    providers = {name: config(name=name).providers[name] for name in ("glm", "glm51", "later")}
    registry = CircuitBreakerRegistry(providers, state_file=path)
    for name in providers:
        registry.get(name).force_open()
    data = json.loads(path.read_text())
    data["glm"]["incident_provider"] = "glm51"
    data["glm"]["incident_identity"] = None
    data["glm51"]["incident_identity"] = "a" * 64
    path.write_text(json.dumps(data))
    writes = []
    from genesis.routing import circuit_breaker
    real_write = circuit_breaker.atomic_write_text

    def observe_write(target, text):
        writes.append(json.loads(text))
        real_write(target, text)

    monkeypatch.setattr(circuit_breaker, "atomic_write_text", observe_write)
    for _ in range(2):
        registry = CircuitBreakerRegistry(providers, state_file=path)
        assert all(registry.get(name).state == ProviderState.OPEN for name in providers)
    assert len(writes) == 2
    assert all(set(w) == set(providers) and all(row["state"] == "open" for row in w.values()) for w in writes)


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("mode", ["reload", "restart"])
def test_pending_retirement_does_not_poison_replacement(tmp_path, count, mode):
    path = tmp_path / "pending-replacement.json"
    registry = CircuitBreakerRegistry(config().providers, state_file=path)
    for _ in range(count):
        registry.get("provider").record_failure(ErrorCategory.TRANSIENT, retirement=True)
    registry.save_state()
    replacement = config("replacement").providers
    if mode == "reload":
        registry.update_providers(replacement)
    else:
        registry = CircuitBreakerRegistry(replacement, state_file=path)
    cb = registry.get("provider")
    assert cb._consecutive_failures == 0
    assert cb._failure_cause is None
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.CLOSED
