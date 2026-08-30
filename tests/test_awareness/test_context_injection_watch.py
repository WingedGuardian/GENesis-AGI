"""The context-injection watcher: ground truth for silent context loss.

The Claude Code harness persists a hook's stdout above an undocumented,
version-volatile threshold — the model gets a ~2 KB preview and the session
runs without its identity/charter/EK, with nothing anywhere saying so. These
tests pin the watcher that makes that class impossible to miss again: a fresh
persisted filing (the harness's own artifact) or the emitter's over-budget
marker produces exactly one deduped critical alert, and recovery resolves it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import aiosqlite
import pytest

from genesis.awareness import context_injection_watch_config as cfg_mod
from genesis.db.schema import create_all_tables
from genesis.observability.snapshots.context_injection import (
    InjectionHealth,
    _collect_sync,
    context_injection,
    derive_findings,
)

pytestmark = pytest.mark.asyncio

NOW = 1_800_000_000.0  # fixed epoch for age arithmetic


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _mk_filing(projects: Path, session: str, name: str, *, size: int, age_h: float) -> Path:
    d = projects / "-home-user-genesis" / session / "tool-results"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"hook-{name}-stdout.txt"
    f.write_text("A" * size)
    mtime = NOW - age_h * 3600
    os.utime(f, (mtime, mtime))
    return f


def _mk_marker(
    sessions: Path, *, sid: str = "s1", part: str = "knowledge", chars: int = 12_345, **extra
) -> Path:
    """Write a per-(session, part) marker the way the emitter does."""
    d = sessions / sid
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"injection_over_budget_{part}.json"
    payload = {
        "part": part,
        "session_id": sid,
        "chars": chars,
        "budget": 9_800,
        "ts": "2026-08-30T18:00",
    }
    payload.update(extra)
    f.write_text(json.dumps(payload))
    return f


def _mk_probe_filing(projects: Path, session: str, name: str, *, age_h: float = 1) -> Path:
    d = projects / "-home-user-genesis" / session / "tool-results"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"hook-{name}-stdout.txt"
    f.write_text("PROBE-START " + "A" * 500 + " PROBE-END")
    mtime = NOW - age_h * 3600
    os.utime(f, (mtime, mtime))
    return f


def _collect(tmp_path: Path, lookback: float = 24) -> InjectionHealth:
    return _collect_sync(
        tmp_path / "projects", tmp_path / "sessions", tmp_path / "legacy", lookback, NOW
    )


# ── collector ───────────────────────────────────────────────────────────


def test_fresh_filing_is_collected(tmp_path):
    _mk_filing(tmp_path / "projects", "sess-1", "aa", size=28_000, age_h=2)
    health = _collect(tmp_path, 24)
    assert health.filing_sessions == 1
    assert health.fresh_filings[0]["size"] == 28_000
    assert derive_findings(health), "a fresh filing must produce a finding"


def test_stale_filing_is_ignored(tmp_path):
    _mk_filing(tmp_path / "projects", "sess-1", "aa", size=28_000, age_h=48)
    health = _collect(tmp_path, 24)
    assert health.fresh_filings == []
    assert derive_findings(health) == []


def test_marker_alone_produces_finding_with_the_real_numbers(tmp_path):
    """F4 regression: the finding must render the emitter's ACTUAL per-part shape.

    A first version read a legacy flat dict and printed "? B against ? B
    (session , )" against every real marker — unit drift (bytes vs chars) inside
    one change, masked by a test that only asserted the word "self-audit".
    """
    (tmp_path / "projects").mkdir()
    _mk_marker(tmp_path / "sessions", sid="abcdef123456", part="knowledge", chars=12_345)
    findings = derive_findings(_collect(tmp_path, 24))
    assert len(findings) == 1
    assert "knowledge" in findings[0]
    assert "12345/9800 chars" in findings[0]
    assert "abcdef12" in findings[0]
    assert "?" not in findings[0]


def test_miswire_marker_reads_as_a_wiring_fault_not_a_budget_overrun(tmp_path):
    (tmp_path / "projects").mkdir()
    _mk_marker(
        tmp_path / "sessions", part="wiring", reason="no --part argument", chars=1043
    )
    findings = derive_findings(_collect(tmp_path, 24))
    assert "MIS-WIRED" in findings[0]
    assert "no --part argument" in findings[0]


def test_unreadable_marker_still_surfaces(tmp_path):
    (tmp_path / "projects").mkdir()
    d = tmp_path / "sessions" / "s1"
    d.mkdir(parents=True)
    (d / "injection_over_budget_knowledge.json").write_text("{not json")
    findings = derive_findings(_collect(tmp_path, 24))
    assert findings and "could not be read" in findings[0]


def test_legacy_sessionless_marker_is_still_swept(tmp_path):
    """An older emitter's marker must not go unnoticed after this change."""
    (tmp_path / "projects").mkdir()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "injection_over_budget_knowledge.json").write_text(
        json.dumps({"part": "knowledge", "session_id": "old", "chars": 11_000, "budget": 9_800})
    )
    assert derive_findings(_collect(tmp_path, 24))


def test_unreadable_projects_dir_reports_degraded_not_clean(tmp_path):
    """The silent all-clear is the failure this watcher exists to kill.

    MEASURED (py3.12): Path.glob SWALLOWS PermissionError and returns [], so a
    glob inside try/except OSError cannot tell "no filings" from "cannot look".
    The first version of this test asserted `health.error or filings == []`,
    which is TRUE on the clean path — it tested nothing. Assert the error.
    """
    blocked = tmp_path / "projects"
    blocked.mkdir()
    (blocked / "x").mkdir()
    os.chmod(blocked, 0o000)
    try:
        health = _collect(tmp_path, 24)
    finally:
        os.chmod(blocked, 0o755)
    assert health.error, "an unreadable projects dir must set health.error"
    assert derive_findings(health), "and must produce a DEGRADED finding"


def test_file_where_projects_dir_expected_is_degraded(tmp_path):
    (tmp_path / "projects").write_text("not a directory")
    health = _collect(tmp_path, 24)
    assert health.error


def test_absent_projects_dir_is_clean_not_degraded(tmp_path):
    """Control: a fresh install has no projects dir yet — that is NOT a fault."""
    health = _collect(tmp_path, 24)
    assert health.error is None
    assert derive_findings(health) == []


def test_healthy_dir_sets_no_error(tmp_path):
    """Control for the two above: a readable dir with a filing errors on nothing."""
    _mk_filing(tmp_path / "projects", "sess-1", "aa", size=28_000, age_h=1)
    health = _collect(tmp_path, 24)
    assert health.error is None
    assert len(health.fresh_filings) == 1


def test_probe_artifacts_are_excluded_but_counted(tmp_path):
    """F8: our own cap-measurement runs file oversized output BY DESIGN.

    MEASURED in one real window: 17 of 27 filings were probe artifacts. Paging
    on those would make every re-measurement cry wolf.
    """
    _mk_probe_filing(tmp_path / "projects", "sess-1", "p1")
    _mk_probe_filing(tmp_path / "projects", "sess-1", "p2")
    health = _collect(tmp_path, 24)
    assert health.probe_filings == 2
    assert health.fresh_filings == []
    assert derive_findings(health) == []


def test_a_real_filing_beside_probe_artifacts_still_alerts(tmp_path):
    """Control: the probe filter must not swallow a genuine loss next to it."""
    _mk_probe_filing(tmp_path / "projects", "sess-1", "p1")
    _mk_filing(tmp_path / "projects", "sess-1", "real", size=28_000, age_h=1)
    health = _collect(tmp_path, 24)
    assert health.probe_filings == 1
    assert len(health.fresh_filings) == 1
    assert derive_findings(health)


def test_multiple_sessions_counted_distinctly(tmp_path):
    _mk_filing(tmp_path / "projects", "sess-1", "aa", size=28_000, age_h=1)
    _mk_filing(tmp_path / "projects", "sess-1", "bb", size=29_000, age_h=2)
    _mk_filing(tmp_path / "projects", "sess-2", "cc", size=30_000, age_h=3)
    health = _collect(tmp_path, 24)
    assert len(health.fresh_filings) == 3
    assert health.filing_sessions == 2


def test_findings_list_caps_visibly(tmp_path):
    for i in range(8):
        _mk_filing(tmp_path / "projects", "sess-1", f"f{i}", size=28_000, age_h=i * 0.5)
    health = _collect(tmp_path, 24)
    findings = derive_findings(health, max_listed=5)
    assert "and 3 more" in findings[0]


async def test_async_entry_reads_real_fs(tmp_path):
    _mk_filing(tmp_path / "projects", "sess-9", "zz", size=27_500, age_h=0.1)
    health = await context_injection(
        projects_dir=tmp_path / "projects",
        sessions_dir=tmp_path / "sessions",
        legacy_marker_dir=tmp_path / "legacy",
        lookback_hours=24,
        now=NOW,
    )
    assert health.filing_sessions == 1


# ── awareness check wiring ──────────────────────────────────────────────


async def _alerts(db):
    cur = await db.execute(
        "SELECT content, priority, resolved_at FROM observations"
        " WHERE source = 'context_injection_monitor' AND type = 'infrastructure_alert'"
    )
    return [dict(r) for r in await cur.fetchall()]


@pytest.fixture
def _wire(monkeypatch, tmp_path):
    """Point the collector's defaults at tmp dirs for the loop-level test."""
    import genesis.observability.snapshots.context_injection as snap

    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(snap, "_default_projects_dir", lambda: projects)
    monkeypatch.setattr(snap, "_default_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(snap, "_default_legacy_marker_dir", lambda: tmp_path / "legacy")
    monkeypatch.setattr(snap.time, "time", lambda: NOW)
    return projects, tmp_path / "sessions"


async def test_check_creates_one_critical_alert(db, _wire):
    from genesis.awareness.loop import _check_context_injection_health

    projects, _ = _wire
    _mk_filing(projects.parent / "projects", "sess-1", "aa", size=28_000, age_h=1)
    await _check_context_injection_health(db)
    alerts = await _alerts(db)
    live = [a for a in alerts if not a["resolved_at"]]
    assert len(live) == 1
    assert live[0]["priority"] == "critical"
    assert "SILENTLY LOST" in live[0]["content"]


async def test_check_is_idempotent_for_same_state(db, _wire):
    from genesis.awareness.loop import _check_context_injection_health

    projects, _ = _wire
    _mk_filing(projects.parent / "projects", "sess-1", "aa", size=28_000, age_h=1)
    await _check_context_injection_health(db)
    await _check_context_injection_health(db)
    live = [a for a in await _alerts(db) if not a["resolved_at"]]
    assert len(live) == 1


async def test_recovery_resolves_the_alert(db, _wire, tmp_path):
    from genesis.awareness.loop import _check_context_injection_health

    projects, _ = _wire
    f = _mk_filing(projects.parent / "projects", "sess-1", "aa", size=28_000, age_h=1)
    await _check_context_injection_health(db)
    # age the filing out of the window
    old = NOW - 48 * 3600
    os.utime(f, (old, old))
    await _check_context_injection_health(db)
    live = [a for a in await _alerts(db) if not a["resolved_at"]]
    assert live == []


async def test_env_kill_switch_silences(db, _wire, monkeypatch):
    from genesis.awareness.loop import _check_context_injection_health

    projects, _ = _wire
    _mk_filing(projects.parent / "projects", "sess-1", "aa", size=28_000, age_h=1)
    monkeypatch.setenv("GENESIS_CONTEXT_INJECTION_WATCH_DISABLED", "1")
    await _check_context_injection_health(db)
    assert await _alerts(db) == []


# ── config lever ────────────────────────────────────────────────────────


def test_config_defaults_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg_mod, "_base_path", lambda: tmp_path / "absent.yaml")
    cfg = cfg_mod.load_config()
    assert cfg["enabled"] is True
    assert cfg_mod.knob_int(cfg, "lookback_hours") == 24
    assert cfg_mod.alert_priority(cfg) == "critical"


def test_damaged_knobs_degrade_to_defaults(monkeypatch, tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("lookback_hours: -3\nalert_priority: shout\n")
    monkeypatch.setattr(cfg_mod, "_base_path", lambda: p)
    cfg = cfg_mod.load_config()
    assert cfg_mod.knob_int(cfg, "lookback_hours") == 24
    assert cfg_mod.alert_priority(cfg) == "critical"


def test_settings_domain_registered_with_validator():
    from genesis.mcp.health.settings import _DOMAIN_REGISTRY, _DOMAIN_VALIDATORS

    assert "context_injection_watch" in _DOMAIN_REGISTRY
    validator = _DOMAIN_VALIDATORS["context_injection_watch"]
    assert validator({"lookback_hours": 12}) == []
    assert validator({"alert_priority": "shout"})
    assert validator({"bogus_key": 1})


# ── bounded scan (security review WARNING 2) ────────────────────────────


def test_scan_cap_is_reported_never_silent(monkeypatch, tmp_path):
    """A cap that hides what it dropped reads as 'all clear' — the exact
    failure this collector exists to catch. Bounded, but never silently."""
    import genesis.observability.snapshots.context_injection as snap

    monkeypatch.setattr(snap, "_MAX_SCAN", 3)
    for i in range(6):
        _mk_filing(tmp_path / "projects", f"sess-{i}", f"f{i}", size=28_000, age_h=1)
    health = _collect(tmp_path, 24)
    assert health.scan_truncated is True
    assert any("STOPPED at 3 files" in f for f in derive_findings(health))


def test_no_cap_no_truncation_claim(tmp_path):
    """Control: under the cap, nothing claims a partial reading."""
    _mk_filing(tmp_path / "projects", "sess-1", "aa", size=28_000, age_h=1)
    health = _collect(tmp_path, 24)
    assert health.scan_truncated is False
    assert not any("STOPPED at" in f for f in derive_findings(health))
