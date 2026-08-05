"""NetworkSentinel — hysteresis, recovery hook, windows, publishing.

All tests are network-free and clock-free: a scripted prober feeds round
classes and a fake clock advances manually, so the asymmetric hysteresis is
exercised deterministically (no real sockets, no wall-clock dependence).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from genesis.resilience.network_config import NetworkTuning
from genesis.resilience.network_sentinel import (
    ALL_FAIL,
    CLEAN,
    DNS_ONLY,
    PARTIAL,
    NetworkSentinel,
    classify_round,
)
from genesis.resilience.state import NetworkStatus, ResilienceStateMachine


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def make_tuning(**overrides) -> NetworkTuning:
    base = dict(
        dns_tcp_anchors=("one.one.one.one",),
        ip_anchors=("1.1.1.1",),
        probe_port=443,
        probe_timeout_s=3,
        fast_cadence_s=20,
        steady_cadence_s=120,
        offline_all_fail_rounds=2,
        online_clean_rounds=3,
        stable_online_s=300,
        merge_gap_s=600,
    )
    base.update(overrides)
    return NetworkTuning(**base)


def make_sentinel(tmp_path, rounds, *, clock=None, on_stable_online=None, tuning=None):
    """Build a sentinel driven by a scripted list of round classes."""
    clock = clock or FakeClock(datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC))
    tuning = tuning or make_tuning()
    it = iter(rounds)

    async def prober(_tuning):
        return next(it)

    sm = ResilienceStateMachine(clock=clock)
    sentinel = NetworkSentinel(
        state_machine=sm,
        prober=prober,
        clock=clock,
        tuning_provider=lambda: tuning,
        state_path=tmp_path / "network_state.json",
        on_stable_online=on_stable_online,
    )
    return sentinel, sm, clock


async def _drive(sentinel, clock, n, *, step_s=20):
    for _ in range(n):
        await sentinel._probe_round()
        clock.advance(step_s)


# ── pure classifier ──────────────────────────────────────────────────────────


def test_classify_clean():
    assert classify_round([_ok := "ok"], [_ok]) == CLEAN


def test_classify_all_fail():
    assert classify_round(["dns_fail"], ["tcp_fail"]) == ALL_FAIL


def test_classify_dns_only_requires_ip_ok_and_dns_resolution_fail():
    # routing alive (IP ok), resolution dead (dns_fail) → dns_only
    assert classify_round(["dns_fail"], ["ok"]) == DNS_ONLY


def test_classify_partial_when_dns_tcp_fail_is_not_resolution():
    # DNS resolved but TCP failed while IP ok → not a pure DNS outage → partial
    assert classify_round(["tcp_fail"], ["ok"]) == PARTIAL


def test_classify_partial_mixed():
    assert classify_round(["ok"], ["tcp_fail"]) == PARTIAL


def test_classify_no_anchors_is_clean():
    # empty anchor sets cannot assert an outage
    assert classify_round([], []) == CLEAN


# ── core hysteresis ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_normal_to_degraded_on_first_nonclean(tmp_path):
    sentinel, sm, clock = make_sentinel(tmp_path, [PARTIAL])
    await sentinel._probe_round()
    assert sentinel._state == NetworkStatus.DEGRADED
    assert sm.current.network == NetworkStatus.DEGRADED  # propagated to state machine


@pytest.mark.asyncio
async def test_degraded_to_offline_needs_two_consecutive_all_fail(tmp_path):
    sentinel, sm, clock = make_sentinel(tmp_path, [PARTIAL, ALL_FAIL, ALL_FAIL])
    await sentinel._probe_round()  # NORMAL → DEGRADED
    await sentinel._probe_round()  # all_fail #1 → still DEGRADED
    assert sentinel._state == NetworkStatus.DEGRADED
    await sentinel._probe_round()  # all_fail #2 → OFFLINE
    assert sentinel._state == NetworkStatus.OFFLINE
    assert sm.current.network == NetworkStatus.OFFLINE


@pytest.mark.asyncio
async def test_dns_only_never_reaches_offline(tmp_path):
    sentinel, sm, clock = make_sentinel(tmp_path, [PARTIAL, DNS_ONLY, DNS_ONLY, DNS_ONLY, DNS_ONLY])
    await _drive(sentinel, clock, 5)
    assert sentinel._state == NetworkStatus.DEGRADED  # capped, never OFFLINE


@pytest.mark.asyncio
async def test_all_fail_streak_reset_by_dns_only(tmp_path):
    # all_fail, dns_only (resets streak), all_fail → still only 1 consecutive → DEGRADED
    sentinel, sm, clock = make_sentinel(tmp_path, [PARTIAL, ALL_FAIL, DNS_ONLY, ALL_FAIL])
    await _drive(sentinel, clock, 4)
    assert sentinel._state == NetworkStatus.DEGRADED


@pytest.mark.asyncio
async def test_slow_clear_requires_consecutive_clean(tmp_path):
    # F1b: a non-clean round in the middle of the clean streak resets it.
    # DEGRADED: clean(1), partial(reset), clean(1), clean(2), clean(3)→NORMAL
    sentinel, sm, clock = make_sentinel(tmp_path, [PARTIAL, CLEAN, PARTIAL, CLEAN, CLEAN, CLEAN])
    await sentinel._probe_round()  # → DEGRADED
    await sentinel._probe_round()  # clean #1
    assert sentinel._state == NetworkStatus.DEGRADED
    await sentinel._probe_round()  # partial → streak reset, still DEGRADED
    assert sentinel._state == NetworkStatus.DEGRADED
    await sentinel._probe_round()  # clean #1
    await sentinel._probe_round()  # clean #2
    assert sentinel._state == NetworkStatus.DEGRADED
    await sentinel._probe_round()  # clean #3 → NORMAL
    assert sentinel._state == NetworkStatus.NORMAL


@pytest.mark.asyncio
async def test_offline_recovers_to_degraded_on_any_ok(tmp_path):
    sentinel, sm, clock = make_sentinel(tmp_path, [PARTIAL, ALL_FAIL, ALL_FAIL, PARTIAL])
    await _drive(sentinel, clock, 3)  # → OFFLINE
    assert sentinel._state == NetworkStatus.OFFLINE
    await sentinel._probe_round()  # partial (any ok) → DEGRADED
    assert sentinel._state == NetworkStatus.DEGRADED


# ── recovery hook (F1a) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_hook_fires_once_after_stable_hold(tmp_path):
    hook = Mock()
    clock = FakeClock(datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC))
    # drive to NORMAL, then hold clean while advancing past stable_online_s
    rounds = [PARTIAL, ALL_FAIL, ALL_FAIL]  # → OFFLINE (arms recovery)
    rounds += [CLEAN, CLEAN, CLEAN]  # OFFLINE→DEGRADED→…→NORMAL
    rounds += [CLEAN, CLEAN, CLEAN]  # hold NORMAL
    sentinel, sm, _ = make_sentinel(tmp_path, rounds, clock=clock, on_stable_online=hook)

    for _ in range(6):  # reach NORMAL (3 cleans from OFFLINE)
        await sentinel._probe_round()
        clock.advance(20)
    assert sentinel._state == NetworkStatus.NORMAL
    assert hook.call_count == 0  # not yet held long enough

    clock.advance(400)  # now well past stable_online_s=300 since NORMAL entry
    await sentinel._probe_round()
    assert hook.call_count == 1
    await sentinel._probe_round()  # still NORMAL — must NOT re-fire
    assert hook.call_count == 1


@pytest.mark.asyncio
async def test_recovery_hook_rearms_two_full_cycles(tmp_path):
    hook = Mock()
    clock = FakeClock(datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC))
    # Each cycle: outage → recover to NORMAL (3 clean) → 1 hold-clean to fire hook.
    rounds = [PARTIAL, ALL_FAIL, ALL_FAIL, CLEAN, CLEAN, CLEAN, CLEAN] + [
        PARTIAL,
        ALL_FAIL,
        ALL_FAIL,
        CLEAN,
        CLEAN,
        CLEAN,
        CLEAN,
    ]
    sentinel, sm, _ = make_sentinel(tmp_path, rounds, clock=clock, on_stable_online=hook)

    async def run_cycle():
        for _ in range(6):  # → NORMAL
            await sentinel._probe_round()
            clock.advance(20)
        assert sentinel._state == NetworkStatus.NORMAL
        clock.advance(400)  # exceed stable_online_s
        await sentinel._probe_round()  # the 7th (hold) round fires the hook

    await run_cycle()
    assert hook.call_count == 1
    await run_cycle()
    assert hook.call_count == 2  # re-armed by the 2nd OFFLINE


# ── outage windows ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_windows_merge_within_gap(tmp_path):
    clock = FakeClock(datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC))
    # outage1 → recover → SHORT gap → outage2 : should be ONE merged window
    rounds = [PARTIAL, ALL_FAIL, ALL_FAIL, PARTIAL, ALL_FAIL, ALL_FAIL]
    sentinel, sm, _ = make_sentinel(
        tmp_path, rounds, clock=clock, tuning=make_tuning(merge_gap_s=600)
    )
    await sentinel._probe_round()  # DEGRADED
    await sentinel._probe_round()  # all_fail1
    await sentinel._probe_round()  # OFFLINE (window opens)
    start = sentinel._open_window["start"]
    clock.advance(30)
    await sentinel._probe_round()  # partial → DEGRADED (window closes)
    assert len(sentinel._closed_windows) == 1
    clock.advance(30)  # short gap (< 600)
    await sentinel._probe_round()  # all_fail1
    await sentinel._probe_round()  # OFFLINE again → merge
    assert len(sentinel._closed_windows) == 0  # merged back into open
    assert sentinel._open_window["start"] == start  # original start preserved


@pytest.mark.asyncio
async def test_windows_separate_beyond_gap(tmp_path):
    clock = FakeClock(datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC))
    rounds = [PARTIAL, ALL_FAIL, ALL_FAIL, PARTIAL, ALL_FAIL, ALL_FAIL]
    sentinel, sm, _ = make_sentinel(
        tmp_path, rounds, clock=clock, tuning=make_tuning(merge_gap_s=60)
    )
    await sentinel._probe_round()
    await sentinel._probe_round()
    await sentinel._probe_round()  # OFFLINE
    clock.advance(30)
    await sentinel._probe_round()  # DEGRADED (close window 1)
    clock.advance(300)  # long gap (> 60)
    await sentinel._probe_round()  # all_fail1
    await sentinel._probe_round()  # OFFLINE → new window
    assert len(sentinel._closed_windows) == 1  # window 1 stays closed
    assert sentinel._open_window is not None


# ── snapshot immutability (F3) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_is_immutable_copy(tmp_path):
    sentinel, sm, clock = make_sentinel(tmp_path, [ALL_FAIL, PARTIAL])
    await sentinel._probe_round()
    snap = sentinel.snapshot()
    snap["state"] = "TAMPERED"
    snap["window_open"] = "TAMPERED"
    snap2 = sentinel.snapshot()
    assert snap2["state"] != "TAMPERED"


@pytest.mark.asyncio
async def test_snapshot_carries_last_probe_at(tmp_path):
    sentinel, sm, clock = make_sentinel(tmp_path, [CLEAN])
    assert sentinel.snapshot()["last_probe_at"] is None  # before first probe
    await sentinel._probe_round()
    assert sentinel.snapshot()["last_probe_at"] is not None


@pytest.mark.asyncio
async def test_publish_writes_state_file_every_round(tmp_path):
    import json

    sentinel, sm, clock = make_sentinel(tmp_path, [CLEAN, CLEAN])
    await sentinel._probe_round()
    path = tmp_path / "network_state.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["state"] == "NORMAL"
    assert "last_probe_at" in data and data["last_probe_at"] is not None


# ── SHOULD-FIX regressions (code review) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_anchors_surfaces_not_configured_without_probing(tmp_path):
    # An enabled sentinel with zero anchors must NOT probe (a CLEAN round would
    # pin NORMAL — a false green) and must surface 'no_anchors' explicitly.
    import json

    empty = make_tuning(dns_tcp_anchors=(), ip_anchors=())
    probed = {"n": 0}

    async def prober(_t):
        probed["n"] += 1
        return CLEAN

    sm = ResilienceStateMachine()
    sen = NetworkSentinel(
        state_machine=sm,
        prober=prober,
        tuning_provider=lambda: empty,
        state_path=tmp_path / "network_state.json",
    )
    cadence = await sen._probe_round()
    assert probed["n"] == 0  # prober NEVER called — no anchors to probe
    assert sm.current.network == NetworkStatus.NORMAL  # axis untouched (inert)
    snap = sen.snapshot()
    assert snap["cause"] == "no_anchors"
    assert snap["last_probe_at"] is not None  # fresh marker, not omitted
    data = json.loads((tmp_path / "network_state.json").read_text())
    assert data["cause"] == "no_anchors"  # dashboard renders "not configured"
    assert cadence == empty.fast_cadence_s


@pytest.mark.asyncio
async def test_loop_survives_raising_tuning_provider(tmp_path):
    # A tuning_provider that raises must NOT permanently kill the probe loop —
    # the round is caught, a fallback cadence is used, and probing resumes.
    calls = {"tuning": 0, "probe": 0}

    def tuning_provider():
        calls["tuning"] += 1
        if calls["tuning"] == 1:
            raise RuntimeError("transient tuning failure")
        return make_tuning(fast_cadence_s=0, steady_cadence_s=0)

    async def prober(_t):
        calls["probe"] += 1
        if calls["probe"] >= 2:
            raise asyncio.CancelledError  # stop the loop after it recovered
        return CLEAN

    sm = ResilienceStateMachine()
    sen = NetworkSentinel(
        state_machine=sm,
        prober=prober,
        tuning_provider=tuning_provider,
        state_path=tmp_path / "network_state.json",
    )
    sen._FALLBACK_CADENCE_S = 0  # instant fallback sleep for the test
    with pytest.raises(asyncio.CancelledError):
        await sen._loop()
    # The first round raised (in tuning) and the loop CAUGHT it and kept probing.
    assert calls["probe"] >= 1


@pytest.mark.asyncio
async def test_all_fail_first_reaches_offline_in_two_rounds(tmp_path):
    # Codex F1: locks the intended semantics — `offline_all_fail_rounds` (2)
    # CONSECUTIVE all_fail rounds → OFFLINE, counting the NORMAL→DEGRADED round.
    sentinel, sm, clock = make_sentinel(tmp_path, [ALL_FAIL, ALL_FAIL])
    await sentinel._probe_round()  # all_fail #1: NORMAL → DEGRADED
    assert sentinel._state == NetworkStatus.DEGRADED
    await sentinel._probe_round()  # all_fail #2 → OFFLINE
    assert sentinel._state == NetworkStatus.OFFLINE


@pytest.mark.asyncio
async def test_offline_entry_arms_recovery_and_opens_window(tmp_path):
    # Codex F4: OFFLINE entry must re-arm recovery AND open the window (recovery
    # re-armed BEFORE the window op so a window failure can't strand it).
    sentinel, sm, clock = make_sentinel(tmp_path, [PARTIAL, ALL_FAIL, ALL_FAIL])
    await _drive(sentinel, clock, 3)
    assert sentinel._state == NetworkStatus.OFFLINE
    assert sentinel._recovery_hook_fired is False  # armed for this outage
    assert sentinel._open_window is not None  # window opened


@pytest.mark.asyncio
async def test_no_anchors_round_resets_clean_streak(tmp_path):
    # Codex F3: a no_anchors round (live config gap) must reset the clean streak
    # so a later clean can't reach NORMAL on a stale streak.
    clock = FakeClock(datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC))
    tunings = iter(
        [
            make_tuning(online_clean_rounds=2),  # r1
            make_tuning(online_clean_rounds=2),  # r2
            make_tuning(dns_tcp_anchors=(), ip_anchors=(), online_clean_rounds=2),  # r3
            make_tuning(online_clean_rounds=2),  # r4
            make_tuning(online_clean_rounds=2),  # r5
        ]
    )

    def tuning_provider():
        return next(tunings)

    probe_vals = iter([PARTIAL, CLEAN, CLEAN, CLEAN])  # r3 (no_anchors) skips prober

    async def prober(_t):
        return next(probe_vals)

    sm = ResilienceStateMachine(clock=clock)
    sen = NetworkSentinel(
        state_machine=sm,
        prober=prober,
        clock=clock,
        tuning_provider=tuning_provider,
        state_path=tmp_path / "n.json",
    )
    await sen._probe_round()  # r1 partial → DEGRADED
    await sen._probe_round()  # r2 clean → streak 1
    assert sen._consecutive_clean == 1
    await sen._probe_round()  # r3 no_anchors → reset streak
    assert sen._consecutive_clean == 0
    assert sen.snapshot()["cause"] == "no_anchors"
    await sen._probe_round()  # r4 clean → streak 1 (NOT 2)
    assert sen._state == NetworkStatus.DEGRADED  # would be NORMAL without the reset
    await sen._probe_round()  # r5 clean → streak 2 → NORMAL
    assert sen._state == NetworkStatus.NORMAL


# ── restart persistence / hydration (cloud Codex P2) ─────────────────────────


def _write_state_file(path, **fields):
    import json

    path.write_text(json.dumps(fields))


def _new_sentinel(tmp_path, *, clock=None):
    clock = clock or FakeClock(datetime(2026, 7, 30, 13, 0, 0, tzinfo=UTC))
    sm = ResilienceStateMachine(clock=clock)

    async def prober(_t):  # unused for pure-hydration assertions
        return CLEAN

    sen = NetworkSentinel(
        state_machine=sm,
        prober=prober,
        clock=clock,
        tuning_provider=lambda: make_tuning(),
        state_path=tmp_path / "network_state.json",
    )
    return sen, sm, clock


def test_hydrate_restores_closed_windows(tmp_path):
    _write_state_file(
        tmp_path / "network_state.json",
        state="NORMAL",
        closed_windows=[
            {
                "start": "2026-07-30T12:00:00+00:00",
                "end": "2026-07-30T12:05:00+00:00",
                "cause": "all_fail",
            },
        ],
    )
    sen, sm, _ = _new_sentinel(tmp_path)
    assert len(sen._closed_windows) == 1
    assert sen._state == NetworkStatus.NORMAL  # post-outage → axis re-detects
    assert sen._open_window is None


def test_hydrate_restores_open_outage_and_axis(tmp_path):
    _write_state_file(
        tmp_path / "network_state.json",
        state="OFFLINE",
        since="2026-07-30T12:00:00+00:00",
        cause="all_fail",
        window_open=True,
        open_window={"start": "2026-07-30T12:00:00+00:00", "cause": "all_fail"},
        closed_windows=[],
    )
    sen, sm, _ = _new_sentinel(tmp_path)
    assert sen._state == NetworkStatus.OFFLINE
    assert sm.current.network == NetworkStatus.OFFLINE  # axis reflected immediately
    assert sen._open_window is not None
    assert sen._open_window["start"] == "2026-07-30T12:00:00+00:00"  # original start preserved
    assert sen._recovery_hook_fired is False  # armed to fire on recovery


def test_hydrate_ignores_corrupt_file(tmp_path):
    (tmp_path / "network_state.json").write_text("{not json")
    sen, sm, _ = _new_sentinel(tmp_path)
    assert sen._closed_windows == []
    assert sen._open_window is None
    assert sen._state == NetworkStatus.NORMAL


@pytest.mark.asyncio
async def test_hydrated_outage_continues_then_closes_with_original_start(tmp_path):
    # Restore an open outage; still-down keeps it open (same start); recovery
    # closes it with the ORIGINAL start (span not reset by the restart).
    clock = FakeClock(datetime(2026, 7, 30, 13, 0, 0, tzinfo=UTC))
    _write_state_file(
        tmp_path / "network_state.json",
        state="OFFLINE",
        since="2026-07-30T12:00:00+00:00",
        cause="all_fail",
        window_open=True,
        open_window={"start": "2026-07-30T12:00:00+00:00", "cause": "all_fail"},
        closed_windows=[],
    )
    sm = ResilienceStateMachine(clock=clock)
    probes = iter([ALL_FAIL, CLEAN])

    async def prober(_t):
        return next(probes)

    sen = NetworkSentinel(
        state_machine=sm,
        prober=prober,
        clock=clock,
        tuning_provider=lambda: make_tuning(),
        state_path=tmp_path / "network_state.json",
    )
    await sen._probe_round()  # still all_fail → stays OFFLINE, window preserved
    assert sen._state == NetworkStatus.OFFLINE
    assert sen._open_window["start"] == "2026-07-30T12:00:00+00:00"
    await sen._probe_round()  # clean → OFFLINE→DEGRADED, closes the window
    assert sen._state == NetworkStatus.DEGRADED
    assert len(sen._closed_windows) == 1
    assert sen._closed_windows[0]["start"] == "2026-07-30T12:00:00+00:00"  # span preserved
