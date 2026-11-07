"""Tests for _check_pid_budget — the per-user-slice PID/task-budget alert.

The task budget maxes under many concurrent CC sessions (each spawns MCP
subprocess trees) and precedes `Cannot fork` while memory/cpu/disk read green.
degraded (>=80%) -> 'high'; error (>=90%) -> 'critical'. The alert is
EXPLANATORY: it names the sub-cap and the remedy so a cryptic `Cannot fork`
becomes actionable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.awareness import loop


@pytest.fixture(autouse=True)
def _reset_cooldown_and_mock_create(monkeypatch):
    monkeypatch.setattr(loop, "_last_pid_alerts", {})
    create = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", create)
    return create


def _budget(status, pct, current=2000, maximum=2400, scope=None):
    b = {"status": status, "pct": pct, "current": current, "max": maximum}
    if scope is not None:
        b["scope"] = scope
    return b


@pytest.mark.asyncio
async def test_error_creates_critical_explanatory_alert(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(object(), budget=_budget("error", 95.0, 2280, 2400))
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["type"] == "infrastructure_alert"
    assert kwargs["priority"] == "critical"
    assert kwargs["source"] == "pid_budget_monitor"
    # explanatory: names the failure mode and the remedy
    assert "Cannot fork" in kwargs["content"]
    assert "TasksMax" in kwargs["content"]
    assert "2280/2400" in kwargs["content"]


@pytest.mark.asyncio
async def test_service_scope_names_the_unit_remedy(_reset_cooldown_and_mock_create):
    # A service-scope binding (e.g. the server unit's own git/subprocess trees)
    # must name that unit and its TasksMax, not the generic user-slice remedy.
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(
        object(), budget=_budget("error", 92.0, 552, 600, scope="genesis-server.service")
    )
    content = create.await_args.kwargs["content"]
    assert "genesis-server.service" in content
    assert "TasksMax" in content
    assert "552/600" in content


@pytest.mark.asyncio
async def test_root_scope_says_container_wide(_reset_cooldown_and_mock_create):
    # A container-root binding is shared with system/other processes — the remedy
    # must NOT tell the user to raise a user-slice sub-cap that wouldn't help.
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(
        object(), budget=_budget("error", 95.0, 3800, 4000, scope="container-root")
    )
    content = create.await_args.kwargs["content"].lower()
    assert "container" in content
    assert "user-.slice tasksmax" not in content


@pytest.mark.asyncio
async def test_slice_scope_names_user_slice_remedy(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(
        object(), budget=_budget("degraded", 83.0, 2000, 2400, scope="user-1000.slice")
    )
    content = create.await_args.kwargs["content"]
    assert "TasksMax" in content
    assert "60%" in content  # the provisioned user-slice default


@pytest.mark.asyncio
async def test_degraded_is_high_priority(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(object(), budget=_budget("degraded", 83.0))
    create.assert_awaited_once()
    assert create.await_args.kwargs["priority"] == "high"


@pytest.mark.asyncio
async def test_healthy_no_alert(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(object(), budget=_budget("healthy", 41.0))
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_no_alert(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(object(), budget={"status": "unavailable"})
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_subcap_max_sentinel_no_alert(_reset_cooldown_and_mock_create):
    # pids.max == "max" surfaces as healthy/pct None → must never alarm.
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(
        object(), budget={"status": "healthy", "current": 1500, "max": "max", "pct": None}
    )
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_suppresses_second_alert(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    db = object()
    await loop._check_pid_budget(db, budget=_budget("error", 95.0))
    await loop._check_pid_budget(db, budget=_budget("error", 96.0))
    create.assert_awaited_once()  # second is within the 1h cooldown


@pytest.mark.asyncio
async def test_cross_scope_critical_not_suppressed(_reset_cooldown_and_mock_create):
    # A minor alert on ONE scope must NOT swallow a critical alert on a DIFFERENT
    # scope within the cooldown — that is the alert this monitor exists to raise.
    create = _reset_cooldown_and_mock_create
    db = object()
    await loop._check_pid_budget(
        db, budget=_budget("degraded", 82.0, 2000, 2400, scope="user-1000.slice")
    )
    await loop._check_pid_budget(
        db, budget=_budget("error", 95.0, 3800, 4000, scope="container-root")
    )
    assert create.await_count == 2  # scope changed AND escalated → both fire


@pytest.mark.asyncio
async def test_escalation_same_scope_not_suppressed(_reset_cooldown_and_mock_create):
    # degraded → error on the SAME scope inside the cooldown must still fire.
    create = _reset_cooldown_and_mock_create
    db = object()
    await loop._check_pid_budget(db, budget=_budget("degraded", 82.0, scope="user-1000.slice"))
    await loop._check_pid_budget(db, budget=_budget("error", 92.0, scope="user-1000.slice"))
    assert create.await_count == 2  # escalated degraded→error → not suppressed


@pytest.mark.asyncio
async def test_same_scope_same_severity_suppressed(_reset_cooldown_and_mock_create):
    # Same scope, same severity within the cooldown → suppressed (no alert storm).
    create = _reset_cooldown_and_mock_create
    db = object()
    await loop._check_pid_budget(db, budget=_budget("error", 95.0, scope="user-1000.slice"))
    await loop._check_pid_budget(db, budget=_budget("error", 96.0, scope="user-1000.slice"))
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_de_escalation_same_scope_suppressed(_reset_cooldown_and_mock_create):
    # error → degraded on the SAME scope must NOT alert — announcing an improvement
    # is noise. Only the initial error fires.
    create = _reset_cooldown_and_mock_create
    db = object()
    await loop._check_pid_budget(db, budget=_budget("error", 95.0, scope="user-1000.slice"))
    await loop._check_pid_budget(db, budget=_budget("degraded", 82.0, scope="user-1000.slice"))
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_scope_oscillation_suppressed(_reset_cooldown_and_mock_create):
    # Two near-tied levels alternating as the binding scope must NOT re-alert every
    # tick — each (scope, severity) has its own cooldown, so A→B→A→B fires once per
    # scope, not four times.
    create = _reset_cooldown_and_mock_create
    db = object()
    for scope in ("user-1000.slice", "container-root", "user-1000.slice", "container-root"):
        await loop._check_pid_budget(db, budget=_budget("error", 95.0, scope=scope))
    assert create.await_count == 2  # A and B each alert once; the repeats are suppressed


@pytest.mark.asyncio
async def test_db_none_does_not_write_or_consume_cooldown(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(None, budget=_budget("error", 95.0))
    create.assert_not_awaited()
    assert loop._last_pid_alerts == {}  # cooldown not consumed
    await loop._check_pid_budget(object(), budget=_budget("error", 95.0))
    create.assert_awaited_once()  # a later tick with a live db still alerts


@pytest.mark.asyncio
async def test_create_failure_never_raises(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    create.side_effect = RuntimeError("db locked")
    await loop._check_pid_budget(object(), budget=_budget("error", 95.0))  # must not propagate


@pytest.mark.asyncio
async def test_budget_none_reads_live_without_raising(monkeypatch):
    # When budget is not injected it reads the live cgroup; on any host it must
    # return without raising (healthy/unavailable → no alert).
    create = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", create)
    monkeypatch.setattr(loop, "_last_pid_alerts", {})
    await loop._check_pid_budget(object(), budget=None)  # no exception


@pytest.mark.asyncio
async def test_the_alert_names_who_holds_the_budget(_reset_cooldown_and_mock_create, monkeypatch):
    """The alert carries the breakdown summary and the snapshot file, so the next
    exhausted budget names the tree that filled it (2026-10-13: only the count)."""
    from genesis.observability import pid_breakdown

    seen = []

    def fake_snapshot(budget):
        seen.append(budget)
        return {"text": "Top task holders: claude (pid 12): 613 tasks.", "file": "/s/x.json"}

    monkeypatch.setattr(pid_breakdown, "snapshot_for", fake_snapshot)
    create = _reset_cooldown_and_mock_create
    budget = _budget("error", 100.0, 2400, 2400, scope="user-1000.slice")
    budget["path"] = "/sys/fs/cgroup/user.slice/user-1000.slice"
    await loop._check_pid_budget(object(), budget=budget)
    content = create.await_args.kwargs["content"]
    assert "claude (pid 12): 613 tasks" in content
    assert "/s/x.json" in content
    assert seen == [budget]


@pytest.mark.asyncio
async def test_a_pruned_breakdown_keeps_its_summary_without_advertising_a_file(
    _reset_cooldown_and_mock_create, monkeypatch
):
    from genesis.observability import pid_breakdown

    monkeypatch.setattr(
        pid_breakdown, "snapshot_for", lambda b: {"text": "Top task holders: claude.", "file": None}
    )
    budget = _budget("error", 100.0, 2400, 2400, scope="user-1000.slice")
    budget["path"] = "/sys/fs/cgroup/user.slice/user-1000.slice"
    await loop._check_pid_budget(object(), budget=budget)
    content = _reset_cooldown_and_mock_create.await_args.kwargs["content"]
    assert "Top task holders: claude." in content
    assert "Full breakdown:" not in content


@pytest.mark.asyncio
async def test_the_breakdown_is_taken_inside_the_cooldown(
    _reset_cooldown_and_mock_create, monkeypatch
):
    """A suppressed repeat alert still records who holds the budget: the holders
    at the second tick may be different processes from the first."""
    from genesis.observability import pid_breakdown

    calls = []
    monkeypatch.setattr(pid_breakdown, "snapshot_for", lambda b: calls.append(b) or None)
    create = _reset_cooldown_and_mock_create
    budget = _budget("error", 95.0, scope="user-1000.slice")
    budget["path"] = "/cg"
    await loop._check_pid_budget(object(), budget=budget)
    await loop._check_pid_budget(object(), budget=budget)
    assert create.await_count == 1  # second alert suppressed by the cooldown
    assert len(calls) == 2  # but both ticks were recorded


@pytest.mark.asyncio
async def test_a_failed_breakdown_never_drops_the_alert(
    _reset_cooldown_and_mock_create, monkeypatch
):
    from genesis.observability import pid_breakdown

    def boom(_b):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(pid_breakdown, "snapshot_for", boom)
    create = _reset_cooldown_and_mock_create
    budget = _budget("error", 99.0, scope="user-1000.slice")
    budget["path"] = "/cg"
    await loop._check_pid_budget(object(), budget=budget)
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_healthy_budget_takes_no_breakdown(_reset_cooldown_and_mock_create, monkeypatch):
    from genesis.observability import pid_breakdown

    calls = []
    monkeypatch.setattr(pid_breakdown, "snapshot_for", lambda b: calls.append(b))
    budget = _budget("healthy", 40.0)
    budget["path"] = "/cg"
    await loop._check_pid_budget(object(), budget=budget)
    assert calls == []


@pytest.mark.asyncio
async def test_no_thread_to_start_still_names_the_holders(
    _reset_cooldown_and_mock_create, monkeypatch
):
    """At 100% the worker thread itself may fail to start; the snapshot then runs
    inline so the alert for the exhausted tick still carries the breakdown."""
    from genesis.observability import pid_breakdown

    async def no_thread(*_a, **_k):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(loop.asyncio, "to_thread", no_thread)
    monkeypatch.setattr(
        pid_breakdown, "snapshot_for", lambda b: {"text": "Top task holders: x.", "file": "/f"}
    )
    create = _reset_cooldown_and_mock_create
    budget = _budget("error", 100.0, scope="user-1000.slice")
    budget["path"] = "/cg"
    await loop._check_pid_budget(object(), budget=budget)
    assert "Top task holders: x." in create.await_args.kwargs["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("path", ["/fixture/\udcff/x.json", "/fixture/é/x.json", None])
async def test_final_alert_path_is_utf8_even_for_filesystem_surrogates(
    _reset_cooldown_and_mock_create, monkeypatch, inline, path
):
    from genesis.observability import pid_breakdown

    if inline:

        async def no_thread(*_a, **_k):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(loop.asyncio, "to_thread", no_thread)
    monkeypatch.setattr(
        pid_breakdown, "snapshot_for", lambda b: {"text": "Top task holders: x.", "file": path}
    )
    budget = _budget("error", 100.0, scope="user-1000.slice")
    budget["path"] = "/cg"
    await loop._check_pid_budget(object(), budget=budget)
    create = _reset_cooldown_and_mock_create
    assert create.await_count == 1
    content = create.await_args.kwargs["content"]
    content.encode("utf-8")
    assert "Top task holders: x." in content
    if path is None:
        assert "Full breakdown:" not in content
    else:
        assert path.encode("utf-8", "backslashreplace").decode("utf-8") in content


# ── Resolve on healthy ─────────────────────────────────────────────────────
# A healthy tick resolves an open pid_budget_monitor alert, found in the STORE
# (no in-memory guard: an alert written before a restart must still resolve).
# A read-only probe gates the write, so a healthy tick with nothing open writes
# nothing. Hysteresis: resolve only below _PID_RESOLVE_PCT (or no sub-cap).


@pytest.fixture
def store(monkeypatch):
    """Mock the probe + resolve; ``store.open`` is what the probe reports."""

    class _Store:
        open = True

    st = _Store()

    async def _probe(db, *, source, content_like):
        st.probe_calls.append(source)
        return st.open

    st.probe_calls = []
    st.resolve = AsyncMock(return_value=1)
    monkeypatch.setattr(loop.observations, "has_unresolved_matching", _probe)
    monkeypatch.setattr(loop.observations, "resolve_by_source_and_type", st.resolve)
    return st


@pytest.mark.asyncio
async def test_alert_from_before_restart_resolves_on_healthy(
    _reset_cooldown_and_mock_create, store
):
    # Fresh process: nothing was written in-process, but the store holds one.
    await loop._check_pid_budget(object(), budget=_budget("healthy", 41.0))
    assert store.probe_calls == ["pid_budget_monitor"]
    store.resolve.assert_awaited_once()
    kwargs = store.resolve.await_args.kwargs
    assert kwargs["source"] == "pid_budget_monitor"
    assert kwargs["type"] == "infrastructure_alert"


@pytest.mark.asyncio
async def test_healthy_with_nothing_open_does_no_write(_reset_cooldown_and_mock_create, store):
    store.open = False
    for _ in range(3):
        await loop._check_pid_budget(object(), budget=_budget("healthy", 41.0))
    store.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncapped_healthy_is_resolvable(_reset_cooldown_and_mock_create, store):
    await loop._check_pid_budget(
        object(), budget={"status": "healthy", "current": 1500, "max": "max", "pct": None}
    )
    store.resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_hysteresis_band_does_not_resolve(_reset_cooldown_and_mock_create, store):
    # Healthy (<80%) but not below the resolve threshold: hold the alert open.
    assert loop._PID_RESOLVE_PCT < 80
    await loop._check_pid_budget(object(), budget=_budget("healthy", loop._PID_RESOLVE_PCT + 1.0))
    store.resolve.assert_not_awaited()
    assert store.probe_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["degraded", "error", "unavailable"])
async def test_non_healthy_tick_never_resolves(_reset_cooldown_and_mock_create, store, status):
    await loop._check_pid_budget(object(), budget=_budget(status, 85.0))
    store.resolve.assert_not_awaited()
    assert store.probe_calls == []


@pytest.fixture
def clock(monkeypatch):
    """A controllable monotonic clock for the alert cooldown."""

    class _Clock:
        t = 10_000.0

        def __call__(self):
            return self.t

    c = _Clock()
    monkeypatch.setattr(loop, "_pid_clock", c)
    return c


@pytest.mark.asyncio
async def test_flap_realerts_after_resolve(_reset_cooldown_and_mock_create, store, clock):
    # degraded -> healthy -> degraded: once the first episode is resolved, a new
    # one re-alerts after a quarter of the cooldown, not the full hour.
    create = _reset_cooldown_and_mock_create
    quarter = loop._PID_BUDGET_ALERT_COOLDOWN_S / 4
    await loop._check_pid_budget(object(), budget=_budget("degraded", 83.0))
    clock.t += 60
    await loop._check_pid_budget(object(), budget=_budget("healthy", 41.0))
    store.resolve.assert_awaited_once()
    clock.t += quarter
    await loop._check_pid_budget(object(), budget=_budget("degraded", 84.0))
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_rapid_oscillation_alerts_at_most_once_per_quarter_cooldown(
    _reset_cooldown_and_mock_create, store, clock
):
    # Load swinging 70 <-> 85 every tick: each healthy tick resolves, but the
    # resolve must not lift the rate limit entirely.
    create = _reset_cooldown_and_mock_create
    quarter = loop._PID_BUDGET_ALERT_COOLDOWN_S / 4
    start = clock.t
    while clock.t - start < quarter - 60:
        await loop._check_pid_budget(object(), budget=_budget("degraded", 85.0))
        clock.t += 30
        await loop._check_pid_budget(object(), budget=_budget("healthy", 70.0))
        clock.t += 30
    assert create.await_count == 1
    # Past the quarter-cooldown since the last alert: the next swing re-alerts.
    clock.t = start + quarter + 1
    await loop._check_pid_budget(object(), budget=_budget("degraded", 85.0))
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_unresolved_episode_keeps_the_full_cooldown(
    _reset_cooldown_and_mock_create, store, clock
):
    # No resolve in between (still open): a same-severity repeat after the
    # quarter but inside the hour stays suppressed.
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(object(), budget=_budget("degraded", 83.0))
    clock.t += loop._PID_BUDGET_ALERT_COOLDOWN_S / 2
    await loop._check_pid_budget(object(), budget=_budget("degraded", 84.0))
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_escalation_after_resolve_bypasses_the_quarter(
    _reset_cooldown_and_mock_create, store, clock
):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(object(), budget=_budget("degraded", 83.0))
    clock.t += 30
    await loop._check_pid_budget(object(), budget=_budget("healthy", 41.0))
    clock.t += 30
    await loop._check_pid_budget(object(), budget=_budget("error", 95.0))
    assert create.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget", "note_has", "note_lacks"),
    [
        (_budget("healthy", 41.0), "75%", "sub-cap"),
        ({"status": "healthy", "current": 9, "max": "max", "pct": None}, "no sub-cap", "75%"),
    ],
)
async def test_resolution_note_matches_the_reading(
    _reset_cooldown_and_mock_create, store, budget, note_has, note_lacks
):
    await loop._check_pid_budget(object(), budget=budget)
    note = store.resolve.await_args.kwargs["resolution_notes"]
    assert note_has in note
    assert note_lacks not in note


@pytest.mark.asyncio
async def test_resolving_nothing_keeps_the_cooldown(_reset_cooldown_and_mock_create, store):
    create = _reset_cooldown_and_mock_create
    store.resolve.return_value = 0
    await loop._check_pid_budget(object(), budget=_budget("degraded", 83.0))
    await loop._check_pid_budget(object(), budget=_budget("healthy", 41.0))
    await loop._check_pid_budget(object(), budget=_budget("degraded", 84.0))
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_resolve_failures_never_raise(_reset_cooldown_and_mock_create, store):
    store.resolve.side_effect = RuntimeError("db locked")
    await loop._check_pid_budget(object(), budget=_budget("healthy", 41.0))


@pytest.mark.asyncio
async def test_healthy_with_no_db_does_nothing(_reset_cooldown_and_mock_create, store):
    await loop._check_pid_budget(None, budget=_budget("healthy", 41.0))
    assert store.probe_calls == []
    store.resolve.assert_not_awaited()
