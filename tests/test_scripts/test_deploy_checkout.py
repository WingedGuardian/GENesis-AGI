from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "lib" / "deploy_checkout.sh"

_INHERITED_PREFIXES = ("GENESIS_", "GIT_")


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(_INHERITED_PREFIXES)
    }
    env.update(overrides)
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


def _repo(tmp_path: Path, *, branch: str = "main") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", branch)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("initial\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _run_helper(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{HELPER}"\n{script}'],
        capture_output=True,
        text=True,
        env=_clean_env(**env),
    )


def test_resolve_uses_the_fetch_remote_head(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/public/stable", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/public/HEAD", "refs/remotes/public/stable")
    _git(repo, "update-ref", "refs/remotes/origin/other", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/other")

    result = _run_helper(
        f'genesis_resolve_deploy_branch "{repo}" public',
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "stable"


def test_resolve_refreshes_the_live_remote_head(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    repo = _repo(tmp_path)
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "main")
    _git(repo, "checkout", "-b", "stable")
    (repo / "file.txt").write_text("stable\n")
    _git(repo, "commit", "-am", "stable")
    _git(repo, "push", "origin", "stable")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/stable")
    _git(repo, "remote", "set-branches", "origin", "main")
    _git(repo, "update-ref", "-d", "refs/remotes/origin/stable")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    result = _run_helper(
        f'genesis_resolve_deploy_branch "{repo}" origin',
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "stable"


def test_linked_worktree_is_refused_before_the_shared_remote_head_is_written(
    tmp_path: Path,
) -> None:
    """The primary-checkout check must run BEFORE the resolver's one write.

    `genesis_resolve_deploy_branch` refreshes `refs/remotes/<remote>/HEAD` from a
    live `ls-remote`. That ref lives in the COMMON git dir, shared with every
    worktree — so run from a linked worktree, the old order (resolve, then
    assert) rewrote the primary checkout's ref and only then refused.

    Control arm: the old order, from the same worktree, DOES move the ref. Without
    it this test could not tell "the order prevents the write" from "the write
    never happens", and would pass against a resolver that silently stopped
    caching.
    """
    remote = tmp_path / "remote"
    repo = _repo(tmp_path)
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "main")
    _git(repo, "checkout", "-b", "stable")
    _git(repo, "push", "origin", "stable")
    _git(repo, "checkout", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/stable")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    worktree = tmp_path / "elsewhere" / "linked"
    _git(repo, "worktree", "add", str(worktree), "-b", "linked")
    home = tmp_path / "home"
    home.mkdir()  # no genesis.yaml, so no persisted deploy_branch override

    def remote_head() -> str:
        return _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD").stdout.strip()

    # The order update.sh now uses.
    new_order = _run_helper(
        f'genesis_assert_primary_checkout "{worktree}" || exit 1\n'
        f'genesis_resolve_deploy_branch "{worktree}" origin',
        HOME=str(home),
    )
    assert new_order.returncode != 0
    assert "must not run from a linked worktree" in new_order.stderr
    assert remote_head() == "refs/remotes/origin/main", (
        "a refused worktree run must not rewrite the shared remote-tracking HEAD"
    )

    # Control: the previous order writes the shared ref before refusing.
    old_order = _run_helper(
        f'genesis_resolve_deploy_branch "{worktree}" origin >/dev/null\n'
        f'genesis_assert_deploy_checkout "{worktree}" stable',
        HOME=str(home),
    )
    assert old_order.returncode != 0
    assert remote_head() == "refs/remotes/origin/stable", (
        "control arm: the resolver must actually write the ref, or the assertion "
        "above proves nothing about ordering"
    )


def test_update_checks_the_primary_checkout_before_resolving_the_branch() -> None:
    """Pin the ORDER in update.sh itself, which the test above cannot see."""
    text = (REPO_ROOT / "scripts" / "update.sh").read_text()
    primary = text.index('genesis_assert_primary_checkout "$GENESIS_ROOT" || exit 1')
    resolve = text.index('genesis_resolve_deploy_branch "$GENESIS_ROOT" "$UPDATE_REMOTE"')
    assert primary < resolve


def test_resolve_falls_back_when_the_live_probe_fails_under_pipefail(
    tmp_path: Path,
) -> None:
    """An unsuccessful remote probe must read as EMPTY, never as fatal.

    update.sh runs with `set -Eeuo pipefail`, so a non-zero `ls-remote` inside the
    probe's command substitution takes the shell down at the ASSIGNMENT — before
    the cached-HEAD fallback underneath it can run. An unreachable or slow remote
    would then refuse a deployment that the local symbolic ref could resolve
    perfectly well.

    The shim fails ONLY `ls-remote`, so every other git call still works and the
    cached ref really is readable; otherwise this would pass for the wrong reason.
    """
    repo = _repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/public/stable", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/public/HEAD", "refs/remotes/public/stable")

    real_git = shutil.which("git")
    assert real_git, "git must be on PATH for this test to mean anything"
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '    [ "$arg" = "ls-remote" ] && exit 128\n'
        "done\n"
        f'exec {real_git} "$@"\n'
    )
    shim.chmod(0o755)

    result = _run_helper(
        f'set -Eeuo pipefail\ngenesis_resolve_deploy_branch "{repo}" public',
        PATH=f"{shim_dir}:{os.environ['PATH']}",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stable"


def test_resolve_honors_the_persisted_deploy_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True)
    (home / ".genesis" / "config" / "genesis.yaml").write_text(
        "github:\n  deploy_branch: release/test\n"
    )

    result = _run_helper(
        f'genesis_resolve_deploy_branch "{repo}" public',
        HOME=str(home),
        GENESIS_DEPLOY_BRANCH="ignored-per-process-override",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "release/test"


def test_local_github_value_declines_rather_than_guessing_without_pyyaml(
    tmp_path: Path,
) -> None:
    """With no yaml, resolve NOTHING — do not hand-parse a branch name.

    This asserted the opposite until 2026-09-26: a 55-line fallback parser read
    the config by hand and returned a branch — a guess emitted by the very path
    whose premise is that the config is unreadable, handed to the one caller that
    acts on it by mutating a checkout. Three review rounds fixed three defects in
    it (a lossy quoted scalar, ignored YAML hierarchy, and being reached on any
    `safe_load` failure rather than a missing module) before anyone asked whether
    it should exist.

    Declining is correct on its own terms: the caller falls through to the
    remote's advertised HEAD, and `genesis_assert_deploy_checkout` refuses the
    deploy if that disagrees with the branch actually checked out.
    """
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True)
    (home / ".genesis" / "config" / "genesis.yaml").write_text(
        "github:\n  public_repo: GENesis-AGI\n  deploy_branch: 'release/test' # keep\n"
    )
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    # `-S` drops site-packages, so `import yaml` fails the way a bare
    # interpreter without the dependency would.
    shim.write_text('#!/bin/sh\nexec /usr/bin/python3 -S "$@"\n')
    shim.chmod(0o755)

    result = _run_helper(
        "genesis_local_github_value deploy_branch",
        HOME=str(home),
        PATH=f"{shim_dir}:{os.environ['PATH']}",
        # No venv: the helper prefers "$VENV_DIR/bin/python", so an inherited
        # VENV_DIR would read the config successfully and this test would stop
        # exercising the no-PyYAML path at all.
        VENV_DIR="",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        "an unreadable config must resolve to nothing, not to a hand-parsed guess"
    )


def test_local_github_value_prefers_the_venv_over_a_bare_python_without_yaml(
    tmp_path: Path,
) -> None:
    """The venv is read FIRST, so a system Python without PyYAML loses nothing.

    PyYAML is a hard dependency of the venv, not of the system interpreter. Before
    this helper existed, update.sh read `public_repo` through
    "$VENV_DIR/bin/python"; reading it with bare python3 instead would silently
    drop the key on any host whose system Python lacks PyYAML — and for
    `public_repo` that selects the wrong fetch remote rather than failing.

    Here bare python3 CANNOT import yaml (the `-S` shim), and the venv can. The
    value must still come back, which proves the venv was the interpreter used.
    """
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True)
    (home / ".genesis" / "config" / "genesis.yaml").write_text(
        "github:\n  public_repo: My-Fork\n  deploy_branch: release/test\n"
    )
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "python3").write_text('#!/bin/sh\nexec /usr/bin/python3 -S "$@"\n')
    (shim_dir / "python3").chmod(0o755)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    # The interpreter running this suite has PyYAML (the helper-under-test
    # imports it on the path above), so it stands in for a real venv.
    (venv / "bin" / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (venv / "bin" / "python").chmod(0o755)

    for key, expected in (("public_repo", "My-Fork"), ("deploy_branch", "release/test")):
        result = _run_helper(
            f"genesis_local_github_value {key}",
            HOME=str(home),
            PATH=f"{shim_dir}:{os.environ['PATH']}",
            VENV_DIR=str(venv),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected, (
            f"{key}: a system Python without PyYAML must not hide the key when a "
            f"venv is present; got {result.stdout.strip()!r}"
        )


def test_local_github_value_reads_the_config_when_yaml_is_available(
    tmp_path: Path,
) -> None:
    """The positive control for the test above — otherwise it passes vacuously.

    Without this, a helper that returned "" unconditionally would satisfy the
    decline assertion perfectly.
    """
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True)
    (home / ".genesis" / "config" / "genesis.yaml").write_text(
        "github:\n  public_repo: GENesis-AGI\n  deploy_branch: 'release/test' # keep\n"
    )

    result = _run_helper(
        "genesis_local_github_value deploy_branch",
        HOME=str(home),
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "release/test"


def test_resolve_rejects_invalid_branch_name(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True)
    (home / ".genesis" / "config" / "genesis.yaml").write_text(
        "github:\n  deploy_branch: ../escape\n"
    )

    result = _run_helper(
        f'genesis_resolve_deploy_branch "{repo}" public',
        HOME=str(home),
    )

    assert result.returncode != 0
    assert "invalid Genesis deploy branch" in result.stderr


def test_assert_accepts_primary_checkout_on_deploy_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path, branch="main")

    result = _run_helper(
        f'genesis_assert_deploy_checkout "{repo}" main',
    )

    assert result.returncode == 0, result.stderr


def test_assert_refuses_non_deploy_branch_before_mutation(tmp_path: Path) -> None:
    repo = _repo(tmp_path, branch="feature")

    result = _run_helper(
        f'genesis_assert_deploy_checkout "{repo}" main',
    )

    assert result.returncode != 0
    assert "refusing to deploy from branch 'feature'" in result.stderr
    assert "Deploy branch: main" in result.stderr


def test_non_deploy_branch_override_admits_only_the_exact_value_one(tmp_path: Path) -> None:
    """The escape hatch on the branch refusal, in both directions.

    `GENESIS_ALLOW_NON_DEPLOY_BRANCH=1` is the documented way to deploy from a
    branch deliberately. Untested, a renamed variable silently disables the
    override, and an inverted comparison silently disables the REFUSAL — the
    control this whole change exists to add. So: exactly "1" admits, and nothing
    else does, including values a reader might assume are truthy.

    `_clean_env` strips inherited GENESIS_* variables, so each case sets its own.
    """
    repo = _repo(tmp_path, branch="feature")
    check = f'genesis_assert_deploy_checkout "{repo}" main'

    admitted = _run_helper(check, GENESIS_ALLOW_NON_DEPLOY_BRANCH="1")
    assert admitted.returncode == 0, admitted.stderr

    for value in ("", "0", "true", "yes"):
        refused = _run_helper(check, GENESIS_ALLOW_NON_DEPLOY_BRANCH=value)
        assert refused.returncode != 0, f"override value {value!r} must not admit"
        assert "refusing to deploy from branch 'feature'" in refused.stderr

    # The override relaxes the BRANCH check only. It must never admit a linked
    # worktree, which is the case #1634 exists to refuse.
    worktree = tmp_path / "elsewhere" / "linked"
    _git(repo, "worktree", "add", str(worktree), "-b", "linked")
    still_refused = _run_helper(
        f'genesis_assert_deploy_checkout "{worktree}" main',
        GENESIS_ALLOW_NON_DEPLOY_BRANCH="1",
    )
    assert still_refused.returncode != 0
    assert "must not run from a linked worktree" in still_refused.stderr

    # Nor a detached HEAD: there is no branch to be "the wrong one".
    _git(repo, "checkout", "--detach")
    detached = _run_helper(check, GENESIS_ALLOW_NON_DEPLOY_BRANCH="1")
    assert detached.returncode != 0
    assert "must not run from detached HEAD" in detached.stderr


def test_assert_refuses_detached_head(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "--detach", "HEAD")

    result = _run_helper(
        f'genesis_assert_deploy_checkout "{repo}" main',
    )

    assert result.returncode != 0
    assert "must not run from detached HEAD" in result.stderr


def test_assert_refuses_linked_worktree_at_arbitrary_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worktree = tmp_path / "outside-docs" / "linked"
    _git(repo, "worktree", "add", str(worktree), "-b", "linked")

    result = _run_helper(
        f'genesis_assert_deploy_checkout "{worktree}" main',
    )

    assert result.returncode != 0
    assert "must not run from a linked worktree" in result.stderr
    assert "Run from the primary checkout" in result.stderr


def test_assert_refuses_non_git_directory(tmp_path: Path) -> None:
    repo = tmp_path / "not-a-repo"
    repo.mkdir()

    result = _run_helper(
        f'genesis_assert_deploy_checkout "{repo}" main',
    )

    assert result.returncode != 0
    assert "must run from a Git checkout" in result.stderr
