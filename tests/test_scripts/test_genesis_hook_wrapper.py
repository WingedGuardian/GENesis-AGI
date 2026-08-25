"""Regression guard + smoke for the `.claude/hooks/genesis-hook` launcher.

The launcher's worktree venv-fallback used `git worktree list --porcelain | head`
under `set -euo pipefail`. `head` closes the pipe after one line, so when git's
output exceeds the pipe buffer (many worktrees) git dies with SIGPIPE; pipefail
+ set -e then kill the launcher silently (exit 141, no stderr), breaking EVERY
hook in that worktree. Fixed by resolving the main worktree via
`git rev-parse --git-common-dir` (no pipe). These tests lock that in.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

_WRAPPER = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "genesis-hook"


def _code_only() -> str:
    """Wrapper text with comment-only lines stripped (test code, not comments)."""
    return "\n".join(
        ln for ln in _WRAPPER.read_text().splitlines()
        if not ln.lstrip().startswith("#")
    )


def test_no_sigpipe_prone_pipe_in_code():
    """The fragile `git worktree list … | head` must not be in executable code.

    (Would FAIL on the pre-fix launcher — that's the regression this guards.)
    """
    code = _code_only()
    assert "git worktree list" not in code, "fragile worktree-list pipeline returned"
    assert "| head" not in code, "early-closing pipe under pipefail returned"


def test_uses_git_common_dir_for_main_root():
    """The venv fallback resolves the main worktree via the no-pipe rev-parse."""
    assert "git rev-parse --git-common-dir" in _code_only()


def test_wrapper_never_sigpipes_on_invocation():
    """Invoking the launcher must never die with SIGPIPE (exit 141).

    GENESIS_CC_SESSION=1 makes the hook exit early, so this exercises the
    wrapper's venv resolution (the fixed path) without hook side effects. We
    only assert it is not 141 — exit 0 (venv found) or 1 (clear "venv not
    found" error) are both acceptable across environments.
    """
    env = {**os.environ, "GENESIS_CC_SESSION": "1"}
    for _ in range(10):
        proc = subprocess.run(
            [str(_WRAPPER), "hooks/session_observer_hook.py"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=env,
        )
        assert proc.returncode != 141, f"SIGPIPE (141)! stderr={proc.stderr!r}"


# ── main-tree hook resolution (fleet-drift fix, 2026-08) ─────────────────────


def _make_main_and_worktree(tmp_path):
    """A fake main repo + a linked worktree, each with a DIFFERENT scripts/probe.py."""
    main = tmp_path / "main"
    (main / ".claude" / "hooks").mkdir(parents=True)
    (main / "scripts").mkdir()
    shutil.copy(_WRAPPER, main / ".claude" / "hooks" / "genesis-hook")
    (main / ".claude" / "hooks" / "genesis-hook").chmod(0o755)
    (main / "scripts" / "probe.py").write_text("print('MAIN')\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=main, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=main, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=main, check=True, env=env)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt)], cwd=main, check=True, env=env)
    # Divergent (uncommitted) worktree probe — simulates a branch-frozen hook copy.
    (wt / "scripts" / "probe.py").write_text("print('WORKTREE')\n")
    # venv lives in MAIN only (worktrees never pip-install); created AFTER the
    # worktree checkout so it stays untracked and absent from the worktree.
    venv_bin = main / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    return main, wt


def _invoke(root, *, dev_local=False):
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_HOOK_DEV_LOCAL"}
    if dev_local:
        env["GENESIS_HOOK_DEV_LOCAL"] = "1"
    return subprocess.run(
        [str(root / ".claude" / "hooks" / "genesis-hook"), "probe.py"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
    )


def test_worktree_session_runs_MAIN_tree_hook(tmp_path):
    """A worktree session must run the MAIN-tree hook copy, not its branch-frozen
    one — otherwise a stale/weaker security gate stays live until the branch rebases."""
    _main, wt = _make_main_and_worktree(tmp_path)
    proc = _invoke(wt)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "MAIN", f"ran the worktree copy: {proc.stdout!r}"


def test_dev_local_override_runs_worktree_hook(tmp_path):
    """GENESIS_HOOK_DEV_LOCAL=1 runs the worktree's OWN copy (for testing a hook
    change live in-worktree)."""
    _main, wt = _make_main_and_worktree(tmp_path)
    proc = _invoke(wt, dev_local=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "WORKTREE"
    # The override is ANNOUNCED (never a silent downgrade of the session's gates).
    assert "GENESIS_HOOK_DEV_LOCAL" in proc.stderr


def test_main_tree_install_runs_its_own_hook(tmp_path):
    """A normal (non-worktree) install: MAIN_ROOT resolves to its own root."""
    main, _wt = _make_main_and_worktree(tmp_path)
    proc = _invoke(main)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "MAIN"


def test_script_path_resolves_from_main_root_in_code():
    """Code-level lock: the hook script path is built from HOOK_ROOT (main-tree),
    with the dev-local escape hatch."""
    code = _code_only()
    assert "HOOK_ROOT" in code
    assert 'SCRIPT_PATH="$HOOK_ROOT/scripts/$SCRIPT_NAME"' in code
    assert "GENESIS_HOOK_DEV_LOCAL" in code


def test_ambient_git_dir_env_ignored_for_hook_discovery(tmp_path):
    """An exported GIT_DIR must NOT redirect hook resolution to a foreign repo —
    otherwise an ambient Git env could point every hook (security gates included)
    at a foreign checkout's same-named script. The launcher scrubs GIT_* for the
    git-common-dir discovery."""
    main, wt = _make_main_and_worktree(tmp_path)
    foreign = tmp_path / "foreign"
    (foreign / ".claude" / "hooks").mkdir(parents=True)
    (foreign / "scripts").mkdir()
    shutil.copy(_WRAPPER, foreign / ".claude" / "hooks" / "genesis-hook")
    (foreign / "scripts" / "probe.py").write_text("print('FOREIGN')\n")
    genv = {
        **os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=foreign, check=True, env=genv)
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_HOOK_DEV_LOCAL"}
    env["GIT_DIR"] = str(foreign / ".git")  # ambient override pointing at the foreign repo
    proc = subprocess.run(
        [str(wt / ".claude" / "hooks" / "genesis-hook"), "probe.py"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "MAIN", f"ambient GIT_DIR leaked into discovery: {proc.stdout!r}"


def test_separate_git_dir_falls_back_to_own_scripts(tmp_path):
    """A `git init --separate-git-dir` checkout makes `--git-common-dir` return
    external metadata whose parent has no `scripts/`; the launcher must REJECT that
    MAIN_ROOT and fall back to running its OWN copy, not resolve a bogus path."""
    workdir = tmp_path / "work"
    (workdir / ".claude" / "hooks").mkdir(parents=True)
    (workdir / "scripts").mkdir()
    shutil.copy(_WRAPPER, workdir / ".claude" / "hooks" / "genesis-hook")
    (workdir / ".claude" / "hooks" / "genesis-hook").chmod(0o755)
    (workdir / "scripts" / "probe.py").write_text("print('OWN')\n")
    vb = workdir / ".venv" / "bin"
    vb.mkdir(parents=True)
    (vb / "python").symlink_to(sys.executable)
    sepgit = tmp_path / "sepmeta" / "gitdir"
    sepgit.parent.mkdir(parents=True)  # git init --separate-git-dir requires the parent to exist
    genv = {
        **os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "init", "-q", f"--separate-git-dir={sepgit}", str(workdir)], check=True, env=genv
    )
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_HOOK_DEV_LOCAL"}
    proc = subprocess.run(
        [str(workdir / ".claude" / "hooks" / "genesis-hook"), "probe.py"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OWN", f"resolved a bogus MAIN_ROOT: {proc.stdout!r}"
