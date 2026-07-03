"""Tests for genesis.observability.snapshots.services — sentinel key.

The Services snapshot exposes a "sentinel" sub-dict so the dashboard can
surface the container-side guardian's 4-state lifecycle. These tests cover
the happy path, in-flight states, escalation, the rt._sentinel-is-None
fallback, and the real "runtime never bootstrapped" path.

Tests mutate ``GenesisRuntime._instance`` directly to inject a stub runtime.
The snapshot reads ``_instance`` (read-only peek) instead of calling
``.instance()``, so an observability call cannot spawn a zombie runtime.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from genesis.observability.snapshots.services import services
from genesis.runtime._core import GenesisRuntime


def _fake_sentinel(current_state="healthy", is_active=False, **kw):
    from datetime import UTC, datetime
    state = SimpleNamespace(
        current_state=current_state,
        last_trigger_source=kw.get("last_trigger_source", ""),
        last_trigger_reason=kw.get("last_trigger_reason", ""),
        last_cc_dispatch_at=kw.get("last_cc_dispatch_at", ""),
        escalated_count=kw.get("escalated_count", 0),
        last_heartbeat_at=kw.get("last_heartbeat_at", datetime.now(UTC).isoformat()),
    )
    return SimpleNamespace(state=state, is_active=is_active)


@pytest.fixture
def runtime_singleton():
    """Save/restore GenesisRuntime._instance so tests can inject a stub safely."""
    original = GenesisRuntime._instance
    yield
    GenesisRuntime._instance = original


def _install_runtime(sentinel) -> None:
    GenesisRuntime._instance = SimpleNamespace(_sentinel=sentinel)


def test_services_includes_sentinel_key_when_healthy(runtime_singleton):
    _install_runtime(_fake_sentinel())
    result = services()
    assert "sentinel" in result
    assert result["sentinel"]["enabled"] is True
    assert result["sentinel"]["current_state"] == "healthy"
    assert result["sentinel"]["is_active"] is False
    assert result["sentinel"]["escalated_count"] == 0


def test_services_sentinel_investigating_active(runtime_singleton):
    _install_runtime(_fake_sentinel(
        current_state="investigating",
        is_active=True,
        last_trigger_source="fire_alarm",
        last_trigger_reason="router.all_exhausted",
    ))
    result = services()
    assert result["sentinel"]["current_state"] == "investigating"
    assert result["sentinel"]["is_active"] is True
    assert result["sentinel"]["last_trigger_source"] == "fire_alarm"
    assert result["sentinel"]["last_trigger_reason"] == "router.all_exhausted"


def test_services_sentinel_escalated_counts(runtime_singleton):
    _install_runtime(_fake_sentinel(current_state="escalated", escalated_count=2))
    result = services()
    assert result["sentinel"]["current_state"] == "escalated"
    assert result["sentinel"]["escalated_count"] == 2


def test_services_sentinel_unavailable_when_sentinel_is_none(runtime_singleton):
    """Runtime exists but sentinel init didn't run → 'unavailable' fallback."""
    _install_runtime(sentinel=None)
    result = services()
    assert result["sentinel"]["enabled"] is False
    assert result["sentinel"]["current_state"] == "unavailable"
    assert result["sentinel"]["is_active"] is False


def test_services_sentinel_unavailable_when_runtime_not_bootstrapped(
    runtime_singleton, monkeypatch,
):
    """The real bootstrap-never-ran path: the snapshot reads the runtime via
    ``peek()`` and reports 'unavailable' without ever constructing one.

    Enforced via a ``peek()`` spy rather than a post-call
    ``GenesisRuntime._instance is None`` assertion. That global check was fragile
    to cross-test singleton pollution: another test that constructs a runtime on
    a background awareness tick can set ``_instance`` *after* this call returns,
    failing the assertion purely as a function of collection order (see the
    tracked follow-up for the underlying singleton-leak isolation bug). Asserting
    that ``services()`` went through ``peek()`` (the non-constructing read) both
    proves the real invariant — an observability call must never spawn a zombie
    runtime via ``instance()`` — and is immune to what other tests leave behind.
    """
    GenesisRuntime._instance = None
    peek_spy = MagicMock(return_value=None)
    monkeypatch.setattr(GenesisRuntime, "peek", peek_spy)

    result = services()

    assert result["sentinel"]["enabled"] is False
    assert result["sentinel"]["current_state"] == "unavailable"
    assert "host_framework" in result
    # The snapshot must read the runtime via peek() (never instance(), which would
    # construct). If a future change swaps peek() for instance(), this fails.
    peek_spy.assert_called()
