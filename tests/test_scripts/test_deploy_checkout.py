"""scripts/lib/deploy_checkout.sh — the one "may a deploy touch this checkout?" answer.

deploy_code_only.sh and update.sh both source it, so the two deploy paths cannot
disagree about what a deployable checkout is, which tracked edits block a deploy,
or which untracked files an incoming range would overwrite (#1634, #2303, #2589).
The checks were lifted unchanged from deploy_code_only.sh. The lib is driven
against REAL scratch repositories — a linked worktree at an arbitrary path, a bare
repository, a root pointed at the .git directory, a symlinked root, a detached
HEAD, another branch — because git's own answers are what it relies on, and an
`is-inside-work-tree` that exits 0 while printing "false" is exactly the kind of
detail a stub would get wrong.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "deploy_checkout.sh"
MARKER_LIB = REPO_ROOT / "scripts" / "lib" / "deploy_marker.sh"
UPDATE = REPO_ROOT / "scripts" / "update.sh"
CODE_ONLY = REPO_ROOT / "scripts" / "deploy_code_only.sh"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_NOSYSTEM": "1",
    "PATH": "/usr/bin:/bin",
}


def _git(cwd: Path, *args: str) -> str:
    env = {**_GIT_ENV, "HOME": str(cwd)}
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


def _bash(body: str, **env: str) -> subprocess.CompletedProcess:
    """Source both libs exactly as the callers do, under the callers' shell
    options, then run *body*."""
    script = f'set -Eeuo pipefail\n. "{MARKER_LIB}"\n. "{LIB}"\n{body}\n'
    full_env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GENESIS_"))}
    full_env.update(env)
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, env=full_env
    )


def _primary(root: Path) -> subprocess.CompletedProcess:
    return _bash(
        f'genesis_checkout_git_dirs "{root}"\n'
        f'genesis_is_primary_checkout "{root}" "$_git_dir" "$_common_dir" && echo PRIMARY || echo NOT'
    )


def _branch_ok(root: Path, *args: str, **env: str) -> tuple[bool, str]:
    r = _bash(
        f'if genesis_deploy_branch_ok "{root}" {" ".join(args)}; then echo "OK[$_branch]"; '
        f'else echo "NO[$_branch]"; fi',
        **env,
    )
    assert r.returncode == 0, r.stderr
    m = re.search(r"(OK|NO)\[(.*)\]", r.stdout)
    return m.group(1) == "OK", m.group(2)


@pytest.fixture
def repos(tmp_path: Path) -> dict[str, Path]:
    primary = tmp_path / "primary"
    _git(tmp_path, "init", "-q", "-b", "main", str(primary))
    (primary / "AGENTS.md").write_text("stats\n")
    (primary / "code.py").write_text("x = 1\n")
    _git(primary, "add", ".")
    _git(primary, "commit", "-qm", "init")
    linked = tmp_path / "anywhere" / "wt"  # NOT under a .claude/worktrees path
    _git(primary, "worktree", "add", "-q", "-b", "wt", str(linked))
    bare = tmp_path / "bare.git"
    _git(tmp_path, "clone", "-q", "--bare", str(primary), str(bare))
    parked = tmp_path / "x" / ".claude" / "worktrees" / "clone"
    parked.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(primary), str(parked))
    link = tmp_path / "primary-link"
    link.symlink_to(primary, target_is_directory=True)
    return {
        "primary": primary,
        "linked": linked,
        "bare": bare,
        "dotgit": primary / ".git",
        "parked": parked,
        "link": link,
    }


# ── genesis_is_primary_checkout ─────────────────────────────────────────────


def test_primary_checkout_and_a_symlink_to_it_are_admitted(repos):
    for name in ("primary", "link"):
        r = _primary(repos[name])
        assert r.stdout.strip() == "PRIMARY", (name, r.stdout, r.stderr)


@pytest.mark.parametrize("name", ["linked", "bare", "dotgit", "parked"])
def test_non_primary_shapes_are_refused(repos, name):
    """A linked worktree ANYWHERE (git decides, not the path), a bare repository
    and a root pointed at .git (both pass the git-dir comparison, and
    is-inside-work-tree exits 0 for them while printing "false"), and a clone
    parked under a worktree directory."""
    r = _primary(repos[name])
    assert r.stdout.strip() == "NOT", (name, r.stdout, r.stderr)


def test_the_bare_and_dotgit_refusals_come_from_the_work_tree_check(repos):
    """Guard the guard: without the is-inside-work-tree arm, the git-dir
    comparison alone ADMITS a bare repository — so that arm is load-bearing."""
    for name in ("bare", "dotgit"):
        r = _bash(
            f'genesis_checkout_git_dirs "{repos[name]}"\n'
            '[ "$(unset CDPATH; cd -- "$_git_dir" && pwd -P)" = "$_common_dir" ] '
            "&& echo SAME || echo DIFF"
        )
        assert r.stdout.strip() == "SAME", (name, r.stdout, r.stderr)


def test_a_path_that_is_not_a_repository_is_refused(tmp_path):
    assert _primary(tmp_path / "nope").stdout.strip() == "NOT"


def test_the_git_dirs_are_what_the_status_reader_needs(repos):
    """deploy_status.sh reads <_git_dir>/logs/HEAD; the lib must set it."""
    r = _bash(f'genesis_checkout_git_dirs "{repos["primary"]}"\necho "$_git_dir"')
    assert Path(r.stdout.strip()).resolve() == (repos["primary"] / ".git").resolve()


# ── genesis_deploy_branch_ok ────────────────────────────────────────────────


def test_main_is_deployable_and_sets_the_branch(repos):
    assert _branch_ok(repos["primary"]) == (True, "main")


def test_a_detached_head_is_refused_even_with_the_override(repos):
    _git(repos["primary"], "checkout", "-q", "--detach")
    ok, branch = _branch_ok(
        repos["primary"], "--allow-override", GENESIS_ALLOW_NON_DEPLOY_BRANCH="1"
    )
    assert (ok, branch) == (False, "")


@pytest.mark.parametrize("branch", ["feature", "live"])
def test_another_branch_is_refused(repos, branch):
    _git(repos["primary"], "checkout", "-q", "-b", branch)
    assert _branch_ok(repos["primary"]) == (False, branch)


def test_the_override_admits_another_named_branch_only_when_the_caller_allows_it(repos):
    _git(repos["primary"], "checkout", "-q", "-b", "feature")
    root = repos["primary"]
    # The caller passes --allow-override AND the operator set the variable.
    assert _branch_ok(root, "--allow-override", GENESIS_ALLOW_NON_DEPLOY_BRANCH="1") == (
        True,
        "feature",
    )
    # Variable set, caller did NOT opt in (deploy_code_only.sh): still refused.
    assert _branch_ok(root, GENESIS_ALLOW_NON_DEPLOY_BRANCH="1")[0] is False
    # Caller opted in, variable unset or any value but "1": still refused.
    for value in (None, "0", "yes", "true"):
        env = {} if value is None else {"GENESIS_ALLOW_NON_DEPLOY_BRANCH": value}
        assert _branch_ok(root, "--allow-override", **env)[0] is False, value


def test_the_override_never_admits_live(repos):
    """An integration branch stays refused until deploying one is decided on its
    own — the override included."""
    _git(repos["primary"], "checkout", "-q", "-b", "live")
    assert _branch_ok(
        repos["primary"], "--allow-override", GENESIS_ALLOW_NON_DEPLOY_BRANCH="1"
    ) == (False, "live")


# ── genesis_tracked_dirty_paths ─────────────────────────────────────────────


def _dirty(root: Path) -> subprocess.CompletedProcess:
    return _bash(
        f'rc=0; out="$(genesis_tracked_dirty_paths "{root}")" || rc=$?\n'
        'printf "RC=%s\\n%s" "$rc" "$out"'
    )


def test_tracked_dirty_paths_excuse_only_the_ephemeral_allowlist(repos):
    root = repos["primary"]
    (root / "untracked.txt").write_text("u\n")
    (root / "AGENTS.md").write_text("rewritten by an indexer\n")
    r = _dirty(root)
    assert r.stdout == "RC=0\n", r.stdout
    (root / "code.py").write_text("x = 2\n")
    r = _dirty(root)
    assert r.stdout.startswith("RC=0\n") and r.stdout.rstrip().endswith("code.py"), r.stdout


def test_a_rename_into_an_excused_path_is_still_dirty(repos):
    """With rename detection, `R code.py -> config/procedure_triggers.yaml` is ONE
    line naming the excused path, and the whole line would be excused. Split, the
    deletion of code.py shows. (The destination must be NEW to the index, or git
    reports a modify plus a delete rather than a rename, and the test would pass
    with or without --no-renames.)"""
    root = repos["primary"]
    (root / "config").mkdir()
    _git(root, "mv", "code.py", "config/procedure_triggers.yaml")
    renamed = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert "R  code.py -> config/procedure_triggers.yaml" in renamed, (
        "control: without --no-renames git reports ONE rename line"
    )
    r = _dirty(root)
    assert r.stdout.startswith("RC=0\n")
    assert "code.py" in r.stdout, r.stdout


def test_an_unreadable_status_returns_2_not_a_clean_tree(tmp_path):
    r = _dirty(tmp_path / "not-a-repo")
    assert r.stdout.startswith("RC=2"), r.stdout


# ── genesis_range_collisions ────────────────────────────────────────────────


@pytest.fixture
def ranged(tmp_path: Path) -> tuple[Path, str, str]:
    """<root>, the local head, and an incoming commit that adds `new.env`,
    `pkg/mod.py` and `data/seed.txt`."""
    root = tmp_path / "root"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    (root / "f").write_text("1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "c1")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-b", "incoming")
    (root / "pkg").mkdir()
    (root / "data").mkdir()
    for rel, body in (("new.env", "up\n"), ("pkg/mod.py", "up\n"), ("data/seed.txt", "up\n")):
        (root / rel).write_text(body)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "adds")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")
    return root, base, incoming


def _collisions(root: Path, frm: str, to: str) -> tuple[int, list[str]]:
    r = _bash(
        f'rc=0; out="$(genesis_range_collisions "{root}" "{frm}" "{to}")" || rc=$?\n'
        'printf "RC=%s\\n%s\\n" "$rc" "$out"'
    )
    assert r.returncode == 0, r.stderr
    first, *rest = r.stdout.splitlines()
    return int(first[3:]), [ln for ln in rest if ln]


def test_no_collision_returns_0(ranged):
    root, base, incoming = ranged
    assert _collisions(root, base, incoming) == (0, [])


def test_an_ignored_file_in_the_way_collides(ranged):
    root, base, incoming = ranged
    (root / ".git" / "info" / "exclude").write_text("new.env\n")
    (root / "new.env").write_text("LOCAL\n")
    assert _collisions(root, base, incoming) == (1, ["new.env"])


def test_an_untracked_file_where_a_parent_directory_goes_collides(ranged):
    root, base, incoming = ranged
    (root / "pkg").write_text("a file where the incoming range needs a directory\n")
    assert _collisions(root, base, incoming) == (1, ["pkg/mod.py"])


def test_a_tracked_file_in_the_way_is_gits_to_replace(tmp_path):
    root = tmp_path / "root"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    (root / "a").write_text("tracked\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "c1")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "rm", "-q", "a")
    (root / "a").mkdir()
    (root / "a" / "b").write_text("new\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "a becomes a directory")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", base)
    assert _collisions(root, base, incoming) == (0, [])


def test_the_range_is_taken_from_the_merge_base(ranged):
    """On a diverged local branch the scan lists what the INCOMING side adds. The
    local side stopped tracking `f` and keeps it on disk, ignored; upstream never
    touched `f`, so the merge leaves it alone. A two-endpoint diff (HEAD vs the
    incoming commit) would list `f` as added and refuse for nothing."""
    root, base, incoming = ranged
    (root / ".git" / "info" / "exclude").write_text("f\nnew.env\n")
    _git(root, "rm", "-q", "--cached", "f")
    _git(root, "commit", "-qm", "local stops tracking f")
    head = _git(root, "rev-parse", "HEAD")
    assert (root / "f").exists()
    two_endpoint = _git(root, "diff", "--name-only", "--diff-filter=A", head, incoming)
    assert "f" in two_endpoint.splitlines(), "control: the two-endpoint diff lists f"
    assert _collisions(root, head, incoming) == (0, [])
    (root / "new.env").write_text("LOCAL\n")
    assert _collisions(root, head, incoming) == (1, ["new.env"])


def test_an_incoming_modification_over_a_locally_detracked_file_collides(tmp_path):
    """The local branch deleted tracked `X` and keeps an ignored copy; upstream
    MODIFIES `X`. From the merge base that is `M`, not `A`, and git writes the
    incoming version over the local file in the modify/delete conflict (measured,
    git 2.43), so the scan must cover every change but a deletion."""
    root = tmp_path / "root"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    (root / "X").write_text("base\n")
    (root / "f").write_text("1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "incoming")
    (root / "X").write_text("upstream modification\n")
    _git(root, "commit", "-qam", "upstream modifies X")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")
    _git(root, "rm", "-q", "X")
    _git(root, "commit", "-qm", "local deletes X")
    (root / ".git" / "info" / "exclude").write_text("X\n")
    (root / "X").write_text("LOCAL-SECRET\n")
    head = _git(root, "rev-parse", "HEAD")
    add_only = _git(root, "diff", "--name-only", "--diff-filter=A", f"{head}...{incoming}")
    assert "X" not in add_only.splitlines(), "control: an additions-only scan misses X"
    assert _collisions(root, head, incoming) == (1, ["X"])


def test_an_incoming_modification_of_a_tracked_file_is_not_a_collision(ranged):
    """A path the local side still tracks is git's own to merge."""
    root, base, _ = ranged
    _git(root, "checkout", "-q", "-b", "touch-f")
    (root / "f").write_text("changed upstream\n")
    _git(root, "commit", "-qam", "modifies f")
    modifies_f = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")
    assert _collisions(root, base, modifies_f) == (0, [])


def test_an_unlistable_range_returns_2(ranged):
    root, base, _ = ranged
    assert _collisions(root, base, "0" * 40) == (2, [])


# ── genesis_checkout_unmoved ────────────────────────────────────────────────


def _unmoved(root: Path, head: str, branch: str) -> tuple[bool, str, str]:
    r = _bash(
        f'if genesis_checkout_unmoved "{root}" "{head}" "{branch}"; then v=SAME; else v=MOVED; fi\n'
        'echo "$v[$_now_head][$_now_branch]"'
    )
    assert r.returncode == 0, r.stderr
    m = re.search(r"(SAME|MOVED)\[(.*)\]\[(.*)\]", r.stdout)
    return m.group(1) == "SAME", m.group(2), m.group(3)


def test_unmoved_matches_only_the_exact_branch_and_commit(repos):
    root = repos["primary"]
    head = _git(root, "rev-parse", "HEAD")
    assert _unmoved(root, head, "main") == (True, head, "main")
    # Same commit, another branch: moved.
    _git(root, "checkout", "-q", "-b", "elsewhere")
    assert _unmoved(root, head, "main") == (False, head, "elsewhere")
    # Same commit, detached: moved, and the branch reads empty.
    _git(root, "checkout", "-q", "--detach", "HEAD")
    assert _unmoved(root, head, "main") == (False, head, "")
    # Back on main with a new commit: moved, and the new commit is reported.
    _git(root, "checkout", "-q", "main")
    _git(root, "commit", "-q", "--allow-empty", "-m", "theirs")
    new = _git(root, "rev-parse", "HEAD")
    assert _unmoved(root, head, "main") == (False, new, "main")


def test_unmoved_refuses_an_empty_expectation_or_an_unreadable_checkout(repos, tmp_path):
    root = repos["primary"]
    head = _git(root, "rev-parse", "HEAD")
    assert _unmoved(root, "", "main")[0] is False
    assert _unmoved(root, head, "")[0] is False
    assert _unmoved(tmp_path / "not-a-repo", head, "main") == (False, "", "")


# ── the two callers ─────────────────────────────────────────────────────────


def _code_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]


def test_both_deploy_paths_source_the_lib_and_keep_no_inline_copy():
    for script in (UPDATE, CODE_ONLY):
        text = script.read_text()
        assert re.search(r'^\. "\$\w+/lib/deploy_checkout\.sh"$', text, re.M), script.name
        code = "\n".join(_code_lines(script))
        for inline in ("--git-common-dir", "--absolute-git-dir", "--is-inside-work-tree"):
            assert inline not in code, f"{script.name} carries an inline {inline} check"
        for spelling in ("--diff-filter=A", "--diff-filter=d"):
            assert spelling not in code, f"{script.name} carries an inline collision scan"
        for call in (
            "genesis_checkout_git_dirs",
            "genesis_is_primary_checkout",
            "genesis_deploy_branch_ok",
            "genesis_tracked_dirty_paths",
            "genesis_range_collisions",
        ):
            assert call in code, f"{script.name} does not call {call}"


def test_only_update_sh_passes_the_override():
    assert 'genesis_deploy_branch_ok "$GENESIS_ROOT" --allow-override' in UPDATE.read_text()
    assert "--allow-override" not in "\n".join(_code_lines(CODE_ONLY))


def test_deploy_branch_is_defined_once_in_the_lib():
    scripts = [p for p in (REPO_ROOT / "scripts").rglob("*.sh") if p.is_file()]
    defining = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in scripts
        if re.search(r"^\s*DEPLOY_BRANCH=", p.read_text(errors="replace"), re.M)
    ]
    assert defining == ["scripts/lib/deploy_checkout.sh"], defining


def test_neither_script_names_a_branch_literal_in_its_git_commands():
    """The deploy branch is named through $DEPLOY_BRANCH in every git command, so
    widening what is deployable stays a change to the lib."""
    for script in (UPDATE, CODE_ONLY):
        offenders = [
            ln.strip()
            for ln in _code_lines(script)
            if re.search(r"\bgit\b", ln) and re.search(r"(?<![\w/.-])main\b", ln)
        ]
        assert offenders == [], (script.name, offenders)


def test_update_sh_refuses_before_touching_any_state():
    """The checkout checks come before the suppression-outcome clear, the lock,
    the rollback tag and the backup."""
    text = UPDATE.read_text()
    first = text.index('genesis_checkout_git_dirs "$GENESIS_ROOT"')
    last = text.index('genesis_deploy_branch_ok "$GENESIS_ROOT" --allow-override')
    for later in (
        'rm -f "$HOME/.genesis/cc_suppression_outcome"',
        'exec {_UPDATE_LOCK_FD}>"$UPDATE_LOCK_FILE"',
        'ROLLBACK_TAG="pre-update-',
        "--- Pre-update backup ---",
    ):
        assert last < text.index(later), later
    assert text.index('. "$SCRIPT_DIR/lib/deploy_checkout.sh"') < first


def _update_sh_dirty_block() -> str:
    """update.sh's REAL dirty-tree check, from the lib call to its refusal's `fi`."""
    text = UPDATE.read_text()
    start = text.index(
        '    if ! DIRTY_FILES="$(genesis_tracked_dirty_paths "$GENESIS_ROOT")"; then'
    )
    return text[start : text.index("\n    fi\n", start) + 8]


def test_update_sh_refuses_an_unreadable_status(tmp_path):
    r = _bash(f'GENESIS_ROOT="{tmp_path / "not-a-repo"}"\n{_update_sh_dirty_block()}echo PASSED')
    assert r.returncode == 1, r.stdout
    assert "cannot read the working tree's status" in r.stdout
    assert "PASSED" not in r.stdout


def test_update_sh_passes_a_readable_clean_status(repos):
    """Control for the test above: the same block on a clean checkout falls through."""
    r = _bash(f'GENESIS_ROOT="{repos["primary"]}"\n{_update_sh_dirty_block()}echo PASSED')
    assert r.returncode == 0 and "PASSED" in r.stdout, r.stdout + r.stderr


def test_update_sh_scans_for_collisions_before_the_stop():
    text = UPDATE.read_text()
    scan = text.index('genesis_range_collisions "$GENESIS_ROOT" HEAD "$DEPLOY_HEAD"')
    assert (
        text.index('DEPLOY_HEAD=""\n') < scan < text.index("--- Stopping services for update ---")
    )


def test_lib_functions_return_and_never_exit():
    """update.sh's EXIT trap was hardened against an exit from inside a sourced
    lib function; the lib must leave exiting to its callers, and print no
    refusal of its own."""
    code = "\n".join(_code_lines(LIB))
    assert not re.search(r"\bexit\b", code), "deploy_checkout.sh must return, never exit"
    assert not re.search(r"\becho\b", code.replace("echo /nonexistent", "")), code
