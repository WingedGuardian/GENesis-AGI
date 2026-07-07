"""Wiring for the capability-build lane at task-executor init.

`_wire_build_lane` must: always construct the lane (so the inbox monitor hook
is a clean no-op when disabled), late-wire the monitor, spawn the poll loop
ONLY when enabled, and force the lane OFF when the approval gate is missing.
These lock the "if the system restarts now, will this work?" wiring in place
without the full task-executor bootstrap.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from genesis.autonomy.build_lane import BuildLane
from genesis.runtime.init.tasks import _wire_build_lane


class _FakeMonitor:
    def __init__(self):
        self._build_lane = None

    def set_build_lane(self, bl):
        self._build_lane = bl


# Module-level singleton so the default isn't a call in the signature (B008).
_PRESENT_GATE = object()


def _fake_rt(*, gate=_PRESENT_GATE, monitor=None):
    return SimpleNamespace(
        _db=object(),
        _autonomous_cli_approval_gate=gate,
        _inbox_monitor=monitor if monitor is not None else _FakeMonitor(),
        _build_lane=None,
        _build_lane_poll=None,
        record_job_success=lambda *a, **k: None,
        record_job_failure=lambda *a, **k: None,
    )


def _set_flag(monkeypatch, value: bool):
    monkeypatch.setattr("genesis.env.build_lane_enabled", lambda: value)


class TestWireBuildLane:
    async def test_enabled_constructs_wires_and_spawns_poll(self, monkeypatch):
        _set_flag(monkeypatch, True)
        rt = _fake_rt()
        _wire_build_lane(rt, dispatcher=object())
        try:
            assert isinstance(rt._build_lane, BuildLane)
            assert rt._build_lane.enabled is True
            # Monitor hook wired to the SAME instance.
            assert rt._inbox_monitor._build_lane is rt._build_lane
            # Poll loop spawned.
            assert rt._build_lane_poll is not None
        finally:
            if rt._build_lane_poll is not None:
                rt._build_lane_poll.cancel()

    async def test_disabled_constructs_wires_but_no_poll(self, monkeypatch):
        _set_flag(monkeypatch, False)
        rt = _fake_rt()
        _wire_build_lane(rt, dispatcher=object())
        assert isinstance(rt._build_lane, BuildLane)
        assert rt._build_lane.enabled is False
        # Hook still wired (so a later flag-flip+restart works); no-op while dark.
        assert rt._inbox_monitor._build_lane is rt._build_lane
        # No idle poll loop while dark.
        assert rt._build_lane_poll is None

    async def test_enabled_but_no_gate_forced_off(self, monkeypatch):
        _set_flag(monkeypatch, True)
        rt = _fake_rt(gate=None)
        _wire_build_lane(rt, dispatcher=object())
        assert rt._build_lane.enabled is False  # cards need the gate
        assert rt._build_lane_poll is None

    async def test_missing_monitor_does_not_crash(self, monkeypatch):
        _set_flag(monkeypatch, False)
        rt = _fake_rt()
        rt._inbox_monitor = None
        _wire_build_lane(rt, dispatcher=object())  # must not raise
        assert isinstance(rt._build_lane, BuildLane)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
