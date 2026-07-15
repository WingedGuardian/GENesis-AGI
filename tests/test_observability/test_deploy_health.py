"""Tests for the deploy-staleness snapshot collectors (deploy_health.py).

The collectors answer "is what's MERGED actually DEPLOYED here?" — a bare
git-merge deploys code but silently skips tier-2 activation (systemd units,
guardian host redeploy, CC/Node pins). Everything is best-effort: collectors
degrade to None/empty and never raise.

Git-facts tests build a real throwaway repo (subprocess git) — the collector
shells out to git, so a fake would test nothing.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from genesis.observability.snapshots.deploy_health import (
    GUARDIAN_HOST_PATHS,
    collect_git_facts,
    collect_host_gateway,
    collect_missing_units,
    collect_tier2_pending,
    derive_findings,
    last_success_update,
)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin",
        },
    )
    return out.stdout.strip()


@pytest.fixture
def repo_with_upstream(tmp_path):
    """A clone whose origin is 2 commits ahead (fetched, not merged)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "a.txt").write_text("1")
    _git(origin, "add", "a.txt")
    _git(origin, "commit", "-qm", "c1")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    for i in (2, 3):
        (origin / "a.txt").write_text(str(i))
        _git(origin, "add", "a.txt")
        _git(origin, "commit", "-qm", f"c{i}")
    _git(clone, "fetch", "-q", "origin")
    return clone


# ── git facts ────────────────────────────────────────────────────────


def test_git_facts_counts_behind_and_fetch_age(repo_with_upstream):
    facts = collect_git_facts(repo_with_upstream)
    assert facts["head"]
    assert facts["commits_behind_upstream"] == 2
    assert facts["fetch_age_hours"] is not None
    assert facts["fetch_age_hours"] < 1


def test_git_facts_degrade_on_non_repo(tmp_path):
    facts = collect_git_facts(tmp_path / "not-a-repo")
    assert facts["head"] is None
    assert facts["commits_behind_upstream"] is None


# ── missing units ────────────────────────────────────────────────────


def test_missing_units_lists_absent_files(tmp_path):
    templates = tmp_path / "templates"
    units = tmp_path / "units"
    templates.mkdir()
    units.mkdir()
    (templates / "a.service.template").write_text("")
    (templates / "b.timer.template").write_text("")
    (units / "a.service").write_text("")
    assert collect_missing_units(templates, units) == ["b.timer"]


def test_missing_units_none_when_undeterminable(tmp_path):
    assert collect_missing_units(tmp_path / "nope", tmp_path) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert collect_missing_units(empty, tmp_path) is None


# ── tier-2 pending ───────────────────────────────────────────────────


def test_tier2_pending_lists_update_only_changes(repo_with_upstream):
    repo = repo_with_upstream
    baseline = _git(repo, "rev-parse", "HEAD")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "update.sh").write_text("#!/bin/bash\n")
    (repo / "unrelated.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "tier2 change")
    pending = collect_tier2_pending(repo, baseline)
    assert pending == ["scripts/update.sh"]  # unrelated.py is not tier-2


def test_tier2_pending_none_without_baseline(repo_with_upstream):
    assert collect_tier2_pending(repo_with_upstream, None) is None
    assert collect_tier2_pending(repo_with_upstream, "0" * 40) is None


# ── host gateway ─────────────────────────────────────────────────────


def _write_state(path: Path, deployed: str) -> None:
    path.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(UTC).isoformat(),
                "version": {"deployed_commit": deployed},
            }
        )
    )


def test_host_gateway_no_data_without_state_file(tmp_path, repo_with_upstream):
    assert collect_host_gateway(repo_with_upstream, tmp_path / "nope.json") == {"status": "no_data"}


def test_host_gateway_unknown_commit(tmp_path, repo_with_upstream):
    state = tmp_path / "state.json"
    _write_state(state, "unknown")
    assert collect_host_gateway(repo_with_upstream, state)["status"] == "unknown_commit"
    _write_state(state, "deadbeef")  # does not resolve in this repo
    assert collect_host_gateway(repo_with_upstream, state)["status"] == "unknown_commit"


def test_host_gateway_ok_at_head(tmp_path, repo_with_upstream):
    state = tmp_path / "state.json"
    _write_state(state, _git(repo_with_upstream, "rev-parse", "HEAD"))
    out = collect_host_gateway(repo_with_upstream, state)
    assert out["status"] == "ok"
    assert out["drift_files"] == 0
    assert out["age_hours"] is not None


def test_host_gateway_drift_on_guardian_path_change(tmp_path, repo_with_upstream):
    repo = repo_with_upstream
    deployed = _git(repo, "rev-parse", "HEAD")
    guardian_file = repo / GUARDIAN_HOST_PATHS[0] / "core.py"
    guardian_file.parent.mkdir(parents=True)
    guardian_file.write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "guardian change")
    state = tmp_path / "state.json"
    _write_state(state, deployed)
    out = collect_host_gateway(repo, state)
    assert out["status"] == "drift"
    assert out["drift_files"] == 1


# ── last successful update ───────────────────────────────────────────


async def _update_history_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        "CREATE TABLE update_history (id TEXT PRIMARY KEY, old_tag TEXT, new_tag TEXT,"
        " old_commit TEXT, new_commit TEXT, status TEXT, rollback_tag TEXT,"
        " failure_reason TEXT, degraded_subsystems TEXT, started_at TEXT, completed_at TEXT)"
    )
    return db


async def test_last_success_update_age():
    db = await _update_history_db()
    old = (datetime.now(UTC) - timedelta(days=9)).isoformat()
    newer = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await db.execute(
        "INSERT INTO update_history (id, status, new_commit, completed_at) VALUES"
        f" ('1', 'success', 'aaa', '{old}'), ('2', 'failed', 'bbb', '{newer}'),"
        f" ('3', 'success', 'ccc', '{newer}')"
    )
    out = await last_success_update(db)
    await db.close()
    assert out["new_commit"] == "ccc"  # newest SUCCESS, failed row ignored
    assert 1.9 < out["age_days"] < 2.1


async def test_last_success_update_empty_table():
    db = await _update_history_db()
    out = await last_success_update(db)
    await db.close()
    assert out == {"completed_at": None, "new_commit": None, "age_days": None}


async def test_last_success_update_no_db():
    out = await last_success_update(None)
    assert out["age_days"] is None


# ── findings contract ────────────────────────────────────────────────


def test_derive_findings_keys_are_stable():
    findings = derive_findings(
        missing_units=["b.timer", "a.service"],
        tier2_pending=["scripts/update.sh"],
        host_gateway={"status": "drift"},
        commits_behind=60,
        update_age_days=8.0,
    )
    assert findings == [
        "missing_units:a.service,b.timer",  # sorted -> deterministic
        "tier2_pending:1",
        "host_guardian_drift",
        "stale_update:8.0d,60behind",
        "behind_upstream:60",
    ]


def test_derive_findings_quiet_when_healthy():
    assert (
        derive_findings(
            missing_units=[],
            tier2_pending=None,
            host_gateway={"status": "ok"},
            commits_behind=3,
            update_age_days=0.5,
        )
        == []
    )


def test_stale_update_fires_below_behind_threshold():
    """Review BLOCKER regression guard: 7+ days stale and 20-49 commits behind
    must produce a finding — previously the only behind-axis finding required
    >50 commits, so this exact range (a genuinely stale install) alerted
    NOTHING while the awareness formula was written against >=20."""
    findings = derive_findings(
        missing_units=[],
        tier2_pending=None,
        host_gateway={"status": "ok"},
        commits_behind=25,
        update_age_days=7.5,
    )
    assert findings == ["stale_update:7.5d,25behind"]


def test_stale_update_requires_both_axes():
    common = dict(missing_units=[], tier2_pending=None, host_gateway={"status": "ok"})
    # Recently updated, even if well behind at last fetch: not stale.
    assert derive_findings(**common, commits_behind=25, update_age_days=2.0) == []
    # Old update but nearly caught up by bare merges: not the paging condition.
    assert derive_findings(**common, commits_behind=5, update_age_days=30.0) == []
    # Unknown age (no update_history yet): never fabricates staleness.
    assert derive_findings(**common, commits_behind=25, update_age_days=None) == []
