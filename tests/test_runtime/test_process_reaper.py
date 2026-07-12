"""Tests for the idle-aware process reaper (fix/reaper-idle-semantics).

Covers the pure classifier, the dry-run/armed orchestrator, the auto-arm
lifecycle, the hard kill-switch, non-claude age paths, marker GC, protected
PIDs, and job wiring. The 2026-07-11 incident (interactive claude sessions
killed purely on 7d age) is the regression these lock down.
"""

from __future__ import annotations

import os

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import genesis.runtime.init.process_reaper as pr
from genesis.runtime.init.process_reaper import (
    _normalize_tty,
    _wire_process_reaper,
    classify_claude_pid,
    run_reaper,
)

# asyncio_mode = "auto" (pyproject) runs async tests automatically; the sync
# classifier/normalize tests here run as plain functions — no global mark.

_DAY = 86400.0
_NOW = 1_000_000_000.0


# ── Pure classifier ─────────────────────────────────────────────────────
def test_classify_young_spares():
    reap, reason = classify_claude_pid(
        age_secs=100,
        now=_NOW,
        marker_mtime=None,
        controlling_tty=None,
        live_ttys=set(),
    )
    assert reap is False and reason == "young"


def test_classify_fresh_marker_spares():
    # Old process (8d) but active 2h ago → the incident case: must survive.
    reap, reason = classify_claude_pid(
        age_secs=8 * _DAY,
        now=_NOW,
        marker_mtime=_NOW - 2 * 3600,
        controlling_tty=None,
        live_ttys=set(),
    )
    assert reap is False and reason == "fresh-marker"


def test_classify_stale_marker_live_tty_spares():
    # No fresh marker, but attached to a live terminal → spare (backstop).
    reap, reason = classify_claude_pid(
        age_secs=8 * _DAY,
        now=_NOW,
        marker_mtime=_NOW - 9 * _DAY,
        controlling_tty="pts/5",
        live_ttys={"pts/5"},
    )
    assert reap is False and reason == "live-tty"


def test_classify_detached_idle_reaps():
    # 8d old, no fresh marker, tty not live → the true-leak class.
    reap, reason = classify_claude_pid(
        age_secs=8 * _DAY,
        now=_NOW,
        marker_mtime=None,
        controlling_tty="pts/9",
        live_ttys={"pts/5"},
    )
    assert reap is True and reason == "stale-detached"


def test_classify_stale_marker_detached_reaps():
    reap, reason = classify_claude_pid(
        age_secs=8 * _DAY,
        now=_NOW,
        marker_mtime=_NOW - 10 * _DAY,
        controlling_tty=None,
        live_ttys={"pts/5"},
    )
    assert reap is True and reason == "stale-detached"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/dev/pts/5", "pts/5"),
        ("pts/3", "pts/3"),
        ("?", None),
        ("", None),
        ("  /dev/tty1 ", "tty1"),
    ],
)
def test_normalize_tty(raw, expected):
    assert _normalize_tty(raw) == expected


# ── Orchestrator harness ────────────────────────────────────────────────
class _FakeRT:
    def __init__(self, *, pipeline=None):
        self._db = None
        self._outreach_pipeline = pipeline
        self.successes: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def record_job_success(self, name):
        self.successes.append(name)

    def record_job_failure(self, name, err):
        self.failures.append((name, err))


def _patch_io(
    monkeypatch,
    *,
    pids_by_pattern,
    ages,
    markers=None,
    ttys=None,
    live_ttys=None,
    descendants=None,
    state=None,
):
    markers = markers or {}
    ttys = ttys or {}
    descendants = descendants or {}
    signals: list[tuple[int, int]] = []
    saved: dict = {}
    gc_calls: list[set] = []

    async def fake_pgrep(flag, pattern):
        return list(pids_by_pattern.get(pattern, []))

    async def fake_tty(pid):
        return ttys.get(pid)

    async def fake_live():
        return set(live_ttys or [])

    async def fake_desc(pid, depth=0):
        return list(descendants.get(pid, []))

    def fake_save(s):
        saved.clear()
        saved.update(s)

    monkeypatch.setattr(pr, "_pgrep", fake_pgrep)
    monkeypatch.setattr(pr, "_read_uptime", lambda: 10_000_000.0)
    monkeypatch.setattr(pr, "_proc_age_secs", lambda pid, up, ct: ages.get(pid))
    monkeypatch.setattr(pr, "_marker_mtime", lambda pid: markers.get(pid))
    monkeypatch.setattr(pr, "_process_tty", fake_tty)
    monkeypatch.setattr(pr, "_live_ttys", fake_live)
    monkeypatch.setattr(pr, "_get_descendants", fake_desc)
    monkeypatch.setattr(pr, "_gc_markers", lambda live: gc_calls.append(set(live)))
    monkeypatch.setattr(pr, "_signal", lambda pid, s: signals.append((pid, s)))
    monkeypatch.setattr(pr, "_load_state", lambda: dict(state or {}))
    monkeypatch.setattr(pr, "_save_state", fake_save)
    monkeypatch.setattr(pr, "_KILL_GRACE_SECS", 0)
    monkeypatch.delenv(pr._ENV_HARD_DISABLE, raising=False)
    return signals, saved, gc_calls


async def test_dry_run_never_signals(monkeypatch, caplog):
    signals, saved, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001]},
        ages={900001: 8 * _DAY},
        markers={},
        ttys={},
        live_ttys=set(),
        state={},  # dry_run defaults True
    )
    captured = {}

    async def fake_obs(rt, cands, *, dry_run, claude_hit=False):
        captured["dry_run"] = dry_run
        captured["count"] = len(cands)

    monkeypatch.setattr(pr, "_record_observation", fake_obs)
    rt = _FakeRT()
    with caplog.at_level("WARNING"):
        await run_reaper(rt, now=_NOW)

    assert signals == []  # dry-run never signals
    assert captured == {"dry_run": True, "count": 1}
    assert "WOULD KILL pid 900001" in caplog.text
    assert rt.successes == ["process_reaper"]


async def test_armed_kills_detached_claude_tree(monkeypatch):
    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001]},
        ages={900001: 8 * _DAY},
        markers={},
        ttys={},
        live_ttys=set(),
        descendants={900001: [900002]},
        state={"dry_run": False, "armed_at": 1.0},
    )
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    # SIGTERM (15) then SIGKILL (9) for both tree members.
    assert (900001, 15) in signals and (900002, 15) in signals
    assert (900001, 9) in signals and (900002, 9) in signals
    assert rt.successes == ["process_reaper"]


async def test_armed_spares_fresh_marker_claude(monkeypatch):
    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001]},
        ages={900001: 30 * _DAY},  # very old…
        markers={900001: _NOW - 3600},  # …but active 1h ago
        ttys={},
        live_ttys=set(),
        state={"dry_run": False, "armed_at": 1.0},
    )
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert signals == []  # active session never dies — the whole point


async def test_armed_spares_live_tty_claude(monkeypatch):
    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001]},
        ages={900001: 30 * _DAY},
        markers={},  # no marker (e.g. hooks didn't fire)
        ttys={900001: "pts/5"},
        live_ttys={"pts/5"},
        state={"dry_run": False, "armed_at": 1.0},
    )
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert signals == []


async def test_non_claude_reaped_by_age(monkeypatch):
    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"opencode-ai": [900010, 900011]},
        ages={900010: 25 * 3600, 900011: 10 * 3600},  # 25h stale, 10h young
        state={"dry_run": False, "armed_at": 1.0},
    )
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    killed = {pid for pid, _ in signals}
    assert 900010 in killed  # >24h
    assert 900011 not in killed  # <24h


async def test_protected_pid_never_signalled(monkeypatch):
    my_pid = os.getpid()
    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [my_pid]},
        ages={my_pid: 99 * _DAY},
        markers={},
        ttys={},
        live_ttys=set(),
        descendants={my_pid: []},
        state={"dry_run": False, "armed_at": 1.0},
    )
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert all(pid != my_pid for pid, _ in signals)


async def test_marker_gc_receives_live_pids(monkeypatch):
    _, _, gc_calls = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001], "opencode-ai": [900010]},
        ages={900001: 1 * _DAY, 900010: 1 * 3600},  # both young → no reap
        state={"dry_run": False, "armed_at": 1.0},
    )
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert gc_calls and {900001, 900010} <= gc_calls[0]


async def test_auto_arm_lifecycle(monkeypatch):
    notified: list[str] = []

    async def fake_notify(rt, msg):
        notified.append(msg)
        return True  # delivered → arming may proceed

    monkeypatch.setattr(pr, "_notify_owner", fake_notify)

    # Pass 1: fresh dry-run. 900001 is a detached candidate; 900002 is a
    # live session with a fresh marker (hook proof-of-life). Should record
    # dry_run_since + hook_verified, but NOT arm, NOT signal.
    signals, saved, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001, 900002]},
        ages={900001: 8 * _DAY, 900002: 8 * _DAY},
        markers={900002: _NOW - 100},  # fresh marker → hook is alive
        ttys={},
        live_ttys=set(),
        state={},
    )
    rt = _FakeRT(pipeline=object())
    await run_reaper(rt, now=_NOW)
    assert signals == []
    assert saved.get("dry_run_since") == _NOW
    assert saved.get("hook_verified") is True
    assert saved.get("dry_run", True) is True
    assert not saved.get("armed_at")
    assert notified == []

    # Pass 2: >3 days later, hook already verified. Should FLIP to armed +
    # notify, but still NOT signal this pass (arm gives the owner a veto window).
    signals2, saved2, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001]},
        ages={900001: 8 * _DAY},
        markers={},
        ttys={},
        live_ttys=set(),
        state={"dry_run": True, "dry_run_since": _NOW, "hook_verified": True},
    )
    rt2 = _FakeRT(pipeline=object())
    await run_reaper(rt2, now=_NOW + 3 * _DAY + 60)
    assert signals2 == []  # arm pass does not kill
    assert saved2.get("dry_run") is False
    assert saved2.get("armed_at") == _NOW + 3 * _DAY + 60
    assert len(notified) == 1 and "auto-armed" in notified[0]


async def test_no_arm_without_hook_proof(monkeypatch):
    """Elapsed window alone must NOT arm — the hook must be proven live first."""
    notified: list[str] = []

    async def fake_notify(rt, msg):
        notified.append(msg)

    monkeypatch.setattr(pr, "_notify_owner", fake_notify)
    signals, saved, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001]},
        ages={900001: 8 * _DAY},
        markers={},  # NO fresh marker anywhere → hook never proven
        ttys={},
        live_ttys=set(),
        state={"dry_run": True, "dry_run_since": _NOW},  # no hook_verified
    )
    rt = _FakeRT(pipeline=object())
    await run_reaper(rt, now=_NOW + 30 * _DAY)  # long past the window
    assert signals == []
    assert saved.get("armed_at") is None  # refused to arm without hook proof
    assert notified == []


async def test_no_arm_when_notification_fails(monkeypatch):
    """All arm criteria met, but the owner veto notice can't be delivered →
    stay in dry-run, do NOT arm (never arm silently)."""

    async def fake_notify_fail(rt, msg):
        return False  # delivery failed / outreach unavailable

    monkeypatch.setattr(pr, "_notify_owner", fake_notify_fail)
    signals, saved, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001]},
        ages={900001: 8 * _DAY},
        markers={},
        ttys={},
        live_ttys=set(),
        state={"dry_run": True, "dry_run_since": _NOW, "hook_verified": True},
    )
    rt = _FakeRT(pipeline=object())
    await run_reaper(rt, now=_NOW + 3 * _DAY + 60)
    assert signals == []
    assert saved.get("armed_at") is None  # not armed — notice undelivered
    assert saved.get("dry_run", True) is True


async def test_hard_disable_prevents_arm(monkeypatch):
    notified: list[str] = []

    async def fake_notify(rt, msg):
        notified.append(msg)

    monkeypatch.setattr(pr, "_notify_owner", fake_notify)
    signals, saved, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001]},
        ages={900001: 8 * _DAY},
        markers={},
        ttys={},
        live_ttys=set(),
        state={"dry_run": True, "dry_run_since": _NOW},
    )
    monkeypatch.setenv(pr._ENV_HARD_DISABLE, "1")
    rt = _FakeRT(pipeline=object())
    await run_reaper(rt, now=_NOW + 10 * _DAY)
    assert signals == []
    assert saved.get("armed_at") is None  # never armed while kill-switch on
    assert notified == []


async def test_job_failure_recorded(monkeypatch):
    async def boom(flag, pattern):
        raise RuntimeError("pgrep exploded")

    monkeypatch.setattr(pr, "_pgrep", boom)
    monkeypatch.setattr(pr, "_read_uptime", lambda: 10_000_000.0)

    async def fake_live():
        return set()

    monkeypatch.setattr(pr, "_live_ttys", fake_live)
    monkeypatch.setattr(pr, "_load_state", lambda: {})
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert rt.failures and rt.failures[0][0] == "process_reaper"
    assert rt.successes == []


# ── Wiring ──────────────────────────────────────────────────────────────
class _StubRT:
    _db = None

    def record_job_success(self, *_a):
        pass

    def record_job_failure(self, *_a):
        pass


async def test_wire_process_reaper_registers_job():
    sched = AsyncIOScheduler()
    _wire_process_reaper(sched, _StubRT())
    sched.start(paused=True)
    try:
        job = sched.get_job("process_reaper")
        assert job is not None
        assert isinstance(job.trigger, CronTrigger)
    finally:
        sched.shutdown(wait=False)
