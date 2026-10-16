"""update.sh on `live`: rebuild through the engine instead of merging (#2978 C1b, #2526).

Every test runs SHIPPED blocks of scripts/update.sh (extracted between their
``# BEGIN``/``# END`` markers, never copied) against scratch repositories with
HOME pointed at a scratch directory. The rebuild tests drive the REAL engine,
copied into a scratch checkout (tests/test_scripts/_deploy_candidates_world.py);
its serving-commit read goes to a stub ``scripts/deploy_code_only.sh`` there.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tests.test_scripts.test_bootstrap_guards import (
    _assert_recovery_refused,
    _recover,
    _recovery_commit_file,
    _recovery_git,
    _recovery_repo,
)

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
UPDATE = SCRIPTS / "update.sh"
CHECKER = SCRIPTS / "lib" / "managed_units.py"
LIVE_PY = SCRIPTS / "lib" / "live_checkout.py"
PEER_RESTORE = REPO / "src" / "genesis" / "db" / "crud" / "peer_restore.py"
TAG = "pre-update-test"
LIBS = "".join(
    f'. "{SCRIPTS / "lib" / name}"\n'
    for name in ("deploy_marker.sh", "deploy_checkout.sh", "checkout_lock.sh", "deploy_recovery.sh")
)
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _text() -> str:
    return UPDATE.read_text()


def _block(marker: str) -> str:
    m = re.search(
        rf"# BEGIN {re.escape(marker)}[^\n]*\n(.*?)# END {re.escape(marker)}", _text(), re.DOTALL
    )
    assert m, f"missing {marker} block"
    return m.group(1)


def _function(name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", _text(), re.DOTALL | re.MULTILINE)
    assert m, f"missing {name}()"
    return m.group(0)


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GIT_", "GENESIS_", "DEPLOY_CANDIDATES_", "CLAUDECODE"))
    }
    env.update(_GIT_ENV)
    env["HOME"] = str(home)
    env.update(extra)
    return env


def _bash(script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -Eeuo pipefail\n" + LIBS + script],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=_env(repo),
    ).stdout.strip()


def _assign(var: str, text: str) -> str:
    """A bash assignment of ``text`` verbatim (a quoted heredoc: no escapes)."""
    return f"{var}=\"$(cat <<'__TEXT__'\n{text}__TEXT__\n)\"\n"


def _out(r: subprocess.CompletedProcess) -> str:
    return r.stdout + r.stderr


def _snapshot_defs() -> str:
    """The database snapshot as update.sh defines it, on a scratch database
    ($HOME/live.db, absent unless a test makes it), with the authority check's
    source read the way update.sh reads it at its start."""
    return (
        'DB_FILE="$HOME/live.db"\nDB_SNAPSHOT_TAKEN=0\n'
        f'_PEER_RESTORE_PY="$(cat "{PEER_RESTORE}")"\n' + _block("db-snapshot")
    )


def _result(r: subprocess.CompletedProcess) -> dict[str, str]:
    m = re.search(r"^RESULT (.*)$", r.stdout, re.MULTILINE)
    assert m, _out(r)
    return dict(kv.split("=", 1) for kv in m.group(1).split())


# ── the rebuild, against the real engine ────────────────────────────────────


def _serving_stub(w, lines: str) -> None:
    stub = w.root / "scripts" / "deploy_code_only.sh"
    stub.write_text(
        "#!/bin/bash\n" + "".join(f"echo {json.dumps(ln)}\n" for ln in lines.splitlines())
    )


def _live_world(dc, w, capsys, files: dict | None = None) -> str:
    """`live` = origin/main + feat/a, rebuilt in-process; then the engine is
    installed into the checkout for update.sh to run, ready against the old base.
    Returns the old base."""
    w.candidate("feat/a", files or {"src/a.py": "a = 1\n"})
    assert w.add(dc, "feat/a") == 0, capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "live"
    old_base = w.rev("refs/remotes/origin/main")
    w.install_engine()
    gate = w.root / "scripts" / "deploy_candidates_gate.py"
    gate.write_text(gate.read_text() + f'\nREQUIRED_MERGED = (("scratch base", "{old_base}"),)\n')
    _serving_stub(w, f"serving: {w.rev('HEAD')}\nserver-state: active inactive")
    w.git(w.root, "tag", TAG)
    return old_base


def _rebuild_script(w, old_base: str, mu_src: str | None = None, pre: str = "") -> str:
    mu = f'_mu_src="$(cat "{CHECKER}")"\n' if mu_src is None else _assign("_mu_src", mu_src)
    return (
        f'GENESIS_ROOT="{w.root}"\nSCRIPT_DIR="$GENESIS_ROOT/scripts"\n'
        f"POST_MERGE=false\nON_LIVE=1\nORIGINAL_BRANCH=live\nROLLBACK_TAG={TAG}\n"
        'VALIDATED_HEAD="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"\n'
        'UPDATE_OWN_HEAD="$VALIDATED_HEAD"\n'
        f'_LIVE_BASE="{old_base}"\nDEPLOY_HEAD="$_LIVE_BASE"\nNEW_COMMIT=""\nNEW_TAG=""\n'
        'GENESIS_TAKE_TEMPLATES=""\nREFUSED_FILE="$HOME/.genesis/update_refused.json"\n'
        'STATE_FILE="$HOME/.genesis/update_state.json"\n_SELF_COPY="$HOME/self-copy"\n'
        "WERE_RUNNING=()\n"
        "_guardian_resume() { :; }\n_flush_deploy_summary() { :; }\n"
        'mkdir -p "$HOME/.genesis/locks"\n'
        'exec {_UPDATE_LOCK_FD}>"$HOME/.genesis/locks/update.lock"\nflock -n "$_UPDATE_LOCK_FD"\n'
        + mu
        + "".join(
            re.search(
                rf"{name}\(\) \{{\n.*?\n\}}\n", _block("managed-units-preflight"), re.DOTALL
            ).group(0)
            for name in ("_mu_record_refusal", "_mu_remedy")
        )
        + _function("_clear_deploy_marker")
        + _function("_clear_deploy_state")
        + _function("_on_signal_prestop")
        + _block("live-prestop")
        + _snapshot_defs()
        + _block("live-marker")
        + pre
        + _block("live-rebuild")
        + 'echo "RESULT OWN=$UPDATE_OWN_HEAD DEPLOY=$DEPLOY_HEAD NEW=$NEW_COMMIT PID=$$ '
        'MARKER=$(cat "$DEPLOY_MARKER_FILE" 2>/dev/null || echo none) SNAP=$DB_SNAPSHOT_TAKEN"\n'
    )


def _no_state(w) -> bool:
    return not (w.home / ".genesis" / "update_state.json").exists()


def _marker(w) -> Path:
    return w.home / ".genesis" / "update_in_progress.pid"


def _tag(w) -> str | None:
    r = w.git(w.root, "rev-parse", "-q", "--verify", f"refs/tags/{TAG}", check=False)
    return r.stdout.strip() or None


def test_u1_update_on_live_rebuilds_onto_new_main(dc, dc_ready, capsys):
    """#2526: update.sh on `live` puts the candidate back on top of the NEW main,
    with only rebuild merges above origin/main; it records the base, keeps the
    tip as its own head, and holds the deploy marker into the stop."""
    w = dc_ready
    old_base = _live_world(dc, w, capsys)
    old_tip = w.rev("HEAD")
    feat = w.rev("refs/heads/feat/a")
    new_main = w.advance_main({"c.txt": "c\n"})
    r = _bash(_rebuild_script(w, old_base), _env(w.home))
    assert r.returncode == 0, _out(r)
    tip = w.rev("refs/heads/live")
    assert w.rev("HEAD") == tip != old_tip
    assert w.rev("refs/remotes/origin/main") == new_main
    above = set(w.git(w.root, "rev-list", tip, "--not", new_main).stdout.split())
    merge = w.git(w.root, "rev-list", "--first-parent", "-1", tip).stdout.strip()
    assert above == {merge, *w.git(w.root, "rev-list", feat, "--not", old_base).stdout.split()}
    assert w.git(w.root, "rev-parse", f"{merge}^1", f"{merge}^2").stdout.split() == [new_main, feat]
    res = _result(r)
    assert res["OWN"] == tip
    assert res["DEPLOY"] == new_main
    assert res["NEW"] == w.git(w.root, "rev-parse", "--short", new_main).stdout.strip()
    assert res["MARKER"] == res["PID"], "the deploy marker is not held into the stop"
    assert _no_state(w), "the rebuild wrote an update state before the stop"
    assert _tag(w) == old_tip


def test_u1_server_down_still_rebuilds(dc, dc_ready, capsys):
    """Nothing runs (both units definitely stopped): readiness judges the
    checkout, and the rebuild goes ahead."""
    w = dc_ready
    old_base = _live_world(dc, w, capsys)
    _serving_stub(
        w,
        "serving: unknown (genesis-server is not running (inactive))\nserver-state: inactive inactive",
    )
    w.advance_main({"c.txt": "c\n"})
    r = _bash(_rebuild_script(w, old_base), _env(w.home))
    assert r.returncode == 0, _out(r)
    assert _result(r)["OWN"] == w.rev("refs/heads/live")


def test_u1_server_starting_still_refuses(dc, dc_ready, capsys):
    """Control for the test above: a unit in any other state is not "down"."""
    w = dc_ready
    old_base = _live_world(dc, w, capsys)
    old_tip = w.rev("HEAD")
    _serving_stub(
        w,
        "serving: unknown (genesis-server is not running (activating))\nserver-state: activating inactive",
    )
    w.advance_main({"c.txt": "c\n"})
    r = _bash(_rebuild_script(w, old_base), _env(w.home))
    assert r.returncode == 1, _out(r)
    assert w.rev("HEAD") == old_tip


def test_u2_a_readiness_refusal_changes_nothing(dc, dc_ready, capsys):
    w = dc_ready
    old_base = _live_world(dc, w, capsys)
    old_tip = w.rev("HEAD")
    new_main = w.advance_main({"c.txt": "c\n"})
    # A required fix the running server's base lacks.
    w.git(w.root, "fetch", "-q", "origin")
    gate = w.root / "scripts" / "deploy_candidates_gate.py"
    gate.write_text(gate.read_text() + f'\nREQUIRED_MERGED = (("newer fix", "{new_main}"),)\n')
    r = _bash(_rebuild_script(w, old_base), _env(w.home))
    assert r.returncode == 1, _out(r)
    assert "not ready" in _out(r) and "live was not moved" in r.stdout
    assert w.rev("HEAD") == old_tip
    assert w.git(w.root, "status", "--porcelain", "--untracked-files=no").stdout == ""
    assert _tag(w) is None, "an unmoved refusal left the rollback tag"
    assert not _marker(w).exists(), "the deploy marker was not released"
    assert _no_state(w)


def test_u4_dirty_excused_files_are_backed_up_and_cleared(dc, dc_ready, capsys):
    """AGENTS.md changes upstream and is dirty here: without the clear the
    switch refuses over it. USER.md, excused but not in the merge's clear list,
    is cleared too."""
    w = dc_ready
    user_md = "src/genesis/identity/USER.md"
    w.advance_main({"AGENTS.md": "v1\n", user_md: "template\n"})
    old_base = _live_world(dc, w, capsys)
    w.advance_main({"AGENTS.md": "v2\n"})
    (w.root / "AGENTS.md").write_text("local edit\n")
    (w.root / user_md).write_text("filled in\n")
    r = _bash(_rebuild_script(w, old_base), _env(w.home))
    assert r.returncode == 0, _out(r)
    assert (w.root / "AGENTS.md").read_text() == "v2\n"
    saved = list((w.home / ".genesis" / "premerge-backups").glob("*/live/AGENTS.md/current"))
    assert len(saved) == 1 and saved[0].read_text() == "local edit\n"
    saved = list((w.home / ".genesis" / "premerge-backups").glob(f"*/live/{user_md}/current"))
    assert len(saved) == 1 and saved[0].read_text() == "filled in\n"


def test_u6_post_move_unit_refusal_keeps_the_tag_and_says_what_moved(dc, dc_ready, capsys):
    w = dc_ready
    old_base = _live_world(dc, w, capsys)
    old_tip = w.rev("HEAD")
    w.advance_main({"c.txt": "c\n"})
    refuse = 'import sys\nprint("REFUSED: demo.service (edited by hand)")\nsys.exit(4)\n'
    r = _bash(_rebuild_script(w, old_base, mu_src=refuse), _env(w.home))
    assert r.returncode == 4, _out(r)
    tip = w.rev("refs/heads/live")
    assert tip != old_tip and _tag(w) == old_tip
    assert "live moved to" in r.stdout and "after fixing the units" in r.stdout
    record = json.loads((w.home / ".genesis" / "update_refused.json").read_text())
    assert record["units"] == ["demo.service"] and record["deploy_head"] == tip
    assert "--take-template" in r.stdout and "override.conf" in r.stdout, r.stdout
    assert not _marker(w).exists()
    assert _no_state(w)


# ── the rebuild, against a stub engine ──────────────────────────────────────

_STUB_ENGINE = """\
import os, subprocess, sys
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + " lockfd=" + os.environ.get("DEPLOY_CANDIDATES_LOCK_FD", "-")
             + " re=" + ("yes" if os.environ.get("DEPLOY_CANDIDATES_EPHEMERAL_RE") else "no") + "\\n")
mode = os.environ.get("STUB_MODE", "")
root = os.environ["STUB_ROOT"]
if sys.argv[1] == "rebuild":
    if mode == "move-fail":
        subprocess.run(["git", "-C", root, "-c", "core.hooksPath=/dev/null", "commit", "-q",
                        "--allow-empty", "-m", "moved"], check=True)
    sys.exit(1 if mode in ("move-fail", "fail") else 0)
if sys.argv[1] == "live-shaped":
    print(os.environ["STUB_BASE"])
sys.exit(0)
"""


@pytest.fixture
def plain_live(tmp_path: Path):
    """A checkout on `live` with the rollback tag at HEAD, and a stub engine."""
    root, home, stub = tmp_path / "root", tmp_path / "home", tmp_path / "stub"
    (home / ".genesis").mkdir(parents=True)
    stub.mkdir()
    (stub / "deploy_candidates.py").write_text(_STUB_ENGINE)
    subprocess.run(["git", "init", "-q", "-b", "live", str(root)], check=True, env=_env(home))
    (root / "f.txt").write_text("1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    _git(root, "tag", TAG)
    env = _env(
        home,
        STUB_LOG=str(tmp_path / "engine.log"),
        STUB_ROOT=str(root),
        STUB_BASE=_git(root, "rev-parse", "HEAD"),
    )
    return root, home, stub, env, tmp_path / "engine.log"


def _plain_script(root: Path, stub: Path) -> str:
    return (
        f'GENESIS_ROOT="{root}"\nSCRIPT_DIR="{stub}"\n'
        f"POST_MERGE=false\nON_LIVE=1\nORIGINAL_BRANCH=live\nROLLBACK_TAG={TAG}\n"
        'VALIDATED_HEAD="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"\nUPDATE_OWN_HEAD="$VALIDATED_HEAD"\n'
        '_LIVE_BASE="$VALIDATED_HEAD"\nDEPLOY_HEAD="$_LIVE_BASE"\n_mu_src="import sys"\n'
        'GENESIS_TAKE_TEMPLATES=""\nREFUSED_FILE="$HOME/.genesis/update_refused.json"\n'
        'STATE_FILE="$HOME/.genesis/update_state.json"\n_SELF_COPY="$HOME/self-copy"\nWERE_RUNNING=()\n'
        "_guardian_resume() { :; }\n_flush_deploy_summary() { :; }\n"
        'mkdir -p "$HOME/.genesis/locks"\n'
        'exec {_UPDATE_LOCK_FD}>"$HOME/.genesis/locks/update.lock"\nflock -n "$_UPDATE_LOCK_FD"\n'
        + _function("_clear_deploy_marker")
        + _function("_clear_deploy_state")
        + _function("_on_signal_prestop")
        + _block("live-prestop")
        + _snapshot_defs()
        + _block("live-marker")
        + _block("live-rebuild")
        + 'echo "RESULT OWN=$UPDATE_OWN_HEAD"\n'
    )


def test_u3_a_move_then_failure_keeps_the_tag_and_writes_no_state(plain_live):
    root, home, stub, env, log = plain_live
    old = _git(root, "rev-parse", "HEAD")
    r = _bash(_plain_script(root, stub), {**env, "STUB_MODE": "move-fail"})
    assert r.returncode == 1, _out(r)
    assert _git(root, "rev-parse", "HEAD") != old
    assert _git(root, "rev-parse", TAG) == old, "a moved run lost the name of the old tip"
    assert "live moved to" in r.stdout and "re-run scripts/update.sh" in r.stdout
    assert r.stdout.count("live moved to") == 1, r.stdout
    assert not (home / ".genesis" / "update_state.json").exists()
    assert not (home / ".genesis" / "update_in_progress.pid").exists()


def test_u3_control_an_unmoved_failure_deletes_the_tag(plain_live):
    root, home, stub, env, log = plain_live
    r = _bash(_plain_script(root, stub), {**env, "STUB_MODE": "fail"})
    assert r.returncode == 1, _out(r)
    assert "live was not moved" in r.stdout and "live moved to" not in r.stdout
    assert _git(root, "tag", "-l", TAG) == ""


def test_the_engine_gets_the_update_lock_and_the_excuse_list(plain_live):
    root, home, stub, env, log = plain_live
    r = _bash(_plain_script(root, stub), env)
    assert r.returncode == 0, _out(r)
    first = log.read_text().splitlines()[0]
    assert (
        first.startswith("rebuild lockfd=") and "lockfd=-" not in first and first.endswith("re=yes")
    )


def test_u5_a_held_checkout_lock_refuses_before_the_engine(plain_live):
    root, home, stub, env, log = plain_live
    r = _bash("exec {GENESIS_CHECKOUT_LOCK_FD}>/dev/null\n" + _plain_script(root, stub), env)
    assert r.returncode == 1, _out(r)
    assert "genesis-checkout.lock" in r.stdout
    assert not log.exists(), "the engine ran while this run held the checkout lock"
    assert _git(root, "tag", "-l", TAG) == ""


def test_u5_order_tag_preflight_snapshot_rebuild_stop():
    text = _text()
    tag = text.index('git -C "$GENESIS_ROOT" tag "$ROLLBACK_TAG"')
    pre = text.index("# BEGIN managed-units-preflight")
    snap = text.index("--- Snapshotting database ---")
    staged = text.index("# BEGIN staged-excused-prestop")
    rebuild = text.index("# BEGIN live-rebuild")
    stop = text.index('echo "--- Stopping services for update ---"')
    assert tag < pre < snap < staged < rebuild < stop
    block = _block("live-rebuild")
    lock = block.index("GENESIS_CHECKOUT_LOCK_FD")
    clear = block.index('checkout -q HEAD -- "$_eph"')
    engine = block.index('"$SCRIPT_DIR/deploy_candidates.py" rebuild')
    recheck = block.index('--upto "$_live_tip" --also "$ROLLBACK_TAG"')
    assert lock < clear < engine < recheck
    # The marker, its EXIT trap and the signal traps are armed before the rebuild.
    marker = text.index("# BEGIN live-marker")
    assert text.index("trap '_on_signal_prestop TERM' TERM") < marker < snap < rebuild
    assert "_acquire_deploy_marker" not in block and "trap " not in block
    # The checker that runs on `live` is the old base's, read from git.
    assert 'git -C "$GENESIS_ROOT" show "$_LIVE_BASE:scripts/lib/managed_units.py"' in _block(
        "managed-units-preflight"
    )
    # The first state on `live` is written after the stop.
    assert '\nif [ "${ON_LIVE:-0}" != 1 ]; then\n_write_state "fetching"\nfi\n' in text


# ── branch admission ────────────────────────────────────────────────────────


@pytest.fixture
def checkout(tmp_path: Path):
    root, home = tmp_path / "root", tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, env=_env(home))
    (root / "f.txt").write_text("1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    return root, home


def _manifest(home: Path, repo: str | None, raw: str | None = None) -> None:
    path = home / ".genesis" / "deploy_manifest.json"
    path.write_text(
        raw if raw is not None else json.dumps({"version": 3, "repo": repo, "candidates": []})
    )


_SENTINEL_PY = "import pathlib, sys\npathlib.Path(sys.argv[1], 'PREDICATE_RAN').touch()\nprint('other')\nsys.exit(1)\n"


def _admit(root: Path, home: Path, *, predicate: str | None = None, **env: str):
    py = (
        f'_LIVE_CHECKOUT_PY="$(cat "{LIVE_PY}")"\n'
        if predicate is None
        else _assign("_LIVE_CHECKOUT_PY", predicate)
    )
    return _bash(
        f'GENESIS_ROOT="{root}"\n' + py + _block("branch-admission") + 'echo "ON_LIVE=$ON_LIVE"\n',
        _env(home, **env),
    )


def test_u7_main_is_admitted_and_never_asks_the_manifest(checkout):
    root, home = checkout
    r = _admit(root, home, predicate=_SENTINEL_PY)
    assert r.returncode == 0 and "ON_LIVE=0" in r.stdout, _out(r)
    assert "WARNING" not in r.stdout
    assert not (root / "PREDICATE_RAN").exists()


def test_u7_override_admits_a_named_branch_without_the_manifest(checkout):
    root, home = checkout
    _git(root, "checkout", "-qb", "feature")
    r = _admit(root, home, predicate=_SENTINEL_PY, GENESIS_ALLOW_NON_DEPLOY_BRANCH="1")
    assert (
        r.returncode == 0
        and "ON_LIVE=0" in r.stdout
        and "WARNING: updating branch 'feature'" in r.stdout
    )
    assert not (root / "PREDICATE_RAN").exists()


@pytest.mark.parametrize("override", ["", "1"], ids=["plain", "override"])
def test_u7_live_bound_to_this_repository_is_admitted(checkout, override):
    root, home = checkout
    _git(root, "checkout", "-qb", "live")
    _manifest(home, os.path.realpath(root / ".git"))
    r = _admit(root, home, GENESIS_ALLOW_NON_DEPLOY_BRANCH=override)
    assert r.returncode == 0 and "ON_LIVE=1" in r.stdout, _out(r)
    assert "WARNING" not in r.stdout


@pytest.mark.parametrize("override", ["", "1"], ids=["plain", "override"])
def test_u7_live_built_for_another_repository_keeps_the_branch_refusal(
    checkout, tmp_path, override
):
    root, home = checkout
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True, env=_env(home))
    _git(root, "checkout", "-qb", "live")
    _manifest(home, os.path.realpath(other / ".git"))
    r = _admit(root, home, GENESIS_ALLOW_NON_DEPLOY_BRANCH=override)
    assert r.returncode == 1 and "is on 'live', not main" in r.stdout, _out(r)
    assert "ON_LIVE" not in r.stdout


def test_u7_live_with_an_unreadable_manifest_refuses(checkout):
    root, home = checkout
    _git(root, "checkout", "-qb", "live")
    _manifest(home, None, raw="{not json")
    r = _admit(root, home)
    assert r.returncode == 1 and "cannot tell whether" in r.stdout, _out(r)


def test_u7_an_inherited_on_live_is_ignored(checkout):
    root, home = checkout
    r = _admit(root, home, ON_LIVE="1", _LIVE_BASE="deadbeef")
    assert r.returncode == 0 and "ON_LIVE=0" in r.stdout, _out(r)


def test_u7_the_verdict_is_asked_again_under_the_lock(checkout):
    root, home = checkout
    _git(root, "checkout", "-qb", "live")
    script = (
        f'GENESIS_ROOT="{root}"\n_LIVE_CHECKOUT_PY="$(cat "{LIVE_PY}")"\nON_LIVE=1\n'
        + _block("live-lock-recheck")
        + "echo PASSED\n"
    )
    _manifest(home, os.path.realpath(root / ".git"))
    assert "PASSED" in _bash(script, _env(home)).stdout
    (home / ".genesis" / "deploy_manifest.json").unlink()
    r = _bash(script, _env(home))
    assert r.returncode == 1 and "no longer on the live branch" in r.stdout, _out(r)


def test_u7_the_predicate_is_read_before_the_branch_check():
    text = _text()
    assert text.index('_LIVE_CHECKOUT_PY="$(cat "$SCRIPT_DIR/lib/live_checkout.py")"') < text.index(
        "# BEGIN branch-admission"
    )
    flock = text.index('if ! flock -n "$_UPDATE_LOCK_FD"; then')
    assert flock < text.index("# BEGIN live-lock-recheck") < text.index("Genesis Update")


# ── the remote ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("on_live", "post_merge", "remote", "refused"),
    [
        ("1", "false", "upstream", True),
        ("1", "false", "origin", False),
        ("1", "true", "", False),
        ("0", "false", "upstream", False),
    ],
    ids=["live-other-remote", "live-origin", "live-post-merge", "main-other-remote"],
)
def test_u8_live_refuses_any_remote_but_origin(tmp_path, on_live, post_merge, remote, refused):
    script = (
        f"ON_LIVE={on_live}\nPOST_MERGE={post_merge}\nUPDATE_REMOTE={remote}\n"
        + _block("live-remote")
        + "echo PASSED\n"
    )
    r = _bash(script, _env(tmp_path))
    assert (r.returncode == 1 and "PASSED" not in r.stdout) is refused, _out(r)


# ── nothing new ─────────────────────────────────────────────────────────────


@pytest.fixture
def history(tmp_path: Path):
    """A checkout with base <- tip, the rollback tag at the tip, and an
    update_history table."""
    root = tmp_path / "root"
    subprocess.run(["git", "init", "-q", "-b", "live", str(root)], check=True, env=_env(tmp_path))
    (root / "f.txt").write_text("1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "commit", "-q", "--allow-empty", "-m", "candidate merge")
    tip = _git(root, "rev-parse", "HEAD")
    _git(root, "tag", TAG)
    (root / "data").mkdir()
    con = sqlite3.connect(root / "data" / "genesis.db")
    con.execute(
        "CREATE TABLE update_history (id TEXT, new_commit TEXT, status TEXT, completed_at TEXT)"
    )
    con.commit()
    con.close()
    return root, base, tip


def _success_row(root: Path, new_commit: str) -> None:
    con = sqlite3.connect(root / "data" / "genesis.db")
    con.execute(
        "INSERT INTO update_history VALUES ('x', ?, 'success', '2026-10-13T00:00:00+00:00')",
        (new_commit,),
    )
    con.commit()
    con.close()


def _nothing_new(tmp_path: Path, root: Path, **vars_: str) -> bool:
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True, exist_ok=True)
    if not (venv / "python").exists():
        (venv / "python").symlink_to(sys.executable)
    script = (
        f'GENESIS_ROOT="{root}"\nVENV_DIR="{tmp_path / "venv"}"\nROLLBACK_TAG={TAG}\n'
        + "".join(f'{k}="{v}"\n' for k, v in vars_.items())
        + _function("_metadata_python")
        + _block("tier2-baseline-check")
        + _block("nothing-new")
        + "if _nothing_new; then echo NOTHING-NEW; else echo SOMETHING-NEW; fi\n"
    )
    r = _bash(script, _env(tmp_path))
    assert r.returncode == 0, _out(r)
    return "NOTHING-NEW" in r.stdout


@pytest.mark.parametrize(("old", "new", "nothing"), [("abc", "abc", True), ("abc", "def", False)])
def test_u9_off_live_it_is_the_commit_comparison(tmp_path, history, old, new, nothing):
    root, base, tip = history
    assert _nothing_new(tmp_path, root, OLD_COMMIT=old, NEW_COMMIT=new) is nothing


def test_u9_on_live_unmoved_and_recorded_is_nothing_new(tmp_path, history):
    root, base, tip = history
    _success_row(root, base[:9])
    assert _nothing_new(
        tmp_path,
        root,
        ON_LIVE="1",
        UPDATE_OWN_HEAD=tip,
        DEPLOY_HEAD=base,
        OLD_COMMIT="a",
        NEW_COMMIT="a",
    )


def test_u9_on_live_a_moved_tip_is_something_new(tmp_path, history):
    root, base, tip = history
    _success_row(root, base[:9])
    _git(root, "commit", "-q", "--allow-empty", "-m", "rebuilt")
    moved = _git(root, "rev-parse", "HEAD")
    assert not _nothing_new(
        tmp_path,
        root,
        ON_LIVE="1",
        UPDATE_OWN_HEAD=moved,
        DEPLOY_HEAD=base,
        OLD_COMMIT="a",
        NEW_COMMIT="a",
    )


@pytest.mark.parametrize("recorded", ["tip", "unresolvable", "none"])
def test_u9_on_live_an_unrecorded_base_is_something_new(tmp_path, history, recorded):
    """A moved-then-refused run left `live` on a base no success row names: the
    rerun must activate and record it, not read "Already up to date"."""
    root, base, tip = history
    if recorded == "tip":
        _success_row(root, tip[:9])
    elif recorded == "unresolvable":
        _success_row(root, "0" * 9)
    assert not _nothing_new(
        tmp_path,
        root,
        ON_LIVE="1",
        UPDATE_OWN_HEAD=tip,
        DEPLOY_HEAD=base,
        OLD_COMMIT="a",
        NEW_COMMIT="a",
    )


def test_u9_the_take_branch_stays_first():
    text = _text()
    first = text.index('if _nothing_new && [ -n "$GENESIS_TAKE_TEMPLATES" ]; then')
    assert (
        first
        < text.index("elif _nothing_new && _tier2_pending_since_baseline; then")
        < text.index("elif _nothing_new; then")
    )
    assert '[[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]' in _block("nothing-new")


# ── NEW_COMMIT is the base at both recompute sites ──────────────────────────

_SITE = re.compile(
    r'NEW_TAG=\$\(git -C "\$GENESIS_ROOT" describe --tags --match \'v\*\' --abbrev=0 2>/dev/null \|\| echo "untagged"\)\n'
    r'NEW_COMMIT=\$\(git -C "\$GENESIS_ROOT" rev-parse --short HEAD\)\n'
    r'(?:if \[ "\$\{ON_LIVE:-0\}" = 1 \]; then\n    _live_new_commit\nfi\n)?'
)


def test_u10_new_commit_is_the_base_at_both_sites(tmp_path, history):
    root, base, tip = history
    _git(root, "tag", "v1.0", base)
    _git(root, "tag", "v9.9", tip)
    sites = [m.group(0) for m in _SITE.finditer(_text())]
    assert len(sites) == 2, sites
    for site in sites:
        script = (
            f'GENESIS_ROOT="{root}"\nON_LIVE=1\nDEPLOY_HEAD="{base}"\n'
            + _block("live-prestop")
            + site
            + 'echo "NEW=$NEW_COMMIT TAG=$NEW_TAG"\n'
        )
        r = _bash(script, _env(tmp_path))
        assert r.returncode == 0, _out(r)
        assert f"NEW={_git(root, 'rev-parse', '--short', base)} TAG=v1.0" in r.stdout, site


# ── rollback: restore-hooks ─────────────────────────────────────────────────


def _rollback_reset(root: Path, home: Path, stub: Path, own_head: str, on_live: str) -> str:
    return (
        "exec {GENESIS_CHECKOUT_LOCK_FD}>/dev/null\n"
        'mkdir -p "$HOME/.genesis/locks"\nexec {_UPDATE_LOCK_FD}>"$HOME/.genesis/locks/update.lock"\n'
        f'GENESIS_ROOT="{root}"\nSCRIPT_DIR="{stub}"\nORIGINAL_BRANCH=live\nROLLBACK_TAG={TAG}\n'
        f'UPDATE_OWN_HEAD="{own_head}"\nON_LIVE={on_live}\nMERGE_ATTEMPTED=0\n'
        "guard() {\n"
        + _block("rollback-code-guard")
        + '    echo "ACTION=$code_action OK=$checkout_ok"\n}\nguard\n'
    )


@pytest.mark.parametrize("on_live", ["1", "0"])
def test_u11_a_live_rollback_restores_the_hooks(plain_live, on_live):
    root, home, stub, env, log = plain_live
    _git(root, "commit", "-q", "--allow-empty", "-m", "the rebuilt tip")
    tip = _git(root, "rev-parse", "HEAD")
    r = _bash(_rollback_reset(root, home, stub, tip, on_live), env)
    assert r.returncode == 0, _out(r)
    assert "ACTION=reset OK=true" in r.stdout
    assert _git(root, "rev-parse", "HEAD") == _git(root, "rev-parse", TAG)
    if on_live == "1":
        line = log.read_text().strip()
        assert line.startswith(f"restore-hooks {tip} {TAG} lockfd=") and line.endswith("re=yes"), (
            line
        )
        assert "lockfd=-" not in line
    else:
        assert not log.exists()


def test_u11_a_failed_restore_only_warns(plain_live):
    root, home, stub, env, log = plain_live
    (stub / "deploy_candidates.py").write_text("import sys\nsys.exit(1)\n")
    _git(root, "commit", "-q", "--allow-empty", "-m", "the rebuilt tip")
    tip = _git(root, "rev-parse", "HEAD")
    r = _bash(_rollback_reset(root, home, stub, tip, "1"), env)
    assert (
        "ACTION=reset OK=true" in r.stdout
        and "WARNING: could not restore the git hooks" in r.stdout
    )


# ── the live prestop handler ────────────────────────────────────────────────


def _prestop(root: Path, on_live: str, before: str = "", *, written: bool = False) -> str:
    return (
        f'GENESIS_ROOT="{root}"\nPOST_MERGE=false\nON_LIVE={on_live}\nORIGINAL_BRANCH=live\n'
        f'ROLLBACK_TAG={TAG}\nSTATE_FILE="$HOME/.genesis/update_state.json"\nWERE_RUNNING=()\n'
        'VALIDATED_HEAD="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"\n'
        + ("_STATE_WRITTEN=1\n" if written else "")
        + _function("_clear_deploy_marker")
        + _function("_clear_deploy_state")
        + _function("_on_signal_prestop")
        + _block("live-prestop")
        + "_acquire_deploy_marker\n"
        + before
        + "_on_signal_prestop TERM\n"
    )


def _other_runs_state(home: Path) -> Path:
    state = home / ".genesis" / "update_state.json"
    state.write_text(json.dumps({"phase": "merging", "pid": 999999}))
    return state


def test_u12_unmoved_keeps_the_other_runs_state_and_deletes_the_tag(plain_live):
    root, home, stub, env, log = plain_live
    state = _other_runs_state(home)
    r = _bash(_prestop(root, "1"), env)
    assert r.returncode == 1, _out(r)
    assert state.exists(), "the live prestop handler deleted another run's state"
    assert not (home / ".genesis" / "update_in_progress.pid").exists()
    assert _git(root, "tag", "-l", TAG) == ""
    assert "live was not moved" in r.stdout


@pytest.mark.parametrize("how", ["moved", "index-lock"])
def test_u12_a_move_or_a_half_done_git_keeps_the_tag(plain_live, how):
    root, home, stub, env, log = plain_live
    state = _other_runs_state(home)
    before = (
        'git -C "$GENESIS_ROOT" commit -q --allow-empty -m moved\n'
        if how == "moved"
        else ': > "$GENESIS_ROOT/.git/index.lock"\n'
    )
    r = _bash(_prestop(root, "1", before), env)
    assert r.returncode == 1, _out(r)
    assert state.exists()
    assert _git(root, "tag", "-l", TAG) == TAG
    if how == "moved":
        assert "live moved to" in r.stdout and "re-run scripts/update.sh" in r.stdout
    else:
        # HEAD never moved: what is on disk may be a half-written switch, and
        # re-running is not the next step.
        assert "half-written tree" in r.stdout and "git status" in r.stdout, r.stdout
        assert "live moved to" not in r.stdout and "re-run" not in r.stdout
    assert not (home / ".genesis" / "update_in_progress.pid").exists()


def test_u12_control_off_live_clears_the_state(plain_live):
    """Off `live` the handler is armed after this run wrote its own "fetching"
    state, and that file goes."""
    root, home, stub, env, log = plain_live
    state = _other_runs_state(home)
    r = _bash(_prestop(root, "0", written=True), env)
    assert r.returncode == 1, _out(r)
    assert not state.exists()


def test_u12_off_live_a_state_this_run_never_wrote_stays(plain_live):
    """#3139 (N1): an exit before this run's first write never deletes another
    run's state file."""
    root, home, stub, env, log = plain_live
    state = _other_runs_state(home)
    r = _bash(_prestop(root, "0"), env)
    assert r.returncode == 1, _out(r)
    assert state.exists()


# ── staged-excused prestop ──────────────────────────────────────────────────


@pytest.mark.parametrize(("on_live", "rc"), [("1", 0), ("0", 127)])
def test_u13_the_staged_guard_is_not_called_on_live(tmp_path, on_live, rc):
    """The guard is defined inside the fetch block, which `live` skips."""
    r = _bash(
        f"POST_MERGE=false\nON_LIVE={on_live}\n" + _block("staged-excused-prestop"), _env(tmp_path)
    )
    assert r.returncode == rc, _out(r)


# ── the guardian source ─────────────────────────────────────────────────────

_SSH_STUB = """#!/bin/bash
cmd="${@: -1}"
echo "$cmd" >> "$STUB_SSH_LOG"
case "$cmd" in
    version) echo '{"deployed_commit": "unknown"}' ;;
    redeploy*) cat > "$STUB_ARCHIVE" ;;
esac
exit 0
"""


@pytest.mark.parametrize("on_live", ["1", "0"])
def test_u14_the_guardian_gets_the_base_on_live(tmp_path, on_live):
    home, root, bin_ = tmp_path / "home", tmp_path / "root", tmp_path / "bin"
    (home / ".genesis").mkdir(parents=True)
    bin_.mkdir()
    (bin_ / "ssh").write_text(_SSH_STUB)
    (bin_ / "ssh").chmod(0o755)
    key = home / "key"
    key.write_text("k\n")
    (home / ".genesis" / "guardian_remote.yaml").write_text(
        f'host_ip: "192.0.2.7"\nssh_key: "{key}"\n'
    )
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    subprocess.run(["git", "init", "-q", "-b", "live", str(root)], check=True, env=_env(home))
    # Every path the archive names must exist, or `git archive` refuses.
    for path in (
        "scripts/x.sh",
        "pyproject.toml",
        "config/guardian-claude.md",
        "config/genesis-guardian.service",
        "config/genesis-guardian.timer",
        "config/genesis-guardian-watchman.service",
        "config/genesis-guardian-watchman.timer",
    ):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("x\n")
    base = _recovery_like_commit(root, "src/genesis/util/base.py")
    tip = _recovery_like_commit(root, "src/genesis/util/candidate.py")
    archive = tmp_path / "archive.tar"
    log = tmp_path / "ssh.log"
    script = (
        f'GENESIS_ROOT="{root}"\nVENV_DIR="{tmp_path / "venv"}"\nSCRIPT_DIR="{tmp_path / "noscripts"}"\n'
        f'ON_LIVE={on_live}\nDEPLOY_HEAD="{base}"\nOLD_COMMIT="{base[:9]}"\n'
        + _function("_guardian_redeploy_reason")
        + _function("_sync_deploy_targets")
        + "_sync_deploy_targets\n"
    )
    env = _env(home, STUB_SSH_LOG=str(log), STUB_ARCHIVE=str(archive))
    env["PATH"] = f"{bin_}{os.pathsep}{env['PATH']}"
    r = _bash(script, env)
    assert r.returncode == 0, _out(r)
    want = base if on_live == "1" else tip
    sent = [ln for ln in log.read_text().splitlines() if ln.startswith("redeploy ")]
    assert sent and sent[0].split()[1] == _git(root, "rev-parse", "--short", want), _out(r)
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "src/genesis/util/base.py" in names
    assert ("src/genesis/util/candidate.py" in names) is (on_live == "0"), names


def _recovery_like_commit(root: Path, path: str) -> str:
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {path}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", path)
    return _git(root, "rev-parse", "HEAD")


# ── bootstrap crash recovery ────────────────────────────────────────────────


def test_b1_bootstrap_does_not_roll_live_back(tmp_path):
    root, _common, rollback = _recovery_repo(tmp_path)
    _recovery_git(root, "checkout", "-qb", "live")
    merged = _recovery_commit_file(root, "changed.txt", "rebuilt\n", "the rebuilt tip")
    proc, state_path = _recover(tmp_path, root, original_branch="live", own_head=merged)
    _assert_recovery_refused(proc, state_path)
    assert "the update ran on live" in proc.stdout + proc.stderr
    assert _recovery_git(root, "rev-parse", "HEAD") == merged


# ── what a refusal says after the rebuild began ─────────────────────────────


def _stop_abort_branch() -> str:
    """The stop's abort branch: genesis-server could not be stopped."""
    text = _text()
    start = text.index("    if ! _ensure_server_down; then\n")
    end = text.index("\n    fi\n", start) + len("\n    fi\n")
    return text[start:end]


@pytest.mark.parametrize(("on_live", "post_merge"), [("1", "false"), ("1", "true"), ("0", "false")])
def test_sf1_a_failed_stop_on_live_says_where_live_is(plain_live, on_live, post_merge):
    """The EXIT trap is quiet once the stop began, so the abort branch itself
    names the rebuilt tip and the old one."""
    root, home, stub, env, log = plain_live
    _git(root, "commit", "-q", "--allow-empty", "-m", "the rebuilt tip")
    short = _git(root, "rev-parse", "--short", "HEAD")
    script = (
        f'GENESIS_ROOT="{root}"\nON_LIVE={on_live}\nPOST_MERGE={post_merge}\n'
        f"ORIGINAL_BRANCH=live\nROLLBACK_TAG={TAG}\n"
        + _block("live-prestop")
        + "_LIVE_STOPPED=1\n_ensure_server_down() { return 1; }\n"
        + _stop_abort_branch()
    )
    r = _bash(script, env)
    assert r.returncode == 1, _out(r)
    if on_live == "0":
        assert "live is at" not in r.stdout
        return
    assert f"live is at {short}" in r.stdout and f"tag {TAG}" in r.stdout, r.stdout
    assert "without activation" in r.stdout
    if post_merge == "true":
        assert "re-run scripts/update.sh --post-merge." in r.stdout
    else:
        assert "re-run scripts/update.sh." in r.stdout and "--post-merge" not in r.stdout


@pytest.mark.parametrize("post_merge", ["false", "true"])
def test_the_moved_message_names_the_run_to_repeat(plain_live, post_merge):
    root, home, stub, env, log = plain_live
    _git(root, "commit", "-q", "--allow-empty", "-m", "the rebuilt tip")
    script = (
        f'GENESIS_ROOT="{root}"\nPOST_MERGE={post_merge}\nORIGINAL_BRANCH=live\n'
        f"ROLLBACK_TAG={TAG}\n" + _block("live-prestop") + "_live_moved_message\n"
    )
    r = _bash(script, env)
    assert r.returncode == 0, _out(r)
    assert "live moved to" in r.stdout
    assert ("re-run scripts/update.sh --post-merge" in r.stdout) is (post_merge == "true")


def test_a_marker_refusal_comes_before_any_edit_is_cleared(plain_live):
    """The marker is taken before the rebuild block, so a refusal over it leaves
    the excused local edits where they are and changes nothing."""
    root, home, stub, env, log = plain_live
    (root / "AGENTS.md").write_text("v1\n")
    _git(root, "add", "AGENTS.md")
    _git(root, "commit", "-qm", "agents")
    _git(root, "tag", "-f", TAG)
    (root / "AGENTS.md").write_text("local edit\n")
    # A live foreign holder: this test's own process.
    marker = home / ".genesis" / "update_in_progress.pid"
    marker.write_text(f"{os.getpid()}\n")
    r = _bash(_plain_script(root, stub), env)
    assert r.returncode == 1, _out(r)
    assert "holds the deploy marker" in r.stdout and "Nothing was changed" in r.stdout
    assert (root / "AGENTS.md").read_text() == "local edit\n"
    assert not log.exists(), "the engine ran without the deploy marker"
    assert _git(root, "tag", "-l", TAG) == ""
    assert marker.read_text() == f"{os.getpid()}\n", "a refused run took the holder's marker"


# ── F5: the deploy marker, in both modes ────────────────────────────────────


def _marker_script(root: Path, on_live: str, post_merge: str, after: str = "") -> str:
    return (
        f'GENESIS_ROOT="{root}"\nON_LIVE={on_live}\nPOST_MERGE={post_merge}\n'
        f"ORIGINAL_BRANCH=live\nROLLBACK_TAG={TAG}\n"
        'VALIDATED_HEAD="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"\n_SELF_COPY="$HOME/self-copy"\n'
        "_guardian_resume() { :; }\n_flush_deploy_summary() { :; }\n"
        + _block("live-prestop")
        + _block("live-marker")
        + 'echo "RESULT PID=$$ MARKER=$(cat "$DEPLOY_MARKER_FILE" 2>/dev/null || echo none)"\n'
        + after
    )


@pytest.mark.parametrize("post_merge", ["true", "false"])
def test_f5_live_takes_the_marker_before_the_stop_in_both_modes(plain_live, post_merge):
    """--post-merge skips the rebuild, yet it holds the server stopped until the
    bootstrap state row: the marker must cover it too, and go on exit."""
    root, home, stub, env, log = plain_live
    # The stop and the activation: the EXIT trap still releases the marker.
    r = _bash(_marker_script(root, "1", post_merge, "_LIVE_STOPPED=1\nexit 0\n"), env)
    assert r.returncode == 0, _out(r)
    res = _result(r)
    assert res["MARKER"] == res["PID"], "live ran toward the stop without the deploy marker"
    marker = home / ".genesis" / "update_in_progress.pid"
    assert not marker.exists(), "the deploy marker outlived the run"


def test_f5_main_never_takes_the_marker(plain_live):
    root, home, stub, env, log = plain_live
    r = _bash(_marker_script(root, "0", "true"), env)
    assert r.returncode == 0, _out(r)
    assert _result(r)["MARKER"] == "none"


def test_f5_the_marker_is_taken_for_every_live_run_before_the_stop():
    text = _text()
    block = _block("live-marker")
    # Gated on `live` alone: never on POST_MERGE, which only decides the tag.
    gate = block.index('\nif [ "${ON_LIVE:-0}" = 1 ]; then\n')
    assert "POST_MERGE" not in block[: block.index("_acquire_deploy_marker")]
    assert gate < block.index("_acquire_deploy_marker")
    assert "_acquire_deploy_marker" in block and "trap '_live_prestop_exit;" in block
    marker = text.index("# BEGIN live-marker")
    assert text.index('_write_state "fetching"') < marker
    assert marker < text.index('echo "--- Stopping services for update ---"')
    assert marker < text.index("# BEGIN live-rebuild")
    assert "_acquire_deploy_marker" not in _block("live-rebuild")


# ── F1: the database snapshot, retaken after the rebuild ────────────────────


def test_f1_live_retakes_the_snapshot_after_the_rebuild_before_the_stop():
    text = _text()
    block = _block("live-rebuild")
    recheck = block.index('--upto "$_live_tip" --also "$ROLLBACK_TAG"')
    engine = block.index('"$SCRIPT_DIR/deploy_candidates.py" rebuild')
    snap = block.index("_take_db_snapshot")
    assert engine < recheck < snap
    assert "_live_abandon 1" in block[snap:]
    # The first snapshot still runs early, where a failure has moved nothing.
    assert text.index("_take_db_snapshot || exit 1") < text.index("# BEGIN live-rebuild")
    # The authority check is the copy read at the start, run isolated.
    snapshot = _block("db-snapshot")
    assert 'python3 -I -S -c "$_PEER_RESTORE_PY"' in snapshot
    assert "-m genesis" not in snapshot
    assert '_PEER_RESTORE_PY="$(cat ' in text[: text.index("# BEGIN live-prestop")]


def _db_rows(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT v FROM t ORDER BY rowid")]
    finally:
        con.close()


def test_f1_a_write_during_the_rebuild_is_in_the_snapshot(dc, dc_ready, capsys):
    """The server keeps writing while the engine rebuilds; the snapshot a
    rollback would restore must hold those writes."""
    w = dc_ready
    old_base = _live_world(dc, w, capsys)
    db = w.home / "live.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES ('before')")
    con.commit()
    con.close()
    # The engine reads the serving commit through this stub mid-rebuild: it
    # stands in for the server writing in that window.
    stub = w.root / "scripts" / "deploy_code_only.sh"
    stub.write_text(
        "#!/bin/bash\n"
        f'python3 -c "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); '
        f'c.execute(\\"INSERT INTO t VALUES (\'during\')\\"); c.commit()" "{db}"\n'
        f'echo "serving: {w.rev("HEAD")}"\necho "server-state: active inactive"\n'
    )
    w.advance_main({"c.txt": "c\n"})
    # The early snapshot, as update.sh takes it before the rebuild.
    r = _bash(_rebuild_script(w, old_base, pre="_take_db_snapshot\n"), _env(w.home))
    assert r.returncode == 0, _out(r)
    assert _result(r)["SNAP"] == "1"
    assert "during" in _db_rows(db), "the stub never wrote; nothing was tested"
    for name in ("live.db.pre-update", "live.db.pre-update.peer-restore"):
        assert "during" in _db_rows(w.home / name), f"{name} predates the rebuild"


def test_f1_a_failed_refresh_after_the_move_says_live_moved(dc, dc_ready, capsys):
    w = dc_ready
    old_base = _live_world(dc, w, capsys)
    old_tip = w.rev("HEAD")
    con = sqlite3.connect(w.home / "live.db")
    con.execute("CREATE TABLE t (v TEXT)")
    con.commit()
    con.close()
    w.advance_main({"c.txt": "c\n"})
    refuse = "_PEER_RESTORE_PY='import sys; sys.exit(1)'\n"
    r = _bash(_rebuild_script(w, old_base, pre=refuse), _env(w.home))
    assert r.returncode == 1, _out(r)
    assert "RESTORE AUTHORITY CHECK FAILED" in r.stderr
    assert "live moved to" in r.stdout and "RESULT" not in r.stdout
    assert w.rev("refs/heads/live") != old_tip and _tag(w) == old_tip
    assert not _marker(w).exists()
    assert _no_state(w)


# ── F3: tier-2 against the activated tip ───────────────────────────────────


def _tier2(w, tmp_path: Path) -> int:
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True, exist_ok=True)
    if not (venv / "python").exists():
        (venv / "python").symlink_to(sys.executable)
    script = (
        f'GENESIS_ROOT="{w.root}"\nSCRIPT_DIR="$GENESIS_ROOT/scripts"\n'
        f'VENV_DIR="{tmp_path / "venv"}"\nON_LIVE=1\n'
        + _function("_metadata_python")
        + _block("tier2-baseline-check")
        + "if _tier2_pending_since_baseline; then echo PENDING; else echo CLEAN; fi\n"
    )
    r = _bash(script, _env(w.home))
    assert r.returncode == 0, _out(r)
    return "PENDING" in r.stdout


def _tier2_world(dc, w, capsys) -> tuple[str, str]:
    """`live` with a candidate touching pyproject.toml; the history's last success
    is the base, as update.sh records it on `live`. Returns (base, tip)."""
    base = _live_world(dc, w, capsys, files={"pyproject.toml": "[project]\nname = 'x'\n"})
    (w.root / "data").mkdir()
    con = sqlite3.connect(w.root / "data" / "genesis.db")
    con.execute(
        "CREATE TABLE update_history (id TEXT, new_commit TEXT, status TEXT, completed_at TEXT)"
    )
    con.execute(
        "INSERT INTO update_history VALUES ('x', ?, 'success', '2026-10-13T00:00:00+00:00')",
        (base[:9],),
    )
    con.commit()
    con.close()
    return base, w.rev("HEAD")


def test_f3_an_activated_tip_is_not_pending_again(dc, dc_ready, capsys, tmp_path):
    w = dc_ready
    _base, tip = _tier2_world(dc, w, capsys)
    w.git(w.root, "update-ref", "refs/genesis/live-activated", tip)
    assert not _tier2(w, tmp_path), "a candidate's tier-2 change reads pending forever"


def test_f3_with_no_activated_tip_it_falls_back_to_the_base(dc, dc_ready, capsys, tmp_path):
    w = dc_ready
    _tier2_world(dc, w, capsys)
    assert _tier2(w, tmp_path)


def test_f3_a_foreign_activated_ref_falls_back_to_the_base(dc, dc_ready, capsys, tmp_path):
    """A ref that is not a rebuild's tip (here the candidate's own commit) is not
    trusted, even though it holds the same tier-2 files as HEAD."""
    w = dc_ready
    _tier2_world(dc, w, capsys)
    w.git(w.root, "update-ref", "refs/genesis/live-activated", w.rev("refs/heads/feat/a"))
    assert _tier2(w, tmp_path)


@pytest.mark.parametrize("on_live", ["1", "0"])
def test_f3_the_success_path_records_the_activated_tip_on_live(plain_live, on_live):
    root, home, stub, env, log = plain_live
    head = _git(root, "rev-parse", "HEAD")
    script = f'GENESIS_ROOT="{root}"\nON_LIVE={on_live}\nUPDATE_OWN_HEAD="{head}"\n' + _block(
        "live-activated-record"
    )
    r = _bash(script, env)
    assert r.returncode == 0, _out(r)
    got = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "-q", "--verify", "refs/genesis/live-activated"],
        capture_output=True,
        text=True,
        env=_env(home),
    ).stdout.strip()
    assert got == (head if on_live == "1" else "")


def test_f3_the_record_follows_the_success_row():
    text = _text()
    success = text.index('_record_update_history "success" "" "$_p6_degraded"')
    record = text.index("# BEGIN live-activated-record")
    assert success < record < text.index('_write_state "done"', success)


# ── R3-A: what ran before the rebuild is restarted after it ────────────────

_FAKE_SERVICES = """#!/bin/bash
# systemctl / pgrep stand-in: "running" iff $FAKE_STATE_FILE holds the unit's name.
state="$(cat "$FAKE_STATE_FILE" 2>/dev/null || true)"
case "$(basename "$0")" in
    systemctl) unit="${@: -1}"; unit="${unit%.service}" ;;
    pgrep) unit=genesis-server ;;
esac
[[ " $state " == *" $unit "* ]]
"""


def _stop_detection() -> str:
    """update.sh's detection of what runs, just before the stop."""
    text = _text()
    start = text.index("\nWERE_RUNNING=()\n") + 1
    return text[start : text.index("# Now stop them.", start)]


def _services_env(tmp_path: Path, home: Path) -> tuple[dict[str, str], Path]:
    bin_ = tmp_path / "fakebin"
    bin_.mkdir(exist_ok=True)
    for name in ("systemctl", "pgrep"):
        (bin_ / name).write_text(_FAKE_SERVICES)
        (bin_ / name).chmod(0o755)
    state = tmp_path / "services"
    env = _env(home, FAKE_STATE_FILE=str(state))
    env["PATH"] = f"{bin_}{os.pathsep}{env['PATH']}"
    return env, state


def _were_running(plain_live, tmp_path, on_live: str, at_marker: str, at_stop: str) -> list[str]:
    """Run live-marker with services `at_marker` running, then the stop's
    detection with `at_stop` running; returns WERE_RUNNING."""
    root, home, stub, _env_unused, log = plain_live
    env, state = _services_env(tmp_path, home)
    script = (
        # Set where branch-admission sets it, never inherited.
        f'_LIVE_WERE_RUNNING=()\necho "{at_marker}" > "{state}"\n'
        + _marker_script(root, on_live, "false")
        + f'echo "{at_stop}" > "{state}"\n'
        + _stop_detection()
        + 'echo "WERE=${WERE_RUNNING[*]:-}"\n'
    )
    r = _bash(script, env)
    assert r.returncode == 0, _out(r)
    m = re.search(r"^WERE=(.*)$", r.stdout, re.MULTILINE)
    assert m, _out(r)
    return m.group(1).split()


def test_r3a_a_server_that_died_during_the_rebuild_is_still_restarted(plain_live, tmp_path):
    assert _were_running(plain_live, tmp_path, "1", "genesis-server", "") == ["genesis-server"]


def test_r3a_a_server_still_running_is_listed_once(plain_live, tmp_path):
    assert _were_running(plain_live, tmp_path, "1", "genesis-server", "genesis-server") == [
        "genesis-server"
    ]


def test_r3a_main_reads_only_the_stop(plain_live, tmp_path):
    """Off `live` nothing is sampled at the marker: the stop alone decides."""
    assert _were_running(plain_live, tmp_path, "0", "genesis-server", "") == []
    assert _were_running(plain_live, tmp_path, "0", "", "genesis-bridge") == ["genesis-bridge"]


def test_r3a_the_sample_precedes_the_marker():
    block = _block("live-marker")
    sample = block.index('_LIVE_WERE_RUNNING+=("genesis-server")')
    assert sample < block.index("_acquire_deploy_marker")
    assert '_LIVE_WERE_RUNNING+=("genesis-bridge")' in block[: sample + 400]
    # Never inherited: set where the other `live` variables are.
    assert "\n_LIVE_WERE_RUNNING=()\n" in _block("branch-admission")


# ── R3-B: the restore authority check comes from the reviewed base ─────────

_PEER_PATH = "src/genesis/db/crud/peer_restore.py"


def _live_shape(plain_live, base_holds: str | None, tip_holds: str):
    """`live` = a base (holding ``base_holds`` as peer_restore.py, or no such
    file) with a candidate commit on top that writes ``tip_holds`` there."""
    root, home, stub, env, log = plain_live
    if base_holds is not None:
        (root / _PEER_PATH).parent.mkdir(parents=True, exist_ok=True)
        (root / _PEER_PATH).write_text(base_holds)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / _PEER_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / _PEER_PATH).write_text(tip_holds)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "candidate")
    script = (
        f'GENESIS_ROOT="{root}"\nSCRIPT_DIR="{stub}"\nON_LIVE=1\n'
        f'_PEER_RESTORE_PY="$(cat "$GENESIS_ROOT/{_PEER_PATH}")"\n'
        'OLD_TAG=""\nOLD_COMMIT=""\nNEW_TAG=""\nNEW_COMMIT=""\nDEPLOY_HEAD=""\n'
        + _block("live-shape")
        + 'printf "PEER=%s\\n" "$_PEER_RESTORE_PY"\n'
    )
    return _bash(script, {**env, "STUB_BASE": base})


def test_r3b_the_check_is_the_base_copy_not_the_candidate(plain_live):
    r = _live_shape(plain_live, "print('base')", "print('candidate')")
    assert r.returncode == 0, _out(r)
    assert "PEER=print('base')\n" in r.stdout, r.stdout


def test_r3b_a_base_without_the_check_refuses(plain_live):
    r = _live_shape(plain_live, None, "print('candidate')")
    assert r.returncode == 1, _out(r)
    assert "cannot be read from the base live was rebuilt on" in r.stdout
    assert "Nothing was changed." in r.stdout and "PEER=" not in r.stdout


def test_r3b_an_empty_check_fails_the_snapshot_closed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    con = sqlite3.connect(home / "live.db")
    con.execute("CREATE TABLE t (v TEXT)")
    con.commit()
    con.close()
    script = (
        _snapshot_defs()
        + '_PEER_RESTORE_PY=""\n'
        + 'if _take_db_snapshot; then echo "SNAP=ok $DB_SNAPSHOT_TAKEN"; '
        + 'else echo "SNAP=refused $DB_SNAPSHOT_TAKEN"; fi\n'
    )
    r = _bash(script, _env(home))
    assert r.returncode == 0, _out(r)
    assert "SNAP=refused 0" in r.stdout, _out(r)
    assert "RESTORE AUTHORITY CHECK FAILED" in r.stderr


# ── R3-A: an interrupt during the rebuild restores what ran at the marker ────

# systemctl: `is-active` answers from FAKE_SERVER_ACTIVE (1 = active) and is not
# recorded; every other call is recorded. pgrep: finds a bare server iff FAKE_PGREP=1.
_RECORDING_SYSTEMCTL = """#!/bin/bash
for a in "$@"; do
    if [ "$a" = is-active ]; then [ "${FAKE_SERVER_ACTIVE:-0}" = 1 ]; exit; fi
done
echo "systemctl $*" >> "$CALLS"
exit 0
"""
_FAKE_PGREP = '#!/bin/bash\n[ "${FAKE_PGREP:-0}" = 1 ]\n'


def _prestop_restarts(
    tmp_path: Path, were: str, live_were: str, active: bool = False, bare: bool = False
) -> tuple[list[str], str]:
    """Run the real _on_signal_prestop with `WERE_RUNNING=(were)` and
    `_LIVE_WERE_RUNNING=(live_were)`. Returns the service calls it made (a
    `_start_genesis_server` call is a RESTART, recorded as such) and the
    `_LIVE_STOPPED` value it left for the moved-message."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in (("systemctl", _RECORDING_SYSTEMCTL), ("pgrep", _FAKE_PGREP)):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    calls = tmp_path / "calls"
    env = _env(
        tmp_path,
        CALLS=str(calls),
        FAKE_SERVER_ACTIVE="1" if active else "0",
        FAKE_PGREP="1" if bare else "0",
    )
    env["PATH"] = f"{bin_}{os.pathsep}{env['PATH']}"
    script = (
        f"WERE_RUNNING=({were})\n_LIVE_WERE_RUNNING=({live_were})\nON_LIVE=1\n"
        '_start_genesis_server() { echo "restart-server" >> "$CALLS"; }\n'
        '_live_abandon() { echo "LIVE_STOPPED=${_LIVE_STOPPED:-0}"; }\n'
        "_clear_deploy_state() { :; }\n"
        + _function("_on_signal_prestop")
        + "_on_signal_prestop TERM\n"
    )
    r = _bash(script, env)
    assert r.returncode == 1, _out(r)
    m = re.search(r"^LIVE_STOPPED=(\S*)$", r.stdout, re.MULTILINE)
    assert m, _out(r)
    return (calls.read_text().splitlines() if calls.exists() else []), m.group(1)


def test_r3a_an_interrupt_brings_back_a_server_that_died_during_the_rebuild(tmp_path):
    """The rebuild runs before WERE_RUNNING is read: what the marker sampled and
    is down now comes back, each once, and the moved-message says so."""
    got, stopped = _prestop_restarts(tmp_path, "", "genesis-server genesis-bridge")
    assert got == ["restart-server", "systemctl --user start genesis-bridge.service"], got
    assert stopped == "1"


def test_r3a_an_interrupt_never_bounces_a_running_server(tmp_path):
    """A server still running when the rebuild is interrupted gets no call: a
    restart would put it on the moved tree without activation."""
    got, stopped = _prestop_restarts(tmp_path, "", "genesis-server genesis-bridge", active=True)
    assert got == ["systemctl --user start genesis-bridge.service"], got
    assert stopped == "0"


def test_r3a_an_interrupt_leaves_a_bare_server_alone(tmp_path):
    """Not active per systemd, but a bare `genesis serve` runs: still running."""
    got, stopped = _prestop_restarts(tmp_path, "", "genesis-server", bare=True)
    assert got == [], got
    assert stopped == "0"


def test_r3a_an_interrupt_restarts_a_service_listed_twice_once(tmp_path):
    got, _stopped = _prestop_restarts(tmp_path, "genesis-server", "genesis-server")
    assert got == ["restart-server"], got


# ── R3-B: on `live` the backup gate is the reviewed base's ─────────────────

_PASS_GATE = 'print("ok")\nraise SystemExit(0)\n'


def _backup_region() -> str:
    text = _text()
    start = text.index("# ── Pre-update backup")
    return text[start : text.index("# ── Dirty-tree guard", start)]


def _gate_run(plain_live, on_live: str, base_gate: str | None) -> subprocess.CompletedProcess:
    """`live` whose base holds ``base_gate`` as backup_status_gate.py (or none),
    with a candidate on top whose copy always passes. No backup status file
    exists, so the real gate aborts and the candidate's passes."""
    root, home, stub, env, log = plain_live
    gate_path = root / "scripts" / "lib" / "backup_status_gate.py"
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    if base_gate is not None:
        gate_path.write_text(base_gate)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base gate")
    base = _git(root, "rev-parse", "HEAD")
    gate_path.write_text(_PASS_GATE)
    backup = root / "scripts" / "backup.sh"
    backup.write_text("#!/bin/bash\nexit 0\n")
    backup.chmod(0o755)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "candidate: a gate that always passes")
    script = (
        f'GENESIS_ROOT="{root}"\nON_LIVE={on_live}\n_LIVE_BASE="{base}"\n'
        + _backup_region()
        + 'echo "GATE-PASSED degraded=${PRE_UPDATE_DEGRADED}"\n'
    )
    return _bash(script, env)


def test_r3b_on_live_the_base_gate_decides(plain_live):
    real = (SCRIPTS / "lib" / "backup_status_gate.py").read_text()
    r = _gate_run(plain_live, "1", real)
    assert r.returncode == 1, _out(r)
    assert "DATABASE BACKUP NOT VERIFIED" in r.stderr
    assert "abort_db:status_missing_or_invalid" in r.stderr
    assert "GATE-PASSED" not in r.stdout


def test_r3b_on_live_a_base_without_the_gate_aborts(plain_live):
    r = _gate_run(plain_live, "1", None)
    assert r.returncode == 1, _out(r)
    assert "DATABASE BACKUP NOT VERIFIED" in r.stderr and "GATE-PASSED" not in r.stdout


def test_r3b_off_live_the_checkout_gate_still_decides(plain_live):
    """Control: main runs the checkout's own copy, as before."""
    real = (SCRIPTS / "lib" / "backup_status_gate.py").read_text()
    r = _gate_run(plain_live, "0", real)
    assert r.returncode == 0, _out(r)
    assert "GATE-PASSED" in r.stdout
