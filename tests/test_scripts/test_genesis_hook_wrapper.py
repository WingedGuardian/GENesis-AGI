"""Regression guard + smoke for the `.claude/hooks/genesis-hook` launcher.

The launcher's worktree venv-fallback used `git worktree list --porcelain | head`
under `set -euo pipefail`. `head` closes the pipe after one line, so when git's
output exceeds the pipe buffer (many worktrees) git dies with SIGPIPE; pipefail
+ set -e then kill the launcher silently (exit 141, no stderr), breaking EVERY
hook in that worktree. Fixed by resolving the main worktree via
`git rev-parse --git-common-dir` (no pipe). These tests lock that in.
"""

import json
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


def _invoke(root, *, dev_local=False, cwd=None, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_HOOK_DEV_LOCAL"}
    if dev_local:
        env["GENESIS_HOOK_DEV_LOCAL"] = "1"
    env.update(extra_env or {})
    return subprocess.run(
        [str(root / ".claude" / "hooks" / "genesis-hook"), "probe.py"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
        cwd=None if cwd is None else str(cwd),
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


def test_ambient_git_env_is_scrubbed_for_the_LAUNCHED_HOOK_too(tmp_path):
    """The mirror of the discovery test above, one layer down — and the layer that
    was missing until 2026-09-17.

    Scrubbing for the launcher's own ``git rev-parse`` protects WHICH script runs.
    It says nothing about what that script's OWN git queries see: until the
    ``exec`` line scrubbed as well, a launched hook inherited the ambient
    ``GIT_DIR``/``GIT_WORK_TREE`` and resolved a foreign repository despite being
    handed an explicit cwd. All four shared decision inputs the enforcement hooks
    read were MEASURED to fail OPEN that way (see
    ``tests/test_hooks/test_git_env_scrub.py``).

    Asserted on the CHILD's own view, not on the launcher's text: the variables
    must be absent from the hook's environment, and its git must resolve the
    worktree it was invoked in rather than the foreign repo the poison names.
    """
    _main, wt = _make_main_and_worktree(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    genv = {
        **os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=foreign, check=True, env=genv)

    # The probe reports what the CHILD sees. Run through the default (main-tree)
    # path so this exercises the same resolution a real session uses.
    (_main / "scripts" / "probe.py").write_text(
        "import json, os, subprocess\n"
        "top = subprocess.run(['git', 'rev-parse', '--show-toplevel'],\n"
        "                     capture_output=True, text=True)\n"
        "print(json.dumps({\n"
        "    'git_env': sorted(k for k in os.environ if k.startswith('GIT_')),\n"
        "    'toplevel': top.stdout.strip(),\n"
        "}))\n"
    )
    poison = {
        "GIT_DIR": str(foreign / ".git"),
        "GIT_WORK_TREE": str(foreign),
        "GIT_INDEX_FILE": str(foreign / ".git" / "index"),
    }

    proc = _invoke(wt, cwd=wt, extra_env=poison)
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout)

    leaked = sorted(set(poison) & set(seen["git_env"]))
    assert not leaked, f"ambient git env reached the launched hook: {leaked}"
    assert Path(seen["toplevel"]).resolve() == wt.resolve(), (
        f"the launched hook resolved a foreign repository: {seen['toplevel']!r}"
    )

    # Control: the same poison DOES redirect a child the launcher did not scrub,
    # so a pass above is the scrub working rather than an inert fixture.
    unscrubbed = subprocess.run(
        [sys.executable, str(_main / "scripts" / "probe.py")],
        cwd=str(wt), capture_output=True, text=True, env={**os.environ, **poison},
    )
    assert unscrubbed.returncode == 0, unscrubbed.stderr
    assert Path(json.loads(unscrubbed.stdout)["toplevel"]).resolve() == foreign.resolve(), (
        "the poisoned environment did not redirect an unscrubbed child — the "
        "assertions above would pass vacuously"
    )


def test_the_launcher_passes_git_config_vars_THROUGH_to_the_launched_hook(tmp_path):
    """The deliberate NON-scrub, asserted on the child rather than on the array.

    The location variables above are removed; the config FILE variables must
    NOT be, and that asymmetry is load-bearing rather than an oversight. The
    launcher scrubs for every launched hook, and ``git_push_guard`` exists to
    PREDICT what a ``git push`` will do: ``_push_config_is_simple`` is an
    allowlist over the user's effective config, where a broadening value makes
    it return False and PROMPT. MEASURED 2026-09-19 through that real function,
    with a control that moves — with ``push.default = matching`` in
    ``~/.gitconfig`` it returns False (prompts) when the config is visible and
    True (ALLOWS SILENTLY) once GIT_CONFIG_GLOBAL is pinned to /dev/null. A
    guard that predicts a command's effect has to see what that command sees.

    ``tests/test_hooks/test_git_env_scrub.py`` pins the same rule from the other
    end, by asserting those names are absent from the launcher's array. This is
    the BEHAVIOURAL half: the array could be right while the ``exec`` line
    scrubbed them some other way, and only the child's own environment can tell
    the difference.
    """
    _main, wt = _make_main_and_worktree(tmp_path)
    (_main / "scripts" / "probe.py").write_text(
        "import json, os\n"
        "print(json.dumps(sorted(k for k in os.environ if k.startswith('GIT_'))))\n"
    )

    passthrough = {
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_SYSTEM": str(tmp_path / "gitconfig"),
    }
    proc = _invoke(wt, cwd=wt, extra_env={**passthrough, "GIT_DIR": str(tmp_path / "decoy")})
    assert proc.returncode == 0, proc.stderr
    seen = set(json.loads(proc.stdout))

    missing = sorted(set(passthrough) - seen)
    assert not missing, (
        f"the launcher stripped {missing} on the way to the hook. That re-introduces "
        "a MEASURED fail-open: git_push_guard stops seeing the user's effective push "
        "config and a broadening push.default becomes a silent allow."
    )
    # Control: the location scrub is still working in the SAME invocation, so a
    # pass above is the asymmetry rather than a launcher that scrubs nothing.
    assert "GIT_DIR" not in seen, (
        "GIT_DIR survived — this test would pass vacuously against a launcher "
        "whose scrub had stopped working altogether"
    )


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
