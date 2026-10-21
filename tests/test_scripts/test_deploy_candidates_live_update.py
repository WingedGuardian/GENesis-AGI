"""The engine pieces update.sh uses on `live` (#2978 C1b, #2526).

- With nothing running Genesis's code (genesis-server and genesis-bridge both in
  a definite stopped state), readiness judges the code the next start runs,
  HEAD, instead of refusing for want of a serving commit.
- `restore-hooks <before> <after>`: update.sh's rollback moves the checkout back
  itself; this puts back the git hooks the move left behind.
- `live-shaped <commit>`: is the commit what a rebuild makes, and on which base.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PRE_COMMIT = "scripts/hooks/pre-commit"
BASE_HOOK = "#!/bin/sh\n# pre-commit\nexit 0\n"


def _installed(w, name: str = "pre-commit") -> str:
    return (w.root / ".git" / "hooks" / name).read_text()


# ── server down ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("states", "down"),
    [
        ("inactive inactive", True),
        ("failed inactive", True),
        ("inactive failed", True),
        ("inactive active", False),  # the bridge runs the full stack
        ("activating inactive", False),
        ("deactivating inactive", False),
        ("unknown inactive", False),
        ("inactive", False),
        (None, False),
    ],
)
def test_only_two_definitely_stopped_units_read_as_down(dc, states, down):
    stdout = "serving: unknown (genesis-server is not running (inactive))\n"
    if states is not None:
        stdout += f"server-state: {states}\n"
    commit, why = dc.core.parse_serving(stdout, 0)
    assert commit is None
    assert (why == dc.core.SERVER_DOWN) is down


def test_a_serving_commit_wins_over_the_state_line(dc):
    sha = "a" * 40
    commit, _why = dc.core.parse_serving(f"serving: {sha}\nserver-state: inactive inactive\n", 0)
    assert commit == sha


def test_with_the_server_down_readiness_judges_the_checkout(dc, dc_ready, capsys):
    w = dc_ready
    w.serving = lambda root: (None, dc.core.SERVER_DOWN)
    w.candidate("feat/a", {"src/a.py": "a = 1\n"})
    assert w.add(dc, "feat/a") == 0, capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()


def test_with_the_server_down_a_checkout_too_old_still_refuses(dc, dc_ready, monkeypatch, capsys):
    w = dc_ready
    w.candidate("feat/a", {"src/a.py": "a = 1\n"})
    assert w.add(dc, "feat/a") == 0, capsys.readouterr()
    required = w.advance_main({"src/required.py": "r = 1\n"})
    w.git(w.root, "fetch", "-q", "origin")
    monkeypatch.setattr(dc.gate, "REQUIRED_MERGED", (("scratch required", required),))
    w.serving = lambda root: (None, dc.core.SERVER_DOWN)
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    assert "the checkout's base" in capsys.readouterr().err


def test_a_server_state_that_is_not_definitely_down_still_refuses(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"src/a.py": "a = 1\n"})
    assert w.add(dc, "feat/a") == 0, capsys.readouterr()
    w.serving = lambda root: (None, "genesis-server is not running (activating)")
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    assert "a candidate needs a running server" in capsys.readouterr().err


# ── restore-hooks ────────────────────────────────────────────────────────


def _hook_live(w, dc, capsys) -> tuple[str, str]:
    """A rebuild installing an approved hook. Returns (checkout before, tip)."""
    before = w.rev("HEAD", w.root)
    w.candidate("feat/h", {PRE_COMMIT: "#!/bin/sh\n# approved\nexit 0\n"})
    assert w.add(dc, "feat/h", "--approve-hooks", "owner, in chat") == 0, capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert _installed(w) != BASE_HOOK
    return before, w.rev("HEAD", w.root)


def _roll_back(w, to: str) -> None:
    """What update.sh's rollback does on `live`: move the branch back itself."""
    w.git(w.root, "-c", "core.hooksPath=/dev/null", "switch", "-q", "-C", "live", to)


def test_restore_hooks_puts_back_what_the_rollback_left_installed(dc, dc_ready, capsys):
    w = dc_ready
    before, tip = _hook_live(w, dc, capsys)
    _roll_back(w, before)
    assert _installed(w) != BASE_HOOK, "the rollback alone restored the hook; nothing to test"
    assert w.run(dc, "restore-hooks", tip, before, lock=True) == 0, capsys.readouterr()
    assert _installed(w) == BASE_HOOK


def test_restore_hooks_runs_while_the_update_is_unfinished(dc, dc_ready, capsys):
    """It is called from update.sh's rollback, while update_state.json records
    the update: none of the move refusals may apply."""
    w = dc_ready
    before, tip = _hook_live(w, dc, capsys)
    _roll_back(w, before)
    (w.home / ".genesis" / "update_state.json").write_text(json.dumps({"phase": "merging"}))
    assert w.run(dc, "restore-hooks", tip, before, lock=True) == 0, capsys.readouterr()
    assert _installed(w) == BASE_HOOK


def test_restore_hooks_refuses_when_the_checkout_is_elsewhere(dc, dc_ready, capsys):
    w = dc_ready
    before, tip = _hook_live(w, dc, capsys)
    capsys.readouterr()
    assert w.run(dc, "restore-hooks", tip, before, lock=True) == 1
    assert "not where the rollback put it" in capsys.readouterr().err
    assert _installed(w) != BASE_HOOK, "a refused restore changed a hook"


def test_restore_hooks_needs_update_lock(dc, dc_ready, capsys):
    w = dc_ready
    before, tip = _hook_live(w, dc, capsys)
    _roll_back(w, before)
    capsys.readouterr()
    assert w.run(dc, "restore-hooks", tip, before, lock=False) == 1
    assert "update.lock" in capsys.readouterr().err


# restore-hooks exits 1 whenever it leaves an installed hook other than HEAD's:
# update.sh's live rollback reads that exit code as "the hooks did not come back"
# (#3492 gap 2). Each case below names the step that failed.


def _rolled_back(w, dc, capsys) -> tuple[str, str]:
    before, tip = _hook_live(w, dc, capsys)
    _roll_back(w, before)
    capsys.readouterr()
    return before, tip


def _sync_script(w, body: str | None) -> None:
    """Replace sync-hooks.sh on disk only (HEAD's copy, which the engine reads
    the hook list from, is unchanged); None deletes it."""
    path = w.root / "scripts" / "hooks" / "sync-hooks.sh"
    if body is None:
        path.unlink()
    else:
        path.write_text(body)


def test_restore_hooks_fails_when_a_hook_cannot_be_written(dc, dc_ready, capsys):
    w = dc_ready
    before, tip = _rolled_back(w, dc, capsys)
    hooks = w.root / ".git" / "hooks"
    hooks.chmod(0o555)
    try:
        assert w.run(dc, "restore-hooks", tip, before, lock=True) == 1
    finally:
        hooks.chmod(0o755)
    err = capsys.readouterr().err
    assert "ERROR: could not restore git hook pre-commit" in err, err


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (None, "sync-hooks.sh is missing"),
        ("#!/bin/bash\nexit 1\n", "(exit 1)"),
        # Its stderr names the hook it could not copy: the failure carries it.
        ("#!/bin/bash\necho 'cp failed for pre-commit' >&2\nexit 2\n", "cp failed for pre-commit"),
        ("#!/bin/bash\nexec sleep 30\n", "could not run"),
    ],
    ids=["missing", "exit-1", "exit-2-names-the-hook", "timeout"],
)
def test_restore_hooks_fails_when_sync_hooks_fails(dc, dc_ready, capsys, monkeypatch, body, why):
    """The restore itself succeeds (the hook is HEAD's again); only the sync
    step fails, and that alone is exit 1."""
    w = dc_ready
    before, tip = _rolled_back(w, dc, capsys)
    _sync_script(w, body)
    monkeypatch.setattr(dc.plan, "SYNC_HOOKS_TIMEOUT", 2)
    assert w.run(dc, "restore-hooks", tip, before, lock=True) == 1
    err = capsys.readouterr().err
    assert _installed(w) == BASE_HOOK
    assert "ERROR: " in err and why in err, err


def test_restore_hooks_judges_the_installed_hooks_not_the_sync_exit_code(dc, dc_ready, capsys):
    """sync-hooks.sh exits 0 even when one of its copies failed. A hook the
    restore never touches (commit-msg is the same on both sides) and that is
    not HEAD's still fails the run, by name."""
    w = dc_ready
    before, tip = _rolled_back(w, dc, capsys)
    _sync_script(w, "#!/bin/bash\nexit 0\n")
    (w.root / ".git" / "hooks" / "commit-msg").write_text("#!/bin/sh\n# stale\nexit 0\n")
    assert w.run(dc, "restore-hooks", tip, before, lock=True) == 1
    err = capsys.readouterr().err
    assert "installed git hook commit-msg differs from scripts/hooks/commit-msg" in err, err


def test_restore_hooks_fails_on_a_hand_edited_hook(dc, dc_ready, capsys):
    """N-4: a hook equal to neither checkout's copy is left in place, and the
    run says the hooks are not HEAD's."""
    w = dc_ready
    before, tip = _rolled_back(w, dc, capsys)
    mine = "#!/bin/sh\n# edited by hand\nexit 0\n"
    (w.root / ".git" / "hooks" / "pre-commit").write_text(mine)
    assert w.run(dc, "restore-hooks", tip, before, lock=True) == 1
    err = capsys.readouterr().err
    assert _installed(w) == mine
    assert "ERROR: git hook pre-commit was edited by hand" in err, err


def test_restore_hooks_fails_on_a_hand_edited_hook_the_rollback_no_longer_lists(
    dc, dc_ready, capsys
):
    """Only the restore can see this one: sync-hooks.sh and the post-condition
    look at the names HEAD lists, and the rollback's HEAD no longer lists it."""
    w = dc_ready
    before = w.rev("HEAD", w.root)
    sync = (w.root / "scripts" / "hooks" / "sync-hooks.sh").read_text()
    listed = sync.replace(
        '    "pre-merge-commit"\n', '    "pre-merge-commit"\n    "post-merge"\n', 1
    )
    assert listed != sync, "the fixture's sync-hooks.sh changed shape"
    w.candidate("feat/file", {"scripts/hooks/post-merge": "#!/bin/sh\nexit 0\n"})
    w.candidate("feat/list", {"scripts/hooks/sync-hooks.sh": listed})
    for b in ("feat/file", "feat/list"):
        assert w.add(dc, b, "--approve-hooks", "owner, in chat") == 0, capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    tip = w.rev("HEAD", w.root)
    hook = w.root / ".git" / "hooks" / "post-merge"
    assert hook.is_file(), "the approved pair did not install the hook"
    _roll_back(w, before)
    hook.write_text("#!/bin/sh\n# edited by hand\nexit 0\n")
    capsys.readouterr()
    assert w.run(dc, "restore-hooks", tip, before, lock=True) == 1
    err = capsys.readouterr().err
    assert hook.is_file(), "a hand-edited hook was removed"
    assert "ERROR: git hook post-merge was edited by hand" in err, err


def test_a_helper_equal_to_neither_side_is_left_to_sync(dc, dc_ready, capsys):
    """sync-hooks.sh always overwrites a helper (HELPERS_TO_SYNC), so a helper
    that differs from both checkouts' copies is no hand edit: the sync replaces
    it, and the post-condition, not the restore, decides."""
    w = dc_ready
    helper = "scripts/hooks/emit_bugfix_audit.py"
    before = w.rev("HEAD", w.root)
    w.candidate("feat/h", {helper: "# approved helper\n"})
    assert w.add(dc, "feat/h", "--approve-hooks", "owner, in chat") == 0, capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    tip = w.rev("HEAD", w.root)
    installed = w.root / ".git" / "hooks" / "emit_bugfix_audit.py"
    assert installed.read_text() == "# approved helper\n"
    _roll_back(w, before)
    installed.write_text("# neither side\n")
    capsys.readouterr()
    assert w.run(dc, "restore-hooks", tip, before, lock=True) == 0, capsys.readouterr()
    assert installed.read_text() == (w.root / helper).read_text()


def test_restore_hooks_fails_on_a_hook_list_it_cannot_read(dc, dc_ready, capsys):
    w = dc_ready
    sync = (w.root / "scripts" / "hooks" / "sync-hooks.sh").read_text()
    odd = sync.replace("HOOKS_TO_SYNC=(\n", "declare -a HOOKS_TO_SYNC=(\n", 1)
    assert odd != sync, "the fixture's sync-hooks.sh changed shape"
    moved_from = w.candidate("feat/odd", {"scripts/hooks/sync-hooks.sh": odd})
    w.git(w.root, "fetch", "-q", "origin")
    assert w.run(dc, "restore-hooks", moved_from, "HEAD", lock=True) == 1
    err = capsys.readouterr().err
    assert "ERROR: cannot read the hook list at" in err, err


def test_restore_hooks_fails_when_core_hooks_path_is_set(dc, dc_ready, capsys, tmp_path):
    w = dc_ready
    before, tip = _rolled_back(w, dc, capsys)
    w.git(w.root, "config", "core.hooksPath", str(tmp_path / "elsewhere"))
    assert w.run(dc, "restore-hooks", tip, before, lock=True) == 1
    err = capsys.readouterr().err
    assert "ERROR: core.hooksPath is set" in err, err


def test_a_clean_restore_reports_no_error(dc, dc_ready, capsys):
    """Control: the restore and the sync succeed and every hook is HEAD's."""
    w = dc_ready
    before, tip = _rolled_back(w, dc, capsys)
    assert w.run(dc, "restore-hooks", tip, before, lock=True) == 0
    assert "ERROR" not in capsys.readouterr().err
    assert _installed(w) == BASE_HOOK


# ── live-shaped ──────────────────────────────────────────────────────────


def _shaped(w, dc, capsys, commit: str) -> tuple[int, str]:
    capsys.readouterr()
    rc = w.run(dc, "live-shaped", commit)
    return rc, capsys.readouterr().out.strip()


def test_a_rebuilt_tip_names_the_base_it_was_built_on(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"src/a.py": "a = 1\n"})
    assert w.add(dc, "feat/a") == 0, capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    base = w.rev("refs/remotes/origin/main", w.root)
    assert _shaped(w, dc, capsys, "refs/heads/live") == (0, base)
    # main moves on after the rebuild: the tip still names its own base.
    w.advance_main({"src/later.py": "l = 1\n"})
    w.git(w.root, "fetch", "-q", "origin")
    assert _shaped(w, dc, capsys, "refs/heads/live") == (0, base)


def test_a_tip_with_no_candidates_is_its_own_base(dc, dc_ready, capsys):
    w = dc_ready
    base = w.rev("refs/remotes/origin/main", w.root)
    assert _shaped(w, dc, capsys, base) == (0, base)


def test_a_hand_commit_on_live_is_not_shaped(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"src/a.py": "a = 1\n"})
    assert w.add(dc, "feat/a") == 0, capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    w.git(
        w.root, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty", "-m", "by hand"
    )
    capsys.readouterr()
    assert w.run(dc, "live-shaped", "HEAD") == 1
    assert "not a rebuild's tip" in capsys.readouterr().err


# ── the producer of the server-state line ────────────────────────────────

_FAKE_SYSTEMCTL = """#!/bin/bash
# Answers `systemctl --user show <unit> -p ActiveState --value` from FAKE_<UNIT>;
# "none" prints nothing and fails, as an unreachable user manager does. Every
# other query prints nothing.
if [ "$2" = show ] && [ "$4" = -p ] && [ "$5" = ActiveState ]; then
    case "$3" in
        genesis-server|genesis-server.service) v="$FAKE_SERVER" ;;
        genesis-bridge|genesis-bridge.service) v="$FAKE_BRIDGE" ;;
        *) v=none ;;
    esac
    [ "$v" = none ] && exit 1
    echo "$v"
fi
exit 0
"""


def _outside_unit_check(scripts: Path) -> str:
    """deploy_code_only.sh's real `_server_outside_unit`, which `_status_main`
    calls (deploy_code_only.sh defines it before it calls `_status_main`)."""
    text = (scripts / "deploy_code_only.sh").read_text()
    start = text.index("_server_outside_unit() {")
    return text[start : text.index("\n}\n", start) + 3]


def _status(tmp_path, server: str, bridge: str):
    """Run deploy_status.sh's real `_status_main` with a fake systemctl first on
    PATH; returns (stdout, returncode)."""
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    root, bin_ = tmp_path / "root", tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "systemctl").write_text(_FAKE_SYSTEMCTL)
    (bin_ / "systemctl").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(tmp_path),
        PATH=f"{bin_}{os.pathsep}{env['PATH']}",
        FAKE_SERVER=server,
        FAKE_BRIDGE=bridge,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", "c"], check=True, env=env
    )
    script = (
        f'GENESIS_ROOT="{root}"\n_git_dir="{root}/.git"\n'
        f'_SERVING_COMMIT_PY="$(cat "{scripts}/lib/serving_commit.py")"\n'
        f'. "{scripts}/lib/deploy_status.sh"\n' + _outside_unit_check(scripts) + '_status_main ""\n'
    )
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=120)
    return p.stdout, p.returncode


@pytest.mark.parametrize(
    ("server", "bridge", "line", "down"),
    [
        ("inactive", "inactive", "inactive inactive", True),
        ("failed", "inactive", "failed inactive", True),
        ("inactive", "active", "inactive active", False),
        ("none", "inactive", "unknown inactive", False),
        ("inactive", "none", "inactive unknown", False),
    ],
    ids=[
        "both-stopped",
        "server-failed",
        "bridge-active",
        "server-unreadable",
        "bridge-unreadable",
    ],
)
def test_the_status_line_the_engine_reads_is_produced(dc, tmp_path, server, bridge, line, down):
    """deploy_status.sh's real output, fed to the engine's real parser."""
    out, rc = _status(tmp_path, server, bridge)
    assert f"server-state: {line}\n" in out, out
    commit, why = dc.core.parse_serving(out, rc)
    assert commit is None
    assert (why == dc.core.SERVER_DOWN) is down, (why, out)


def test_a_server_outside_the_units_is_not_down(dc, tmp_path):
    """update.sh's degraded fallback runs `genesis serve` with no unit: both units
    read stopped while Genesis's code runs. The server's process lock names it."""
    server = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)", "genesis serve"]
    )
    try:
        (tmp_path / ".genesis").mkdir()
        (tmp_path / ".genesis" / "genesis-server.lock").write_text(str(server.pid))
        out, rc = _status(tmp_path, "inactive", "inactive")
    finally:
        server.kill()
        server.wait()
    assert f"server-state: inactive inactive outside:{server.pid}\n" in out, out
    commit, why = dc.core.parse_serving(out, rc)
    assert commit is None
    assert why != dc.core.SERVER_DOWN, out


# ── update_state.json: the restart-guard rule (#3139) ───────────────────────


def _engine_state(w, dc, content, *, conflict: bool = False) -> list[str]:
    head = w.rev("HEAD", w.root)
    w.git(w.root, "tag", "-f", "pre")
    if isinstance(content, dict) and content.get("deploy_head") == "HEAD":
        content = {**content, "deploy_head": head}
    state = w.home / ".genesis" / "update_state.json"
    state.write_text(content if isinstance(content, str) else json.dumps(content))
    if conflict:
        (w.home / ".genesis" / "update_conflicts.json").write_text(
            json.dumps({"deploy_head": head})
        )
    return [r for r in dc.Engine(w.root, w.env)._move_refusals() if "update_state.json" in r]


@pytest.mark.parametrize(
    "content",
    [
        {"phase": "done"},
        {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "HEAD", "post_merge": False},
        {"phase": "merging", "rollback_tag": "pre", "deploy_head": "HEAD"},
    ],
    ids=["done", "fetching-nothing-merged", "merging-killed-before-merge"],
)
def test_a_state_that_guards_nothing_does_not_refuse_a_move(dc, dc_ready, content):
    """The shared rule: a run that stopped before merging anything left nothing
    for a rebuild to protect."""
    assert _engine_state(dc_ready, dc, content) == []


@pytest.mark.parametrize(
    "content",
    [
        {"phase": "bootstrap", "rollback_tag": "pre"},
        {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "HEAD", "post_merge": True},
        "[1, 2]",
        "{not json",
    ],
    ids=["bootstrap", "post-merge", "array", "malformed"],
)
def test_a_guarding_state_refuses_with_the_shared_remedy(dc, dc_ready, content):
    [reason] = _engine_state(dc_ready, dc, content)
    assert "scripts/update.sh --post-merge" in reason and "Dismiss" in reason


def test_a_pending_conflict_refuses_with_the_conflict_remedy(dc, dc_ready):
    content = {"phase": "merging", "rollback_tag": "pre", "deploy_head": "HEAD"}
    [reason] = _engine_state(dc_ready, dc, content, conflict=True)
    assert "merge conflicts" in reason and "Resolve" in reason


def test_a_harmless_leftover_is_removed_before_a_move_and_kept_by_status(dc, dc_ready):
    """SF-C: rebuild and drop pass _move_preflight under update.lock, then move
    HEAD off the leftover's rollback tag; status only reports."""
    w = dc_ready
    content = {
        "phase": "fetching",
        "rollback_tag": "pre",
        "deploy_head": "HEAD",
        "post_merge": False,
    }
    assert _engine_state(w, dc, content) == []
    state = w.home / ".genesis" / "update_state.json"
    assert state.exists(), "status (_move_refusals) only reports"
    dc.Engine(w.root, w.env)._move_preflight(need_manifest=False)
    assert not state.exists()


def test_a_guarding_state_is_never_removed_before_a_move(dc, dc_ready):
    w = dc_ready
    _engine_state(w, dc, {"phase": "bootstrap", "rollback_tag": "pre"})
    state = w.home / ".genesis" / "update_state.json"
    with pytest.raises(dc.Refusal):
        dc.Engine(w.root, w.env)._move_preflight(need_manifest=False)
    assert state.exists()


# ── the caller's own "merging" row (#3370) ──────────────────────────────────


def _own_row(w, **over) -> Path:
    """update.sh's own row as it stands when it runs the rebuild after the stop:
    "merging", a plain run, its own pid (this process's parent here, as update.sh
    is the engine's parent there), the rollback tag at HEAD."""
    w.git(w.root, "tag", "-f", "pre")
    row = {
        "phase": "merging",
        "rollback_tag": "pre",
        "deploy_head": w.rev("HEAD", w.root),
        "pid": os.getppid(),
        "post_merge": False,
        **over,
    }
    state = w.home / ".genesis" / "update_state.json"
    state.write_text(json.dumps(row))
    return state


def _rebuild_with_a_candidate(w, dc, capsys, **env: str) -> int:
    w.candidate("feat/a", {"src/a.py": "a = 1\n"})
    assert w.add(dc, "feat/a") == 0, capsys.readouterr()
    return w.run(dc, "rebuild", extra_env=env)


def test_the_callers_own_row_is_kept_through_the_rebuild(dc, dc_ready, capsys):
    """A row the shared rule calls harmless is retired before a move, unless it
    is the caller's own: update.sh's restart guard must survive the move it
    guards."""
    w = dc_ready
    state = _own_row(w)
    before = state.read_bytes()
    rc = _rebuild_with_a_candidate(w, dc, capsys, DEPLOY_CANDIDATES_OWN_STATE_PID=str(os.getppid()))
    assert rc == 0, capsys.readouterr()
    assert w.live_merges(), "the rebuild did not move"
    assert state.read_bytes() == before, "the engine retired its caller's restart guard"


def test_the_callers_own_guarding_row_does_not_refuse(dc, dc_ready, capsys):
    """Its row would GUARD (HEAD is off its rollback tag): still the caller's."""
    w = dc_ready
    w.advance_main({"m.txt": "m\n"})
    w.git(w.root, "pull", "-q", "--ff-only", "origin", "main")
    state = _own_row(w)
    w.git(w.root, "tag", "-f", "pre", "HEAD~1")
    rc = _rebuild_with_a_candidate(w, dc, capsys, DEPLOY_CANDIDATES_OWN_STATE_PID=str(os.getppid()))
    assert rc == 0, capsys.readouterr()
    assert state.exists() and w.live_merges()


@pytest.mark.parametrize(
    ("named", "over"),
    [
        (None, {}),
        ("other", {"pid": "other"}),
        ("ppid", {"pid": "other"}),
        ("ppid", {"phase": "fetching"}),
        ("ppid", {"post_merge": True}),
    ],
    ids=["not-named", "not-the-parent", "another-pid", "not-merging", "post-merge"],
)
def test_any_other_row_is_judged_by_the_shared_rule(dc, dc_ready, capsys, named, over):
    """Controls: the exception needs every part. Off its tag, each of these rows
    guards, so the rebuild refuses and the row stays. "other" is a pid that is
    not this process's parent (whatever the parent is: a test sandbox may run
    pytest as pid 1's child)."""
    other, ppid = os.getppid() + 1, os.getppid()
    w = dc_ready
    w.advance_main({"m.txt": "m\n"})
    w.git(w.root, "pull", "-q", "--ff-only", "origin", "main")
    state = _own_row(w, **{k: other if v == "other" else v for k, v in over.items()})
    w.git(w.root, "tag", "-f", "pre", "HEAD~1")
    env = (
        {}
        if named is None
        else {"DEPLOY_CANDIDATES_OWN_STATE_PID": str(ppid if named == "ppid" else other)}
    )
    rc = _rebuild_with_a_candidate(w, dc, capsys, **env)
    assert rc == 1
    assert "update_state.json" in capsys.readouterr().err
    assert state.exists() and not w.live_merges()


def test_the_exception_needs_the_handed_over_update_lock(dc, dc_ready):
    """Without update.lock (status reads refusals unlocked) the same row, named,
    still reads as an unfinished update."""
    w = dc_ready
    w.advance_main({"m.txt": "m\n"})
    w.git(w.root, "pull", "-q", "--ff-only", "origin", "main")
    _own_row(w)
    w.git(w.root, "tag", "-f", "pre", "HEAD~1")
    eng = dc.Engine(w.root, {**w.env, "DEPLOY_CANDIDATES_OWN_STATE_PID": str(os.getppid())})
    assert [r for r in eng._move_refusals() if "update_state.json" in r]
