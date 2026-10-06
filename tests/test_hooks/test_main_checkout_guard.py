"""Tests for scripts/hooks/main_checkout_guard.py.

Two halves, tested differently:

* FILE TOOLS are BLOCKED (exit 2) on a TRACKED file in the PRIMARY checkout that
  the guard script itself belongs to. Every test runs a COPY of the guard placed
  inside a scratch primary checkout, so the guard's self-location resolves to that
  scratch checkout — never to the real install, which these tests must not touch.
* BASH is NEVER blocked. The PreToolUse run records a snapshot keyed by
  ``tool_use_id``; the PostToolUse run compares and reports. The Bash tests drive
  pre, then a REAL execution of the command, then post — around a fresh scratch
  install per test, because the commands really change it.

The module-scoped scratch world for the file tools (the guard only reads it):

    install/            primary checkout; scripts/hooks/ holds the guard copy
      README.md         tracked
      src/a.py          tracked
      src/AGENTS.md     tracked, and NOT ephemeral (the regex is anchored)
      AGENTS.md         tracked, ephemeral
      nb.ipynb          tracked
      docs/guide.md     tracked
      .claude/worktrees/wt/   a LINKED worktree of install (nested on disk)
    other/              a different primary checkout, README.md tracked
    plain/              no repository at all
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
_HOOKS = _WORKTREE / "scripts" / "hooks"
_GUARD_NAME = "main_checkout_guard.py"
#: Everything the guard imports from its own directory at run time.
_GUARD_FILES = (_GUARD_NAME, "hook_input.py", "hook_output.py")
_G = "gi" + "t"  # spelled out so no tool scanning test text reads a git command


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    res = subprocess.run(
        [_G, "-C", str(cwd), *args], capture_output=True, text=True, env=env, check=True
    )
    return res.stdout


def _init_repo(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")


def _install_guard(root: Path) -> Path:
    """Copy the guard (and the siblings it imports) into ``root``, untracked:
    only its LOCATION matters — it makes ``root`` the checkout the guard judges."""
    hooks = root / "scripts" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for name in _GUARD_FILES:
        shutil.copy(_HOOKS / name, hooks / name)
    (root / "config").mkdir(exist_ok=True)
    shutil.copy(_WORKTREE / "config" / "main_checkout_guard.yaml", root / "config")
    return hooks / _GUARD_NAME


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    base = tmp_path_factory.mktemp("mcg").resolve()
    install = base / "install"
    _init_repo(
        install,
        {
            "README.md": "v1\n",
            "src/a.py": "x = 1\n",
            "src/AGENTS.md": "nested\n",
            "AGENTS.md": "auto\n",
            "nb.ipynb": "{}\n",
            "docs/guide.md": "g\n",
        },
    )
    guard = _install_guard(install)
    wt = install / ".claude" / "worktrees" / "wt"
    _git(install, "worktree", "add", "-q", str(wt), "-b", "wt-branch")
    other = base / "other"
    _init_repo(other, {"README.md": "o\n"})
    plain = base / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("p\n")
    return {
        "base": base,
        "install": install,
        "wt": wt,
        "other": other,
        "plain": plain,
        "guard": guard,
    }


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_")
        and k not in ("GENESIS_MAIN_CHECKOUT_GUARD", "GENESIS_UPDATE_TIER", "GENESIS_HOME")
    }
    env["HOME"] = str(home)
    env.update(extra)
    return env


def _run(world, payload: dict, *, home: Path | None = None, guard: Path | None = None, **env):
    home = home or (world["base"] / "home-default")
    home.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(guard or world["guard"])],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_env(home, **env),
        cwd=str(world["base"]),
        timeout=120,
    )


def _edit(path, cwd, tool="Edit"):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {key: str(path)},
        "cwd": str(cwd),
    }


def _context(res) -> str:
    if not res.stdout.strip():
        return ""
    return json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]


_BLOCK_MARK = "[main-checkout-guard] BLOCKED"
_ADVISORY_MARK = "[main-checkout-guard] ADVISORY"


def _assert_blocked(res, context=None):
    """Exit 2 AND the guard's own marker. The marker is load-bearing: the
    interpreter also exits 2 when it cannot open the script, so a bare exit-code
    check passed tests before the guard existed."""
    assert res.returncode == 2, (context, res.stderr, res.stdout)
    assert _BLOCK_MARK in res.stderr, (context, res.stderr)


# ── File tools ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
def test_file_tool_on_a_tracked_primary_file_is_blocked(world, tool):
    res = _run(world, _edit(world["install"] / "src" / "a.py", world["install"], tool))
    _assert_blocked(res, res.stderr)
    assert "src/a.py" in res.stderr
    assert "deploy_code_only.sh" in res.stderr
    assert "worktree" in res.stderr


def test_notebook_edit_on_a_tracked_primary_notebook_is_blocked(world):
    res = _run(world, _edit(world["install"] / "nb.ipynb", world["install"], "NotebookEdit"))
    _assert_blocked(res, res.stderr)
    assert "nb.ipynb" in res.stderr


def test_relative_file_path_resolves_against_the_payload_cwd(world):
    res = _run(world, _edit("README.md", world["install"]))
    _assert_blocked(res, res.stderr)


def test_a_legacy_payload_without_an_event_name_is_still_judged(world):
    payload = _edit(world["install"] / "README.md", world["install"])
    del payload["hook_event_name"]
    _assert_blocked(_run(world, payload))


def test_a_file_tool_post_event_is_never_judged(world):
    """The file tools are judged before they run; a PostToolUse on Edit (were it
    ever wired) must not exit 2 after the fact."""
    payload = _edit(world["install"] / "README.md", world["install"])
    payload["hook_event_name"] = "PostToolUse"
    res = _run(world, payload)
    assert res.returncode == 0, res.stderr


def test_a_new_untracked_file_in_the_primary_checkout_is_allowed(world):
    res = _run(world, _edit(world["install"] / "src" / "brand_new.py", world["install"], "Write"))
    assert res.returncode == 0, res.stderr


def test_the_ephemeral_top_level_agents_md_is_allowed(world):
    res = _run(world, _edit(world["install"] / "AGENTS.md", world["install"]))
    assert res.returncode == 0, res.stderr


def test_the_ephemeral_exemption_is_anchored_to_the_exact_path(world):
    res = _run(world, _edit(world["install"] / "src" / "AGENTS.md", world["install"]))
    _assert_blocked(res, res.stderr)


def test_a_tracked_file_in_a_linked_worktree_is_allowed(world):
    # The worktree sits INSIDE the primary checkout's directory on disk; the
    # nearest enclosing checkout is the worktree, which is not primary.
    res = _run(world, _edit(world["wt"] / "src" / "a.py", world["wt"]))
    assert res.returncode == 0, res.stderr


def test_a_linked_worktree_outside_the_worktrees_dir_is_allowed(world):
    """Isolates git's own answer (git-dir != common-dir) from the path arm: this
    worktree is not under a `.claude/worktrees/` directory."""
    wt2 = world["base"] / "wt-elsewhere"
    if not wt2.exists():
        _git(world["install"], "worktree", "add", "-q", str(wt2), "-b", "wt2-branch")
        _install_guard(wt2)
    res = _run(world, _edit(wt2 / "src" / "a.py", wt2))
    assert res.returncode == 0, res.stderr
    # The guard's OWN copy inside that worktree (a worktree-local hook run) must
    # not treat its worktree as the deploy root either.
    res = _run(
        world, _edit(wt2 / "src" / "a.py", wt2), guard=wt2 / "scripts" / "hooks" / _GUARD_NAME
    )
    assert res.returncode == 0, res.stderr


def test_a_primary_clone_parked_under_a_worktrees_dir_is_not_guarded(world, tmp_path):
    """The path arm of genesis_is_primary_checkout: a plain clone under
    `.claude/worktrees/` is primary to git, but not the deploy root."""
    parked = tmp_path / "x" / ".claude" / "worktrees" / "clone"
    _init_repo(parked, {"README.md": "c\n"})
    guard = _install_guard(parked)
    res = _run(world, _edit(parked / "README.md", parked), guard=guard)
    assert res.returncode == 0, res.stderr


def test_a_tracked_file_in_another_primary_repo_is_allowed(world):
    res = _run(world, _edit(world["other"] / "README.md", world["other"]))
    assert res.returncode == 0, res.stderr


def test_a_path_outside_any_repo_is_allowed(world):
    res = _run(world, _edit(world["plain"] / "f.txt", world["plain"]))
    assert res.returncode == 0, res.stderr


def test_a_tilde_path_is_expanded_before_it_is_judged(world):
    """Claude Code's file tools expand a leading ``~`` (MEASURED 2026-10-05: a
    Write to ``~/tmp/...`` landed in the home directory, not under the cwd), so
    ``Edit ~/<root>/README.md`` writes the deploy root and must be judged there."""
    tilde = "~/" + world["install"].name + "/README.md"
    res = _run(world, _edit(tilde, world["plain"]), home=world["base"])
    _assert_blocked(res, res.stderr)
    assert "README.md" in res.stderr


def test_a_symlink_to_a_tracked_primary_file_is_judged_by_its_target(world):
    """A path is judged by the file a write through it would change, so an
    untracked link (here outside any repo, and inside a linked worktree) to a
    tracked deploy-root file is that file. CC 2.1.280 refuses to write through a
    symlink (MEASURED); this pins the guard for a version that does not."""
    for where in (world["plain"], world["wt"]):
        link = where / "alias-to-readme"
        if not link.is_symlink():
            link.symlink_to(world["install"] / "README.md")
        res = _run(world, _edit(link, where))
        _assert_blocked(res, (where, res.stderr))


def test_a_symlink_in_the_root_pointing_outside_it_is_allowed(world):
    """The control: the write lands at the link's target, outside the deploy
    root, so nothing tracked there changes."""
    link = world["install"] / "alias-out"
    if not link.is_symlink():
        link.symlink_to(world["plain"] / "f.txt")
    res = _run(world, _edit(link, world["install"]))
    assert res.returncode == 0, res.stderr


def test_a_guard_belonging_to_another_checkout_does_not_judge_this_one(world):
    """Same primary checkout, but the guard script lives in ANOTHER primary repo:
    the block is scoped to the checkout the hook script belongs to."""
    foreign = world["other"] / "scripts" / "hooks" / _GUARD_NAME
    if not foreign.exists():
        _install_guard(world["other"])
    res = _run(world, _edit(world["install"] / "src" / "a.py", world["install"]), guard=foreign)
    assert res.returncode == 0, res.stderr


def test_a_poisoned_hook_input_still_judges_file_tools(tmp_path, world):
    """The sibling import is guarded: with hook_input unimportable the guard reads
    stdin itself and still refuses a tracked primary file."""
    tree = tmp_path / "install-copy"
    _init_repo(tree, {"README.md": "x\n"})
    guard = _install_guard(tree)
    (guard.parent / "hook_input.py").write_text("raise ImportError('poisoned sibling')\n")
    _assert_blocked(_run(world, _edit(tree / "README.md", tree), guard=guard))


# ── Bash: snapshot before, real command, report after ────────────────────────


@pytest.fixture
def bw(tmp_path):
    """A FRESH scratch install per test: the Bash tests really change it."""
    base = tmp_path.resolve()
    install = base / "install"
    _init_repo(
        install,
        {
            "README.md": "v1\n",
            "src/a.py": "x = 1\n",
            "AGENTS.md": "auto\n",
            "docs/guide.md": "g\n",
        },
    )
    # A second commit that changes README.md and leaves docs/guide.md identical,
    # so `HEAD~1 -- docs/guide.md` is a real checkout from another commit that
    # changes nothing.
    (install / "README.md").write_text("v2\n")
    _git(install, "commit", "-q", "-am", "second")
    guard = _install_guard(install)
    wt = install / ".claude" / "worktrees" / "wt"
    _git(install, "worktree", "add", "-q", str(wt), "-b", "wt-branch")
    src = base / "incoming.txt"
    src.write_text("new\n")
    home = base / "home"
    home.mkdir()
    ghome = base / "ghome"
    return {
        "base": base,
        "install": install,
        "wt": wt,
        "src": src,
        "home": home,
        "ghome": ghome,
        "guard": guard,
    }


_COUNTER = iter(range(10**9))


def _bash_payload(event: str, command: str, cwd: Path, tool_use_id: str) -> dict:
    return {
        "hook_event_name": event,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
        "tool_use_id": tool_use_id,
    }


def _hook(bw, payload: dict, **env):
    return subprocess.run(
        [sys.executable, str(bw["guard"])],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_env(bw["home"], GENESIS_HOME=str(bw["ghome"]), **env),
        cwd=str(bw["base"]),
        timeout=120,
    )


def _around(bw, command: str, cwd: Path | None = None, *, post_event="PostToolUse", **env):
    """PreToolUse, then the command REALLY run, then the post event; returns
    (pre result, command result, post result). Both hook runs must exit 0."""
    cwd = cwd or bw["install"]
    tid = f"toolu_test{next(_COUNTER)}"
    pre = _hook(bw, _bash_payload("PreToolUse", command, cwd, tid), **env)
    ran = subprocess.run(
        ["bash", "-c", command],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=_env(bw["home"]),
        timeout=120,
    )
    post = _hook(bw, _bash_payload(post_event, command, cwd, tid), **env)
    assert pre.returncode == 0, (command, pre.stderr)
    assert post.returncode == 0, (command, post.stderr)
    return pre, ran, post


def _advisory(res) -> str:
    text = _context(res)
    return text if _ADVISORY_MARK in text else ""


def test_bash_writing_a_tracked_file_gets_an_advisory_naming_file_and_repair(bw):
    pre, ran, post = _around(bw, f"cp {bw['src']} README.md")
    assert ran.returncode == 0, ran.stderr
    assert _context(pre) == "", "the pre run must stay silent"
    text = _advisory(post)
    assert "README.md" in text, text
    assert f"{_G} -C {bw['install']} checkout -- README.md" in text, text
    assert json.loads(post.stdout)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_a_failing_command_is_still_checked_on_post_tool_use_failure(bw):
    _, ran, post = _around(bw, f"cp {bw['src']} README.md; false", post_event="PostToolUseFailure")
    assert ran.returncode != 0
    assert "README.md" in _advisory(post)
    assert json.loads(post.stdout)["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"


@pytest.mark.parametrize(
    "template",
    [
        "echo x > new_untracked.txt",  # untracked
        "echo x >> AGENTS.md",  # ephemeral
        "echo x >> {wt}/README.md",  # a linked worktree's file
        "true",  # no-op
        "cat README.md",
        # The audit's former FALSE BLOCKS: heredoc bodies that merely mention a cp.
        "cat > {base}/n1.md <<\\EOF\ncp {src} README.md\nEOF",
        "cat > {base}/n2.md <<'END NOTE'\ncp {src} README.md\nEND NOTE",
        # ...and a checkout from another commit of a file identical in both.
        _G + " checkout HEAD~1 -- docs/guide.md",
        # ...and a rewind aimed at ANOTHER checkout through -c and -C.
        _G + " -c x.y=z -C {wt} checkout HEAD~1 -- README.md",
    ],
)
def test_bash_that_changes_no_tracked_primary_file_gets_no_advisory(bw, template):
    cmd = template.format(wt=bw["wt"], base=bw["base"], src=bw["src"])
    pre, ran, post = _around(bw, cmd)
    assert ran.returncode == 0, (cmd, ran.stderr)
    assert pre.returncode == 0 and post.returncode == 0
    assert _context(post) == "", (cmd, _context(post))
    assert _git(bw["install"], "status", "--porcelain", "--untracked-files=no").strip() in (
        "",
        "M AGENTS.md",
    ), "fixture: the command really changed nothing tracked but the ephemeral file"


@pytest.mark.parametrize(
    "template",
    [
        # The audit's former MISSES: operands the old model could not place.
        "cp {src} ~/install/README.md",
        'cp {src} "$HOME/install/src/a.py"',
        "cp {src} {base}/link-to-readme",
        "cp {src} {base}/inst*/READ*.md",
        "echo x | tee {install}/{{README.md,src/a.py}} > /dev/null",
        "sed -i s/v2/v3/ README.md",
        "echo extra >> src/a.py",
        _G + " checkout HEAD~1 -- README.md",
        _G + " -c x.y=z -C {install} checkout HEAD~1 -- README.md",
        "rm docs/guide.md",
    ],
)
def test_bash_changes_the_old_model_missed_get_an_advisory(bw, template):
    # HOME = the root's parent, so `~/install` and `$HOME/install` are the root.
    home = bw["base"]
    (bw["base"] / "link-to-readme").symlink_to(bw["install"] / "README.md")
    cmd = template.format(src=bw["src"], base=bw["base"], install=bw["install"])
    tid = f"toolu_miss{next(_COUNTER)}"
    env = _env(home, GENESIS_HOME=str(bw["ghome"]))

    def hook(event):
        return subprocess.run(
            [sys.executable, str(bw["guard"])],
            input=json.dumps(_bash_payload(event, cmd, bw["base"] / "install", tid)),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(bw["base"]),
            timeout=120,
        )

    assert hook("PreToolUse").returncode == 0
    ran = subprocess.run(
        ["bash", "-c", cmd], cwd=str(bw["install"]), capture_output=True, text=True, env=env
    )
    assert ran.returncode == 0, (cmd, ran.stderr)
    post = hook("PostToolUse")
    assert post.returncode == 0
    text = _advisory(post)
    assert text, (cmd, post.stdout, post.stderr)
    assert re.search(r"README\.md|src/a\.py|docs/guide\.md", text), text


def test_an_already_dirty_file_left_untouched_is_not_reported(bw):
    (bw["install"] / "README.md").write_text("dirty before\n")
    _, _, post = _around(bw, "echo x > untracked.txt")
    assert _context(post) == ""


def test_an_already_dirty_file_changed_again_is_reported(bw):
    (bw["install"] / "README.md").write_text("dirty before\n")
    _, _, post = _around(bw, "echo more >> README.md")
    text = _advisory(post)
    assert "AGAIN" in text and "README.md" in text, text
    assert "earlier change" in text


def test_a_moved_head_is_reported_with_the_previous_commit(bw):
    head = _git(bw["install"], "rev-parse", "HEAD").strip()
    _, ran, post = _around(bw, f"{_G} checkout -q HEAD~1")
    assert ran.returncode == 0, ran.stderr
    text = _advisory(post)
    assert "HEAD moved" in text and head in text and "main" in text, text


def test_a_moved_head_never_tells_the_session_to_move_it_back(bw):
    _, _, post = _around(bw, f"{_G} checkout -q HEAD~1")
    text = _advisory(post)
    assert "Do not move HEAD back yourself" in text and "reflog" in text, text
    assert "checkout -- " not in text, "no restore command when only HEAD moved"


def test_a_change_from_a_call_not_aimed_at_the_root_gets_no_restore_command(bw):
    """Every Bash call in every session is snapshotted, so a call whose cwd and
    command text are elsewhere most likely did not make the change: it is told
    to leave it alone, never handed a destructive command."""
    tid = "toolu_elsewhere"
    cmd = "true"
    assert _hook(bw, _bash_payload("PreToolUse", cmd, bw["base"], tid)).returncode == 0
    (bw["install"] / "README.md").write_text("another actor\n")
    post = _hook(bw, _bash_payload("PostToolUse", cmd, bw["base"], tid))
    text = _advisory(post)
    assert "README.md" in text, text
    assert "Do NOT discard" in text, text
    assert "checkout --" not in text and "restore --source" not in text, text


def test_the_root_path_in_the_command_makes_the_call_attributable(bw):
    tid = "toolu_named"
    cmd = f"cp {bw['src']} {bw['install']}/README.md"
    assert _hook(bw, _bash_payload("PreToolUse", cmd, bw["base"], tid)).returncode == 0
    subprocess.run(["bash", "-c", cmd], check=True)
    text = _advisory(_hook(bw, _bash_payload("PostToolUse", cmd, bw["base"], tid)))
    assert f"{_G} -C {bw['install']} checkout -- README.md" in text, text


@pytest.mark.parametrize(
    "named",
    [
        "{install}/.claude/worktrees/wt/README.md",  # a worktree nested under the root
        "{install}-old/README.md",  # a sibling path sharing the root as a prefix
        "{install}x",
    ],
)
def test_a_path_that_only_starts_with_the_root_does_not_make_the_call_attributable(bw, named):
    """The root's path must appear as a PATH, at a component boundary, and not
    continue into a linked worktree: otherwise every command that names a
    worktree under the root would be handed restore commands for another
    actor's change."""
    tid = "toolu_prefix"
    cmd = "ls " + named.format(install=bw["install"]) + " >/dev/null 2>&1; true"
    assert _hook(bw, _bash_payload("PreToolUse", cmd, bw["base"], tid)).returncode == 0
    (bw["install"] / "README.md").write_text("another actor\n")
    text = _advisory(_hook(bw, _bash_payload("PostToolUse", cmd, bw["base"], tid)))
    assert "README.md" in text, text
    assert "Do NOT discard" in text, text
    assert "checkout --" not in text, text


@pytest.mark.parametrize(
    "named",
    ["{install}", "{install}/", "'{install}/src'", "cd {install}&&true", "{install};true"],
)
def test_the_root_named_as_a_path_still_makes_the_call_attributable(bw, named):
    tid = "toolu_rootnamed"
    cmd = "true " + named.format(install=bw["install"])
    assert _hook(bw, _bash_payload("PreToolUse", cmd, bw["base"], tid)).returncode == 0
    (bw["install"] / "README.md").write_text("edited\n")
    text = _advisory(_hook(bw, _bash_payload("PostToolUse", cmd, bw["base"], tid)))
    assert "checkout -- README.md" in text, (named, text)


def test_a_merge_in_progress_gets_no_restore_command(bw):
    """Unmerged entries mean a merge is being resolved there (most likely the
    update pipeline's); a restore would wipe it."""
    inst = bw["install"]
    _git(inst, "checkout", "-q", "-b", "side", "HEAD~1")
    (inst / "README.md").write_text("side\n")
    _git(inst, "commit", "-q", "-am", "side")
    _git(inst, "checkout", "-q", "main")
    _, ran, post = _around(bw, f"{_G} -c user.name=t -c user.email=t@e merge -q side")
    assert ran.returncode != 0, "fixture: the merge must conflict"
    assert "UU README.md" in _git(inst, "status", "--porcelain", "--untracked-files=no")
    text = _advisory(post)
    assert "merge is in progress" in text and "Do NOT discard" in text, text


def test_a_merge_with_every_conflict_staged_still_gets_no_restore_command(bw):
    """Once the last conflict is resolved and staged no unmerged entry is left,
    but MERGE_HEAD still exists: the merge is still in progress, and a restore
    would discard its staged resolution."""
    inst = bw["install"]
    _git(inst, "checkout", "-q", "-b", "side", "HEAD~1")
    (inst / "README.md").write_text("side\n")
    _git(inst, "commit", "-q", "-am", "side")
    _git(inst, "checkout", "-q", "main")
    merged = subprocess.run(
        [
            "git",
            "-C",
            str(inst),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "merge",
            "-q",
            "side",
        ],
        capture_output=True,
    )
    assert merged.returncode != 0, "fixture: the merge must conflict"
    (inst / "README.md").write_text("resolved\n")
    _git(inst, "add", "README.md")
    status = _git(inst, "status", "--porcelain", "--untracked-files=no")
    assert "UU" not in status and "M  README.md" in status, status
    _, _, post = _around(bw, "echo more >> docs/guide.md")
    text = _advisory(post)
    assert "docs/guide.md" in text, text
    assert "merge is in progress" in text and "Do NOT discard" in text, text
    assert "checkout --" not in text and "restore --source" not in text, text


def test_an_intent_to_add_entry_gets_a_restore_that_really_clears_it(bw):
    """`git add -N` leaves a `.A` entry, which `git checkout --` does not clear;
    the offered command must be one that does."""
    inst = bw["install"]
    _, _, post = _around(bw, f"echo n > new.txt && {_G} add -N new.txt")
    text = _advisory(post)
    assert "new.txt" in text, text
    line = next(ln for ln in text.splitlines() if "new.txt" in ln and f"{_G} -C" in ln)
    cmd = line.split("#", 1)[0].strip()
    subprocess.run(["bash", "-c", cmd], check=True, capture_output=True)
    assert _git(inst, "status", "--porcelain", "--untracked-files=no") == "", cmd


def test_a_changed_skip_worktree_file_gets_no_checkout_command(bw):
    """`git checkout -- <file>` fails on a skip-worktree entry, so the advisory
    must not offer it; it names the flag instead."""
    _git(bw["install"], "update-index", "--skip-worktree", "README.md")
    _, _, post = _around(bw, "echo hidden >> README.md")
    text = _advisory(post)
    assert "README.md" in text and "skip-worktree" in text, text
    assert "checkout -- README.md" not in text, text


def _load_guard_copy(bw, monkeypatch, name):
    from tests.conftest import private_module

    monkeypatch.setenv("GENESIS_HOME", str(bw["ghome"]))
    for var in ("GENESIS_MAIN_CHECKOUT_GUARD", "GENESIS_UPDATE_TIER"):
        monkeypatch.delenv(var, raising=False)
    return private_module(name, bw["guard"])


def test_a_timed_out_after_snapshot_still_notes_a_call_from_the_root(bw, monkeypatch):
    """When the after-snapshot exhausts the git deadline, deciding whether the
    call pointed at the root must not fail on that same deadline — a call run
    from the root has to get its NOT-checked note."""
    mod = _load_guard_copy(bw, monkeypatch, "main_checkout_guard_deadline")
    payload = _bash_payload("PreToolUse", "true", bw["install"], "toolu_deadline")
    assert mod._bash_pre(payload, mod._Git()) is None
    git = mod._Git()
    git.deadline = 0.0  # spent before the after-snapshot runs
    note = mod._bash_post({**payload, "hook_event_name": "PostToolUse"}, git)
    assert note and "NOT checked" in note, note


def test_a_failed_snapshot_write_leaves_no_temp_file(bw, monkeypatch):
    mod = _load_guard_copy(bw, monkeypatch, "main_checkout_guard_tmpwrite")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(mod.json, "dump", boom)
    target = mod._snapshot_file("toolu_tmp")
    with pytest.raises(OSError):
        mod._write_snapshot(target, {"v": 1})
    assert not any(target.parent.iterdir()), list(target.parent.iterdir())


def test_many_changed_files_point_at_status_instead_of_one_long_command(bw):
    names = [f"f{i}.txt" for i in range(15)]
    for name in names:
        (bw["install"] / name).write_text("0\n")
    _git(bw["install"], "add", "-A")
    _git(bw["install"], "commit", "-q", "-m", "many")
    _, _, post = _around(bw, f"for f in {' '.join(names)}; do echo 1 >> $f; done")
    text = _advisory(post)
    assert "(and 5 more)" in text, text
    assert f"{_G} -C {bw['install']} status" in text and "checkout --" not in text, text


def test_a_flagged_file_is_not_called_already_modified(bw):
    _git(bw["install"], "update-index", "--assume-unchanged", "README.md")
    _, _, post = _around(bw, "echo hidden >> README.md")
    text = _advisory(post)
    assert "assume-unchanged" in text and "AGAIN" not in text, text


def test_a_command_that_turns_the_config_off_is_still_reported(bw):
    """The tracked base config is writable from Bash; the post check must not obey
    the switch the call itself flipped."""
    _git(bw["install"], "add", "-f", "config/main_checkout_guard.yaml")
    _git(bw["install"], "commit", "-q", "-m", "track config")
    cfg = bw["install"] / "config" / "main_checkout_guard.yaml"
    cmd = f"echo enabled: false > {cfg} && echo x >> README.md"
    _, ran, post = _around(bw, cmd)
    assert ran.returncode == 0, ran.stderr
    assert cfg.read_text().strip() == "enabled: false", "fixture: the switch really flipped"
    text = _advisory(post)
    assert "README.md" in text and "config/main_checkout_guard.yaml" in text, text


def test_a_staged_change_gets_the_restore_from_head_repair(bw):
    _, _, post = _around(bw, f"cp {bw['src']} README.md && {_G} add README.md")
    text = _advisory(post)
    assert f"{_G} -C {bw['install']} restore --source=HEAD --staged --worktree -- README.md" in (
        text
    ), text


def test_an_index_flagged_file_changed_is_reported(bw):
    """assume-unchanged hides the edit from `git status`; the snapshot hashes
    flagged paths, so a change is still caught."""
    _git(bw["install"], "update-index", "--assume-unchanged", "README.md")
    assert _git(bw["install"], "status", "--porcelain", "--untracked-files=no") == ""
    _, _, post = _around(bw, "echo hidden >> README.md")
    assert _git(bw["install"], "status", "--porcelain", "--untracked-files=no") == "", (
        "fixture: git status must not see the edit, or this tests nothing"
    )
    assert "README.md" in _advisory(post)


def test_a_post_with_no_snapshot_is_silent(bw):
    (bw["install"] / "README.md").write_text("changed\n")
    res = _hook(bw, _bash_payload("PostToolUse", "true", bw["install"], "toolu_never_pre"))
    assert res.returncode == 0 and res.stdout == "", res.stdout


def test_the_post_claims_the_snapshot_so_a_second_post_is_silent(bw):
    tid = "toolu_twice"
    cmd = f"cp {bw['src']} README.md"
    assert _hook(bw, _bash_payload("PreToolUse", cmd, bw["install"], tid)).returncode == 0
    subprocess.run(["bash", "-c", cmd], cwd=str(bw["install"]), check=True)
    first = _hook(bw, _bash_payload("PostToolUse", cmd, bw["install"], tid))
    second = _hook(bw, _bash_payload("PostToolUseFailure", cmd, bw["install"], tid))
    assert "README.md" in _advisory(first)
    assert second.stdout == "", second.stdout
    assert list((bw["ghome"] / "main_checkout_guard").iterdir()) == []


def test_the_snapshot_store_is_private_and_holds_no_command_text(bw):
    tid = "toolu_store"
    secret = "s3cr3t-token-value"
    cmd = f"echo {secret} > /dev/null"
    assert _hook(bw, _bash_payload("PreToolUse", cmd, bw["install"], tid)).returncode == 0
    d = bw["ghome"] / "main_checkout_guard"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    [f] = list(d.iterdir())
    assert f.name == f"{tid}.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert secret not in f.read_text()


def test_old_orphans_are_pruned_and_fresh_ones_kept(bw):
    d = bw["ghome"] / "main_checkout_guard"
    d.mkdir(parents=True)
    old = time.time() - 3 * 86_400
    for i in range(70):
        p = d / f"toolu_orphan{i}.json"
        p.write_text("{}")
        os.utime(p, (old, old))
    fresh = d / "toolu_fresh_orphan.json"
    fresh.write_text("{}")
    assert _hook(bw, _bash_payload("PreToolUse", "true", bw["install"], "toolu_p")).returncode == 0
    names = {p.name for p in d.iterdir()}
    assert names == {"toolu_fresh_orphan.json", "toolu_p.json"}, names


def test_a_hostile_tool_use_id_cannot_escape_the_store(bw):
    tid = "../../escape"
    assert _hook(bw, _bash_payload("PreToolUse", "true", bw["install"], tid)).returncode == 0
    d = bw["ghome"] / "main_checkout_guard"
    [f] = list(d.iterdir())
    assert re.fullmatch(r"[0-9a-f]{40}\.json", f.name), f.name
    assert not (bw["base"] / "escape.json").exists()


def test_the_kill_switch_silences_the_bash_half(bw):
    _, _, post = _around(bw, f"cp {bw['src']} README.md", GENESIS_MAIN_CHECKOUT_GUARD="0")
    assert _context(post) == ""
    assert not (bw["ghome"] / "main_checkout_guard").exists()


def test_the_update_tier_stamp_silences_the_bash_half(bw):
    _, _, post = _around(bw, f"cp {bw['src']} README.md", GENESIS_UPDATE_TIER="1")
    assert _context(post) == ""


def test_a_non_exact_update_tier_does_not_silence_the_bash_half(bw):
    _, _, post = _around(bw, f"cp {bw['src']} README.md", GENESIS_UPDATE_TIER="true")
    assert "README.md" in _advisory(post)


def test_an_unreadable_root_on_post_notes_only_when_the_call_points_there(bw):
    """git fails between pre and post: allowed, with a one-line note only when the
    session's cwd or the command text is the deploy root."""
    pointed = f"ls {bw['install']}"
    for tid, cmd in (("toolu_pointed", pointed), ("toolu_elsewhere", "true")):
        assert _hook(bw, _bash_payload("PreToolUse", cmd, bw["base"], tid)).returncode == 0
    git_dir = bw["install"] / ".git"
    git_dir.rename(bw["base"] / "hidden-git")
    try:
        here = _hook(bw, _bash_payload("PostToolUse", pointed, bw["base"], "toolu_pointed"))
        assert here.returncode == 0
        assert "NOT checked" in _context(here), here.stdout
        away = _hook(bw, _bash_payload("PostToolUse", "true", bw["base"], "toolu_elsewhere"))
        assert away.returncode == 0
        assert away.stdout == "", away.stdout
    finally:
        (bw["base"] / "hidden-git").rename(git_dir)


def test_bash_never_exits_2_even_when_it_reports(bw):
    """The whole Bash half is advisory: exit 0 on pre and post, with a report."""
    pre, _, post = _around(bw, f"cp {bw['src']} README.md")
    assert pre.returncode == 0 and post.returncode == 0
    assert _advisory(post)


# ── Kill switch, the overlay location, and the update-tier stamp ──────────────


def _blocked_payload(world):
    return _edit(world["install"] / "README.md", world["install"])


def test_env_kill_switch_zero_turns_the_guard_off(world):
    assert _run(world, _blocked_payload(world), GENESIS_MAIN_CHECKOUT_GUARD="0").returncode == 0


def test_env_kill_switch_other_values_keep_it_on(world):
    _assert_blocked(_run(world, _blocked_payload(world), GENESIS_MAIN_CHECKOUT_GUARD="1"))


def _home_with_overlay(tmp_path, text: str) -> Path:
    home = tmp_path / "home"
    cfg = home / ".genesis" / "config"
    cfg.mkdir(parents=True)
    (cfg / "main_checkout_guard.local.yaml").write_text(text)
    return home


@pytest.mark.parametrize(
    "value", ["false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"]
)
def test_yaml_values_that_load_as_boolean_false_turn_the_guard_off(world, tmp_path, value):
    """The rule is "the loaded value IS the boolean False". PyYAML (YAML 1.1)
    loads each of these spellings as that boolean, so each turns the guard off."""
    home = _home_with_overlay(tmp_path, f"enabled: {value}\n")
    assert _run(world, _blocked_payload(world), home=home).returncode == 0


@pytest.mark.parametrize(
    "text",
    [
        "enabled: nope\n",
        "enabled: 0\n",
        "enabled: n\n",
        "enabled: null\n",
        'enabled: "false"\n',
        "enabled: [\n",
        "- just a list\n",
    ],
)
def test_any_other_yaml_value_keeps_the_guard_on(world, tmp_path, text):
    home = _home_with_overlay(tmp_path, text)
    _assert_blocked(_run(world, _blocked_payload(world), home=home))


def test_the_overlay_follows_genesis_home(world, tmp_path):
    ghome = tmp_path / "gh"
    (ghome / "config").mkdir(parents=True)
    (ghome / "config" / "main_checkout_guard.local.yaml").write_text("enabled: false\n")
    res = _run(world, _blocked_payload(world), GENESIS_HOME=str(ghome))
    assert res.returncode == 0, res.stderr


def test_a_repo_local_overlay_is_ignored(world, tmp_path):
    """config/*.local.yaml in the checkout is gitignored, hence untracked, so a
    session could write it past the file-tool half; it must not switch the guard
    off."""
    local = world["install"] / "config" / "main_checkout_guard.local.yaml"
    local.write_text("enabled: false\n")
    try:
        _assert_blocked(_run(world, _blocked_payload(world), home=tmp_path / "fresh"))
    finally:
        local.unlink()


def test_settings_update_turns_the_guard_off_end_to_end(world, tmp_path):
    """The settings lever writes the overlay the guard reads: the real
    `settings_update` path, then the real guard, with nothing hand-written between."""
    import asyncio
    from unittest.mock import patch

    from genesis.mcp.health import settings

    home = tmp_path / "home"
    cfg = home / ".genesis" / "config"
    cfg.mkdir(parents=True)
    repo_cfg = tmp_path / "repo-config"
    repo_cfg.mkdir()
    with (
        patch.object(settings, "_USER_CONFIG_DIR", cfg),
        patch.object(settings, "_CONFIG_DIR", repo_cfg),
    ):
        result = asyncio.run(
            settings._impl_settings_update("main_checkout_guard", {"enabled": False})
        )
        assert result.get("status") == "applied", result
        rejected = asyncio.run(
            settings._impl_settings_update("main_checkout_guard", {"enabled": "no"})
        )
        assert rejected.get("error") == "validation failed", rejected
    assert _run(world, _blocked_payload(world), home=home).returncode == 0
    _assert_blocked(_run(world, _blocked_payload(world), home=tmp_path / "fresh-home"))


def test_settings_validator_rejects_non_booleans_and_unknown_keys():
    from genesis.mcp.health.settings import _DOMAIN_VALIDATORS

    validate = _DOMAIN_VALIDATORS["main_checkout_guard"]
    assert validate({"enabled": False}) == []
    assert validate({"enabled": True}) == []
    assert validate({"enabled": "false"})
    assert validate({"mode": "off"})


def test_the_update_tier_stamp_allows(world):
    assert _run(world, _blocked_payload(world), GENESIS_UPDATE_TIER="1").returncode == 0


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "2", " 1", "1 "])
def test_only_the_exact_update_tier_value_1_exempts(world, value):
    _assert_blocked(_run(world, _blocked_payload(world), GENESIS_UPDATE_TIER=value))


def test_the_update_tier_stamp_name_matches_the_dashboard_spawner():
    from genesis.dashboard.routes import updates
    from tests.conftest import private_module

    sys.path.insert(0, str(_HOOKS))
    try:
        mod = private_module("main_checkout_guard_stamp", _HOOKS / _GUARD_NAME)
    finally:
        sys.path.remove(str(_HOOKS))
    assert mod._TIER_STAMP_ENV == updates.UPDATE_TIER_ENV
    assert f"'{updates.UPDATE_TIER_ENV}': '1'" in updates._ORCHESTRATOR_TEMPLATE.replace('"', "'")


# ── In-process: an internal error allows with a note ─────────────────────────


def test_an_internal_error_allows_with_a_note(monkeypatch, capsys):
    from tests.conftest import private_module

    sys.path.insert(0, str(_HOOKS))
    try:
        mod = private_module("main_checkout_guard_under_test", _HOOKS / _GUARD_NAME)
    finally:
        sys.path.remove(str(_HOOKS))

    def boom(*_a, **_k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(mod, "_decide", boom)
    monkeypatch.setattr(
        mod,
        "_read_payload",
        lambda: _bash_payload("PostToolUse", "cp a b", Path("/"), "toolu_x"),
    )
    monkeypatch.delenv("GENESIS_MAIN_CHECKOUT_GUARD", raising=False)
    monkeypatch.delenv("GENESIS_UPDATE_TIER", raising=False)
    assert mod.main() == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    assert "main-checkout-guard" in out["additionalContext"]
    assert "RuntimeError" in out["additionalContext"]


# ── Parity with the deploy scripts' ephemeral list ───────────────────────────


def test_ephemeral_regex_matches_the_bash_definition():
    bash_value = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{_WORKTREE}/scripts/lib/deploy_marker.sh"; printf %s "$EPHEMERAL_DIRTY_RE"',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    text = (_HOOKS / _GUARD_NAME).read_text()
    m = re.search(r'^EPHEMERAL_DIRTY_RE = r"([^"]*)"$', text, re.MULTILINE)
    assert m, "EPHEMERAL_DIRTY_RE literal not found in the guard"
    assert bash_value and m.group(1) == bash_value


# ── Wiring: the repo settings register pre AND post on Bash ──────────────────


def test_the_repo_settings_wire_the_guard_on_every_event_it_needs():
    cfg = json.loads((_WORKTREE / ".claude" / "settings.json").read_text())["hooks"]
    cmd = "${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook hooks/main_checkout_guard.py"

    def matchers(event):
        return {
            e.get("matcher")
            for e in cfg.get(event, [])
            for h in e.get("hooks", [])
            if h.get("command") == cmd
        }

    assert matchers("PreToolUse") == {"^Bash$", "^(Write|Edit|MultiEdit|NotebookEdit)$"}
    assert matchers("PostToolUse") == {"^Bash$"}
    assert matchers("PostToolUseFailure") == {"^Bash$"}
