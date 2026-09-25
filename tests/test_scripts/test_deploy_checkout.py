from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "lib" / "deploy_checkout.sh"
UPDATE = REPO_ROOT / "scripts" / "update.sh"


# GIT_* as well as GENESIS_*: a git-hook environment exports GIT_DIR,
# GIT_WORK_TREE and GIT_INDEX_FILE, and any of them redirects a `git -C <tmp>`
# invocation at the OUTER repository -- so a fixture running under a hook would
# mutate the real checkout index, worktree or branch state rather than the
# tmp_path one. Stripping is cheaper than auditing each call for whether it
# happens to be safe.
_INHERITED_PREFIXES = ("GENESIS_", "GIT_")


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_INHERITED_PREFIXES)
    }
    env.update(overrides)
    return env


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(HOME=str(repo.parent)),
    )
    return result.stdout.strip()


@pytest.fixture
def primary_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "primary"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked").write_text("main\n")
    _git(repo, "add", "tracked")
    _git(repo, "commit", "-m", "initial")
    return repo


def _assert_checkout(
    repo: Path,
    home: Path,
    deploy_branch: str = "main",
    *,
    allow_non_deploy: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = _clean_env(
        HOME=str(home),
        GENESIS_DEPLOY_BRANCH=deploy_branch,
        GENESIS_ALLOW_NON_DEPLOY_BRANCH="1" if allow_non_deploy else "0",
    )
    return subprocess.run(
        [
            "bash",
            "-c",
            f'. "{HELPER}"; '
            f'branch="$(genesis_resolve_deploy_branch "$1")"; '
            f'genesis_assert_deploy_checkout "$1" "$branch"',
            "deploy-checkout-test",
            str(repo),
        ],
        env=env,
        capture_output=True,
        text=True,
    )


def test_primary_deploy_branch_is_allowed(primary_repo: Path, tmp_path: Path) -> None:
    result = _assert_checkout(primary_repo, tmp_path / "home")
    assert result.returncode == 0, result.stderr


def test_non_deploy_branch_is_refused(primary_repo: Path, tmp_path: Path) -> None:
    _git(primary_repo, "switch", "-c", "feature")
    result = _assert_checkout(primary_repo, tmp_path / "home")
    assert result.returncode == 1
    assert "refusing to deploy from branch 'feature'" in result.stderr


def test_explicit_opt_out_allows_non_deploy_branch(
    primary_repo: Path, tmp_path: Path
) -> None:
    _git(primary_repo, "switch", "-c", "feature")
    result = _assert_checkout(
        primary_repo,
        tmp_path / "home",
        allow_non_deploy=True,
    )
    assert result.returncode == 0, result.stderr


def test_detached_head_is_refused(primary_repo: Path, tmp_path: Path) -> None:
    _git(primary_repo, "switch", "--detach", "HEAD")
    result = _assert_checkout(primary_repo, tmp_path / "home")
    assert result.returncode == 1
    assert "detached HEAD" in result.stderr


def test_arbitrary_linked_worktree_is_refused(
    primary_repo: Path, tmp_path: Path
) -> None:
    linked = tmp_path / "arbitrary-location"
    _git(primary_repo, "worktree", "add", "-b", "linked", str(linked))
    result = _assert_checkout(
        linked,
        tmp_path / "home",
        deploy_branch="linked",
        allow_non_deploy=True,
    )
    assert result.returncode == 1
    assert "linked worktree" in result.stderr


def test_configured_non_main_deploy_branch_is_allowed(
    primary_repo: Path, tmp_path: Path
) -> None:
    _git(primary_repo, "switch", "-c", "stable")
    home = tmp_path / "home"
    config = home / ".genesis" / "config" / "deploy.conf"
    config.parent.mkdir(parents=True)
    config.write_text("DEPLOY_BRANCH=stable\n")
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{HELPER}"; '
            'branch="$(genesis_resolve_deploy_branch "$1")"; '
            'test "$branch" = stable; '
            'genesis_assert_deploy_checkout "$1" "$branch"',
            "deploy-checkout-test",
            str(primary_repo),
        ],
        env=_clean_env(HOME=str(home)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "config_value",
    ['DEPLOY_BRANCH="stable"\n', "DEPLOY_BRANCH=stable \n", "DEPLOY_BRANCH=stable\r\n"],
)
def test_configured_branch_normalizes_common_file_formats(
    primary_repo: Path, tmp_path: Path, config_value: str
) -> None:
    _git(primary_repo, "switch", "-c", "stable")
    home = tmp_path / "home"
    config = home / ".genesis" / "config" / "deploy.conf"
    config.parent.mkdir(parents=True)
    config.write_bytes(config_value.encode())
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{HELPER}"; test "$(genesis_resolve_deploy_branch "$1")" = stable',
            "deploy-checkout-test",
            str(primary_repo),
        ],
        env=_clean_env(HOME=str(home)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_transient_env_override_is_not_persisted(
    primary_repo: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    config = home / ".genesis" / "config" / "deploy.conf"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{HELPER}"; genesis_ensure_deploy_config "$GENESIS_DEPLOY_BRANCH"',
        ],
        env=_clean_env(
            HOME=str(home),
            GENESIS_DEPLOY_BRANCH="temporary",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not config.exists()


def test_explicit_persistent_env_override_is_written(
    primary_repo: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    config = home / ".genesis" / "config" / "deploy.conf"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{HELPER}"; genesis_ensure_deploy_config "$GENESIS_DEPLOY_BRANCH"',
        ],
        env=_clean_env(
            HOME=str(home),
            GENESIS_DEPLOY_BRANCH="stable",
            GENESIS_PERSIST_DEPLOY_BRANCH="1",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert config.read_text() == "DEPLOY_BRANCH=stable\n"


def test_actual_update_refuses_wrong_branch_before_mutation(
    primary_repo: Path, tmp_path: Path
) -> None:
    scripts = primary_repo / "scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "update.sh").write_text(UPDATE.read_text())
    (scripts / "lib" / "deploy_checkout.sh").write_text(HELPER.read_text())
    _git(primary_repo, "add", "scripts")
    _git(primary_repo, "commit", "-m", "add update")
    _git(primary_repo, "switch", "-c", "feature")

    home = tmp_path / "home"
    breadcrumb = home / ".genesis" / "cc_suppression_outcome"
    breadcrumb.parent.mkdir(parents=True)
    breadcrumb.write_text("must-survive\n")
    result = subprocess.run(
        ["bash", str(scripts / "update.sh")],
        env=_clean_env(
            HOME=str(home),
            GENESIS_UPDATE_FROM_TEMP="1",
            GENESIS_DEPLOY_BRANCH="main",
        ),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert "refusing to deploy from branch 'feature'" in result.stderr
    assert breadcrumb.read_text() == "must-survive\n"


def test_forced_explicit_selection_rewrites_config(
    primary_repo: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    config = home / ".genesis" / "config" / "deploy.conf"
    config.parent.mkdir(parents=True)
    config.write_text("DEPLOY_BRANCH=main\n")
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{HELPER}"; genesis_ensure_deploy_config stable 1',
        ],
        env=_clean_env(
            HOME=str(home),
            GENESIS_DEPLOY_BRANCH="stable",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert config.read_text() == "DEPLOY_BRANCH=stable\n"


def test_persistent_env_override_rewrites_existing_config(
    primary_repo: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    config = home / ".genesis" / "config" / "deploy.conf"
    config.parent.mkdir(parents=True)
    config.write_text("DEPLOY_BRANCH=main\n")
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{HELPER}"; genesis_ensure_deploy_config "$GENESIS_DEPLOY_BRANCH"',
        ],
        env=_clean_env(
            HOME=str(home),
            GENESIS_DEPLOY_BRANCH="stable",
            GENESIS_PERSIST_DEPLOY_BRANCH="1",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert config.read_text() == "DEPLOY_BRANCH=stable\n"


def test_pending_branch_file_round_trips(
    primary_repo: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{HELPER}"; '
            'genesis_write_deploy_pending stable; '
            'test "$(genesis_read_deploy_branch_file "$(genesis_deploy_pending_file)")" = stable; '
            'test "$(stat -c %a "$(genesis_deploy_pending_file)")" = 600',
        ],
        env=_clean_env(HOME=str(home)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
