"""Registering a heartbeat is not the same as being watched.

``HEARTBEAT_EXPECTED`` drives the DISPLAY tool and the morning report; the
staleness ALERT iterates its own hardcoded tuple in ``errors.py``. Adding a
subsystem to the first and not the second gives it a row nobody reads — built,
not wired — which for this subsystem is the whole point: a dead stranded-work
detector keeps answering "what fell through the cracks?" with its last, stale,
confident zero, and nothing else in the system contradicts it.

The enable gate is the other half. Three first-class levers stop the detector,
and a deliberately-stopped subsystem that alarms forever is exactly what
``_subsystem_enabled`` exists to prevent.
"""

from pathlib import Path

import pytest

import genesis.mcp.health.errors as errors_mod
from genesis.mcp.health.manifest import (
    _NO_BOOT_PULSE_SUBSYSTEMS,
    HEARTBEAT_EXPECTED,
    _never_started_grace_s,
    _subsystem_enabled,
)


def test_zero_drop_has_a_heartbeat_expectation():
    interval, overdue = HEARTBEAT_EXPECTED["zero_drop"]
    assert interval == 3600, "the session-boundary debounce is the expected cadence"
    assert overdue >= 2 * 86400, (
        "the wall-clock floor is DAILY, so anything under ~2 days fires on a quiet "
        "weekend and teaches everyone to ignore it"
    )


def test_zero_drop_is_exempt_from_the_boot_pulse_inference():
    """A detached worker takes no part in bootstrap, so it can never emit a
    start pulse. Without the exemption every fresh boot false-flags it as
    never_started until the first sweep lands."""
    assert "zero_drop" in _NO_BOOT_PULSE_SUBSYSTEMS
    interval, overdue = HEARTBEAT_EXPECTED["zero_drop"]
    assert _never_started_grace_s("zero_drop") == float(overdue)


def test_the_staleness_alert_actually_watches_zero_drop():
    """The alert loop is a hardcoded tuple, NOT HEARTBEAT_EXPECTED — so this is
    the difference between a display row and an alarm."""
    source = Path(errors_mod.__file__).read_text()
    assert '"zero_drop")' in source and "for _hb_name in (" in source
    loop_line = next(ln for ln in source.splitlines() if ln.strip().startswith("for _hb_name in ("))
    assert "zero_drop" in loop_line, f"not in the watched tuple: {loop_line}"


@pytest.mark.parametrize("mode,expected", [("off", False), ("observe", True), ("alert", True)])
def test_a_disabled_detector_does_not_alarm_forever(monkeypatch, mode, expected):
    """`enabled: false` and `mode: off` both stop the pulse. Without the enable
    gate the subsystem then reads overdue at 48h, forever — the permanent false
    alarm the gate exists to prevent."""
    import genesis.session_awareness.zero_drop_config as cfg_mod

    monkeypatch.setattr(cfg_mod, "effective_mode", lambda: mode)
    assert _subsystem_enabled("zero_drop") is expected


def test_the_enable_gate_fails_toward_surfacing(monkeypatch):
    """A config read that BLOWS UP must not silence the alarm — an unreadable
    lever is not a decision to disable."""
    import genesis.session_awareness.zero_drop_config as cfg_mod

    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(cfg_mod, "effective_mode", _boom)
    assert _subsystem_enabled("zero_drop") is True
