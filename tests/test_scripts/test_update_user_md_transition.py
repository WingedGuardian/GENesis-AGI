"""USER.md de-track transition in scripts/update.sh.

``src/genesis/identity/USER.md`` shipped as a TRACKED, user-editable template
("Edit it to tell Genesis who you are"). Installs that actually filled it in
went permanently dirty, so ``update.sh`` aborted at the clean-tree gate — the
same failure mode as the ``.claude/settings.local.json`` and ``.serena/project.yml``
transitions. This release de-tracks it (real per-install USER.md is now
.gitignored, seeded from ``USER.md.example``) and mirrors those transitions in
update.sh: back up the filled copy before the merge, let the upstream rename
apply cleanly, restore the copy (untracked) after the merge join point.

These tests extract the marked blocks from the REAL update.sh (between
``# BEGIN/END user-md-premerge`` and ``# BEGIN/END user-md-restore``) and run
them against a scratch git repo, so the logic under test is the shipped script.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

USER_MD_REL = "src/genesis/identity/USER.md"
_TEMPLATE = "# User Profile\n\n- **Name**: Your name\n"
_FILLED = "# User Profile\n\n- **Name**: Jamie\n- **Timezone**: UTC\n"


def _extract_block(name: str) -> str:
    text = UPDATE_SH.read_text()
    begin = f"# BEGIN user-md-{name}"
    end = f"# END user-md-{name}"
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
    """A scratch 'install' repo with USER.md TRACKED as the template (pre-fix)."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    (repo / "src/genesis/identity").mkdir(parents=True)
    _git(tmp_path, "init", "-b", "main", str(repo))
    (repo / USER_MD_REL).write_text(_TEMPLATE)
    _git(repo, "add", USER_MD_REL)
    _git(repo, "commit", "-m", "track USER.md template (pre-fix state)")
    return repo, home


def test_premerge_backs_up_and_clears_filled_template(scratch):
    repo, home = scratch
    live = repo / USER_MD_REL
    live.write_text(_FILLED)  # user filled in the tracked template -> dirty

    _run_block("premerge", repo, home)

    bak = home / ".genesis" / "USER.md.premerge"
    assert bak.read_text() == _FILLED  # personal profile preserved outside the repo
    # Worktree cleared to HEAD so the upstream rename merges clean.
    assert live.read_text() == _TEMPLATE
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_upstream_detrack_merges_clean_then_restore(scratch):
    repo, home = scratch
    live = repo / USER_MD_REL
    live.write_text(_FILLED)
    _run_block("premerge", repo, home)

    # Simulate the upstream de-track landing (rename-equivalent: rm + add .example).
    _git(repo, "rm", "--quiet", USER_MD_REL)
    # git prunes the emptied src/genesis/identity/ dir when USER.md was its only file.
    (repo / "src/genesis/identity").mkdir(parents=True, exist_ok=True)
    (repo / "src/genesis/identity/USER.md.example").write_text(_TEMPLATE)
    _git(repo, "add", "src/genesis/identity/USER.md.example")
    _git(repo, "commit", "-m", "chore: de-track USER.md -> USER.md.example")
    assert not live.exists()

    _run_block("restore", repo, home)

    assert live.read_text() == _FILLED  # filled profile restored
    assert not (home / ".genesis" / "USER.md.premerge").exists()
    # Restored file is untracked — must not re-dirty the tree as a tracked mod.
    assert _git(repo, "ls-files", USER_MD_REL).strip() == ""


def test_restore_overwrites_reverted_content_when_no_merge_landed(scratch):
    """Nothing-to-do path: premerge cleared live edits; restore must put them back."""
    repo, home = scratch
    live = repo / USER_MD_REL
    live.write_text(_FILLED)
    _run_block("premerge", repo, home)
    assert live.read_text() == _TEMPLATE  # cleared

    _run_block("restore", repo, home)

    assert live.read_text() == _FILLED
    assert not (home / ".genesis" / "USER.md.premerge").exists()


def test_steady_state_untracked_is_noop(scratch):
    repo, home = scratch
    _git(repo, "rm", "--quiet", USER_MD_REL)
    _git(repo, "commit", "-m", "de-tracked")
    live = repo / USER_MD_REL
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(_FILLED)  # local-only per-install file

    _run_block("premerge", repo, home)

    assert live.read_text() == _FILLED  # untouched (not tracked -> no backup)
    assert not (home / ".genesis" / "USER.md.premerge").exists()
    _run_block("restore", repo, home)  # no backup -> no-op
    assert live.read_text() == _FILLED


def _ephemeral_dirty_re() -> str:
    match = re.search(r"^EPHEMERAL_DIRTY_RE='(.*)'$", UPDATE_SH.read_text(), re.MULTILINE)
    assert match, "EPHEMERAL_DIRTY_RE assignment missing in update.sh"
    return match.group(1)


def test_dirty_guard_excuses_user_md_porcelain_path():
    """The clean-tree gate must excuse ` M src/genesis/identity/USER.md` but still
    abort on a same-named file elsewhere in the tree."""
    pattern = _ephemeral_dirty_re()
    excused = re.compile(pattern)
    assert excused.search(" M src/genesis/identity/USER.md")
    assert not excused.search(" M docs/identity/USER.md")


def test_no_op_update_path_also_restores_user_md():
    """The 'Already up to date' early-exit branch restores the pre-merge backups
    inline (it returns before the common restore blocks). USER.md must be
    restored there too — otherwise a no-op update run after premerge cleared the
    live file strands the filled profile as the template. Regression guard for
    the exact gap where the transition was added to the merge path but not the
    no-op path."""
    text = UPDATE_SH.read_text()
    assert "Already up to date" in text
    branch = text.split("Already up to date", 1)[1].split("_sync_deploy_targets", 1)[0]
    # The sibling transitions anchor the branch; USER.md must be restored alongside.
    assert "SETTINGS_LOCAL_BAK" in branch and "SERENA_YML_BAK" in branch
    assert "USER_MD_BAK" in branch, "no-op update path must restore the USER.md backup"
