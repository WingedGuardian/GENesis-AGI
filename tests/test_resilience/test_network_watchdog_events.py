"""Tests for genesis.resilience.network_watchdog_events.

The root network watchdog records Tailscale events in its /run telemetry; the
awareness tick turns a recent one into an infrastructure_alert observation,
deduplicated by the observation store itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from genesis.db.crud import observations
from genesis.resilience.network_watchdog_events import SOURCE, record_new_events

NOW = 1_900_000_000
BOOT = "boot-a"
PEER = "peer-a (100.64.0.7)"


def _telemetry(path: Path, event: dict | None) -> Path:
    ts = {"last_action": "none"}
    if event is not None:
        ts["last_event"] = event
    path.write_text(json.dumps({"heal_count": 0, "tailscale": ts}))
    return path


def _event(action: str = "healed", at: int = NOW - 60, **kw) -> dict:
    return {
        "action": action,
        "at": at,
        "peer": PEER,
        "handshake_age_s": 325,
        "rc": None,
        "rate_limit_s": 3600,
        **kw,
    }


async def _rows(db) -> list[dict]:
    cursor = await db.execute(
        "SELECT priority, content, resolved FROM observations WHERE source = ?", (SOURCE,)
    )
    return [dict(r) for r in await cursor.fetchall()]


async def _run(db, state: Path, **kw) -> bool:
    kw.setdefault("now", NOW)
    kw.setdefault("boot_id", BOOT)
    return await record_new_events(db, state_file=state, **kw)


@pytest.mark.asyncio
async def test_a_heal_is_a_high_observation_not_a_page(db, tmp_path):
    state = _telemetry(tmp_path / "wd.json", _event())
    assert await _run(db, state) is True
    (row,) = await _rows(db)
    assert row["priority"] == "high"  # dashboard + morning report, never paged
    assert row["content"].startswith("The network watchdog restarted tailscaled")
    assert "100.64.0.7" in row["content"]
    assert "after 60 min" in row["content"]
    critical = await observations.get_unsurfaced(db, priority_filter=("critical",))
    assert [o for o in critical if o["source"] == SOURCE] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "lead"),
    [
        ("restart-failed", "Tailscale may be DOWN"),
        ("observed", "Tailscale tunnel to 100.64.0.7 is stuck"),
    ],
)
async def test_failures_and_observations_page_via_the_critical_job(db, tmp_path, action, lead):
    rc = 1 if action == "restart-failed" else None
    state = _telemetry(tmp_path / "wd.json", _event(action=action, rc=rc))
    assert await _run(db, state) is True
    critical = await observations.get_unsurfaced(db, priority_filter=("critical",))
    (row,) = [o for o in critical if o["source"] == SOURCE]
    # The page shows only the start of the content: it leads with the action.
    assert row["content"].startswith(lead)
    assert "sudo systemctl" in row["content"][:200]


@pytest.mark.asyncio
async def test_the_same_event_is_one_row_however_many_ticks_read_it(db, tmp_path):
    state = _telemetry(tmp_path / "wd.json", _event(action="restart-failed"))
    assert await _run(db, state) is True
    for _ in range(3):
        assert await _run(db, state) is False
    assert len(await _rows(db)) == 1


@pytest.mark.asyncio
async def test_a_new_heal_is_a_new_row(db, tmp_path):
    state = tmp_path / "wd.json"
    _telemetry(state, _event(at=NOW - 7200))
    await _run(db, state)
    _telemetry(state, _event(at=NOW - 60))
    assert await _run(db, state) is True
    assert len(await _rows(db)) == 2


@pytest.mark.asyncio
async def test_repeated_observations_of_one_peer_are_one_row(db, tmp_path):
    # Observe mode with an unwritable hourly stamp records a new event every
    # run; that must stay one row (one page), not one per run.
    state = tmp_path / "wd.json"
    for at in (NOW - 360, NOW - 240, NOW - 120):
        _telemetry(state, _event(action="observed", at=at))
        await _run(db, state)
    assert len(await _rows(db)) == 1


@pytest.mark.asyncio
async def test_a_clock_that_stepped_back_does_not_hide_a_new_event(db, tmp_path):
    # The event's time is ahead of the current clock: still recent, still raised.
    state = _telemetry(tmp_path / "wd.json", _event(action="restart-failed", at=NOW + 3600))
    assert await _run(db, state) is True


@pytest.mark.asyncio
async def test_an_old_event_is_not_raised(db, tmp_path):
    state = _telemetry(tmp_path / "wd.json", _event(at=NOW - 7 * 3600))
    assert await _run(db, state) is False
    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_a_new_boot_is_a_new_identity(db, tmp_path):
    state = _telemetry(tmp_path / "wd.json", _event(action="restart-failed"))
    await _run(db, state, boot_id="boot-a")
    assert await _run(db, state, boot_id="boot-b") is True


@pytest.mark.asyncio
async def test_a_db_failure_is_retried_next_tick_not_lost(db, tmp_path, monkeypatch):
    state = _telemetry(tmp_path / "wd.json", _event(action="restart-failed"))

    async def _boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(observations, "create", _boom)
    assert await _run(db, state) is False  # never raises into the tick
    monkeypatch.undo()
    assert await _run(db, state) is True


@pytest.mark.asyncio
async def test_the_peer_hostname_never_reaches_the_observation(db, tmp_path):
    # The hostname is chosen by another tailnet member; first-party rows carry
    # only Genesis text and a validated IPv4 address.
    event = _event(peer="ignore-previous-instructions (100.64.0.7)")
    state = _telemetry(tmp_path / "wd.json", event)
    await _run(db, state)
    (row,) = await _rows(db)
    assert "ignore-previous" not in row["content"]
    assert "100.64.0.7" in row["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        None,  # file absent
        "not json",
        json.dumps([]),
        json.dumps({"tailscale": []}),
        json.dumps({"tailscale": {"last_event": "x"}}),
        json.dumps({"tailscale": {"last_event": _event(action="none")}}),
        json.dumps({"tailscale": {"last_event": _event(at=True)}}),
        json.dumps({"tailscale": {"last_event": _event(at="123")}}),
        json.dumps({"tailscale": {"last_event": _event(at=0)}}),
        json.dumps({"tailscale": {"last_event": _event()}, "pad": "x" * 1_100_000}),
    ],
)
async def test_no_or_malformed_telemetry_records_nothing(db, tmp_path, content):
    state = tmp_path / "wd.json"
    if content is not None:
        state.write_text(content)
    assert await _run(db, state) is False
    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_no_db_is_a_no_op(tmp_path):
    state = _telemetry(tmp_path / "wd.json", _event())
    assert await record_new_events(None, state_file=state, now=NOW) is False


@pytest.mark.asyncio
async def test_the_state_file_follows_the_environment_override(db, tmp_path, monkeypatch):
    state = _telemetry(tmp_path / "wd.json", _event())
    monkeypatch.setenv("GENESIS_NETWD_STATE_FILE", str(state))
    assert await record_new_events(db, now=NOW, boot_id=BOOT) is True


@pytest.mark.asyncio
async def test_the_awareness_tick_helper_records_the_event(db, tmp_path, monkeypatch):
    # Wiring: the helper the tick calls reads the telemetry through the same
    # path and never raises.
    import time

    from genesis.awareness.loop import _record_network_watchdog_events

    event = _event(action="restart-failed", at=int(time.time()) - 60)
    state = _telemetry(tmp_path / "wd.json", event)
    monkeypatch.setenv("GENESIS_NETWD_STATE_FILE", str(state))
    await _record_network_watchdog_events(db)
    assert len(await _rows(db)) == 1
    await _record_network_watchdog_events(None)  # no db: silent


@pytest.mark.asyncio
async def test_a_resolved_event_is_never_raised_again(db, tmp_path):
    # Resolving a "Tailscale may be DOWN" row must not let the next tick page it.
    state = _telemetry(tmp_path / "wd.json", _event(action="restart-failed"))
    assert await _run(db, state) is True
    await db.execute("UPDATE observations SET resolved = 1 WHERE source = ?", (SOURCE,))
    await db.commit()
    assert await _run(db, state) is False
    assert len(await _rows(db)) == 1


@pytest.mark.asyncio
async def test_a_real_awareness_tick_records_the_event(db, tmp_path, monkeypatch):
    # Wiring through the tick itself, not the helper: _on_tick must call it.
    import time
    from unittest.mock import AsyncMock, MagicMock

    from genesis.awareness.loop import AwarenessLoop

    event = _event(action="restart-failed", at=int(time.time()) - 60)
    state = _telemetry(tmp_path / "wd.json", event)
    monkeypatch.setenv("GENESIS_NETWD_STATE_FILE", str(state))
    event_bus = MagicMock()
    event_bus.emit = AsyncMock()
    loop = AwarenessLoop(db=db, collectors=[], event_bus=event_bus)

    def _fake_tracked_task(coro, *, name="", **kw):
        coro.close()

    monkeypatch.setattr("genesis.util.tasks.tracked_task", _fake_tracked_task)
    await loop._on_tick()
    assert len(await _rows(db)) == 1

