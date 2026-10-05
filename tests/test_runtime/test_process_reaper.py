"""Global discovery is observation-only, including legacy operator arms.

Tests retain candidate filtering, audit, marker liveness and scheduling coverage.
All CLI/browser trees must survive discovery; owned launchers cancel their own jobs.
"""

from __future__ import annotations

import os
import signal
import subprocess

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
    monkeypatch.setattr(pr.os, "kill", lambda pid, s: signals.append((pid, s)))
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda fd, s, *args: signals.append((fd, s)))
    monkeypatch.setattr(pr, "_load_state", lambda: dict(state or {}))
    monkeypatch.setattr(pr, "_save_state", fake_save)
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
    assert "OBSERVE ONLY: pid 900001" in caplog.text
    assert rt.successes == ["process_reaper"]


async def test_legacy_arm_only_observes_detached_claude_tree(monkeypatch):
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
    assert signals == []  # an arm flag does not prove tree ownership
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


async def test_opencode_age_only_observes(monkeypatch):
    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={"opencode-ai": [900010, 900011]},
        ages={900010: 25 * 3600, 900011: 10 * 3600},  # 25h stale, 10h young
        state={"armed_by_operator": True},
    )
    rt = _FakeRT()
    await run_reaper(rt, now=_NOW)
    assert signals == []
    assert rt.successes == ["process_reaper"]


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


async def test_env_arm_only_observes_detached_claude(monkeypatch):
    """Legacy environment arming cannot authorize a global process signal."""
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
    assert signals == []


@pytest.mark.parametrize("pattern", ["claude", "codex", "opencode", "opencode-ai"])
@pytest.mark.parametrize("arm", ["state", "env", "both"])
async def test_every_cli_tree_is_observation_only(monkeypatch, pattern, arm):
    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={pattern: [900001]},
        ages={900001: 30 * _DAY},
        descendants={900001: [900002, 900003]},
        state={"armed_by_operator": True} if arm in {"state", "both"} else {},
    )
    if arm in {"env", "both"}:
        monkeypatch.setenv(pr._ENV_ARM, "true")
    observed = []

    async def record(rt, candidates, *, dry_run):
        observed.extend(candidates)
        assert dry_run is True

    monkeypatch.setattr(pr, "_record_observation", record)
    await run_reaper(_FakeRT(), now=_NOW)
    assert signals == []
    assert observed and observed[0][4] == [900002, 900003, 900001]


async def test_browser_tree_is_observation_only_when_armed(monkeypatch):
    from genesis.browser.types import BROWSER_PGREP_PATTERNS

    signals, _, _ = _patch_io(
        monkeypatch,
        pids_by_pattern={BROWSER_PGREP_PATTERNS[0]: [900010]},
        ages={900010: 30 * _DAY},
        descendants={900010: [900011]},
        state={"armed_by_operator": True},
    )
    await run_reaper(_FakeRT(), now=_NOW)
    assert signals == []


def test_marker_gc_spares_live_process_not_in_name_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_MARKER_DIR", tmp_path)
    marker = tmp_path / str(os.getpid())
    marker.write_text("live activity")
    pr._gc_markers(set())
    assert marker.exists()


def test_malformed_legacy_state_has_no_arm_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_STATE_PATH", tmp_path / "reaper_state.json")
    pr._STATE_PATH.write_text('["armed_by_operator"]')
    assert pr._load_state() == {}


async def test_real_candidate_survives_legacy_arm(monkeypatch):
    native_kill = os.kill
    with subprocess.Popen(["sleep", "30"]) as child:
        try:
            _patch_io(
                monkeypatch,
                pids_by_pattern={"claude": [child.pid]},
                ages={child.pid: 30 * _DAY},
                state={"armed_by_operator": True},
            )
            # Actual signal transport for this test: a mocked kill would make
            # survival vacuous. Only the owned sleep child is a candidate.
            monkeypatch.setattr(pr.os, "kill", native_kill)
            await run_reaper(_FakeRT(), now=_NOW)
            assert child.poll() is None
        finally:
            child.terminate()
            child.wait(timeout=5)


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


def test_legacy_arm_can_be_cleared_but_not_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_STATE_PATH", tmp_path / "reaper_state.json")
    pr._STATE_PATH.write_text('{"armed_by_operator": true, "other": 7}')
    with pytest.raises(ValueError, match="observation-only"):
        pr.set_operator_armed(True)
    pr.set_operator_armed(False)
    assert pr._load_state() == {"other": 7}


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
