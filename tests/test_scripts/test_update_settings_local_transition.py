"""settings.local.json de-track transition in scripts/update.sh.

PR #792 accidentally re-tracked ``.claude/settings.local.json`` (install-local
by definition, already .gitignored), which made every live install permanently
dirty and blocked ``update.sh`` at the clean-tree gate. The fix de-tracks the
file upstream and adds a transition to update.sh: back up the live copy before
the merge, let the upstream deletion apply cleanly, restore the copy
(untracked) after the merge join point.

These tests extract the two marked blocks from the REAL update.sh (between
``# BEGIN/END settings-local-premerge`` and ``# BEGIN/END
settings-local-restore``) and run them against a scratch git repo, so the
logic under test is the shipped script, not a copy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

SETTINGS_REL = ".claude/settings.local.json"


def _extract_block(name: str) -> str:
    text = UPDATE_SH.read_text()
    begin = f"# BEGIN settings-local-{name}"
    end = f"# END settings-local-{name}"
    assert begin in text and end in text, f"markers for {name} missing in update.sh"
    return text.split(begin, 1)[1].split(end, 1)[0]


def _run_block(name: str, genesis_root: Path, home: Path) -> subprocess.CompletedProcess:
    script = _extract_block(name)
    return subprocess.run(
        ["bash", "-c", script],
        env={"GENESIS_ROOT": str(genesis_root), "HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo),
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout


@pytest.fixture()
def scratch(tmp_path: Path) -> tuple[Path, Path]:
    """A scratch 'install' repo with settings.local.json TRACKED (pre-fix state)."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    (repo / ".claude").mkdir(parents=True)
    _git(tmp_path, "init", "-b", "main", str(repo))
    (repo / SETTINGS_REL).write_text('{"tracked": "upstream-default"}\n')
    _git(repo, "add", SETTINGS_REL)
    _git(repo, "commit", "-m", "track settings.local.json (pre-fix state)")
    return repo, home


def test_premerge_backs_up_and_clears_tracked_modified(scratch):
    repo, home = scratch
    live = repo / SETTINGS_REL
    live.write_text('{"tracked": "LIVE-LOCAL-EDITS"}\n')

    _run_block("premerge", repo, home)

    bak = home / ".genesis" / "settings.local.json.premerge"
    assert bak.read_text() == '{"tracked": "LIVE-LOCAL-EDITS"}\n'
    # Worktree cleared to HEAD so an upstream deletion merges clean.
    assert live.read_text() == '{"tracked": "upstream-default"}\n'
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_upstream_deletion_merges_clean_then_restore(scratch):
    repo, home = scratch
    live = repo / SETTINGS_REL
    live.write_text('{"tracked": "LIVE-LOCAL-EDITS"}\n')
    _run_block("premerge", repo, home)

    # Simulate the upstream de-track landing (merge-equivalent: rm + commit).
    _git(repo, "rm", "--quiet", SETTINGS_REL)
    _git(repo, "commit", "-m", "chore: de-track settings.local.json")
    assert not live.exists()

    _run_block("restore", repo, home)

    assert live.read_text() == '{"tracked": "LIVE-LOCAL-EDITS"}\n'
    assert not (home / ".genesis" / "settings.local.json.premerge").exists()
    # Restored file is untracked — must not re-dirty the tree as a tracked mod.
    assert _git(repo, "ls-files", SETTINGS_REL).strip() == ""


def test_restore_overwrites_reverted_content_when_no_merge_landed(scratch):
    """Nothing-to-do path: premerge cleared live edits; restore must put them back."""
    repo, home = scratch
    live = repo / SETTINGS_REL
    live.write_text('{"tracked": "LIVE-LOCAL-EDITS"}\n')
    _run_block("premerge", repo, home)
    assert live.read_text() == '{"tracked": "upstream-default"}\n'  # cleared

    _run_block("restore", repo, home)

    assert live.read_text() == '{"tracked": "LIVE-LOCAL-EDITS"}\n'
    assert not (home / ".genesis" / "settings.local.json.premerge").exists()


def test_steady_state_untracked_is_noop(scratch):
    repo, home = scratch
    _git(repo, "rm", "--quiet", SETTINGS_REL)
    _git(repo, "commit", "-m", "de-tracked")
    live = repo / SETTINGS_REL
    live.parent.mkdir(exist_ok=True)  # git prunes .claude/ when emptied in the scratch repo
    live.write_text('{"untracked": "local-only"}\n')

    _run_block("premerge", repo, home)

    assert live.read_text() == '{"untracked": "local-only"}\n'  # untouched
    assert not (home / ".genesis" / "settings.local.json.premerge").exists()
    _run_block("restore", repo, home)  # no backup -> no-op
    assert live.read_text() == '{"untracked": "local-only"}\n'


def test_sync_deploy_targets_defined_before_both_call_sites():
    """The no-delta path calls _sync_deploy_targets; the function must be
    defined before EITHER call site executes (bash resolves at call time, but
    definition order in the file is the invariant a refactor could break)."""
    text = UPDATE_SH.read_text()
    def_pos = text.index("_sync_deploy_targets() {")
    calls = [i for i in range(len(text))
             if text.startswith("_sync_deploy_targets\n", i)]
    # exactly two bare call sites (nothing-to-do path + normal path)
    assert len(calls) == 2, f"expected 2 call sites, found {len(calls)}"
    assert all(def_pos < c for c in calls)
