""".serena/project.yml de-track transition in scripts/update.sh.

``.serena/project.yml`` was committed but machine-owned: Serena rewrites the
file's comment block in place on version bumps, so every install where Serena
runs went permanently dirty and ``update.sh`` aborted at the clean-tree gate
(operators resorted to a stash dance on every deploy). Same failure mode as
the ``.claude/settings.local.json`` re-track fixed by the settings-local
transition. The fix de-tracks the file upstream (``.serena/`` is already
.gitignored) and mirrors that transition in update.sh: back up the live copy
before the merge, let the upstream deletion apply cleanly, restore the copy
(untracked) after the merge join point. Serena autogenerates the file when
missing, so fresh clones need nothing.

These tests extract the marked blocks from the REAL update.sh (between
``# BEGIN/END serena-yml-premerge`` and ``# BEGIN/END serena-yml-restore``)
and run them against a scratch git repo, so the logic under test is the
shipped script, not a copy.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

SERENA_REL = ".serena/project.yml"


def _extract_block(name: str) -> str:
    text = UPDATE_SH.read_text()
    begin = f"# BEGIN serena-yml-{name}"
    end = f"# END serena-yml-{name}"
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
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    ).stdout


@pytest.fixture()
def scratch(tmp_path: Path) -> tuple[Path, Path]:
    """A scratch 'install' repo with .serena/project.yml TRACKED (pre-fix state)."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    (repo / ".serena").mkdir(parents=True)
    _git(tmp_path, "init", "-b", "main", str(repo))
    (repo / SERENA_REL).write_text("project_name: genesis  # upstream-default\n")
    _git(repo, "add", SERENA_REL)
    _git(repo, "commit", "-m", "track .serena/project.yml (pre-fix state)")
    return repo, home


def test_premerge_backs_up_and_clears_tracked_modified(scratch):
    repo, home = scratch
    live = repo / SERENA_REL
    live.write_text("project_name: genesis  # SERENA-VERSION-CHURN\n")

    _run_block("premerge", repo, home)

    bak = home / ".genesis" / "serena.project.yml.premerge"
    assert bak.read_text() == "project_name: genesis  # SERENA-VERSION-CHURN\n"
    # Worktree cleared to HEAD so an upstream deletion merges clean.
    assert live.read_text() == "project_name: genesis  # upstream-default\n"
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_upstream_deletion_merges_clean_then_restore(scratch):
    repo, home = scratch
    live = repo / SERENA_REL
    live.write_text("project_name: genesis  # SERENA-VERSION-CHURN\n")
    _run_block("premerge", repo, home)

    # Simulate the upstream de-track landing (merge-equivalent: rm + commit).
    _git(repo, "rm", "--quiet", SERENA_REL)
    _git(repo, "commit", "-m", "chore: de-track .serena/project.yml")
    assert not live.exists()

    _run_block("restore", repo, home)

    assert live.read_text() == "project_name: genesis  # SERENA-VERSION-CHURN\n"
    assert not (home / ".genesis" / "serena.project.yml.premerge").exists()
    # Restored file is untracked — must not re-dirty the tree as a tracked mod.
    assert _git(repo, "ls-files", SERENA_REL).strip() == ""


def test_restore_overwrites_reverted_content_when_no_merge_landed(scratch):
    """Nothing-to-do path: premerge cleared live edits; restore must put them back."""
    repo, home = scratch
    live = repo / SERENA_REL
    live.write_text("project_name: genesis  # SERENA-VERSION-CHURN\n")
    _run_block("premerge", repo, home)
    assert live.read_text() == "project_name: genesis  # upstream-default\n"  # cleared

    _run_block("restore", repo, home)

    assert live.read_text() == "project_name: genesis  # SERENA-VERSION-CHURN\n"
    assert not (home / ".genesis" / "serena.project.yml.premerge").exists()


def test_steady_state_untracked_is_noop(scratch):
    repo, home = scratch
    _git(repo, "rm", "--quiet", SERENA_REL)
    _git(repo, "commit", "-m", "de-tracked")
    live = repo / SERENA_REL
    live.parent.mkdir(exist_ok=True)  # git prunes .serena/ when emptied in the scratch repo
    live.write_text("project_name: genesis  # local-only\n")

    _run_block("premerge", repo, home)

    assert live.read_text() == "project_name: genesis  # local-only\n"  # untouched
    assert not (home / ".genesis" / "serena.project.yml.premerge").exists()
    _run_block("restore", repo, home)  # no backup -> no-op
    assert live.read_text() == "project_name: genesis  # local-only\n"


def _ephemeral_dirty_re() -> str:
    match = re.search(r"^EPHEMERAL_DIRTY_RE='(.*)'$", UPDATE_SH.read_text(), re.MULTILINE)
    assert match, "EPHEMERAL_DIRTY_RE assignment missing in update.sh"
    return match.group(1)


def test_dirty_guard_excuses_serena_yml_porcelain_path():
    """The clean-tree gate must excuse ` M .serena/project.yml` (Serena churn)
    but still abort on a same-named file elsewhere in the tree."""
    pattern = _ephemeral_dirty_re()
    excused = re.compile(pattern)
    assert excused.search(" M .serena/project.yml")
    assert not excused.search(" M src/.serena/project.yml")


def test_serena_yml_not_tracked_in_this_repo():
    """Steady state: the repo itself must not re-track the file (the exact
    regression that made settings.local.json a repeat offender via PR #792)."""
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", SERENA_REL],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tracked == "", f"{SERENA_REL} is tracked again — de-track it (see this test's docstring)"
