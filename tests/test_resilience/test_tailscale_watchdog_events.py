"""Tests for genesis.resilience.tailscale_watchdog_events (the user-side consumer).

The contract tests at the bottom run the REAL root helper file with stub
binaries and feed what it writes to this consumer, so the two ends of the /run
file cannot drift apart unnoticed.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from genesis.db.crud import observations
from genesis.resilience.tailscale_watchdog_events import (
    CATEGORY_BLIND,
    CATEGORY_DOWN,
    CATEGORY_RESTART,
    CATEGORY_SILENT,
    SOURCE,
    record_new_events,
)

BOOT = "5f524ce5-5df1-49b5-a1fe-572ba51e3709"
NOW = 900_000.0  # CLOCK_MONOTONIC
A, B = "100.64.0.7", "100.64.0.8"
REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "systemd" / "genesis-tailscale-watchdog.py"


def _event(action="healed", peers=(A,), cleared=None, n=1, **extra) -> dict:
    return {
        "id": f"{BOOT}:{n}",
        "action": action,
        "boot_id": BOOT,
        "mono": NOW - 60,
        "at": 2_000_000_000,
        "peers": list(peers),
        "cleared": list(peers if cleared is None else cleared),
        "rc": 0,
        "rate_limit_s": 3600,
        **extra,
    }


def _write(
    path: Path,
    *,
    evidence=None,
    present=None,
    complete=False,
    events=(),
    last_action="none",
    checked=NOW - 60,
    boot=BOOT,
    blind_runs=0,
    mode="live",
    ages=None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "boot_id": boot,
                "last_check_mono": checked,
                "last_action": last_action,
                "mode": mode,
                "blind_runs": blind_runs,
                "evidence": evidence or {},
                "handshake_age_s": ages or {},
                "present": present if present is not None else [],
                "present_complete": complete,
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
        "ServiceActiveState": "inactive",
    }


async def _run(db, state: Path, **kw) -> int:
    kw.setdefault("timer_state", _timer_enabled_active)
    return await record_new_events(db, state_file=state, now_mono=NOW, boot_id=BOOT, **kw)


async def _rows(db, resolved=None, category=None) -> list[dict]:
    sql = "SELECT * FROM observations WHERE source = ?"
    params: list = [SOURCE]
    if resolved is not None:
        sql += " AND resolved = ?"
        params.append(int(resolved))
    if category is not None:
        sql += " AND category = ?"
        params.append(category)
    return [dict(r) for r in await db.execute_fetchall(sql + " ORDER BY created_at", params)]


async def _pages(db) -> list[dict]:
    return await observations.get_unsurfaced(db, priority_filter=("critical",))


async def _age_resolution(db, seconds: float) -> None:
    """Pretend every resolved row was resolved ``seconds`` ago."""
    when = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    await db.execute("UPDATE observations SET resolved_at = ? WHERE resolved = 1", (when,))
    await db.commit()


# ── the condition: one alert per stuck peer ──────────────────────────────


@pytest.mark.asyncio
async def test_a_stuck_peer_pages_once_however_many_ticks_see_it(db, tmp_path):
    state = _write(tmp_path / "s.json", evidence={A: "stuck"}, ages={A: 900}, last_action="stuck")
    assert await _run(db, state) == 1
    assert await _run(db, state) == 0
    (row,) = await _pages(db)
    assert (row["category"], row["priority"]) == (CATEGORY_DOWN, "critical")
    assert A in row["content"] and "for 900s" in row["content"]
    assert "sudo systemctl restart tailscaled" in row["content"][:300]


@pytest.mark.asyncio
async def test_two_stuck_peers_are_two_alerts(db, tmp_path):
    state = _write(tmp_path / "s.json", evidence={A: "stuck", B: "stuck"})
    assert await _run(db, state) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["ok", "offline"])
async def test_ok_or_offline_evidence_resolves_the_alert(db, tmp_path, verdict):
    path = tmp_path / "s.json"
    await _run(db, _write(path, evidence={A: "stuck", B: "stuck"}))
    await _run(db, _write(path, evidence={A: verdict}))
    (still_open,) = await _rows(db, resolved=False)
    assert B in still_open["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {},  # no evidence about the peer: unknown
        {"last_action": "unavailable"},
        {"last_action": "incomplete", "present": [B], "complete": False},
        {"checked": NOW - 700, "evidence": {A: "ok"}},  # stale: not evidence of now
        {"boot": "00000000-old", "evidence": {A: "ok"}},
    ],
    ids=["unknown", "blind", "incomplete-list", "stale", "other-boot"],
)
async def test_nothing_but_evidence_resolves_an_alert(db, tmp_path, overrides):
    path = tmp_path / "s.json"
    await _run(db, _write(path, evidence={A: "stuck"}))
    await _run(db, _write(path, **overrides))
    assert len(await _rows(db, resolved=False, category=CATEGORY_DOWN)) == 1


@pytest.mark.asyncio
async def test_a_peer_gone_from_a_complete_list_is_resolved(db, tmp_path):
    path = tmp_path / "s.json"
    await _run(db, _write(path, evidence={A: "stuck", B: "stuck"}))
    await _run(db, _write(path, present=[B], complete=True))
    (still_open,) = await _rows(db, resolved=False)
    assert B in still_open["content"]


@pytest.mark.asyncio
async def test_a_stale_file_raises_nothing(db, tmp_path):
    state = _write(tmp_path / "s.json", evidence={A: "stuck"}, checked=NOW - 700)
    await _run(db, state)
    assert await _rows(db, category=CATEGORY_DOWN) == []


@pytest.mark.asyncio
async def test_a_flap_reopens_the_same_alert_without_paging_again(db, tmp_path):
    path = tmp_path / "s.json"
    await _run(db, _write(path, evidence={A: "stuck"}))
    (first,) = await _rows(db)
    await observations.mark_surfaced(db, [first["id"]], datetime.now(UTC).isoformat())
    await _run(db, _write(path, evidence={A: "ok"}))
    assert await _run(db, _write(path, evidence={A: "stuck"})) == 0
    (row,) = await _rows(db)
    assert (row["id"], row["resolved"]) == (first["id"], 0)
    assert await _pages(db) == []  # it already paged once


@pytest.mark.asyncio
async def test_a_recurrence_long_after_recovery_is_a_new_alert(db, tmp_path):
    path = tmp_path / "s.json"
    await _run(db, _write(path, evidence={A: "stuck"}))
    await _run(db, _write(path, evidence={A: "ok"}))
    await _age_resolution(db, 3601)
    assert await _run(db, _write(path, evidence={A: "stuck"})) == 1
    assert len(await _rows(db, category=CATEGORY_DOWN)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("note", ["handled by hand", "pruned: stale"])
async def test_an_alert_resolved_by_anyone_while_still_true_comes_back_without_paging(
    db, tmp_path, note
):
    state = _write(tmp_path / "s.json", evidence={A: "stuck"})
    await _run(db, state)
    (first,) = await _rows(db)
    await observations.mark_surfaced(db, [first["id"]], datetime.now(UTC).isoformat())
    await db.execute(
        "UPDATE observations SET resolved = 1, resolved_at = ?, resolution_notes = ?",
        (datetime.now(UTC).isoformat(), note),
    )
    await db.commit()
    assert await _run(db, state) == 0
    (row,) = await _rows(db, resolved=False)
    assert row["id"] == first["id"]
    assert await _pages(db) == []


@pytest.mark.asyncio
async def test_a_stuck_alert_expires_a_day_after_it_was_last_seen_stuck(db, tmp_path):
    state = _write(tmp_path / "s.json", evidence={A: "stuck"})
    await _run(db, state)
    (row,) = await _rows(db)
    left = datetime.fromisoformat(row["expires_at"]) - datetime.now(UTC)
    assert timedelta(hours=23) < left <= timedelta(hours=24)
    # Seen stuck again later: the expiry moves a day ahead of that sighting.
    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await observations.set_expires_at(db, row["id"], soon)
    await _run(db, state)
    (row,) = await _rows(db)
    left = datetime.fromisoformat(row["expires_at"]) - datetime.now(UTC)
    assert left > timedelta(hours=23)


@pytest.mark.asyncio
async def test_an_alert_that_expired_unseen_pages_again_when_seen_stuck(db, tmp_path):
    state = _write(tmp_path / "s.json", evidence={A: "stuck"})
    await _run(db, state)
    (row,) = await _rows(db)
    await observations.mark_surfaced(db, [row["id"]], datetime.now(UTC).isoformat())
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await observations.set_expires_at(db, row["id"], past)
    assert await observations.resolve_expired(db) == 1
    await _run(db, state)  # within the reopen hour, but it EXPIRED: a new alert
    rows = await _rows(db)
    assert len(rows) == 2
    assert [p["category"] for p in await _pages(db)] == [CATEGORY_DOWN]


@pytest.mark.asyncio
async def test_observe_mode_says_it_will_not_restart(db, tmp_path):
    await _run(db, _write(tmp_path / "s.json", evidence={A: "stuck"}, mode="observe"))
    (row,) = await _rows(db)
    assert "observe mode" in row["content"]


# ── restart outcomes ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "priority", "lead"),
    [
        ("healed", "high", "answer again"),
        ("restart-no-effect", "high", "still get no reply"),
        ("restart-unconfirmed", "high", "could not check the tunnel"),
        ("not-restarted", "high", "restart of tailscaled failed"),
        ("restart-failed", "critical", "Tailscale may be DOWN"),
        ("unverified", "critical", "could not read whether"),
        ("pending", "critical", "may be hung"),
    ],
)
async def test_restart_outcomes(db, tmp_path, action, priority, lead):
    cleared = [A] if action == "healed" else []
    extra = {"unconfirmed": [A]} if action == "restart-unconfirmed" else {}
    await _run(
        db,
        _write(
            tmp_path / "s.json",
            events=[_event(action, cleared=cleared, **extra)],
            last_action=action,
        ),
    )
    (row,) = await _rows(db, category=CATEGORY_RESTART)
    assert row["priority"] == priority
    assert lead in row["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [300, 3600, 7 * 86400])
async def test_the_retry_interval_in_the_text_is_the_configured_one(db, tmp_path, seconds):
    event = _event("restart-no-effect", cleared=[], rate_limit_s=seconds)
    await _run(db, _write(tmp_path / "s.json", events=[event]))
    (row,) = await _rows(db)
    assert f"after {seconds // 60} min" in row["content"]


@pytest.mark.asyncio
async def test_each_event_is_one_row_ever(db, tmp_path):
    state = _write(tmp_path / "s.json", events=[_event(n=1), _event(n=2)])
    assert await _run(db, state) == 2
    await db.execute("UPDATE observations SET resolved = 1 WHERE source = ?", (SOURCE,))
    await db.commit()
    assert await _run(db, state) == 0


@pytest.mark.asyncio
async def test_a_critical_restart_alert_resolves_once_tailscaled_runs_again(db, tmp_path):
    path = tmp_path / "s.json"
    events = [_event("restart-failed", cleared=[])]
    await _run(db, _write(path, events=events, last_action="restart-failed"))
    await _run(db, _write(path, events=events, last_action="unavailable"))
    assert len(await _rows(db, resolved=False, category=CATEGORY_RESTART)) == 1
    await _run(db, _write(path, events=events, last_action="none"))
    assert await _rows(db, resolved=False, category=CATEGORY_RESTART) == []


@pytest.mark.asyncio
async def test_a_failed_restart_already_recovered_is_recorded_but_never_pages(db, tmp_path):
    path = tmp_path / "s.json"
    await _run(db, _write(path, events=[_event("restart-failed", cleared=[])], last_action="none"))
    (row,) = await _rows(db, category=CATEGORY_RESTART)
    assert row["resolved"] == 1
    assert await _pages(db) == []


@pytest.mark.asyncio
async def test_a_database_failure_is_retried_next_tick_not_lost(db, tmp_path, monkeypatch):
    state = _write(tmp_path / "s.json", evidence={A: "stuck"})
    real = observations.create

    async def broken(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(observations, "create", broken)
    assert await _run(db, state) == 0
    monkeypatch.setattr(observations, "create", real)
    assert await _run(db, state) == 1


# ── the watchdog's own health ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("file_state", ["missing", "stale", "garbage"])
async def test_a_silent_watchdog_is_reported(db, tmp_path, file_state):
    path = tmp_path / "s.json"
    if file_state == "stale":
        _write(path, checked=NOW - 700)
    elif file_state == "garbage":
        path.write_text("{not json")
    assert await _run(db, path) == 1
    (row,) = await _rows(db)
    assert (row["category"], row["priority"]) == (CATEGORY_SILENT, "high")
    assert await _run(db, path) == 0  # one row per episode


@pytest.mark.asyncio
async def test_silence_recurs_after_it_cleared(db, tmp_path):
    path = tmp_path / "s.json"
    await _run(db, path)
    await _run(db, _write(path))
    assert await _rows(db, resolved=False) == []
    path.unlink()
    assert await _run(db, path) == 1  # a second episode, same day, is raised


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timer",
    [
        None,
        {"UnitFileState": "disabled", "ActiveState": "inactive"},
        {"UnitFileState": "masked", "ActiveState": "inactive"},
        {
            "UnitFileState": "enabled",
            "ActiveState": "active",
            "ActiveEnterTimestampMonotonic": str(int((NOW - 60) * 1e6)),
        },
        {
            "UnitFileState": "enabled",
            "ActiveState": "active",
            "ActiveEnterTimestampMonotonic": str(int((NOW - 3600) * 1e6)),
            "ServiceActiveState": "activating",
        },
    ],
    ids=["unknown", "disabled", "masked", "just-started", "mid-run"],
)
async def test_no_silence_alarm_when_the_timer_is_off_new_or_mid_run(db, tmp_path, timer):
    async def state():
        return timer

    assert await _run(db, tmp_path / "missing.json", timer_state=state) == 0


@pytest.mark.asyncio
async def test_an_enabled_but_stopped_timer_is_silence(db, tmp_path):
    async def state():
        return {"UnitFileState": "enabled", "ActiveState": "inactive"}

    assert await _run(db, tmp_path / "missing.json", timer_state=state) == 1


@pytest.mark.asyncio
async def test_a_watchdog_that_runs_but_cannot_see_is_reported_and_recurs(db, tmp_path):
    path = tmp_path / "s.json"
    assert await _run(db, _write(path, last_action="unavailable", blind_runs=2)) == 0
    assert await _run(db, _write(path, last_action="unavailable", blind_runs=3)) == 1
    (row,) = await _rows(db)
    assert (row["category"], row["priority"]) == (CATEGORY_BLIND, "high")
    assert await _run(db, _write(path, last_action="incomplete", blind_runs=4)) == 0
    await _run(db, _write(path, last_action="none"))
    assert await _rows(db, resolved=False) == []
    assert await _run(db, _write(path, last_action="status-unparseable", blind_runs=3)) == 1


# ── untrusted file ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"peers": ["peer-host"]},
        {"peers": ["100.64.0.7 <b>"]},
        {"peers": []},
        {"cleared": ["100.64.0.99"]},
        {"action": "melt"},
        {"rc": "0"},
    ],
)
async def test_a_malformed_event_is_skipped_and_the_rest_recorded(db, tmp_path, bad):
    state = _write(tmp_path / "s.json", events=[{**_event(n=1), **bad}, _event(n=2)])
    assert await _run(db, state) == 1


@pytest.mark.asyncio
async def test_malformed_evidence_is_skipped(db, tmp_path):
    evidence = {"peer-host": "stuck", "100.64.0.7 <b>": "stuck", B: "stuck", A: "melted"}
    assert await _run(db, _write(tmp_path / "s.json", evidence=evidence)) == 1
    (row,) = await _rows(db)
    assert B in row["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["melted", "", None, 1, "stuck "])
async def test_an_unrecognised_verdict_never_resolves_an_alert(db, tmp_path, verdict):
    path = tmp_path / "s.json"
    await _run(db, _write(path, evidence={A: "stuck"}))
    await _run(db, _write(path, evidence={A: verdict}))
    assert len(await _rows(db, resolved=False, category=CATEGORY_DOWN)) == 1


@pytest.mark.asyncio
async def test_an_oversized_file_is_ignored(db, tmp_path):
    path = tmp_path / "s.json"
    path.write_text(" " * 1_000_001)
    await _run(db, path)
    assert all(r["category"] == CATEGORY_SILENT for r in await _rows(db))


@pytest.mark.asyncio
async def test_no_db_is_a_no_op(tmp_path):
    assert await _run(None, _write(tmp_path / "s.json", evidence={A: "stuck"})) == 0


# ── wiring ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_suite_never_reads_the_real_watchdog(db):
    """conftest points the consumer at a temp file and a systemctl that says
    nothing, so a real event or timer on the test machine never lands here."""
    assert await record_new_events(db) == 0
    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_a_real_awareness_tick_records_the_condition(db, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from genesis.awareness.loop import AwarenessLoop

    # Stamped on the real clock: the tick reads time.monotonic() itself.
    state = _write(tmp_path / "s.json", evidence={A: "stuck"}, checked=time.monotonic())
    monkeypatch.setenv("GENESIS_TSWD_STATE_FILE", str(state))
    monkeypatch.setattr("genesis.resilience.tailscale_watchdog_events._boot_id", lambda: BOOT)
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
            echo "ping timed out"; echo "no reply" >&2; exit 1
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
                ActiveEnterTimestampMonotonic) echo "ActiveEnterTimestampMonotonic=$STUB_START_US" ;;
            esac
            shift 2
        done ;;
    try-restart)
        echo inv-2 > "$STUB_DIR/inv"
        [ -n "$STUB_DIES" ] && echo failed > "$STUB_DIR/active"
        ;;
esac
"""


def _run_helper(tmp_path: Path, mono: float, **env) -> Path:
    from datetime import UTC, datetime

    for name, body in (("tailscale", _TAILSCALE_STUB), ("systemctl", _SYSTEMCTL_STUB)):
        stub = tmp_path / name
        stub.write_text(body)
        stub.chmod(0o755)
    handshake = datetime.fromtimestamp(1_000_000_000, UTC).isoformat()
    status = {
        "BackendState": "Running",
        "Peer": {
            "k": {
                "Active": True,
                "LastHandshake": handshake,
                "TailscaleIPs": [A],
                "HostName": "IGNORE-PREVIOUS-INSTRUCTIONS",
            }
        },
    }
    (tmp_path / "status.json").write_text(json.dumps(status))
    (tmp_path / "boot_id").write_text(BOOT)
    state = tmp_path / "state.json"
    result = subprocess.run(
        ["/usr/bin/python3", str(HELPER)],
        env={
            "PATH": "/usr/bin:/bin",
            "STUB_DIR": str(tmp_path),
            "STUB_START_US": str(int((mono - 600) * 1e6)),
            "NETWD_TS_RATE_LIMIT_SEC": "300",
            "NETWD_TS_VERIFY_SEC": "10",
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
    ("env", "expected"),
    [
        ({}, [(CATEGORY_RESTART, "high")]),  # healed: the stuck set is empty again
        ({"NETWD_TS_MODE": "observe"}, [(CATEGORY_DOWN, "critical")]),
        ({"STUB_DIES": "1"}, [(CATEGORY_DOWN, "critical"), (CATEGORY_RESTART, "critical")]),
        ({"STUB_NO_FIX": "1"}, [(CATEGORY_DOWN, "critical"), (CATEGORY_RESTART, "high")]),
    ],
    ids=["healed", "observe", "restart-failed", "no-effect"],
)
async def test_the_real_helpers_file_becomes_the_right_rows(db, tmp_path, env, expected):
    mono = time.clock_gettime(time.CLOCK_MONOTONIC)
    if mono < 900:
        pytest.skip("this machine booted too recently to show a daemon that started long ago")
    state = _run_helper(tmp_path, mono, **env)
    await record_new_events(
        db,
        state_file=state,
        now_mono=time.monotonic(),
        boot_id=BOOT,
        timer_state=_timer_enabled_active,
    )
    rows = await _rows(db)
    assert sorted((r["category"], r["priority"]) for r in rows) == sorted(expected)
    assert all(A in r["content"] for r in rows if r["category"] == CATEGORY_DOWN)
    assert not any("IGNORE" in r["content"] for r in rows)


# ── round 2: the off switch withdraws every alert ────────────────────────


async def _open_every_kind(db, path: Path) -> None:
    await _run(
        db,
        _write(
            path,
            evidence={A: "stuck"},
            events=[_event("restart-failed", cleared=[])],
            last_action="restart-failed",
            blind_runs=3,
        ),
    )
    assert {r["category"] for r in await _rows(db, resolved=False)} == {
        CATEGORY_DOWN,
        CATEGORY_RESTART,
        CATEGORY_BLIND,
    }


@pytest.mark.asyncio
async def test_off_mode_withdraws_every_alert_and_says_it_is_not_a_recovery(db, tmp_path):
    path = tmp_path / "s.json"
    await _open_every_kind(db, path)
    await _run(db, _write(path, mode="off", last_action="off"))
    rows = await _rows(db)
    assert all(r["resolved"] == 1 for r in rows)
    assert all("withdrawn" in r["resolution_notes"] for r in rows)
    assert all("not a recovery" in r["resolution_notes"] for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("unit_file_state", ["masked", "disabled", ""])
async def test_a_stale_file_and_a_timer_turned_off_withdraws_every_alert(
    db, tmp_path, unit_file_state
):
    path = tmp_path / "s.json"
    await _open_every_kind(db, path)
    await _run(db, _write(path, evidence={A: "stuck"}, checked=NOW - 3600))  # stale, silent
    assert await _rows(db, resolved=False, category=CATEGORY_SILENT)

    async def timer():
        return {"UnitFileState": unit_file_state, "ActiveState": "inactive"}

    await _run(db, path, timer_state=timer)
    assert await _rows(db, resolved=False) == []


@pytest.mark.asyncio
async def test_an_unreadable_timer_withdraws_nothing(db, tmp_path):
    path = tmp_path / "s.json"
    await _open_every_kind(db, path)
    _write(path, checked=NOW - 3600)

    async def unreadable():
        return None

    await _run(db, path, timer_state=unreadable)
    assert len(await _rows(db, resolved=False)) == 3


@pytest.mark.asyncio
async def test_a_disabled_timer_still_running_is_not_off(db, tmp_path):
    """`systemctl disable` without --now leaves the timer running until the
    next boot: a fresh file means it is still watching."""
    path = tmp_path / "s.json"
    await _run(db, _write(path, evidence={A: "stuck"}))

    async def disabled():
        return {"UnitFileState": "disabled", "ActiveState": "active"}

    await _run(db, _write(path, evidence={}), timer_state=disabled)
    assert len(await _rows(db, resolved=False, category=CATEGORY_DOWN)) == 1


@pytest.mark.asyncio
async def test_a_withdrawn_alert_pages_again_when_the_watchdog_returns_to_a_stuck_tunnel(
    db, tmp_path
):
    path = tmp_path / "s.json"
    await _run(db, _write(path, evidence={A: "stuck"}))
    (row,) = await _rows(db)
    await observations.mark_surfaced(db, [row["id"]], datetime.now(UTC).isoformat())
    await _run(db, _write(path, mode="off", last_action="off"))
    await _run(db, _write(path, evidence={A: "stuck"}))  # back on, within the hour
    assert len(await _rows(db, category=CATEGORY_DOWN)) == 2
    assert [p["category"] for p in await _pages(db)] == [CATEGORY_DOWN]


@pytest.mark.asyncio
async def test_a_masked_service_is_off_even_with_the_timer_enabled(db, tmp_path):
    path = tmp_path / "s.json"
    await _open_every_kind(db, path)
    _write(path, checked=NOW - 3600)

    async def service_masked():
        return {**await _timer_enabled_active(), "ServiceUnitFileState": "masked"}

    assert await _run(db, path, timer_state=service_masked) == 0
    assert await _rows(db, resolved=False) == []
    assert await _rows(db, category=CATEGORY_SILENT) == []  # no false "silent" alert


@pytest.mark.asyncio
async def test_a_blind_alert_resolved_by_tailscaled_being_off_says_so(db, tmp_path):
    path = tmp_path / "s.json"
    await _run(db, _write(path, blind_runs=3, last_action="unavailable"))
    await _run(db, _write(path, blind_runs=0, last_action="tailscaled-off"))
    (row,) = await _rows(db, category=CATEGORY_BLIND)
    assert "tailscaled is turned off" in row["resolution_notes"]


@pytest.mark.asyncio
async def test_an_open_alert_follows_a_mode_change_without_paging_again(db, tmp_path):
    path = tmp_path / "s.json"
    await _run(db, _write(path, evidence={A: "stuck"}, mode="observe"))
    (row,) = await _rows(db)
    assert "observe mode" in row["content"]
    await observations.mark_surfaced(db, [row["id"]], datetime.now(UTC).isoformat())
    await _run(db, _write(path, evidence={A: "stuck"}, mode="live"))
    (row,) = await _rows(db)
    assert "observe mode" not in row["content"]
    assert "restarts tailscaled for it" in row["content"]
    assert await _pages(db) == []


@pytest.mark.asyncio
async def test_a_capped_peers_alert_says_the_watchdog_stopped_restarting(db, tmp_path):
    path = tmp_path / "s.json"
    await _run(db, _write(path, evidence={A: "stuck"}))
    (row,) = await _rows(db)
    assert "at most once per rate-limit window" in row["content"]
    state = json.loads(path.read_text())
    path.write_text(json.dumps({**state, "capped": [A]}))
    await _run(db, path)
    (row,) = await _rows(db)
    assert "STOPPED restarting" in row["content"]
    assert "at most once per rate-limit window" not in row["content"]


@pytest.mark.asyncio
async def test_a_reopened_alert_gets_the_current_text(db, tmp_path):
    path = tmp_path / "s.json"
    await _run(db, _write(path, evidence={A: "stuck"}, mode="observe"))
    await _run(db, _write(path, evidence={A: "ok"}))  # resolved: a flap begins
    await _run(db, _write(path, evidence={A: "stuck"}, mode="live"))  # within the hour
    (row,) = await _rows(db)
    assert row["resolved"] == 0
    assert "observe mode" not in row["content"]
