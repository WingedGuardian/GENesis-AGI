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


# ── the rebuild in the merge slot, against the real engine ──────────────────

_DOWN = (
    "serving: unknown (genesis-server is not running (inactive))\nserver-state: inactive inactive"
)


def _serving_stub(w, lines: str) -> None:
    stub = w.root / "scripts" / "deploy_code_only.sh"
    stub.write_text(
        "#!/bin/bash\n" + "".join(f"echo {json.dumps(ln)}\n" for ln in lines.splitlines())
    )


def _live_world(dc, w, capsys, files: dict | None = None) -> str:
    """`live` = origin/main + feat/a, rebuilt in-process; then the engine is
    installed into the checkout for update.sh to run, ready against the old base,
    with the server stopped (update.sh runs the rebuild after the stop).
    Returns the old base."""
    w.candidate("feat/a", files or {"src/a.py": "a = 1\n"})
    assert w.add(dc, "feat/a") == 0, capsys.readouterr()
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "live"
    old_base = w.rev("refs/remotes/origin/main")
    w.install_engine()
    gate = w.root / "scripts" / "deploy_candidates_gate.py"
    gate.write_text(gate.read_text() + f'\nREQUIRED_MERGED = (("scratch base", "{old_base}"),)\n')
    _serving_stub(w, _DOWN)
    w.git(w.root, "tag", TAG)
    return old_base


def _do_rollback_fn() -> str:
    """update.sh's _do_rollback, whole (its body holds a column-0 `}` in an
    embedded python program, so _function cannot cut it)."""
    text = _text()
    start = text.index("_do_rollback() {\n")
    end = text.index("\n}\n", text.index("# END rollback-state-clear", start)) + len("\n}\n")
    return text[start:end]


# What the merge slot's rollback reaches outside git, recorded in $CALLS.
_SERVICE_STUBS = (
    '_stop_genesis_server() { echo "stop-server" >> "$CALLS"; }\n'
    "_ensure_server_down() { return 0; }\n"
    '_start_genesis_server() { echo "restart-server" >> "$CALLS"; }\n'
    '_server_health_ok() { echo "health-probe" >> "$CALLS"; return 0; }\n'
    'systemctl() { echo "systemctl $*" >> "$CALLS"; }\n'
    '_record_update_history() { echo "history $1" >> "$CALLS"; }\n'
)


def _engine_script(root: Path, script_dir: Path, base: str, mu: str, pre: str = "") -> str:
    """update.sh from the armed traps through the live-engine block: the real
    _do_rollback, ERR and signal handlers, state rows and the live helpers; only
    the services are stubs."""
    return (
        f'GENESIS_ROOT="{root}"\nSCRIPT_DIR="{script_dir}"\n'
        f"POST_MERGE=false\nON_LIVE=1\nORIGINAL_BRANCH=live\nROLLBACK_TAG={TAG}\n"
        'VALIDATED_HEAD="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"\n'
        'UPDATE_OWN_HEAD="$VALIDATED_HEAD"\n'
        f'_LIVE_BASE="{base}"\nDEPLOY_HEAD="$_LIVE_BASE"\n'
        'OLD_TAG=untagged\nOLD_COMMIT=old\nNEW_TAG=""\nNEW_COMMIT=""\nSTARTED_AT=now\n'
        'MERGE_ATTEMPTED=0\nMIGRATIONS_RAN=0\nDB_FILE="$HOME/none.db"\n'
        'GENESIS_TAKE_TEMPLATES=""\nREFUSED_FILE="$HOME/.genesis/update_refused.json"\n'
        'STATE_FILE="$HOME/.genesis/update_state.json"\nWERE_RUNNING=(genesis-server)\n'
        '_STATE_WRITTEN=0\n_STATE_PHASE=""\n_LIVE_ENGINE_RAN=0\nCALLS="$HOME/calls"\n'
        'VENV_DIR="$HOME/venv"\nmkdir -p "$VENV_DIR/bin" "$HOME/.genesis/locks"\n'
        'printf \'#!/bin/sh\\nexit 0\\n\' > "$VENV_DIR/bin/pip"\nchmod +x "$VENV_DIR/bin/pip"\n'
        'exec {_UPDATE_LOCK_FD}>"$HOME/.genesis/locks/update.lock"\nflock -n "$_UPDATE_LOCK_FD"\n'
        + mu
        + "".join(
            re.search(
                rf"{name}\(\) \{{\n.*?\n\}}\n", _block("managed-units-preflight"), re.DOTALL
            ).group(0)
            for name in ("_mu_record_refusal", "_mu_remedy")
        )
        + _function("_write_state")
        + _function("_clear_deploy_marker")
        + _block("live-helpers")
        + _SERVICE_STUBS
        + _do_rollback_fn()
        + _function("_on_err")
        + _function("_on_signal")
        + "trap _on_err ERR\ntrap '_on_signal INT' INT\ntrap '_on_signal TERM' TERM\n"
        + '_write_state "merging"\n'
        + pre
        + _block("live-engine")
        + 'echo "RESULT OWN=$UPDATE_OWN_HEAD DEPLOY=$DEPLOY_HEAD NEW=$NEW_COMMIT"\n'
    )


def _rebuild_script(w, old_base: str, mu_src: str | None = None) -> str:
    mu = f'_mu_src="$(cat "{CHECKER}")"\n' if mu_src is None else _assign("_mu_src", mu_src)
    return _engine_script(w.root, w.root / "scripts", old_base, mu)


def _state(home: Path) -> dict | None:
    path = home / ".genesis" / "update_state.json"
    return json.loads(path.read_text()) if path.exists() else None


def _calls(home: Path) -> list[str]:
    path = home / "calls"
    return path.read_text().splitlines() if path.exists() else []


def _tag(w) -> str | None:
    r = w.git(w.root, "rev-parse", "-q", "--verify", f"refs/tags/{TAG}", check=False)
    return r.stdout.strip() or None


def _assert_rolled_back_onto(home: Path, head: str, want: str) -> None:
    """The normal rollback ran and put the old tip back: the server restarted on
    it, the history row says so, and the state row went with the move."""
    assert head == want
    calls = _calls(home)
    assert "restart-server" in calls and "history rolled_back" in calls, calls
    assert _state(home) is None, "the rollback kept a state row for a checkout back at its tag"


def test_u1_update_on_live_rebuilds_after_the_stop_onto_new_main(dc, dc_ready, capsys):
    """#2526, #3370: the rebuild runs in the merge's place, with the server
    stopped, puts the candidate back on top of the NEW main, and records the
    rebuilt tip as this run's own in the "merging" row."""
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
    state = _state(w.home)
    assert state and state["phase"] == "merging" and state["own_head"] == tip, state
    assert state["deploy_head"] == new_main
    assert _tag(w) == old_tip
    assert _calls(w.home) == [], "a successful rebuild touched the services"


def test_u1_a_server_not_definitely_down_refuses_and_rolls_back(dc, dc_ready, capsys):
    """A unit in any state but stopped is not "down": readiness refuses, and the
    refusal, after the stop, rolls back like a failed merge."""
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
    assert "the live rebuild stopped" in r.stdout
    _assert_rolled_back_onto(w.home, w.rev("HEAD"), old_tip)


def test_u2_a_readiness_refusal_rolls_back_onto_the_old_tip(dc, dc_ready, capsys):
    w = dc_ready
    old_base = _live_world(dc, w, capsys)
    old_tip = w.rev("HEAD")
    new_main = w.advance_main({"c.txt": "c\n"})
    # A required fix the stopped server's checkout lacks.
    w.git(w.root, "fetch", "-q", "origin")
    gate = w.root / "scripts" / "deploy_candidates_gate.py"
    gate.write_text(gate.read_text() + f'\nREQUIRED_MERGED = (("newer fix", "{new_main}"),)\n')
    r = _bash(_rebuild_script(w, old_base), _env(w.home))
    assert r.returncode == 1, _out(r)
    assert "not ready" in _out(r) and "UPDATE FAILED" in r.stdout
    _assert_rolled_back_onto(w.home, w.rev("HEAD"), old_tip)
    assert w.git(w.root, "status", "--porcelain", "--untracked-files=no").stdout == ""
    assert _tag(w) == old_tip, "the rollback deleted the tag it rolled back to"


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


def test_u6_a_unit_refusal_after_the_move_rolls_back_and_exits_4(dc, dc_ready, capsys):
    """The units only the rebuilt tip brings are judged after the move, which is
    after the stop: a refusal rolls the rebuild back (old tip, its hooks, the
    services restarted on it) and still exits 4 with the record."""
    w = dc_ready
    old_base = _live_world(dc, w, capsys)
    old_tip = w.rev("HEAD")
    w.advance_main({"c.txt": "c\n"})
    refuse = 'import sys\nprint("REFUSED: demo.service (edited by hand)")\nsys.exit(4)\n'
    r = _bash(_rebuild_script(w, old_base, mu_src=refuse), _env(w.home))
    assert r.returncode == 4, _out(r)
    record = json.loads((w.home / ".genesis" / "update_refused.json").read_text())
    assert record["units"] == ["demo.service"] and record["deploy_head"] != old_tip
    assert "--take-template" in r.stdout and "override.conf" in r.stdout, r.stdout
    _assert_rolled_back_onto(w.home, w.rev("HEAD"), old_tip)
    assert w.rev("refs/heads/live") == old_tip


def test_u6_a_unit_refusal_whose_rollback_does_not_complete_exits_1(dc, dc_ready, capsys):
    """Exit 4 promises the old tip is back. A hand edit to a file only the rebuilt
    tip has makes the non-forced rollback checkout refuse, so the rollback is
    incomplete and the run exits 1, not 4."""
    w = dc_ready
    old_base = _live_world(dc, w, capsys)
    w.advance_main({"c.txt": "c\n"})
    refuse = (
        "import sys\n"
        "root = sys.argv[sys.argv.index('--repo') + 1]\n"
        "open(root + '/c.txt', 'a').write('hand edit\\n')\n"
        "print('REFUSED: demo.service (edited by hand)')\n"
        "sys.exit(4)\n"
    )
    r = _bash(_rebuild_script(w, old_base, mu_src=refuse), _env(w.home))
    assert r.returncode == 1, _out(r)
    assert "ROLLBACK INCOMPLETE" in r.stdout, r.stdout
    assert not (w.home / ".genesis" / "update_refused.json").exists(), (
        "exit 1 left the refusal record that means exit 4"
    )


# ── the rebuild in the merge slot, against a stub engine ────────────────────

_STUB_ENGINE = """\
import fcntl, json, os, signal, subprocess, sys
mode = os.environ.get("STUB_MODE", "")
root = os.environ["STUB_ROOT"]


def record(to, phase):
    # The engine's move record, as deploy_candidates_plan.py writes it.
    path = os.environ.get("DEPLOY_CANDIDATES_MOVE_RECORD")
    if path:
        with open(path, "w") as fh:
            fh.write(json.dumps({"to": to, "phase": phase}) if phase != "garbage" else "{")


def head():
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def checkout_lock():
    fd = os.open(os.path.join(root, ".git", "genesis-checkout.lock"), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return "free"
    except BlockingIOError:
        return "held"
    finally:
        os.close(fd)


with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + " lockfd=" + os.environ.get("DEPLOY_CANDIDATES_LOCK_FD", "-")
             + " re=" + ("yes" if os.environ.get("DEPLOY_CANDIDATES_EPHEMERAL_RE") else "no")
             + " own=" + os.environ.get("DEPLOY_CANDIDATES_OWN_STATE_PID", "-")
             + " ppid=" + str(os.getppid()) + " checkout=" + checkout_lock() + "\\n")
if sys.argv[1] == "rebuild":
    if mode in ("move", "move-fail", "move-signal", "foreign", "foreign-success",
                "unrecorded-success", "move-ref", "move-moving"):
        subprocess.run(["git", "-C", root, "-c", "core.hooksPath=/dev/null", "commit", "-q",
                        "--allow-empty", "-m", "moved"], check=True)
        # move-signal: killed between the switch and its "done" (the record says
        # "moving", HEAD is at its target). foreign: the record names another commit.
        if mode != "unrecorded-success":
            record("f" * 40 if mode in ("foreign", "foreign-success") else head(),
                   "moving" if mode in ("move-signal", "move-moving") else
                   "ref" if mode == "move-ref" else "done")
    if mode in ("partial", "garbage", "interrupted"):
        # git wrote part of the new tree (an untracked file) and failed, or was
        # killed mid-switch (the record still says "moving"); HEAD stayed.
        open(os.path.join(root, "half-written.py"), "w").write("x = 1\\n")
        record("e" * 40, "moving" if mode == "interrupted" else mode)
    if mode == "move-signal":
        os.kill(os.getppid(), signal.SIGTERM)
    sys.exit(0 if mode in ("", "move", "foreign-success", "unrecorded-success",
                          "move-ref", "move-moving") else 1)
if sys.argv[1] == "restore-hooks" and os.environ.get("STUB_HOOKS_FAIL") == "1":
    sys.exit(1)
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


def _plain_script(root: Path, stub: Path, mu_src: str = "import sys\n", pre: str = "") -> str:
    return _engine_script(root, stub, "$VALIDATED_HEAD", _assign("_mu_src", mu_src), pre)


def _move_record(home: Path) -> Path:
    return home / ".genesis" / "live_move.json"


def _engine_calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_the_engine_gets_the_update_lock_its_own_state_pid_and_a_free_checkout_lock(plain_live):
    """update.lock is handed over, the "merging" row is named as the caller's,
    and genesis-checkout.lock is NOT held: the engine's move takes it itself."""
    root, home, stub, env, log = plain_live
    r = _bash(_plain_script(root, stub), {**env, "STUB_MODE": "move"})
    assert r.returncode == 0, _out(r)
    first = _engine_calls(log)[0]
    fields = dict(kv.split("=", 1) for kv in first.split() if "=" in kv)
    assert first.startswith("rebuild ") and fields["lockfd"] != "-" and fields["re"] == "yes"
    assert fields["own"] == fields["ppid"], "the engine was not told the row is its caller's"
    assert fields["checkout"] == "free", "update.sh held genesis-checkout.lock across the rebuild"
    state = _state(home)
    assert state["phase"] == "merging" and state["pid"] == int(fields["own"])
    assert state["own_head"] == _git(root, "rev-parse", "HEAD") != state["rollback_tag"]
    assert not _move_record(home).exists(), "the adopted move's record outlived the adoption"


def test_the_engine_is_told_where_update_sh_left_the_checkout(plain_live):
    """#3492: the engine re-checks the branch and head this run verified, under
    its own checkout lock, instead of trusting what it reads after its fetch."""
    root, home, stub, env, log = plain_live
    r = _bash(_plain_script(root, stub), {**env, "STUB_MODE": "move"})
    assert r.returncode == 0, _out(r)
    own = _git(root, "rev-parse", TAG)
    first = _engine_calls(log)[0]
    assert first.startswith(f"rebuild --expect-branch live --expect-head {own} "), first


def test_p2_an_interrupt_during_the_rebuild_rolls_back_onto_the_old_tip(plain_live):
    """#3370 P2 #1: an interrupt that lands while the engine runs (after the
    stop, after its move) goes through the normal rollback: the old tip comes
    back with its hooks, and the server is restarted on it, never on the moved
    tree."""
    root, home, stub, env, log = plain_live
    old = _git(root, "rev-parse", "HEAD")
    r = _bash(_plain_script(root, stub), {**env, "STUB_MODE": "move-signal"})
    assert r.returncode == 1, _out(r)
    assert "interrupted by SIGTERM" in r.stderr and "RESULT" not in r.stdout
    restores = [c.split() for c in _engine_calls(log) if c.startswith("restore-hooks ")]
    assert len(restores) == 1 and restores[0][2] == TAG, _engine_calls(log)
    moved = restores[0][1]
    assert moved != old and _git(root, "cat-file", "-t", moved) == "commit"
    _assert_rolled_back_onto(home, _git(root, "rev-parse", "HEAD"), old)


def test_a_move_then_failure_rolls_back_onto_the_old_tip(plain_live):
    root, home, stub, env, log = plain_live
    old = _git(root, "rev-parse", "HEAD")
    r = _bash(_plain_script(root, stub), {**env, "STUB_MODE": "move-fail"})
    assert r.returncode == 1, _out(r)
    assert "rolling that back" in r.stdout
    _assert_rolled_back_onto(home, _git(root, "rev-parse", "HEAD"), old)
    assert any(c.startswith("restore-hooks ") for c in _engine_calls(log))


def test_an_unmoved_refusal_restarts_the_old_tip_and_keeps_the_tag(plain_live):
    """The normal rollback, not the old pre-stop abandon: the tag stays (as
    after any rollback), the services restart, no hook restore is needed."""
    root, home, stub, env, log = plain_live
    old = _git(root, "rev-parse", "HEAD")
    r = _bash(_plain_script(root, stub), {**env, "STUB_MODE": "fail"})
    assert r.returncode == 1, _out(r)
    _assert_rolled_back_onto(home, _git(root, "rev-parse", "HEAD"), old)
    assert _git(root, "tag", "-l", TAG) == TAG
    assert not any(c.startswith("restore-hooks ") for c in _engine_calls(log))


@pytest.mark.parametrize("mode", ["foreign", "foreign-success", "unrecorded-success"])
def test_a_moved_tip_the_record_does_not_name_is_never_adopted(plain_live, mode):
    """Codex P2 (exact ownership): HEAD moved, but not to the commit the engine's
    record names. It is someone else's move: the code is not reset to the tag,
    nothing restarts on it, and the restart guard stays."""
    root, home, stub, env, log = plain_live
    old = _git(root, "rev-parse", "HEAD")
    r = _bash(_plain_script(root, stub), {**env, "STUB_MODE": mode})
    assert r.returncode == 1, _out(r)
    assert _git(root, "rev-parse", "HEAD") != old, "the rollback reset a move it does not own"
    assert "rolling that back" not in r.stdout
    assert "restart-server" not in _calls(home)
    assert not any(c.startswith("restore-hooks ") for c in _engine_calls(log))
    assert _state(home) is not None, "the restart guard went while moved code stays"
    assert not _move_record(home).exists()


@pytest.mark.parametrize("mode", ["", "move-ref", "move-moving"])
def test_success_adopts_only_the_unchanged_or_recorded_tip(plain_live, mode):
    """No-op success has no receipt; moved success may retain a pre-move phase
    if the final receipt write failed. Neither loses exact ownership."""
    root, home, stub, env, log = plain_live
    old = _git(root, "rev-parse", "HEAD")
    r = _bash(_plain_script(root, stub, _REFUSE_UNITS), {**env, "STUB_MODE": mode})
    assert r.returncode == 4, _out(r)
    _assert_rolled_back_onto(home, _git(root, "rev-parse", "HEAD"), old)
    assert any(c.startswith("restore-hooks ") for c in _engine_calls(log)) is bool(mode)
    assert not _move_record(home).exists()


@pytest.mark.parametrize("mode", ["partial", "garbage", "interrupted"])
def test_a_partly_written_tree_is_never_restarted(plain_live, mode):
    """Codex P2: git wrote part of the new tree (here an untracked file, which
    no tracked-dirt check sees) and failed with HEAD unmoved; or it was killed
    mid-switch (record "moving", HEAD unmoved); or the record cannot be read. The rollback restarts nothing and says the tree may be mixed."""
    root, home, stub, env, log = plain_live
    old = _git(root, "rev-parse", "HEAD")
    r = _bash(_plain_script(root, stub), {**env, "STUB_MODE": mode})
    assert r.returncode == 1, _out(r)
    assert _git(root, "rev-parse", "HEAD") == old
    assert "restart-server" not in _calls(home), "services restarted on a mixed tree"
    assert "ROLLBACK INCOMPLETE" in r.stdout and "mixed working tree" in r.stdout, r.stdout
    assert _state(home) is not None


_REFUSE_UNITS = 'import sys\nprint("REFUSED: demo.service (edited by hand)")\nsys.exit(4)\n'


@pytest.mark.parametrize(
    ("hooks_fail", "pre", "rc"),
    [
        ("0", "", 4),
        ("1", "", 1),
        ("0", '_start_genesis_server() { echo "restart-server" >> "$CALLS"; return 1; }\n', 1),
        ("0", "_server_health_ok() { return 1; }\n", 1),
    ],
    ids=["clean", "hook-restore-fails", "restart-fails", "restarted-but-unhealthy"],
)
def test_exit_4_needs_a_rollback_that_completed(plain_live, hooks_fail, pre, rc):
    """Codex P2 (F5, F6): exit 4 promises the old tip is back with its hooks and
    its services; a failed hook restore or service restart exits 1 instead, and
    only an exit 4 leaves the refusal record the dashboard reads."""
    root, home, stub, env, log = plain_live
    r = _bash(
        _plain_script(root, stub, _REFUSE_UNITS, pre),
        {**env, "STUB_MODE": "move", "STUB_HOOKS_FAIL": hooks_fail},
    )
    assert r.returncode == rc, _out(r)
    assert any(c.startswith("restore-hooks ") for c in _engine_calls(log))
    refused = home / ".genesis" / "update_refused.json"
    assert refused.exists() is (rc == 4), "a refusal record without exit 4 (or the reverse)"
    # One verdict for every report: a rollback whose hooks or services did not
    # come back is recorded as incomplete, not "rolled_back" (Devin/CodeRabbit).
    complete = hooks_fail == "0" and "_start_genesis_server" not in pre
    calls = _calls(home)
    assert ("history rolled_back" in calls) is complete, calls
    assert ("history failed" in calls) is (not complete), calls
    failure = json.loads((home / ".genesis" / "last_update_failure.json").read_text())
    assert failure["rollback_complete"] is complete, failure


def test_u5_order_snapshot_stop_merging_engine_bootstrap():
    """One snapshot, before the stop; the rebuild after the stop, under the armed
    rollback trap and the "merging" row; the same pre-stop "fetching" row as main;
    no deploy marker on the CLI path."""
    text = _text()
    snap = text.index("_take_db_snapshot || exit 1")
    stop = text.index('echo "--- Stopping services for update ---"')
    arm = text.index("trap '_on_signal TERM' TERM\n")
    merging = text.index('_write_state "merging"', arm)
    engine = text.index("# BEGIN live-engine")
    assert snap < stop < arm < merging < engine < text.index('_write_state "bootstrap"')
    assert text.count("_take_db_snapshot") == 2, "a second snapshot call came back"
    assert "_acquire_deploy_marker" not in text and "_LIVE_WERE_RUNNING" not in text
    assert '\n_write_state "fetching"\n' in text
    block = _block("live-engine")
    lock = block.index('genesis_checkout_lock "$GENESIS_ROOT"')
    clear = block.index('checkout -q HEAD -- "$_eph"')
    unlock = block.index("genesis_checkout_unlock")
    rebuild = block.index('"$SCRIPT_DIR/deploy_candidates.py" rebuild')
    recheck = block.index('--upto "$_live_tip" --also "$ROLLBACK_TAG"')
    assert lock < clear < unlock < rebuild < recheck
    assert 'DEPLOY_CANDIDATES_OWN_STATE_PID="$$"' in block
    # --post-merge never runs the engine: the block sits in the plain run's merge.
    plain = text.index('if [[ "$POST_MERGE" == "false" ]]; then\n_write_state "merging"\n')
    assert plain < engine < text.index('fi  # end: [[ "$POST_MERGE" == "false" ]]', plain)
    # The checker that runs on `live` is the old base's, read from git.
    assert 'git -C "$GENESIS_ROOT" show "$_LIVE_BASE:scripts/lib/managed_units.py"' in _block(
        "managed-units-preflight"
    )


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
            + _block("live-helpers")
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
        assert line.startswith(f"restore-hooks {tip} {TAG} lockfd=") and " re=yes " in line, line
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


# ── the prestop handler ─────────────────────────────────────────────────────


def _prestop(root: Path, on_live: str, before: str = "", *, written: bool = False) -> str:
    return (
        f'GENESIS_ROOT="{root}"\nPOST_MERGE=false\nON_LIVE={on_live}\nORIGINAL_BRANCH=live\n'
        f'ROLLBACK_TAG={TAG}\nSTATE_FILE="$HOME/.genesis/update_state.json"\nWERE_RUNNING=()\n'
        'VALIDATED_HEAD="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"\n'
        + ("_STATE_WRITTEN=1\n" if written else "")
        + _function("_clear_deploy_marker")
        + _function("_clear_deploy_state")
        + _function("_on_signal_prestop")
        + before
        + "_on_signal_prestop TERM\n"
    )


def _other_runs_state(home: Path) -> Path:
    state = home / ".genesis" / "update_state.json"
    state.write_text(json.dumps({"phase": "merging", "pid": 999999}))
    return state


@pytest.mark.parametrize("on_live", ["1", "0"])
def test_u12_a_prestop_interrupt_clears_its_own_state_on_both_branches(plain_live, on_live):
    """Before the stop nothing has moved on `live` either (the rebuild runs after
    it), so the handler is main's: its own "fetching" row goes, the tag stays for
    the caller to drop, and nothing describes a move."""
    root, home, stub, env, log = plain_live
    state = _other_runs_state(home)
    r = _bash(_prestop(root, on_live, written=True), env)
    assert r.returncode == 1, _out(r)
    assert not state.exists()
    assert "live moved" not in r.stdout and "live was not moved" not in r.stdout


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


# ── a stop that fails, on `live` ────────────────────────────────────────────


def _snapshot_to_stop() -> str:
    """update.sh from its one snapshot call through the stop."""
    text = _text()
    start = text.index("_take_db_snapshot || exit 1\n")
    return text[start : text.index("# Restart the tmp watchgod", start)]


def test_p2_a_failed_stop_on_live_has_moved_nothing(plain_live):
    """#3370 P2 #2: a server that cannot be stopped aborts the update before
    anything moved on `live` (the rebuild runs after the stop), so main's own
    message is the whole story: the checkout is still at the tag, untouched, and
    the engine never ran."""
    root, home, stub, env, log = plain_live
    old = _git(root, "rev-parse", "HEAD")
    script = (
        _plain_script(root, stub).split("trap _on_err ERR")[0]
        + "_take_db_snapshot() { :; }\n"
        + "_guardian_pause() { :; }\n_guardian_resume() { :; }\n"
        + "_drop_own_prestop_state() { :; }\n_flush_deploy_summary() { :; }\n"
        + '_SELF_COPY="$HOME/self-copy"\n'
        + 'systemctl() { [ "$2" = is-active ] && [ "${@: -1}" = genesis-server.service ]; }\n'
        + "_ensure_server_down() { return 1; }\n"
        + _snapshot_to_stop()
        + 'echo "NOT REACHED"\n'
    )
    r = _bash(script, env)
    assert r.returncode == 1, _out(r)
    assert "could not stop genesis-server" in r.stdout and "NOT REACHED" not in r.stdout
    assert _git(root, "rev-parse", "HEAD") == old
    assert _git(root, "status", "--porcelain", "--untracked-files=no") == ""
    assert _engine_calls(log) == [], "the rebuild ran before the stop"


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
