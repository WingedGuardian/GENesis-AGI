"""OutreachHeartbeat gate — pulses ONLY while the outreach scheduler is running.

The daemon exists to give outreach a channel-independent liveness pulse without the
old false-alarm trap. Its emit condition is the load-bearing logic: a Telegram-less /
never-started / cleanly-stopped scheduler must NOT pulse (so the subsystem reads stale
or benign, never a false alive), while a running scheduler must.
"""

from __future__ import annotations

from types import SimpleNamespace

from genesis.outreach.heartbeat import OutreachHeartbeat


def _rt(*, bootstrapped=True, bus=True, scheduler=True, running=True):
    sched = SimpleNamespace(is_running=running) if scheduler else None
    return SimpleNamespace(
        is_bootstrapped=bootstrapped,
        event_bus=object() if bus else None,
        _outreach_scheduler=sched,
    )


def test_emits_when_scheduler_running():
    assert OutreachHeartbeat._should_emit(_rt()) is True


def test_no_emit_when_scheduler_stopped():
    # is_running False → never-started or cleanly-stopped → pulse ceases → goes stale.
    assert OutreachHeartbeat._should_emit(_rt(running=False)) is False


def test_no_emit_when_scheduler_absent():
    # Telegram-less install: scheduler was never constructed/started.
    assert OutreachHeartbeat._should_emit(_rt(scheduler=False)) is False


def test_no_emit_before_bootstrap():
    assert OutreachHeartbeat._should_emit(_rt(bootstrapped=False)) is False


def test_no_emit_without_event_bus():
    assert OutreachHeartbeat._should_emit(_rt(bus=False)) is False
