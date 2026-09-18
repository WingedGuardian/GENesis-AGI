"""Tests for the deploy-staleness snapshot collectors (deploy_health.py).

The collectors answer "is what's MERGED actually DEPLOYED here?" — a bare
git-merge deploys code but silently skips tier-2 activation (systemd units,
guardian host redeploy, CC/Node pins). Everything is best-effort: collectors
degrade to None/empty and never raise.

Git-facts tests build a real throwaway repo (subprocess git) — the collector
shells out to git, so a fake would test nothing.
"""

from __future__ import annotations

import importlib
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
    collect_stale_units,
    collect_tier2_pending,
    derive_findings,
    last_success_update,
    resolve_commit,
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


def test_stale_units_running_daemon_older_than_script(tmp_path):
    """Acceptance bar — the inert-detector shape: a resident daemon whose
    ExecMainStart predates its backing script's mtime reads as stale."""
    script = tmp_path / "w.sh"
    script.write_text("#!/bin/bash\n")
    import os

    os.utime(script, (2000, 2000))

    def runner(unit: str) -> tuple[bool, float | None]:
        assert unit == "genesis-tmp-watchgod.service"
        return True, 1000.0

    assert collect_stale_units(
        {"genesis-tmp-watchgod.service": script}, probe=runner
    ) == ["genesis-tmp-watchgod.service"]


def test_stale_units_fresh_and_inactive_and_unreadable(tmp_path):
    script = tmp_path / "w.sh"
    script.write_text("#!/bin/bash\n")
    import os

    os.utime(script, (2000, 2000))
    # Fresh daemon (started after the code arrived) → not stale.
    assert collect_stale_units({"u.service": script}, probe=lambda u: (True, 3000.0)) == []
    # Inactive → never stale (starting it is bootstrap's decision).
    assert collect_stale_units({"u.service": script}, probe=lambda u: (False, None)) == []
    # Unreadable start time on an ACTIVE unit → "could not determine",
    # never a clean [] that resolves a live alert.
    assert collect_stale_units({"u.service": script}, probe=lambda u: (True, None)) is None
    # Missing script file → that unit is unjudgeable, skipped (no flag).
    gone = tmp_path / "missing.sh"
    assert collect_stale_units({"u.service": gone}, probe=lambda u: (True, 1000.0)) == []


def test_stale_units_unreadable_sibling_voids_unit_verdict(tmp_path):
    """External finding: one unreadable startup file must void the WHOLE
    unit — the remaining readable paths must not force a verdict on
    incomplete facts (a missing ExecStart + newer library shape)."""
    root = tmp_path
    gone = root / "missing.sh"
    lib = root / "lib.sh"
    lib.write_text("#!/bin/bash\n")
    import os

    os.utime(lib, (3000, 3000))
    assert collect_stale_units(
        {"u.service": (gone, lib)}, probe=lambda u: (True, 1000.0)
    ) == []


def test_stale_units_whole_second_precision(tmp_path):
    """The systemd start timestamp is rendered only to whole seconds; a
    fractional mtime in the SAME second must not read as newer (shell heal
    truncates both via date +%s / stat -c %Y — the two verdicts must agree)."""
    script = tmp_path / "w.sh"
    script.write_text("#!/bin/bash\n")
    import os

    os.utime(script, (2000.9, 2000.9))
    # Daemon started at 2000.9 → rendered epoch 2000. Without int() this
    # compared 2000 < 2000.9 and flagged a fresh daemon stale forever.
    assert collect_stale_units({"u.service": script}, probe=lambda u: (True, 2000.0)) == []


def test_stale_units_future_dated_file_skips_unit(tmp_path):
    """A future-dated mtime (clock rollback, restored snapshot) is
    untrustworthy — the unit is skipped rather than flagged stale forever."""
    import time

    script = tmp_path / "w.sh"
    script.write_text("#!/bin/bash\n")
    import os

    fut = int(time.time()) + 10**6
    os.utime(script, (fut, fut))
    assert collect_stale_units({"u.service": script}, probe=lambda u: (True, 1000.0)) == []


def test_stale_units_probe_error_returns_none(tmp_path):
    """A broken probe is 'could not determine', never a clean []."""

    def boom(unit: str):
        raise RuntimeError("systemctl unavailable")

    script = tmp_path / "w.sh"
    script.write_text("x")
    assert collect_stale_units({"u.service": script}, probe=boom) is None


def test_derive_findings_keys_are_stable():
    findings = derive_findings(
        missing_units=["b.timer", "a.service"],
        tier2_pending=["scripts/update.sh"],
        host_gateway={"status": "drift"},
        commits_behind=60,
        update_age_days=8.0,
        stale_units=["genesis-tmp-watchgod.service"],
    )
    assert findings == [
        "missing_units:a.service,b.timer",  # sorted -> deterministic
        "stale_units:genesis-tmp-watchgod.service",
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


# ── resolve_commit: the short-SHA adapter, with one home ─────────────
#
# update_history.new_commit and the guardian state file both store ABBREVIATED
# commit names. Three places in this repo expanded them independently; two of
# them are now routed here. Every test drives a real throwaway repo, because
# the defects being pinned are git's behaviour, not ours, and a fake would
# return whatever the test author already believed.


@pytest.fixture
def linear_repo(tmp_path):
    """Three commits on a line. Returns (path, first, middle, tip)."""
    r = tmp_path / "linear"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    shas = []
    for name in ("first", "middle", "tip"):
        (r / f"{name}.txt").write_text(name)
        _git(r, "add", f"{name}.txt")
        _git(r, "commit", "-qm", name)
        shas.append(_git(r, "rev-parse", "HEAD"))
    return r, shas[0], shas[1], shas[2]


def test_resolve_expands_an_abbreviation(linear_repo):
    repo, _first, middle, _tip = linear_repo
    resolved, reason = resolve_commit(repo, middle[:8])
    assert resolved == middle, reason


def test_resolve_ignores_a_branch_that_shadows_the_abbreviation(linear_repo):
    """MEASURED on git 2.43.0, and the reason this is not a bare rev-parse.

    With a branch named after an 8-hex prefix of a DIFFERENT commit,
    `rev-parse --verify --quiet '<prefix>^{commit}'` returns the BRANCH's
    commit at exit 0 with EMPTY stderr — a confident, silently wrong SHA.
    `--disambiguate` reads only the object store, so no ref can shadow it.
    """
    repo, first, _middle, tip = linear_repo
    short = first[:8]
    _git(repo, "branch", short, tip)
    # Guard-the-guard: the shadowing ref must really exist, or this passes for
    # the ordinary reason and proves nothing about the ref namespace.
    assert _git(repo, "rev-parse", f"refs/heads/{short}") == tip

    resolved, reason = resolve_commit(repo, short)
    assert resolved == first, f"a refname shadowed the object name: {reason}"
    assert resolved != tip


def test_resolve_ignores_a_tag_that_shadows_the_abbreviation(linear_repo):
    """A tag shadows exactly as a branch does — same namespace lookup."""
    repo, first, _middle, tip = linear_repo
    short = first[:8]
    _git(repo, "tag", short, tip)
    assert _git(repo, "rev-parse", f"refs/tags/{short}") == tip

    resolved, reason = resolve_commit(repo, short)
    assert resolved == first, f"a tag shadowed the object name: {reason}"


def test_resolve_refuses_an_ambiguous_prefix(tmp_path):
    """Two objects, one prefix. Picking either would hand an ancestry check a
    coin flip as if it were a fact.

    This mechanism survived a mutation sweep (`if len(names) > 1` -> `if False`)
    because nothing constructed the collision.
    """
    r = tmp_path / "collide"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    seen: dict[str, str] = {}
    prefix = None
    # Hash blobs until two share a 4-hex prefix. Birthday bound on 16**4 makes
    # this a few hundred iterations; the loop is capped so a failure is a
    # readable skip rather than a hang.
    for i in range(20000):
        sha = subprocess.run(
            ["git", "-C", str(r), "hash-object", "-w", "--stdin"],
            input=f"blob-{i}\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        head4 = sha[:4]
        if head4 in seen and seen[head4] != sha:
            prefix = head4
            break
        seen[head4] = sha
    assert prefix, "could not construct a 4-hex collision"

    resolved, reason = resolve_commit(r, prefix)
    assert resolved is None
    assert "ambiguous" in reason


def test_resolve_accepts_gits_real_minimum_abbreviation(linear_repo):
    """The floor is 4, not the 8 this install happens to store.

    MEASURED on git 2.43.0: `core.abbrev=4` emits a 4-hex name and
    `core.abbrev=3` errors. An install configured that way would have every
    stored commit refused by a higher floor — the same permanent-unanswerable
    this resolver exists to prevent. Survived a `{4,40}` -> `{8,40}` mutation
    until this test existed.
    """
    repo, _first, _middle, tip = linear_repo
    resolved, reason = resolve_commit(repo, tip[:4])
    assert resolved == tip, reason


def test_resolve_refuses_an_option_shaped_name(linear_repo):
    """A stored value is still an argv input, and a ref name is not a commit
    name — `HEAD` and `main` are refused by shape, before git sees them."""
    repo, _first, _middle, _tip = linear_repo
    for hostile in ("--upload-pack=x", "-c core.pager=x", "--help", "", "HEAD", "main", "ABCDEF12"):
        resolved, reason = resolve_commit(repo, hostile)
        assert resolved is None, f"{hostile!r} should not resolve"
        assert "not a commit name" in reason


def test_resolve_refuses_a_non_string(linear_repo):
    repo, _first, _middle, _tip = linear_repo
    resolved, reason = resolve_commit(repo, None)
    assert resolved is None and "not a commit name" in reason


def test_resolve_refuses_a_blob_whose_name_is_hex(linear_repo):
    """`--disambiguate` returns blobs and trees too, so "resolved" must not be
    allowed to mean merely "exists"."""
    repo, _first, _middle, _tip = linear_repo
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input="not a commit\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert _git(repo, "cat-file", "-t", blob) == "blob", "fixture did not make a blob"

    resolved, reason = resolve_commit(repo, blob)
    assert resolved is None
    assert "not a commit" in reason


def test_resolve_reports_an_absent_commit(linear_repo):
    """`--disambiguate` reports an absent object as rc=0 with EMPTY output, not
    a non-zero rc — so the emptiness check is what catches this, not the rc."""
    repo, _first, _middle, _tip = linear_repo
    resolved, reason = resolve_commit(repo, "0" * 40)
    assert resolved is None
    assert "no object in this clone" in reason


def test_a_git_failure_is_reported_as_unchecked_not_as_absent(linear_repo, monkeypatch):
    """ "I could not look" and "it is not here" have different remedies.

    _run_git returns rc=-1 on timeout and -2 on exec failure. Folding those
    into the absent branch would tell an operator to fetch a commit that is
    sitting right there.
    """
    # importlib, not a plain import: snapshots/__init__.py re-exports the
    # `deploy_health` FUNCTION under the same name as its module, so
    # `import ...snapshots.deploy_health as dh` binds the function and
    # monkeypatch then fails on a missing attribute.
    dh = importlib.import_module("genesis.observability.snapshots.deploy_health")

    repo, _first, _middle, tip = linear_repo
    for rc, word in ((-1, "timed out"), (-2, "failed")):
        monkeypatch.setattr(dh, "_run_git", lambda *a, _rc=rc, **k: (_rc, "", ""))
        resolved, reason = dh.resolve_commit(repo, tip[:8])
        assert resolved is None
        assert word in reason, reason
        assert "no object in this clone" not in reason


def test_a_non_sha_from_disambiguate_is_refused(linear_repo, monkeypatch):
    """Guard on the value git hands back, not just on what we sent it.

    Survived a mutation (`if not _FULL_SHA.match(resolved)` -> `if False`)
    because nothing fed the resolver a malformed success.
    """
    # importlib, not a plain import: snapshots/__init__.py re-exports the
    # `deploy_health` FUNCTION under the same name as its module, so
    # `import ...snapshots.deploy_health as dh` binds the function and
    # monkeypatch then fails on a missing attribute.
    dh = importlib.import_module("genesis.observability.snapshots.deploy_health")

    repo, _first, _middle, tip = linear_repo
    monkeypatch.setattr(dh, "_run_git", lambda *a, **k: (0, "not-a-sha\n", ""))
    resolved, reason = dh.resolve_commit(repo, tip[:8])
    assert resolved is None
    assert "not a SHA" in reason


# ── the call sites routed onto it ────────────────────────────────────


def test_tier2_pending_still_works_through_the_shared_resolver(linear_repo):
    repo, first, _middle, _tip = linear_repo
    (repo / "pyproject.toml").write_text("[tool]\n")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-qm", "tier2 change")
    assert collect_tier2_pending(repo, first[:8]) == ["pyproject.toml"]
    assert collect_tier2_pending(repo, None) is None
    assert collect_tier2_pending(repo, "0" * 8) is None


def test_tier2_pending_is_not_fooled_by_a_shadowing_branch(linear_repo):
    """The behavioural payoff of the refactor: before it, a branch named after
    the stored abbreviation silently changed which range was diffed."""
    repo, first, _middle, _tip = linear_repo
    (repo / "pyproject.toml").write_text("[tool]\n")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-qm", "tier2 change")
    head = _git(repo, "rev-parse", "HEAD")
    # The shadowing branch must point at HEAD, not at an earlier commit. An
    # earlier one still has the tier-2 change in its `..HEAD` range, so both the
    # correct and the shadowed path return it and the test binds NOTHING — which
    # is exactly what a mutation sweep caught it doing.
    _git(repo, "branch", first[:8], head)
    # Guard-the-guard: the ref must really shadow, and the two ranges must
    # really disagree.
    assert _git(repo, "rev-parse", f"refs/heads/{first[:8]}") == head
    assert collect_tier2_pending(repo, head) == []

    # Resolved against the object store, `first..HEAD` carries the change.
    # Resolved as a refname it becomes `HEAD..HEAD`, which is empty.
    assert collect_tier2_pending(repo, first[:8]) == ["pyproject.toml"]
