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
import os
import signal
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from genesis.observability.snapshots.deploy_health import (
    GUARDIAN_HOST_PATHS,
    MAIN_CHECKOUT_PATHS_SHOWN,
    collect_git_facts,
    collect_host_gateway,
    collect_main_checkout_dirty,
    collect_missing_units,
    collect_tier2_pending,
    derive_findings,
    last_success_update,
    resolve_commit,
)

# importlib, not a plain import: snapshots/__init__.py re-exports the function
# under the submodule's own name, so `import … as` would bind the function.
dh_module = importlib.import_module("genesis.observability.snapshots.deploy_health")


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


# ── main_checkout: tracked edits in the deploy checkout ──────────────
#
# The collector runs the deploy scripts' OWN predicate (scripts/lib/
# deploy_checkout.sh) through bash, so there is one definition of "a dirty
# deploy root". Every test below runs that real bash against a scratch git
# repository carrying copies of the real libs, never a mocked subprocess:
# the defects worth pinning live in the bash call (a wrong arity, a failed
# source read as clean), and a fake would return what the test believed.

_REAL_LIBS = Path(__file__).resolve().parents[2] / "scripts" / "lib"
_PROBE_LIBS = ("deploy_marker.sh", "deploy_checkout.sh")


@pytest.fixture
def no_deploy(monkeypatch):
    """The collector skips while a deploy is in progress; pin that to False so
    a real deploy on the machine running the suite cannot change a verdict."""
    from genesis import env

    monkeypatch.setattr(env, "update_in_progress", lambda: False)


def _with_libs(root: Path) -> None:
    (root / "scripts" / "lib").mkdir(parents=True)
    for name in _PROBE_LIBS:
        (root / "scripts" / "lib" / name).write_text((_REAL_LIBS / name).read_text())


@pytest.fixture
def deploy_root(tmp_path, no_deploy):
    """A primary checkout holding the real deploy libs, a tracked AGENTS.md (an
    ephemeral path) and a tracked a.txt, all committed: clean."""
    r = tmp_path / "deploy"
    _with_libs(r)
    (r / "AGENTS.md").write_text("stats\n")
    (r / "a.txt").write_text("a\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def test_main_checkout_clean(deploy_root):
    assert collect_main_checkout_dirty(deploy_root) == {
        "status": "clean",
        "count": 0,
        "paths": [],
        "paths_omitted": 0,
    }


def test_main_checkout_tracked_edit_is_dirty(deploy_root):
    (deploy_root / "a.txt").write_text("edited in place\n")
    got = collect_main_checkout_dirty(deploy_root)
    assert got["status"] == "dirty"
    assert got["count"] == 1
    assert got["paths"] == ["a.txt"]


def test_main_checkout_ephemeral_only_is_clean(deploy_root):
    """AGENTS.md is on the deploy scripts' ephemeral allowlist: a deploy does
    not refuse on it, so neither does this finding."""
    (deploy_root / "AGENTS.md").write_text("rewritten stats\n")
    # Guard-the-guard: git really sees the edit.
    assert _git(deploy_root, "status", "--porcelain").strip() == "M AGENTS.md"
    assert collect_main_checkout_dirty(deploy_root)["status"] == "clean"


def test_main_checkout_untracked_only_is_clean(deploy_root):
    (deploy_root / "stray.log").write_text("x\n")
    assert collect_main_checkout_dirty(deploy_root)["status"] == "clean"


def test_main_checkout_sees_a_hidden_assume_unchanged_edit(deploy_root):
    """git status hides an edit behind assume-unchanged; the deploy predicate
    does not, and a deploy refuses on it, so the finding must fire."""
    _git(deploy_root, "update-index", "--assume-unchanged", "a.txt")
    (deploy_root / "a.txt").write_text("hidden edit\n")
    assert _git(deploy_root, "status", "--porcelain") == ""  # really hidden
    got = collect_main_checkout_dirty(deploy_root)
    assert got["status"] == "dirty"
    assert got["paths"] == ["a.txt"]
    # The probe's scratch index is removed after a completed run.
    assert not list((deploy_root / ".git").glob("genesis-hidden-index.*"))


def test_main_checkout_linked_worktree_is_not_the_deploy_root(deploy_root, tmp_path):
    """A dev worktree running the code is not the deploy checkout: no finding,
    even when it is dirty. The worktree sits OUTSIDE any .claude/worktrees
    path, so git's own git-dir comparison is what decides."""
    wt = tmp_path / "elsewhere" / "wt"
    _git(deploy_root, "worktree", "add", "-q", "-b", "dev", str(wt))
    (wt / "a.txt").write_text("dev edit\n")
    assert collect_main_checkout_dirty(wt)["status"] == "not_deploy_root"
    # Control: the same edit in the primary checkout IS dirty, so the verdict
    # above came from the worktree test, not from a probe that sees nothing.
    (deploy_root / "a.txt").write_text("dev edit\n")
    assert collect_main_checkout_dirty(deploy_root)["status"] == "dirty"


@pytest.mark.parametrize("missing", _PROBE_LIBS)
def test_main_checkout_missing_lib_is_unknown(deploy_root, missing):
    """A lib that cannot be sourced must never read as clean (it would resolve
    a standing alert) or as not-the-deploy-root."""
    (deploy_root / "a.txt").write_text("edited\n")
    (deploy_root / "scripts" / "lib" / missing).unlink()
    got = collect_main_checkout_dirty(deploy_root)
    assert got["status"] == "unknown"
    assert got["reason"]


def test_main_checkout_non_repo_is_unknown(tmp_path, no_deploy):
    """git cannot answer at all: unknown, never clean and never 'not ours'."""
    r = tmp_path / "plain"
    _with_libs(r)
    assert collect_main_checkout_dirty(r)["status"] == "unknown"


def _pid_gone(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.rsplit(") ", 1)[1].split(" ", 1)[0] == "Z"


def test_main_checkout_timeout_is_unknown_and_leaves_no_process(deploy_root, tmp_path, monkeypatch):
    """A wedged git: the probe times out, reports unknown, and kills the whole
    process group, so no git grandchild outlives it."""
    shim = tmp_path / "bin"
    shim.mkdir()
    pidfile = tmp_path / "git.pids"
    (shim / "git").write_text('#!/bin/sh\necho $$ >> "$GIT_SHIM_PIDS"\nexec sleep 300\n')
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    monkeypatch.setenv("GIT_SHIM_PIDS", str(pidfile))
    started = time.monotonic()
    got = collect_main_checkout_dirty(deploy_root, timeout=1.0)
    # Returns promptly: a surviving grandchild holding the pipes would make it
    # wait on the shim's 300 s sleep (60 s is a loose bound, not a benchmark).
    assert time.monotonic() - started < 60
    assert got["status"] == "unknown"
    assert "timed out" in got["reason"]
    pids = [int(p) for p in pidfile.read_text().split()]
    assert pids, "precondition: the shim git really ran"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not all(_pid_gone(p) for p in pids):
        time.sleep(0.05)
    leftover = [p for p in pids if not _pid_gone(p)]
    for p in leftover:  # never leak a sleeper past the test, whatever happens
        os.kill(p, signal.SIGKILL)
    assert not leftover, f"git grandchildren survived the timeout: {leftover}"


def test_main_checkout_does_not_rewrite_the_index(deploy_root):
    """GIT_OPTIONAL_LOCKS=0: a stat-stale index must NOT be refreshed and
    written back by the probe, which would take index.lock under a concurrent
    deploy's merge."""
    st = (deploy_root / "a.txt").stat()
    os.utime(deploy_root / "a.txt", ns=(st.st_atime_ns, st.st_mtime_ns - 10**10))
    index = deploy_root / ".git" / "index"
    before = (index.stat().st_ino, index.stat().st_mtime_ns)
    assert collect_main_checkout_dirty(deploy_root)["status"] == "clean"
    assert (index.stat().st_ino, index.stat().st_mtime_ns) == before


def test_main_checkout_index_lock_held_is_still_read(deploy_root):
    """Another process holds index.lock (a deploy mid-merge): the probe reads
    the status anyway and leaves the lock alone."""
    lock = deploy_root / ".git" / "index.lock"
    lock.write_text("")
    (deploy_root / "a.txt").write_text("edited\n")
    assert collect_main_checkout_dirty(deploy_root)["status"] == "dirty"
    assert lock.exists()


def test_main_checkout_skipped_during_a_deploy(deploy_root, monkeypatch):
    from genesis import env

    (deploy_root / "a.txt").write_text("edited\n")
    monkeypatch.setattr(env, "update_in_progress", lambda: True)
    assert collect_main_checkout_dirty(deploy_root)["status"] == "deploying"


def test_main_checkout_bounds_the_display_list_but_counts_exactly(deploy_root):
    n = MAIN_CHECKOUT_PATHS_SHOWN + 3
    for i in range(n):
        (deploy_root / f"f{i:02d}.txt").write_text("x\n")
    _git(deploy_root, "add", "-A")
    _git(deploy_root, "commit", "-qm", "more")
    for i in range(n):
        (deploy_root / f"f{i:02d}.txt").write_text("edited\n")
    got = collect_main_checkout_dirty(deploy_root)
    assert got["count"] == n
    assert len(got["paths"]) == MAIN_CHECKOUT_PATHS_SHOWN
    assert got["paths_omitted"] == 3


# Names git quotes in porcelain output under its default core.quotePath:
# non-ASCII bytes as octal escapes, and a space, quote, backslash or control
# character wherever it appears (measured, git 2.43).
_UNUSUAL_NAMES = (
    "café.md",
    "sp ace.md",
    'quo"te.md',
    "back\\slash.md",
    "tab\there.md",
    "new\nline.md",
)


def _commit_and_edit(root: Path, names) -> None:
    for name in names:
        (root / name).write_text("v1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "unusual names")
    for name in names:
        (root / name).write_text("v2\n")


def test_main_checkout_names_quoted_paths_as_the_files_they_are(deploy_root):
    """A name git quotes is reported as the file an operator can find, not as
    git's escaped form ("caf\\303\\251.md")."""
    _commit_and_edit(deploy_root, _UNUSUAL_NAMES)
    # Guard-the-guard: git really quotes these in the porcelain it prints.
    assert '"caf\\303\\251.md"' in _git(deploy_root, "status", "--porcelain")
    got = collect_main_checkout_dirty(deploy_root)
    assert got["status"] == "dirty"
    assert sorted(got["paths"]) == sorted(_UNUSUAL_NAMES)
    assert got["count"] == len(_UNUSUAL_NAMES)


def test_main_checkout_names_a_hidden_edit_to_a_quoted_path(deploy_root):
    """The assume-unchanged pass reads diff-files --name-status, which quotes a
    non-ASCII or control-character name but NOT a space (measured, git 2.43)."""
    _commit_and_edit(deploy_root, ["café.md"])
    _git(deploy_root, "checkout", "--", "café.md")
    _git(deploy_root, "update-index", "--assume-unchanged", "café.md")
    (deploy_root / "café.md").write_text("hidden edit\n")
    assert _git(deploy_root, "status", "--porcelain") == ""  # really hidden
    assert collect_main_checkout_dirty(deploy_root)["paths"] == ["café.md"]


def test_main_checkout_one_file_quoted_in_one_pass_and_not_the_other(deploy_root):
    """A staged edit to "sp ace.md" arrives quoted from git status and the hidden
    edit unquoted from diff-files: still one file."""
    _commit_and_edit(deploy_root, ["sp ace.md"])
    _git(deploy_root, "add", "sp ace.md")
    _git(deploy_root, "update-index", "--assume-unchanged", "sp ace.md")
    (deploy_root / "sp ace.md").write_text("v2\nthen edited\n")
    got = collect_main_checkout_dirty(deploy_root)
    assert got["paths"] == ["sp ace.md"]
    assert got["count"] == 1


def test_main_checkout_a_non_utf8_name_does_not_raise(deploy_root, monkeypatch):
    """core.quotePath=false makes git print a name's raw bytes, and a POSIX name
    need not be valid UTF-8. The collector must report the edit, with the bad
    byte shown escaped, and never raise: a raise here took the whole deploy-health
    snapshot down to "error", unrelated findings with it. LC_ALL=C makes the
    predicate's grep pass the line through (under a UTF-8 locale it drops it,
    which is the shared predicate's own defect and is tracked separately)."""
    name = b"lat\xe9.md"
    (deploy_root / os.fsdecode(name)).write_bytes(b"v1\n")
    _git(deploy_root, "add", "-A")
    _git(deploy_root, "commit", "-qm", "latin-1 name")
    _git(deploy_root, "config", "core.quotePath", "false")
    (deploy_root / os.fsdecode(name)).write_bytes(b"v2\n")
    monkeypatch.setenv("LC_ALL", "C")
    got = collect_main_checkout_dirty(deploy_root)
    assert got["status"] == "dirty"
    assert got["paths"] == ["lat\\xe9.md"]


def test_main_checkout_counts_a_file_once_when_staged_and_hidden(deploy_root):
    """A staged edit shows in git status and a further worktree edit behind
    assume-unchanged shows in the hidden pass: two status records, one file."""
    (deploy_root / "a.txt").write_text("staged\n")
    _git(deploy_root, "add", "a.txt")
    _git(deploy_root, "update-index", "--assume-unchanged", "a.txt")
    (deploy_root / "a.txt").write_text("staged\nthen edited\n")
    got = collect_main_checkout_dirty(deploy_root)
    assert got["status"] == "dirty"
    assert got["count"] == 1
    assert got["paths"] == ["a.txt"]


def test_main_checkout_a_failed_read_still_kills_the_probe(deploy_root, tmp_path, monkeypatch):
    """A read that fails for any reason other than the timeout also kills the
    probe's group before the failure propagates, so no git grandchild outlives
    it; the collector then reports unknown rather than raising."""
    shim = tmp_path / "bin"
    shim.mkdir()
    pidfile = tmp_path / "git.pids"
    (shim / "git").write_text('#!/bin/sh\necho $$ >> "$GIT_SHIM_PIDS"\nexec sleep 300\n')
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    monkeypatch.setenv("GIT_SHIM_PIDS", str(pidfile))
    real_popen = subprocess.Popen

    class FailingRead(real_popen):
        failed = False

        def communicate(self, input=None, timeout=None):
            if not FailingRead.failed:
                FailingRead.failed = True
                deadline = time.monotonic() + 10  # let the shim git start first
                while time.monotonic() < deadline and not pidfile.exists():
                    time.sleep(0.05)
                raise RuntimeError("read failed")
            return super().communicate(input=input, timeout=timeout)

    monkeypatch.setattr(dh_module.subprocess, "Popen", FailingRead)
    got = collect_main_checkout_dirty(deploy_root, timeout=60.0)
    assert got["status"] == "unknown"
    assert got["reason"] == "collector failed: RuntimeError"
    pids = [int(p) for p in pidfile.read_text().split()]
    assert pids, "precondition: the shim git really ran"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not all(_pid_gone(p) for p in pids):
        time.sleep(0.05)
    leftover = [p for p in pids if not _pid_gone(p)]
    for p in leftover:  # never leak a sleeper past the test, whatever happens
        os.kill(p, signal.SIGKILL)
    assert not leftover, f"git grandchildren survived the failed read: {leftover}"


def test_main_checkout_collector_failure_is_unknown_not_raised(deploy_root, monkeypatch):
    """Like every other collector here, an unexpected failure degrades to its
    own unknown instead of escaping into deploy_health(), whose catch-all would
    drop every unrelated finding on the tick."""

    def broken(repo, timeout):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(dh_module, "_collect_main_checkout_dirty", broken)
    got = collect_main_checkout_dirty(deploy_root)
    assert got["status"] == "unknown"
    assert got["reason"] == "collector failed: RuntimeError"


@pytest.mark.parametrize(
    ("quoted", "raw"),
    [
        (b"plain.txt", b"plain.txt"),
        (b'"caf\\303\\251.md"', "café.md".encode()),
        (b'"a\\tb\\nc\\"d\\\\e"', b'a\tb\nc"d\\e'),
        # Escapes git never emits are kept literally rather than raising.
        (b'"bad\\q\\9z"', b"bad\\q\\9z"),
        (b'"trailing\\"', b"trailing\\"),
    ],
)
def test_git_unquote(quoted, raw):
    assert dh_module._git_unquote(quoted) == raw


def test_derive_findings_main_checkout_keys():
    common = dict(
        missing_units=[], tier2_pending=None, host_gateway={"status": "ok"}, commits_behind=0
    )
    # Default: every existing caller's output is unchanged.
    assert derive_findings(**common) == []
    assert derive_findings(**common, main_checkout={"status": "dirty", "count": 3}) == [
        "main_checkout_dirty:3"
    ]
    assert derive_findings(**common, main_checkout={"status": "unknown"}) == [
        "main_checkout_unreadable"
    ]
    for quiet in ("clean", "not_deploy_root", "deploying"):
        assert derive_findings(**common, main_checkout={"status": quiet}) == []
