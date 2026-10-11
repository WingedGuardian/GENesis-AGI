"""The board reconciler is wired, watched and readable — not just built.

Registering a heartbeat is not being watched: ``HEARTBEAT_EXPECTED`` drives the
display, but the staleness ALERT iterates its own hardcoded tuple. The bootstrap
step is what gives the manifest a ``board`` key, without which a reconciler
that never started reads as benign. And a read tool missing from the
reflection allowlist is silently denied to reflection sessions.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import genesis.mcp.health.errors as errors_mod
from genesis.board import config as board_config
from genesis.board import reconciler
from genesis.mcp.health.manifest import (
    _NO_BOOT_PULSE_SUBSYSTEMS,
    _PAUSE_GATED_HEARTBEATS,
    HEARTBEAT_EXPECTED,
    _never_started_grace_s,
)
from genesis.runtime.init.board import init_board


def test_board_has_a_heartbeat_expectation_matching_its_tick():
    interval, overdue = HEARTBEAT_EXPECTED["board"]
    assert interval == reconciler.INTERVAL_S == 300
    assert overdue >= 3 * interval, "one slow tick must not read as overdue"


def test_board_pulses_through_pause_and_every_mode_so_it_is_neither_gated_nor_exempt():
    assert "board" not in _PAUSE_GATED_HEARTBEATS
    assert "board" not in _NO_BOOT_PULSE_SUBSYSTEMS, "it emits a start pulse at wiring"
    interval, _ = HEARTBEAT_EXPECTED["board"]
    assert _never_started_grace_s("board") == float(interval + 60)


def test_the_staleness_alert_actually_watches_board():
    source = Path(errors_mod.__file__).read_text()
    loop_line = next(ln for ln in source.splitlines() if ln.strip().startswith("for _hb_name in ("))
    assert '"board"' in loop_line, f"not in the watched tuple: {loop_line}"


def test_bootstrap_checks_the_board_step():
    from genesis.runtime._core import GenesisRuntime

    assert GenesisRuntime._INIT_CHECKS["board"] == "_board_reconciler"


def test_board_tools_are_in_the_reflection_read_allowlist():
    from genesis.cc.session_config import _REFLECTION_READ_MCP

    assert {"board_status", "board_item"} <= _REFLECTION_READ_MCP
    assert "board_promote" not in _REFLECTION_READ_MCP, "a write tool is not a read tool"


async def test_both_read_tools_are_registered_on_the_health_server():
    from genesis.mcp.health import mcp

    registered = await mcp.get_tools()
    assert {"board_status", "board_item", "board_promote"} <= set(registered)


def test_the_capability_is_described():
    from genesis.runtime._capabilities import _CAPABILITY_DESCRIPTIONS

    assert "board" in _CAPABILITY_DESCRIPTIONS


class _Scheduler:
    def __init__(self, running=True):
        self.running = running
        self.jobs = []

    def add_job(self, func, trigger, **kw):
        self.jobs.append((func, trigger, kw))


class _Bus:
    def __init__(self):
        self.emits = []

    async def emit(self, subsystem, severity, event_type, message, **details):
        self.emits.append((str(subsystem), event_type, details))


@pytest.mark.parametrize("scheduler", [None, _Scheduler(running=False)])
async def test_init_board_raises_when_the_learning_scheduler_is_unavailable(scheduler):
    """The manifest then records the real cause instead of blaming the board."""
    rt = SimpleNamespace(_learning_scheduler=scheduler, _event_bus=_Bus(), _board_reconciler=None)
    with pytest.raises(RuntimeError, match="learning scheduler unavailable"):
        await init_board(rt)
    assert rt._board_reconciler is None
    assert rt._event_bus.emits == []


async def test_init_board_wires_a_five_minute_cron_job_and_a_start_pulse(monkeypatch):
    monkeypatch.setattr(board_config, "effective_mode", lambda: "off")
    sched = _Scheduler()
    rt = SimpleNamespace(_learning_scheduler=sched, _event_bus=_Bus(), _board_reconciler=None)
    await init_board(rt)
    [(_func, trigger, kw)] = sched.jobs
    assert kw["id"] == reconciler.JOB_ID and kw["max_instances"] == 1
    assert type(trigger).__name__ == "CronTrigger" and "*/5" in str(trigger)
    assert rt._board_reconciler == reconciler.JOB_ID
    [(subsystem, event_type, details)] = rt._event_bus.emits
    assert (subsystem, event_type) == ("board", "heartbeat")
    assert details["board_state"] == "starting" and details["mode"] == "off"
