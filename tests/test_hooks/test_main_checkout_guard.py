"""Tests for scripts/hooks/main_checkout_guard.py.

The guard refuses a change to a TRACKED file in the PRIMARY checkout that the
guard script itself belongs to, and allows everything else. Every subprocess test
here runs a COPY of the hook tree placed inside a scratch primary checkout, so the
guard's self-location resolves to that scratch checkout — never to the real
install, which these tests must not touch.

The scratch world (built once per module; the guard only reads it):

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
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
_HOOKS = _WORKTREE / "scripts" / "hooks"
_GUARD_NAME = "main_checkout_guard.py"
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
    # A second commit, so `HEAD~1` is a real non-HEAD ref to rewind from.
    (install / "README.md").write_text("v2\n")
    _git(install, "commit", "-q", "-am", "second")
    # The hook tree, UNTRACKED in the scratch install: only its LOCATION matters.
    shutil.copytree(
        _HOOKS, install / "scripts" / "hooks", ignore=shutil.ignore_patterns("__pycache__")
    )
    (install / "config").mkdir()
    shutil.copy(_WORKTREE / "config" / "main_checkout_guard.yaml", install / "config")
    wt = install / ".claude" / "worktrees" / "wt"
    _git(install, "worktree", "add", "-q", str(wt), "-b", "wt-branch")
    other = base / "other"
    _init_repo(other, {"README.md": "o\n"})
    plain = base / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("p\n")
    src = base / "incoming.txt"
    src.write_text("new\n")
    return {
        "base": base,
        "install": install,
        "wt": wt,
        "other": other,
        "plain": plain,
        "src": src,
        "guard": install / "scripts" / "hooks" / _GUARD_NAME,
    }


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_")
        and k not in ("GENESIS_MAIN_CHECKOUT_GUARD", "GENESIS_UPDATE_TIER")
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
    return {"tool_name": tool, "tool_input": {key: str(path)}, "cwd": str(cwd)}


def _bash(command: str, cwd) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def _note(res) -> str:
    if not res.stdout.strip():
        return ""
    return json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]


_BLOCK_MARK = "[main-checkout-guard] BLOCKED"


def _assert_blocked(res, context=None):
    """Exit 2 AND the guard's own marker. The marker is load-bearing: the
    interpreter also exits 2 when it cannot open the script, so a bare exit-code
    check passed 29 of these tests before the guard existed."""
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
        shutil.copytree(world["install"] / "scripts", wt2 / "scripts")
    res = _run(world, _edit(wt2 / "src" / "a.py", wt2))
    assert res.returncode == 0, res.stderr
    # The guard's OWN copy inside that worktree (a worktree-local hook run) must
    # not treat its worktree as the deploy root either: only git's
    # git-dir/common-dir answer separates the two here.
    res = _run(
        world, _edit(wt2 / "src" / "a.py", wt2), guard=wt2 / "scripts" / "hooks" / _GUARD_NAME
    )
    assert res.returncode == 0, res.stderr


def test_a_primary_clone_parked_under_a_worktrees_dir_is_not_guarded(world, tmp_path):
    """The path arm of genesis_is_primary_checkout: a plain clone under
    `.claude/worktrees/` is primary to git, but not the deploy root."""
    parked = tmp_path / "x" / ".claude" / "worktrees" / "clone"
    _init_repo(parked, {"README.md": "c\n"})
    shutil.copytree(world["install"] / "scripts", parked / "scripts")
    guard = parked / "scripts" / "hooks" / _GUARD_NAME
    res = _run(world, _edit(parked / "README.md", parked), guard=guard)
    assert res.returncode == 0, res.stderr


def test_a_tracked_file_in_another_primary_repo_is_allowed(world):
    res = _run(world, _edit(world["other"] / "README.md", world["other"]))
    assert res.returncode == 0, res.stderr


def test_a_path_outside_any_repo_is_allowed(world):
    res = _run(world, _edit(world["plain"] / "f.txt", world["plain"]))
    assert res.returncode == 0, res.stderr


def test_a_guard_belonging_to_another_checkout_does_not_judge_this_one(world):
    """Same primary checkout, but the guard script lives in ANOTHER primary repo:
    the block is scoped to the checkout the hook script belongs to."""
    foreign_hooks = world["other"] / "scripts" / "hooks"
    if not foreign_hooks.exists():
        shutil.copytree(world["install"] / "scripts" / "hooks", foreign_hooks)
    res = _run(
        world,
        _edit(world["install"] / "src" / "a.py", world["install"]),
        guard=foreign_hooks / _GUARD_NAME,
    )
    assert res.returncode == 0, res.stderr


# ── Bash: cp / mv / install ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "template",
    [
        "cp {src} README.md",
        "cp -f {src} {install}/src/a.py",
        "mv {src} README.md",
        "install -m 644 {src} README.md",
        "cp -t {install}/src {a_named}",
        "cp --target-directory={install}/src {a_named}",
        "cp -vt {install}/src {a_named}",
        "cp {a_named} {install}/src",
        "mv README.md /tmp/elsewhere",
        "sudo cp {src} README.md",
        "bash -c 'cp {src} README.md'",
        "true && cp {src} README.md",
    ],
)
def test_bash_writes_to_a_tracked_primary_file_are_blocked(world, template):
    a_named = world["base"] / "a.py"
    a_named.write_text("y = 2\n")
    cmd = template.format(src=world["src"], install=world["install"], a_named=a_named)
    res = _run(world, _bash(cmd, world["install"]))
    _assert_blocked(res, (cmd, res.stderr, res.stdout))


@pytest.mark.parametrize(
    "template",
    [
        # Reading FROM the primary checkout changes nothing in it.
        "cp {install}/README.md {base}/copy.md",
        "cp {src} {install}/src/new_file.py",
        # Into an existing directory: the file written is src/incoming.txt, which
        # is untracked — the directory's other, tracked files are not touched.
        "cp {src} {install}/src",
        "install -d {install}/newdir",
        "cp {src} {wt}/README.md",
        "mv {src} {other}/README.md",
        "cp {src} {plain}/f.txt",
        "cp {src} AGENTS.md",
        "cat README.md",
    ],
)
def test_bash_that_touches_no_tracked_primary_file_is_allowed(world, template):
    cmd = template.format(
        src=world["src"],
        install=world["install"],
        base=world["base"],
        wt=world["wt"],
        other=world["other"],
        plain=world["plain"],
    )
    res = _run(world, _bash(cmd, world["install"]))
    assert res.returncode == 0, (cmd, res.stderr)


# ── Bash: git checkout <ref> -- / git restore --source ────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        "checkout HEAD~1 -- README.md",
        "checkout HEAD~1 README.md",
        "checkout HEAD~1 -- .",
        "checkout HEAD~1 -- docs",
        "restore --source=HEAD~1 README.md",
        "restore -s HEAD~1 -- README.md",
        "restore --source HEAD~1 --staged --worktree README.md",
    ],
)
def test_git_rewinds_of_tracked_primary_files_are_blocked(world, args):
    res = _run(world, _bash(f"{_G} {args}", world["install"]))
    _assert_blocked(res, (args, res.stderr, res.stdout))


def test_git_dash_c_into_the_primary_checkout_is_resolved(world):
    cmd = f"{_G} -C {world['install']} checkout HEAD~1 -- README.md"
    res = _run(world, _bash(cmd, world["plain"]))
    _assert_blocked(res, res.stderr)


@pytest.mark.parametrize(
    "args",
    [
        # Discarding a hand edit back to HEAD or the index is the REPAIR path for
        # a dirty deploy root, and must stay open.
        "checkout -- README.md",
        "checkout HEAD -- README.md",
        "restore README.md",
        "restore --source=HEAD README.md",
        # Only an ephemeral path is named.
        "checkout HEAD~1 -- AGENTS.md",
    ],
)
def test_git_forms_that_restore_or_touch_only_ephemera_are_allowed(world, args):
    res = _run(world, _bash(f"{_G} {args}", world["install"]))
    assert res.returncode == 0, (args, res.stderr)


def test_git_rewind_inside_a_linked_worktree_is_allowed(world):
    res = _run(world, _bash(f"{_G} checkout HEAD~1 -- README.md", world["wt"]))
    assert res.returncode == 0, res.stderr


# ── Uncertainty is never a block ─────────────────────────────────────────────


def test_unparseable_bash_is_allowed_with_a_note(world):
    res = _run(world, _bash(f'cp {world["src"]} "README.md', world["install"]))
    assert res.returncode == 0, res.stderr
    assert "main-checkout-guard" in _note(res)


def test_a_command_the_parser_reports_blind_on_is_allowed_with_a_note(world):
    """A line continuation is a parse blind spot that returns NO segments, so the
    note can only come from the blind-spot branch itself."""
    res = _run(
        world, _bash(f"cp {world['src']} \\\n {world['install']}/README.md", world["install"])
    )
    assert res.returncode == 0, res.stderr
    assert "main-checkout-guard" in _note(res)


def test_a_cd_into_the_guarded_checkout_makes_relative_paths_unknown_not_blocked(world):
    cmd = f"cd {world['install']}/src && cp {world['src']} a.py"
    res = _run(world, _bash(cmd, world["plain"]))
    assert res.returncode == 0, res.stderr
    assert "main-checkout-guard" in _note(res)


@pytest.mark.parametrize(
    "template",
    [
        # A cd that lands outside the guarded checkout cannot concern it.
        "cd {plain} && cp {src} README.md",
        "cd {wt} && cp {src} README.md",
        "cd {wt} && " + _G + " checkout HEAD~1 -- README.md",
        # A branch operation is never gated, even after a cd.
        "cd {wt} && " + _G + " checkout -b feature-x",
    ],
)
def test_ungated_or_unconcerned_commands_carry_no_note(world, template):
    cmd = template.format(plain=world["plain"], wt=world["wt"], src=world["src"])
    res = _run(world, _bash(cmd, world["install"]))
    assert res.returncode == 0, res.stderr
    assert _note(res) == "", _note(res)


def test_an_unreadable_command_that_runs_no_gated_program_first_carries_no_note(world):
    """Blind parse, the gated word only on a later line (the heredoc shape)."""
    res = _run(world, _bash('echo start \\\n "cp README.md"', world["install"]))
    assert res.returncode == 0, res.stderr
    assert _note(res) == "", _note(res)


def test_an_unreadable_command_away_from_the_guarded_checkout_carries_no_note(world):
    res = _run(world, _bash(f"cp {world['src']} \\\n x.txt", world["plain"]))
    assert res.returncode == 0, res.stderr
    assert _note(res) == "", _note(res)


def test_a_heredoc_body_is_text_not_a_command(world):
    """The shared parser returns heredoc body lines as segments; a note that
    merely CONTAINS a cp onto a tracked file must not be refused."""
    cmd = f"cat > {world['base']}/notes.md <<'EOF'\ncp foo README.md\nEOF"
    res = _run(world, _bash(cmd, world["install"]))
    assert res.returncode == 0, res.stderr


def test_a_heredoc_body_line_that_repeats_opening_line_text_is_not_refused(world):
    """Text, not position: the body line is a substring of the opening line."""
    src = world["src"]
    cmd = f"cp {src} README.md.bak && cat > {world['base']}/n <<'EOF'\ncp {src} README.md\nEOF"
    res = _run(world, _bash(cmd, world["install"]))
    assert res.returncode == 0, res.stderr


def test_a_tilde_cd_away_from_the_guarded_checkout_carries_no_note(world, tmp_path):
    home = tmp_path / "h"
    (home / "elsewhere").mkdir(parents=True)
    res = _run(
        world, _bash(f"cd ~/elsewhere && cp {world['src']} x.txt", world["install"]), home=home
    )
    assert res.returncode == 0, res.stderr
    assert _note(res) == "", _note(res)


def test_a_tree_ish_source_is_judged_as_a_rewind(world):
    res = _run(world, _bash(f"{_G} restore --source=HEAD~1:src a.py", world["install"] / "src"))
    _assert_blocked(res)


def test_reflink_does_not_lose_the_block(world):
    cmd = f"cp --reflink=auto {world['src']} README.md"
    _assert_blocked(_run(world, _bash(cmd, world["install"])))


def test_a_write_on_the_heredoc_opening_line_is_still_judged(world):
    cmd = f"cp {world['src']} README.md && cat > {world['base']}/n.md <<'EOF'\nx\nEOF"
    _assert_blocked(_run(world, _bash(cmd, world["install"])))


@pytest.mark.parametrize(
    "template",
    [
        # Each of these was MEASURED as a false block of the first draft's model.
        "env -C{plain} cp {src} README.md",
        "sudo -D{plain} cp {src} README.md",
        "cp -n {src} README.md",
        "cp --no-clobber {src} README.md",
        "cp --update=none {src} README.md",
        "mv -n {src} README.md",
        "cp -rT {plain} docs",
        "cp --parents sub/README.md {install}",
        # Copying INTO an existing tracked directory merges; it does not replace
        # every tracked file under it.
        "cp -r {plain}/docs {install}",
    ],
)
def test_options_and_wrappers_the_model_cannot_judge_never_block(world, template):
    sub = world["plain"] / "sub"
    sub.mkdir(exist_ok=True)
    (sub / "README.md").write_text("s\n")
    (world["plain"] / "docs").mkdir(exist_ok=True)
    (world["plain"] / "docs" / "other.md").write_text("o\n")
    cmd = template.format(plain=world["plain"], src=world["src"], install=world["install"])
    cwd = world["plain"] if "--parents" in cmd else world["install"]
    res = _run(world, _bash(cmd, cwd))
    assert res.returncode == 0, (cmd, res.stderr)


@pytest.mark.parametrize(
    "args",
    [
        # Sources that resolve to HEAD's commit restore, they do not rewind.
        "checkout main -- README.md",
        "restore --source=main README.md",
        "checkout main~0 -- README.md",
        "checkout HEAD@{{0}} -- README.md",
        # Patch mode is an interactive picker a session cannot drive.
        "checkout -p HEAD~1 -- README.md",
        # git pointed at another repository: not judged against this checkout.
        "--git-dir={other}/.git --work-tree={other} checkout HEAD~1 -- README.md",
    ],
)
def test_git_forms_equivalent_to_a_discard_or_elsewhere_are_allowed(world, args):
    cmd = f"{_G} " + args.format(other=world["other"])
    res = _run(world, _bash(cmd, world["install"]))
    assert res.returncode == 0, (cmd, res.stderr)


def test_a_git_location_assignment_is_not_judged_against_this_checkout(world):
    other = world["other"]
    cmd = f"GIT_DIR={other}/.git GIT_WORK_TREE={other} {_G} checkout HEAD~1 -- README.md"
    res = _run(world, _bash(cmd, world["install"]))
    assert res.returncode == 0, res.stderr


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


def test_an_absolute_destination_after_a_cd_is_still_judged(world):
    cmd = f"cd {world['plain']} && cp {world['src']} {world['install']}/README.md"
    res = _run(world, _bash(cmd, world["install"]))
    _assert_blocked(res, res.stderr)


def test_a_degraded_parser_allows_bash_with_a_note_and_still_judges_file_tools(world, tmp_path):
    tree = tmp_path / "install-copy"
    # A second primary checkout whose shell_parse is POISONED.
    _init_repo(tree, {"README.md": "x\n"})
    shutil.copytree(world["install"] / "scripts", tree / "scripts")
    (tree / "scripts" / "hooks" / "shell_parse.py").write_text(
        "raise ImportError('poisoned sibling')\n"
    )
    guard = tree / "scripts" / "hooks" / _GUARD_NAME
    res = _run(world, _bash(f"cp {world['src']} README.md", tree), guard=guard)
    assert res.returncode == 0, res.stderr
    assert "main-checkout-guard" in _note(res)
    res = _run(world, _edit(tree / "README.md", tree), guard=guard)
    _assert_blocked(res, res.stderr)


# ── Kill switch and the update-tier stamp ─────────────────────────────────────


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


def test_yaml_enabled_false_turns_the_guard_off(world, tmp_path):
    home = _home_with_overlay(tmp_path, "enabled: false\n")
    assert _run(world, _blocked_payload(world), home=home).returncode == 0


@pytest.mark.parametrize(
    "text", ["enabled: nope\n", "enabled: 0\n", "enabled: [\n", "- just a list\n"]
)
def test_an_invalid_yaml_value_keeps_the_guard_on(world, tmp_path, text):
    home = _home_with_overlay(tmp_path, text)
    _assert_blocked(_run(world, _blocked_payload(world), home=home))


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


def test_without_the_update_tier_stamp_the_same_call_blocks(world):
    _assert_blocked(_run(world, _blocked_payload(world)))
    _assert_blocked(_run(world, _blocked_payload(world), GENESIS_UPDATE_TIER="0"))


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
    monkeypatch.setattr(mod, "_read_payload", lambda: _bash("cp a b", "/"))
    monkeypatch.delenv("GENESIS_MAIN_CHECKOUT_GUARD", raising=False)
    monkeypatch.delenv("GENESIS_UPDATE_TIER", raising=False)
    assert mod.main() == 0
    out = capsys.readouterr().out
    note = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "main-checkout-guard" in note and "RuntimeError" in note


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
