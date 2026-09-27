"""The git hooks that keep `live`, the local integration branch, rebuild-only.

`live` is origin/main plus the candidate branches in the deploy manifest,
rebuilt with `git commit-tree` (which runs no hooks). These tests run the REAL
hook files from ``scripts/hooks`` against scratch repositories through
``core.hooksPath``, so they exercise the shipped shell, not a copy of its logic.

Each refusal is paired with a control that must succeed, so a hook that refuses
everything (or nothing) fails the suite.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
_HOOK_NAMES = ("pre-commit", "pre-merge-commit", "pre-push")


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(home),
        GIT_CONFIG_GLOBAL=str(home / "gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return env


def _git(repo: Path, env: dict[str, str], *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture()
def setup(tmp_path: Path):
    """A bare `origin`, a clone wired to the real hooks, and a `live` branch."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "gitconfig").write_text("")
    env = _env(home)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in _HOOK_NAMES:
        shutil.copy2(_HOOKS / name, hooks / name)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env=env, check=True)
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(repo)], env=env, check=True, capture_output=True
    )
    _git(repo, env, "checkout", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, env, "add", "a.txt")
    _git(repo, env, "commit", "-q", "-m", "base")
    _git(repo, env, "push", "-q", "origin", "main")
    # Hooks go live only AFTER the fixture's own commits and push to main.
    _git(repo, env, "config", "core.hooksPath", str(hooks))
    _git(repo, env, "checkout", "-q", "-b", "live")
    _git(repo, env, "checkout", "-q", "-b", "feature", "main")
    # The manifest is what declares that this install runs an integration branch.
    (home / ".genesis").mkdir()
    (home / ".genesis" / "deploy_manifest.json").write_text("{}\n")
    return repo, env


def _drop_manifest(env) -> None:
    (Path(env["HOME"]) / ".genesis" / "deploy_manifest.json").unlink()


def _commit_file(repo, env, name, text, *, check=True):
    (repo / name).write_text(text)
    _git(repo, env, "add", name)
    return _git(repo, env, "commit", "-q", "-m", f"add {name}", check=check)


# ── pre-commit ──────────────────────────────────────────────────────────


def test_commit_on_live_is_refused(setup):
    repo, env = setup
    _git(repo, env, "checkout", "-q", "live")
    res = _commit_file(repo, env, "b.txt", "b\n", check=False)
    assert res.returncode != 0
    assert "Commit on 'live'" in res.stdout + res.stderr


def test_commit_on_a_live_branch_is_allowed_without_a_manifest(setup):
    """An install's own branch that happens to be named `live` is not captured."""
    repo, env = setup
    _drop_manifest(env)
    _git(repo, env, "checkout", "-q", "live")
    res = _commit_file(repo, env, "b.txt", "b\n", check=False)
    assert res.returncode == 0, res.stdout + res.stderr


def test_commit_on_a_feature_branch_is_allowed(setup):
    repo, env = setup
    res = _commit_file(repo, env, "b.txt", "b\n", check=False)
    assert res.returncode == 0, res.stdout + res.stderr


# ── pre-merge-commit ────────────────────────────────────────────────────


def test_merge_commit_into_live_is_refused(setup):
    repo, env = setup
    _commit_file(repo, env, "b.txt", "b\n")
    _git(repo, env, "checkout", "-q", "live")
    # Diverge `live` from `feature` without a hook (plumbing), so the merge
    # below must create a merge commit and therefore reaches pre-merge-commit.
    tree = _git(repo, env, "write-tree").stdout.strip()
    tip = _git(
        repo, env, "commit-tree", tree, "-p", "HEAD", "-m", "rebuild", "-m", "Deploy-rebuild: x"
    ).stdout.strip()
    _git(repo, env, "update-ref", "refs/heads/live", tip)
    before = _git(repo, env, "rev-parse", "HEAD").stdout.strip()
    res = _git(repo, env, "merge", "--no-edit", "feature", check=False)
    assert res.returncode != 0
    assert "Merge into 'live'" in res.stdout + res.stderr
    assert _git(repo, env, "rev-parse", "HEAD").stdout.strip() == before


def test_merge_commit_into_live_is_allowed_without_a_manifest(setup):
    repo, env = setup
    _drop_manifest(env)
    _commit_file(repo, env, "b.txt", "b\n")
    _git(repo, env, "checkout", "-q", "live")
    _commit_file(repo, env, "c.txt", "c\n")
    res = _git(repo, env, "merge", "--no-edit", "feature", check=False)
    assert res.returncode == 0, res.stdout + res.stderr


def test_merge_commit_into_a_feature_branch_is_allowed(setup):
    repo, env = setup
    _git(repo, env, "checkout", "-q", "-b", "other", "main")
    _commit_file(repo, env, "c.txt", "c\n")
    _git(repo, env, "checkout", "-q", "feature")
    _commit_file(repo, env, "b.txt", "b\n")
    res = _git(repo, env, "merge", "--no-edit", "other", check=False)
    assert res.returncode == 0, res.stdout + res.stderr


def test_fast_forward_into_live_is_not_stopped_by_any_git_hook(setup):
    """Pins the gap the pre-merge-commit header states: a fast-forward creates no
    commit, so git runs no hook, and a feature branch's own commit lands on `live`
    unrefused. Only the Claude Code merge guard refuses this command before git
    runs it; typed in a plain shell it goes through. If a git-level refusal is
    ever added, this test must change with the header."""
    repo, env = setup
    _commit_file(repo, env, "b.txt", "b\n")
    feature_tip = _git(repo, env, "rev-parse", "feature").stdout.strip()
    _git(repo, env, "checkout", "-q", "live")
    res = _git(repo, env, "merge", "--ff-only", "feature", check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _git(repo, env, "rev-parse", "live").stdout.strip() == feature_tip


def test_conflicted_merge_into_live_is_refused_at_commit(setup):
    """Pins the header claim: a merge that stops on a conflict never runs
    pre-merge-commit; the later `git commit` runs pre-commit, which refuses."""
    repo, env = setup
    (repo / "a.txt").write_text("feature\n")
    _git(repo, env, "commit", "-q", "-am", "feature edit")
    _git(repo, env, "checkout", "-q", "live")
    (repo / "a.txt").write_text("live\n")
    _git(repo, env, "add", "a.txt")
    tree = _git(repo, env, "write-tree").stdout.strip()
    tip = _git(
        repo, env, "commit-tree", tree, "-p", "HEAD", "-m", "rebuild", "-m", "Deploy-rebuild: x"
    ).stdout.strip()
    _git(repo, env, "reset", "-q", "--hard", tip)
    res = _git(repo, env, "merge", "--no-edit", "feature", check=False)
    assert res.returncode != 0
    assert "CONFLICT" in res.stdout + res.stderr
    (repo / "a.txt").write_text("resolved\n")
    _git(repo, env, "add", "a.txt")
    res = _git(repo, env, "commit", "--no-edit", check=False)
    assert res.returncode != 0
    assert "Commit on 'live'" in res.stdout + res.stderr


# ── pre-push ────────────────────────────────────────────────────────────


def _rebuild_commit(repo, env, message: tuple[str, ...]) -> str:
    tree = _git(repo, env, "write-tree").stdout.strip()
    args = ["commit-tree", tree, "-p", "HEAD"]
    for m in message:
        args += ["-m", m]
    return _git(repo, env, *args).stdout.strip()


def test_push_of_a_branch_carrying_a_rebuild_commit_is_refused(setup):
    repo, env = setup
    tip = _rebuild_commit(repo, env, ("rebuild live", "Deploy-rebuild: m1"))
    _git(repo, env, "update-ref", "refs/heads/feature", tip)
    _commit_file(repo, env, "b.txt", "b\n")  # own work on top, cut from live
    res = _git(repo, env, "push", "origin", "feature", check=False)
    assert res.returncode != 0
    assert "Deploy-rebuild commit" in res.stdout + res.stderr
    assert _git(repo, env, "ls-remote", "origin", "feature").stdout == ""


def test_push_of_a_branch_without_rebuild_commits_is_allowed(setup):
    repo, env = setup
    _commit_file(repo, env, "b.txt", "b\n")
    res = _git(repo, env, "push", "origin", "feature", check=False)
    assert res.returncode == 0, res.stdout + res.stderr


def test_rebuild_commit_is_refused_on_a_non_origin_remote_too(setup, tmp_path):
    repo, env = setup
    fork = tmp_path / "fork.git"
    subprocess.run(["git", "init", "-q", "--bare", str(fork)], env=env, check=True)
    _git(repo, env, "remote", "add", "fork", str(fork))
    tip = _rebuild_commit(repo, env, ("rebuild live", "Deploy-rebuild: m1"))
    _git(repo, env, "update-ref", "refs/heads/feature", tip)
    res = _git(repo, env, "push", "fork", "feature", check=False)
    assert res.returncode != 0
    assert "Deploy-rebuild commit" in res.stdout + res.stderr


def test_empty_valued_trailer_is_still_a_rebuild_commit(setup):
    repo, env = setup
    tip = _rebuild_commit(repo, env, ("rebuild live", "Deploy-rebuild:"))
    _git(repo, env, "update-ref", "refs/heads/feature", tip)
    res = _git(repo, env, "push", "origin", "feature", check=False)
    assert res.returncode != 0
    assert "Deploy-rebuild commit" in res.stdout + res.stderr


def test_rebuild_trailer_check_ignores_the_manifest(setup):
    """The trailer is Genesis-specific, so its check needs no manifest."""
    repo, env = setup
    _drop_manifest(env)
    tip = _rebuild_commit(repo, env, ("rebuild live", "Deploy-rebuild: m1"))
    _git(repo, env, "update-ref", "refs/heads/feature", tip)
    res = _git(repo, env, "push", "origin", "feature", check=False)
    assert res.returncode != 0


def test_force_push_over_an_unfetched_remote_tip_is_allowed(setup, tmp_path):
    """The remote branch was updated elsewhere and never fetched here, so its
    tip is unknown locally. `$remote_oid..$local_oid` would fail and refuse an
    ordinary push; the hook falls back to walking the pushed tip's whole
    ancestry, which carries no rebuild commit here."""
    repo, env = setup
    _commit_file(repo, env, "b.txt", "b\n")
    _git(repo, env, "push", "-q", "origin", "feature")
    other = tmp_path / "other"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "-b",
            "feature",
            str(tmp_path / "origin.git"),
            str(other),
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    _commit_file(other, env, "elsewhere.txt", "x\n")
    _git(other, env, "push", "-q", "--no-verify", "origin", "feature")
    _commit_file(repo, env, "c.txt", "c\n")
    unknown = _git(other, env, "rev-parse", "HEAD").stdout.strip()
    assert _git(repo, env, "cat-file", "-e", unknown, check=False).returncode != 0  # guard
    res = _git(repo, env, "push", "--force", "origin", "feature", check=False)
    assert res.returncode == 0, res.stdout + res.stderr


def test_rebuild_commit_already_on_the_remote_is_not_re_flagged(setup):
    """Only the commits a push PUBLISHES are checked (remote_oid..local_oid)."""
    repo, env = setup
    tip = _rebuild_commit(repo, env, ("rebuild live", "Deploy-rebuild: m1"))
    _git(repo, env, "update-ref", "refs/heads/feature", tip)
    # Put it on the remote without hooks, as a pre-existing state.
    _git(repo, env, "push", "--no-verify", "-q", "origin", "feature")
    _commit_file(repo, env, "b.txt", "b\n")
    res = _git(repo, env, "push", "origin", "feature", check=False)
    assert res.returncode == 0, res.stdout + res.stderr


def test_new_branch_past_a_stale_tracking_ref_is_still_checked(setup):
    """A remote-tracking ref records the last fetch, not what the remote holds
    now. Here one still points at a rebuild commit the remote no longer has; a
    new branch built on it must be refused, not excused because some tracking
    ref already reaches the commit."""
    repo, env = setup
    tip = _rebuild_commit(repo, env, ("rebuild live", "Deploy-rebuild: m1"))
    _git(repo, env, "update-ref", "refs/remotes/origin/gone", tip)
    _git(repo, env, "update-ref", "refs/heads/feature", tip)
    _commit_file(repo, env, "b.txt", "b\n")
    res = _git(repo, env, "push", "origin", "feature", check=False)
    assert res.returncode != 0
    assert "Deploy-rebuild commit" in res.stdout + res.stderr


def test_trailer_separator_config_cannot_hide_a_rebuild_commit(setup):
    """With `trailer.separators` set to something without `:`, git would not
    parse `Deploy-rebuild: m1` as a trailer at all. The hook pins the separator."""
    repo, env = setup
    _git(repo, env, "config", "trailer.separators", "#")
    tip = _rebuild_commit(repo, env, ("rebuild live", "Deploy-rebuild: m1"))
    _git(repo, env, "update-ref", "refs/heads/feature", tip)
    res = _git(repo, env, "push", "origin", "feature", check=False)
    assert res.returncode != 0
    assert "Deploy-rebuild commit" in res.stdout + res.stderr


def test_push_is_refused_when_its_commits_cannot_be_listed(setup):
    """Fail closed: if git cannot list what the push publishes, refuse rather
    than publish unchecked. An invalid `log.date` makes `git log` fail while
    `git push` itself still works (measured on git 2.43), so without the refusal
    this push would succeed. (A PATH shim cannot stand in: git puts its own
    exec-path first on a hook's PATH. A missing object cannot either: the push
    then fails later on its own, and the test would pass with no refusal.)"""
    repo, env = setup
    _commit_file(repo, env, "b.txt", "b\n")
    _git(repo, env, "config", "log.date", "bogusfmt")
    res = _git(repo, env, "push", "origin", "feature", check=False)
    assert res.returncode != 0
    assert "could not list the commits" in res.stdout + res.stderr
    assert _git(repo, env, "ls-remote", "origin", "feature").stdout == ""


def test_branch_deletion_push_is_allowed_on_a_sha256_repository(tmp_path):
    """A deletion sends an all-zero object id, 64 characters long under SHA-256.
    The hook must recognise it by shape; a 40-zero comparison would treat it as
    a commit, fail to list it, and refuse every deletion."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "gitconfig").write_text("")
    env = _env(home)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(_HOOKS / "pre-push", hooks / "pre-push")
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "--object-format=sha256", "-b", "main", str(origin)],
        env=env,
        check=True,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", "-b", "main", str(repo)],
        env=env,
        check=True,
    )
    _git(repo, env, "remote", "add", "origin", str(origin))
    (repo / "a.txt").write_text("a\n")
    _git(repo, env, "add", "a.txt")
    _git(repo, env, "commit", "-q", "-m", "base")
    _git(repo, env, "config", "core.hooksPath", str(hooks))
    _git(repo, env, "checkout", "-q", "-b", "feature")
    _git(repo, env, "push", "-q", "origin", "feature")
    assert len(_git(repo, env, "rev-parse", "HEAD").stdout.strip()) == 64  # guard
    res = _git(repo, env, "push", "origin", "--delete", "feature", check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _git(repo, env, "ls-remote", "origin", "feature").stdout == ""


def test_signature_display_does_not_pose_as_a_rebuild_commit(setup, tmp_path):
    """With `log.showSignature` set, git writes gpg's report to stdout beside the
    format. Those lines must not read as a trailer hit: an ordinary signed branch
    has to publish."""
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen not available")
    repo, env = setup
    key = tmp_path / "signkey"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        env=env,
        check=True,
        capture_output=True,
    )
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(f"t@example.invalid {(tmp_path / 'signkey.pub').read_text()}")
    _git(repo, env, "config", "gpg.format", "ssh")
    _git(repo, env, "config", "user.signingkey", str(key))
    _git(repo, env, "config", "gpg.ssh.allowedSignersFile", str(allowed))
    (repo / "b.txt").write_text("b\n")
    _git(repo, env, "add", "b.txt")
    _git(repo, env, "commit", "-q", "-S", "-m", "signed work")
    _git(repo, env, "config", "log.showSignature", "true")
    shown = _git(repo, env, "log", "-1", "--format=%H").stdout
    assert "Good" in shown or "signature" in shown.lower()  # guard: the display is on
    res = _git(repo, env, "push", "origin", "feature", check=False)
    assert res.returncode == 0, res.stdout + res.stderr


def test_comment_char_config_cannot_hide_a_rebuild_commit(setup):
    """`core.commentChar` set to the trailer's first letter makes git treat the
    trailer line as a comment. The hook pins it, so the trailer is still seen."""
    repo, env = setup
    _git(repo, env, "config", "core.commentChar", "D")
    tip = _rebuild_commit(repo, env, ("rebuild live", "Deploy-rebuild: m1"))
    _git(repo, env, "update-ref", "refs/heads/feature", tip)
    res = _git(repo, env, "push", "origin", "feature", check=False)
    assert res.returncode != 0
    assert "Deploy-rebuild commit" in res.stdout + res.stderr


def test_a_rebuild_commit_already_on_main_does_not_block_new_branches(setup, tmp_path):
    """Commits on origin's main are public already. If one ever carries the
    trailer (a squash message ending in it), new branches cut from main must
    still publish; only commits NOT on main are this hook's business."""
    repo, env = setup
    tip = _rebuild_commit(repo, env, ("squash with a stray trailer", "Deploy-rebuild: x"))
    origin = tmp_path / "origin.git"
    _git(repo, env, "push", "-q", "--no-verify", "origin", f"{tip}:refs/heads/main")
    _git(repo, env, "fetch", "-q", "origin")
    _git(repo, env, "checkout", "-q", "-b", "topic", "origin/main")
    _commit_file(repo, env, "t.txt", "t\n")
    res = _git(repo, env, "push", "origin", "topic", check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert origin.exists()  # guard: the fixture's origin is the one pushed to


def test_prose_mentioning_the_trailer_is_not_a_rebuild_commit(setup):
    """The check reads git's TRAILER, not a substring: a subject that merely
    mentions `Deploy-rebuild:` is ordinary work and must publish."""
    repo, env = setup
    tip = _rebuild_commit(repo, env, ("docs: explain the Deploy-rebuild: trailer", "prose"))
    _git(repo, env, "update-ref", "refs/heads/feature", tip)
    res = _git(repo, env, "push", "origin", "feature", check=False)
    assert res.returncode == 0, res.stdout + res.stderr


def test_branch_deletion_push_is_allowed(setup):
    repo, env = setup
    _commit_file(repo, env, "b.txt", "b\n")
    _git(repo, env, "push", "-q", "origin", "feature")
    res = _git(repo, env, "push", "origin", "--delete", "feature", check=False)
    assert res.returncode == 0, res.stdout + res.stderr


def test_direct_push_to_main_on_origin_is_still_refused(setup):
    repo, env = setup
    _git(repo, env, "checkout", "-q", "main")
    tree = _git(repo, env, "write-tree").stdout.strip()
    tip = _git(repo, env, "commit-tree", tree, "-p", "HEAD", "-m", "x").stdout.strip()
    _git(repo, env, "update-ref", "refs/heads/main", tip)
    res = _git(repo, env, "push", "origin", "main", check=False)
    assert res.returncode != 0
    assert "Direct push to refs/heads/main" in res.stdout + res.stderr


def test_every_live_refusal_names_the_same_manifest_path():
    """Four refusal sites decide 'is an integration branch in use' by the same
    file. A rename in one of them would silently disarm it, so pin them together."""
    root = _HOOKS.parents[1]
    sites = {
        "scripts/hooks/pre-commit": '"$HOME/.genesis/deploy_manifest.json"',
        "scripts/hooks/pre-merge-commit": '"$HOME/.genesis/deploy_manifest.json"',
        "scripts/review_enforcement_commit.py": '".genesis" / "deploy_manifest.json"',
        "scripts/hooks/git_push_guard.py": '".genesis", "deploy_manifest.json"',
    }
    for rel, needle in sites.items():
        assert needle in (root / rel).read_text(), rel
