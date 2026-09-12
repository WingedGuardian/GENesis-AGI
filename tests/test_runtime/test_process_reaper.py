"""Tests for the idle-aware process reaper.

Covers the pure classifier, the dry-run/armed orchestrator, the manual
operator-arm switch (state flag + env, with the hard kill-switch override),
non-claude age paths, marker GC, protected PIDs, and job wiring. The
2026-07-11 incident (interactive claude sessions killed purely on 7d age)
is the regression these lock down.
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


# ── Attached-pane parsing (WS-D2: detached slots are reapable) ──────────
def test_attached_panes_included_detached_excluded():
    listing = "1 /dev/pts/5\n0 /dev/pts/9\n2 /dev/pts/2\n"
    assert pr._attached_pane_ttys(listing) == {"pts/5", "pts/2"}


def test_attached_panes_all_detached_yields_empty():
    # The persistent-slot regression: bare pane existence must NOT read as
    # live — an idle detached cc-N slot has to be reapable.
    assert pr._attached_pane_ttys("0 /dev/pts/4\n0 /dev/pts/8\n") == set()


def test_attached_panes_malformed_line_fails_toward_sparing():
    # Unknown format → treated as attached (never reap on parse drift).
    assert pr._attached_pane_ttys("wat /dev/pts/7\n") == {"pts/7"}


def test_attached_panes_tty_only_line_fails_toward_sparing():
    # Format drift back to bare '#{pane_tty}' output: a tty-only line must
    # read as attached, not silently vanish from the live set.
    assert pr._attached_pane_ttys("/dev/pts/9\npts/3\n") == {"pts/9", "pts/3"}


def test_attached_panes_garbage_and_blanks_ignored():
    assert pr._attached_pane_ttys("\n?\n1 ?\n") == set()


# ── Orchestrator harness ────────────────────────────────────────────────
class _FakeRT:
    def __init__(self, *, pipeline=None):
        self._db = None
        self._outreach_pipeline = pipeline
        self.successes: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def record_job_success(self, name):
        self.successes.append(name)

    def record_job_failure(self, name, error=None, *, exc=None, error_type=None, emit_event=True):
        self.failures.append((name, error if error is not None else (str(exc) if exc else None)))


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
    monkeypatch.delenv(pr._ENV_ARM, raising=False)
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
        state={"armed_by_operator": True},
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
        state={"armed_by_operator": True},
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
        state={"armed_by_operator": True},
    )
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert signals == []


async def test_non_claude_reaped_by_age(monkeypatch):
    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"opencode-ai": [900010, 900011]},
        ages={900010: 25 * 3600, 900011: 10 * 3600},  # 25h stale, 10h young
        state={"armed_by_operator": True},
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
        state={"armed_by_operator": True},
    )
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert all(pid != my_pid for pid, _ in signals)


async def test_marker_gc_receives_live_pids(monkeypatch):
    _, _, gc_calls = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001], "opencode-ai": [900010]},
        ages={900001: 1 * _DAY, 900010: 1 * 3600},  # both young → no reap
        state={"armed_by_operator": True},
    )
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert gc_calls and {900001, 900010} <= gc_calls[0]


async def test_default_state_is_dry_run(monkeypatch):
    """Empty state + no env → dry-run: the reaper never arms itself."""
    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001]},
        ages={900001: 30 * _DAY},  # ancient + detached — would reap IF armed
        markers={},
        ttys={},
        live_ttys=set(),
        descendants={900001: []},
        state={},
    )
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert signals == []  # not armed → never signals


async def test_env_arm_non_affirmative_does_not_arm(monkeypatch):
    """GENESIS_REAPER_ARMED=0/false documents OFF and must NOT arm the reaper."""
    for val in ("0", "false", "no", "off", ""):
        signals, _, _ = _patch_io(
            monkeypatch,
            pids_by_pattern={"claude": [900001]},
            ages={900001: 30 * _DAY},
            markers={},
            ttys={},
            live_ttys=set(),
            descendants={900001: []},
            state={},
        )
        monkeypatch.setenv(pr._ENV_ARM, val)
        rt = _FakeRT()
        await run_reaper(rt, now=_NOW)
        assert signals == [], f"value {val!r} wrongly armed the reaper"


async def test_env_arm_kills_detached_claude(monkeypatch):
    """Arming via GENESIS_REAPER_ARMED (no state flag) reaps a detached idle claude."""
    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001]},
        ages={900001: 8 * _DAY},
        markers={},
        ttys={},
        live_ttys=set(),
        descendants={900001: []},
        state={},  # no state flag…
    )
    monkeypatch.setenv(pr._ENV_ARM, "1")  # …armed via env
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert (900001, 15) in signals and (900001, 9) in signals


async def test_hard_disable_overrides_operator_arm(monkeypatch):
    """The hard kill-switch forces dry-run even when the operator armed it."""
    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"claude": [900001]},
        ages={900001: 8 * _DAY},
        markers={},
        ttys={},
        live_ttys=set(),
        descendants={900001: []},
        state={"armed_by_operator": True},  # operator armed…
    )
    monkeypatch.setenv(pr._ENV_HARD_DISABLE, "1")  # …but kill-switch engaged
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert signals == []  # kill-switch wins over the arm flag


def test_set_operator_armed_roundtrip(tmp_path, monkeypatch):
    """set_operator_armed flips the persisted flag; _operator_armed reflects it."""
    monkeypatch.setattr(pr, "_STATE_PATH", tmp_path / "reaper_state.json")
    monkeypatch.delenv(pr._ENV_ARM, raising=False)
    assert pr._operator_armed(pr._load_state()) is False  # default: dry-run
    pr.set_operator_armed(True)
    assert pr._load_state().get("armed_by_operator") is True
    assert pr._operator_armed(pr._load_state()) is True
    pr.set_operator_armed(False)
    assert "armed_by_operator" not in pr._load_state()
    assert pr._operator_armed(pr._load_state()) is False


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

    def record_job_failure(self, *_a, **_kw):
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
