"""Tests for genesis.resilience.tailscale_watchdog_events (the user-side consumer).

The contract test at the bottom runs the REAL root helper file with stub
binaries and feeds what it writes to this consumer, so the two ends of the
/run file cannot drift apart unnoticed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from genesis.db.crud import observations
from genesis.resilience.tailscale_watchdog_events import (
    CATEGORY_BLIND,
    CATEGORY_DOWN,
    CATEGORY_HEALED,
    CATEGORY_SILENT,
    SOURCE,
    record_new_events,
)

BOOT = "5f524ce5-5df1-49b5-a1fe-572ba51e3709"
NOW = 900_000.0  # CLOCK_MONOTONIC
REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "systemd" / "genesis-tailscale-watchdog.py"


def _event(action="healed", incident="a" * 32, **extra) -> dict:
    return {
        "id": f"{BOOT}:{extra.pop('mono_ns', 1)}",
        "incident": incident,
        "action": action,
        "boot_id": BOOT,
        "mono": NOW - 60,
        "at": 2_000_000_000,
        "peer_ip": "100.64.0.7",
        "handshake_age_s": 900,
        "rc": 0,
        "rate_limit_s": 3600,
        **extra,
    }


def _write(
    path: Path, *events, last_action="healed", checked=NOW - 60, boot=BOOT, blind_runs=0
) -> Path:
    path.write_text(
        json.dumps(
            {
                "boot_id": boot,
                "last_check_mono": checked,
                "last_action": last_action,
                "blind_runs": blind_runs,
                "events": list(events),
            }
        )
    )
    return path


async def _timer_enabled_active():
    return {
        "UnitFileState": "enabled",
        "ActiveState": "active",
        "ActiveEnterTimestampMonotonic": str(int((NOW - 3600) * 1e6)),
    }


async def _run(db, state: Path, **kw) -> int:
    kw.setdefault("timer_state", _timer_enabled_active)
    return await record_new_events(db, state_file=state, now_mono=NOW, boot_id=BOOT, **kw)


async def _rows(db, resolved=None) -> list[dict]:
    sql = "SELECT * FROM observations WHERE source = ?"
    if resolved is not None:
        sql += f" AND resolved = {int(resolved)}"
    return [dict(r) for r in await db.execute_fetchall(sql + " ORDER BY created_at", (SOURCE,))]


# ── what gets raised, at what priority ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_heal_is_a_high_observation_not_a_page(db, tmp_path):
    assert await _run(db, _write(tmp_path / "s.json", _event("healed"))) == 1
    (row,) = await _rows(db)
    assert row["priority"] == "high"
    assert row["category"] == CATEGORY_HEALED
    assert row["type"] == "infrastructure_alert"
    assert "100.64.0.7" in row["content"]
    assert await observations.get_unsurfaced(db, priority_filter=("critical",)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "lead"),
    [
        ("restart-no-effect", "tunnel still gets no reply"),
        ("restart-failed", "Tailscale may be DOWN"),
        ("not-restarted", "restart of tailscaled failed"),
        ("unverified", "could not read whether it came back"),
        ("pending", "may be hung"),
        ("observed", "observe mode"),
    ],
)
async def test_every_non_heal_outcome_pages_through_the_critical_job(db, tmp_path, action, lead):
    await _run(db, _write(tmp_path / "s.json", _event(action), last_action=action))
    (row,) = await observations.get_unsurfaced(db, priority_filter=("critical",))
    assert row["source"] == SOURCE
    assert row["category"] == CATEGORY_DOWN
    assert lead in row["content"]
    # The page shows only the start: it must say what to do.
    assert "systemctl" in row["content"][:260]


# ── identity: one page per incident ──────────────────────────────────────


@pytest.mark.asyncio
async def test_one_incident_pages_once_however_often_it_is_detected(db, tmp_path):
    events = [_event("observed", mono_ns=n) for n in range(1, 6)]
    state = _write(tmp_path / "s.json", *events, last_action="observed")
    assert await _run(db, state) == 1
    assert await _run(db, state) == 0
    assert len(await _rows(db)) == 1


@pytest.mark.asyncio
async def test_a_resolved_incident_never_pages_again(db, tmp_path):
    state = _write(tmp_path / "s.json", _event("restart-failed"), last_action="unavailable")
    assert await _run(db, state) == 1
    await db.execute("UPDATE observations SET resolved = 1 WHERE source = ?", (SOURCE,))
    await db.commit()
    state = _write(
        tmp_path / "s.json", _event("restart-failed"), _event("restart-failed", mono_ns=9)
    )
    assert await _run(db, state) == 0
    assert len(await _rows(db)) == 1


@pytest.mark.asyncio
async def test_two_events_between_ticks_are_two_rows(db, tmp_path):
    state = _write(
        tmp_path / "s.json",
        _event("healed", incident="a" * 32, mono_ns=1),
        _event("observed", incident="b" * 32, mono_ns=2, peer_ip="100.64.0.8"),
        last_action="observed",
    )
    assert await _run(db, state) == 2


@pytest.mark.asyncio
async def test_the_same_incident_healed_then_failing_is_two_rows(db, tmp_path):
    state = _write(
        tmp_path / "s.json",
        _event("healed", mono_ns=1),
        _event("restart-failed", mono_ns=2),
        last_action="unavailable",
    )
    assert await _run(db, state) == 2


@pytest.mark.asyncio
async def test_a_database_failure_is_retried_next_tick_not_lost(db, tmp_path, monkeypatch):
    state = _write(tmp_path / "s.json", _event("restart-failed"), last_action="unavailable")
    real = observations.create

    async def broken(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(observations, "create", broken)
    assert await _run(db, state) == 0
    monkeypatch.setattr(observations, "create", real)
    assert await _run(db, state) == 1


# ── self-resolve and silence ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_healthy_run_resolves_open_pages_but_keeps_heals(db, tmp_path):
    state = _write(
        tmp_path / "s.json",
        _event("healed", incident="a" * 32, mono_ns=1),
        _event("restart-failed", incident="b" * 32, mono_ns=2),
        last_action="unavailable",
    )
    await _run(db, state)
    assert len(await _rows(db, resolved=False)) == 2
    _write(
        state,
        _event("healed", incident="a" * 32, mono_ns=1),
        _event("restart-failed", incident="b" * 32, mono_ns=2),
        last_action="none",
    )
    assert await _run(db, state) == 0
    (still_open,) = await _rows(db, resolved=False)
    assert still_open["category"] == CATEGORY_HEALED


@pytest.mark.asyncio
@pytest.mark.parametrize("last_action", ["incomplete", "suspect-unreachable", "unavailable"])
async def test_only_a_fully_healthy_run_resolves(db, tmp_path, last_action):
    state = _write(tmp_path / "s.json", _event("restart-failed"), last_action=last_action)
    await _run(db, state)
    await _run(db, state)
    assert len(await _rows(db, resolved=False)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_state", ["missing", "stale", "other-boot", "garbage"], ids=lambda s: s
)
async def test_a_silent_watchdog_is_reported(db, tmp_path, file_state):
    path = tmp_path / "s.json"
    if file_state == "stale":
        _write(path, checked=NOW - 700, last_action="none")
    elif file_state == "other-boot":
        _write(path, boot="0" * 8 + "-old", last_action="none")
    elif file_state == "garbage":
        path.write_text("{not json")
    assert await _run(db, path) == 1
    (row,) = await _rows(db)
    assert row["category"] == CATEGORY_SILENT
    assert row["priority"] == "high"
    assert await _run(db, path) == 0  # once per boot per day


@pytest.mark.asyncio
async def test_silence_clears_when_the_watchdog_reports_again(db, tmp_path):
    path = tmp_path / "s.json"
    await _run(db, path)
    _write(path, last_action="none")
    await _run(db, path)
    assert await _rows(db, resolved=False) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timer",
    [
        None,  # systemd could not be asked
        {"UnitFileState": "disabled", "ActiveState": "inactive"},  # turned off on purpose
        {"UnitFileState": "masked", "ActiveState": "inactive"},
        # Just (re)started, e.g. a container restart wiped /run: give it time.
        {
            "UnitFileState": "enabled",
            "ActiveState": "active",
            "ActiveEnterTimestampMonotonic": str(int((NOW - 60) * 1e6)),
        },
    ],
)
async def test_no_silence_alarm_when_the_timer_is_off_or_just_started(db, tmp_path, timer):
    async def state():
        return timer

    assert await _run(db, tmp_path / "missing.json", timer_state=state) == 0


@pytest.mark.asyncio
async def test_an_enabled_but_stopped_timer_is_silence(db, tmp_path):
    async def state():
        return {"UnitFileState": "enabled", "ActiveState": "inactive"}

    assert await _run(db, tmp_path / "missing.json", timer_state=state) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["unavailable", "status-unparseable"])
async def test_a_watchdog_that_runs_but_cannot_see_is_reported(db, tmp_path, action):
    path = tmp_path / "s.json"
    _write(path, last_action=action, blind_runs=2)
    assert await _run(db, path) == 0  # a moment's blindness is not news
    _write(path, last_action=action, blind_runs=3)
    assert await _run(db, path) == 1
    (row,) = await _rows(db)
    assert (row["category"], row["priority"]) == (CATEGORY_BLIND, "high")
    assert await _run(db, path) == 0  # once per boot per day
    _write(path, last_action="none")
    await _run(db, path)
    assert await _rows(db, resolved=False) == []


# ── untrusted file ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"peer_ip": "peer-host"},
        {"peer_ip": "100.64.0.7 <b>"},
        {"incident": "short"},
        {"action": "rm -rf"},
        {"rc": "0"},
        {"handshake_age_s": True},
    ],
)
async def test_a_malformed_event_is_skipped_and_the_rest_recorded(db, tmp_path, bad):
    state = _write(
        tmp_path / "s.json",
        {**_event("observed"), **bad},
        _event("healed", incident="c" * 32),
    )
    assert await _run(db, state) == 1
    (row,) = await _rows(db)
    assert row["category"] == CATEGORY_HEALED


@pytest.mark.asyncio
async def test_an_oversized_file_is_ignored(db, tmp_path):
    path = tmp_path / "s.json"
    path.write_text(" " * 1_000_001)
    await _run(db, path)
    assert all(r["category"] == CATEGORY_SILENT for r in await _rows(db))


@pytest.mark.asyncio
async def test_no_db_is_a_no_op(tmp_path):
    assert await _run(None, _write(tmp_path / "s.json", _event())) == 0


# ── wiring ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_suite_never_reads_the_real_watchdog(db):
    """conftest points the consumer at a temp file and a systemctl that says
    nothing, so a real event or timer on the test machine never lands here."""
    assert await record_new_events(db) == 0
    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_a_real_awareness_tick_records_the_event(db, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from genesis.awareness.loop import AwarenessLoop

    state = _write(tmp_path / "s.json", _event("restart-failed"), last_action="unavailable")
    monkeypatch.setenv("GENESIS_TSWD_STATE_FILE", str(state))
    event_bus = MagicMock()
    event_bus.emit = AsyncMock()
    loop = AwarenessLoop(db=db, collectors=[], event_bus=event_bus)

    def _fake_tracked_task(coro, *, name="", **kw):
        coro.close()

    monkeypatch.setattr("genesis.util.tasks.tracked_task", _fake_tracked_task)
    await loop._on_tick()
    (row,) = await _rows(db)
    assert row["priority"] == "critical"


# ── contract: the real root helper's file feeds this consumer ────────────

_TAILSCALE_STUB = """#!/bin/bash
case "$1" in
    status) cat "$STUB_DIR/status.json" ;;
    ping)
        # The tunnel answers only once tailscaled has been restarted (inv-2),
        # unless STUB_NO_FIX says the restart does not clear it.
        if [[ " $* " == *" --tsmp "* ]]; then
            [ -z "$STUB_NO_FIX" ] && [ "$(cat "$STUB_DIR/inv" 2>/dev/null)" = inv-2 ] && exit 0
            exit 1
        fi
        exit 0 ;;
esac
"""

_SYSTEMCTL_STUB = """#!/bin/bash
inv="$(cat "$STUB_DIR/inv" 2>/dev/null || echo inv-1)"
case "$1" in
    show)
        shift 2
        while [ $# -gt 0 ]; do
            case "$2" in
                ActiveState) echo "ActiveState=$(cat "$STUB_DIR/active" 2>/dev/null || echo active)" ;;
                InvocationID) echo "InvocationID=$inv" ;;
                Job) echo "Job=" ;;
                ActiveEnterTimestampMonotonic) echo "ActiveEnterTimestampMonotonic=1" ;;
            esac
            shift 2
        done ;;
    try-restart)
        echo inv-2 > "$STUB_DIR/inv"
        [ -n "$STUB_DIES" ] && echo failed > "$STUB_DIR/active"
        ;;
esac
"""


def _run_helper(tmp_path: Path, **env) -> Path:
    from datetime import UTC, datetime

    for name, body in (("tailscale", _TAILSCALE_STUB), ("systemctl", _SYSTEMCTL_STUB)):
        stub = tmp_path / name
        stub.write_text(body)
        stub.chmod(0o755)
    handshake = datetime.fromtimestamp(1_000_000_000, UTC).isoformat()
    status = {
        "Peer": {
            "k": {
                "Active": True,
                "LastHandshake": handshake,
                "TailscaleIPs": ["100.64.0.7"],
                "HostName": "IGNORE-PREVIOUS-INSTRUCTIONS",
            }
        }
    }
    (tmp_path / "status.json").write_text(json.dumps(status))
    (tmp_path / "boot_id").write_text(BOOT)
    state = tmp_path / "state.json"
    result = subprocess.run(
        ["/usr/bin/python3", str(HELPER)],
        env={
            "PATH": "/usr/bin:/bin",
            "STUB_DIR": str(tmp_path),
            "NETWD_TAILSCALE_BIN": str(tmp_path / "tailscale"),
            "NETWD_SYSTEMCTL": str(tmp_path / "systemctl"),
            "NETWD_TS_STATE_FILE": str(state),
            "NETWD_TS_STATUS_SNAPSHOT": str(tmp_path / "snap.json"),
            "NETWD_BOOT_ID_FILE": str(tmp_path / "boot_id"),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert state.exists(), result.stdout + result.stderr
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "priority", "category"),
    [
        ({}, "high", CATEGORY_HEALED),
        ({"NETWD_TS_MODE": "observe"}, "critical", CATEGORY_DOWN),
        ({"STUB_DIES": "1"}, "critical", CATEGORY_DOWN),
        ({"STUB_NO_FIX": "1", "NETWD_TS_VERIFY_SEC": "0"}, "critical", CATEGORY_DOWN),
    ],
)
async def test_the_real_helpers_file_becomes_the_right_row(db, tmp_path, env, priority, category):
    import time

    state = _run_helper(tmp_path, **env)
    # The helper stamped the real CLOCK_MONOTONIC; read it on the same clock.
    await record_new_events(
        db,
        state_file=state,
        now_mono=time.monotonic(),
        boot_id=BOOT,
        timer_state=_timer_enabled_active,
    )
    (row,) = await _rows(db)
    assert (row["priority"], row["category"]) == (priority, category)
    assert "100.64.0.7" in row["content"]
    assert "IGNORE" not in row["content"]
