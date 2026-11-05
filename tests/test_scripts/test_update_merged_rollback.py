"""A failed update runs the merged rollback (#3844), and anything wrong with it falls back.

Runs the shipped blocks of scripts/update.sh and the shipped libs against a scratch
checkout. The rollback's real service helpers can reach a running server
(`pkill -f "python -m genesis serve"`), and loading a staged lib replaces any stub
defined before it, so the stubs here sit at the END of each lib copy, and pkill,
pgrep and systemctl are logging shims first on PATH as well.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts._update_source import REPO_ROOT, ROLLBACK_LIB, UPDATE_SH
from tests.test_scripts.test_update_rollback_lib import _top_level_code

LIB_DIR = REPO_ROOT / "scripts" / "lib"
TAG = "pre-update-test"
SHIMMED = ("systemctl", "pkill", "pgrep")

_SERVICE_STUBS = """
_stop_genesis_server() { echo stop-server >> "$CALLS"; }
_ensure_server_down() { return 0; }
_start_genesis_server() { echo restart-server >> "$CALLS"; }
_record_update_history() { echo "history $1" >> "$CALLS"; }
"""


def _block(name: str) -> str:
    m = re.search(rf"# BEGIN {name}[^\n]*\n(.*?)# END {name}", UPDATE_SH.read_text(), re.DOTALL)
    assert m, name
    return m.group(1)


def _function(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", UPDATE_SH.read_text(), re.DOTALL | re.MULTILINE)
    assert m, name
    return m.group(0)


def _exit_trap() -> str:
    """The cleanup update.sh's EXIT traps run, as shipped."""
    clean = '[ -z "${_STAGED_ROLLBACK:-}" ] || rm -rf -- "$_STAGED_ROLLBACK" 2>/dev/null'
    assert clean in UPDATE_SH.read_text()
    return f"trap '{clean}' EXIT\n"


def _tag_body(text: str, marker: str) -> str:
    """A lib copy that says which rollback ran, what it sees, and has stubbed services."""
    head = "_rollback_body() {\n"
    assert text.count(head) == 1
    say = (
        f'    echo "BODY={marker} DIR=$SCRIPT_DIR BACKUP=$EPHEMERAL_BACKUP_ROOT"\n'
        '    echo "BODY-OPTS=$-"\n'
    )
    return text.replace(head, head + say) + _SERVICE_STUBS


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        (self.home / ".genesis").mkdir(parents=True)
        (self.home / "tmp").mkdir()
        self.calls = tmp / "calls"
        self.calls.touch()
        self.root = tmp / "checkout"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name in SHIMMED:
            shim = self.bin / name
            rc = "1" if name == "pgrep" else "0"
            shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\nexit {rc}\n')
            shim.chmod(0o755)
        self.venv = tmp / "venv"
        (self.venv / "bin").mkdir(parents=True)
        for name in ("python", "pip"):
            tool = self.venv / "bin" / name
            tool.write_text("#!/bin/sh\nexit 0\n")
            tool.chmod(0o755)
        # The libs this run launched with: outside the checkout, as in memory.
        self.launch = tmp / "launch" / "scripts"
        shutil.copytree(LIB_DIR, self.launch / "lib")
        lib = self.launch / "lib" / "update_rollback.sh"
        lib.write_text(_tag_body(lib.read_text(), "launched"))
        self._git_init()

    def git(self, *args: str) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def _git_init(self) -> None:
        shutil.copytree(self.launch / "lib", self.root / "scripts" / "lib")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "old")
        self.git("tag", TAG)
        self.old = self.git("rev-parse", "HEAD")

    def merge(self, edit=None) -> str:
        """The update's merge: scripts/lib as the merged tree has it."""
        lib = self.root / "scripts" / "lib" / "update_rollback.sh"
        lib.write_text(_tag_body(ROLLBACK_LIB.read_text(), "merged"))
        if edit:
            edit(lib)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "merged", "--allow-empty")
        self.head = self.git("rev-parse", "HEAD")
        return self.head

    def script(self, *, then: str, validated: str | None = None, deploy: str | None = None) -> str:
        libs = "".join(
            f'. "{self.launch}/lib/{name}"\n'
            for name in (
                "checkout_lock.sh",
                "deploy_marker.sh",
                "deploy_checkout.sh",
                "deploy_recovery.sh",
                "venv_setup.sh",
                "update_rollback.sh",
            )
        )
        return (
            "set -Eeuo pipefail\n"
            f'GENESIS_ROOT="{self.root}"\nSCRIPT_DIR="{self.launch}"\n'
            f'CALLS="{self.calls}"\nVENV_DIR="{self.venv}"\n'
            f"ORIGINAL_BRANCH=main\nROLLBACK_TAG={TAG}\nOLD_TAG=old\nOLD_COMMIT={self.old[:9]}\n"
            f"NEW_TAG=new\nNEW_COMMIT=new\nSTARTED_AT=now\nPOST_MERGE=false\nON_LIVE=0\n"
            f'STATE_FILE="{self.home}/.genesis/update_state.json"\n'
            f'DB_FILE="{self.tmp}/none.db"\nDB_RESTORE_OWED=0\nDB_SNAPSHOT_TAKEN=0\n'
            f'DEPS_STATE_TAKEN=0\nDEPS_STATE_FILE="{self.home}/.genesis/update-deps/{TAG}.json"\n'
            'MERGE_ATTEMPTED=1\nPRE_UPDATE_DEGRADED=""\nWERE_RUNNING=(genesis-server)\n'
            '_CHECKOUT_LOCK_BUSY=0\n_SERVER_DIRECT_PID=""\n_LIVE_ENGINE_RAN=0\n_LIVE_TREE_SUSPECT=0\n'
            '_LIVE_CHECKOUT_PY=""\n_metadata_python() { return 1; }\n'
            f'exec {{_UPDATE_LOCK_FD}}>"{self.tmp}/update.lock"\n'
            f"UPDATE_REMOTE=origin\nDEPLOY_BRANCH=main\n"
            f"VALIDATED_HEAD={validated or self.old}\nUPDATE_OWN_HEAD={self.head}\n"
            f"_merged_head={self.head}\nDEPLOY_HEAD={deploy or self.head}\n"
            + libs
            + 'EPHEMERAL_BACKUP_ROOT="$HOME/launched-backup-root"\n'
            + _block("merged-rollback")
            + _function("_on_err")
            + _function("_on_signal")
            + _exit_trap()
            + "trap _on_err ERR\ntrap '_on_signal INT' INT\ntrap '_on_signal TERM' TERM\n"
            + then
        )

    def run(self, script: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.tmp / "no-bus"),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.tmp}/no-bus/bus",
        }
        return subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=120
        )

    def calls_text(self) -> str:
        return self.calls.read_text()

    def source_record(self) -> dict:
        return json.loads((self.home / ".genesis" / "last_rollback_source.json").read_text())


STAGE_AND_ARM = "_stage_merged_rollback\n" + "{arm}" + 'echo "ARMED=$_ROLLBACK_ARMED"\n'


def _flow(world: World, *, after: str = "false\n", **kw) -> subprocess.CompletedProcess:
    then = STAGE_AND_ARM.replace("{arm}", _block("arm-merged-rollback")) + after
    return world.run(world.script(then=then, **kw))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _out(r) -> str:
    return r.stdout + r.stderr


# ── The merged rollback runs ─────────────────────────────


def test_a_failure_after_the_merge_runs_the_merged_rollback_and_finishes(world):
    world.merge()
    r = _flow(world)
    assert r.returncode == 1, _out(r)
    assert "ARMED=1" in r.stdout
    assert "BODY=merged" in r.stdout and "BODY=launched" not in r.stdout, _out(r)
    assert world.git("rev-parse", "HEAD") == world.old, "the checkout must be back at the tag"
    calls = world.calls_text()
    assert "restart-server" in calls and "history rolled_back" in calls, calls
    assert world.source_record()["source"] == "merged"
    assert world.source_record()["target"] == world.head


def test_the_merged_rollback_reaches_the_merged_helpers_after_the_tree_moves_back(world):
    world.merge()
    r = _flow(world)
    m = re.search(r"BODY=merged DIR=(\S+)", r.stdout)
    assert m, _out(r)
    staged = Path(m.group(1))
    assert "genesis-rollback-stage." in str(staged)
    assert staged.name == "scripts"


def test_a_file_only_the_merged_tree_has_is_reachable_once_the_checkout_is_back(world):
    only = world.root / "scripts" / "lib" / "merged_only.txt"
    only.write_text("from the merge\n")

    def edit(lib: Path) -> None:
        # The history record runs after the checkout restore.
        lib.write_text(
            lib.read_text()
            + '_record_update_history() { echo "late-read=$(cat "$SCRIPT_DIR/lib/merged_only.txt")" >> "$CALLS"; }\n'
        )

    world.merge(edit)
    r = _flow(world)
    assert world.git("rev-parse", "HEAD") == world.old, _out(r)
    assert not only.exists(), "the old tree does not have it"
    assert "late-read=from the merge" in world.calls_text(), _out(r)


def test_it_runs_once_entered_through_the_error_trap(world):
    world.merge()
    r = _flow(world)
    assert r.stdout.count("UPDATE FAILED") == 1, _out(r)


def test_a_signal_after_arming_reaches_the_merged_rollback(world):
    world.merge()
    r = _flow(world, after="kill -TERM $$\nsleep 5\n")
    assert r.returncode == 1, _out(r)
    assert "interrupted by SIGTERM" in r.stdout and "BODY=merged" in r.stdout, _out(r)
    assert world.git("rev-parse", "HEAD") == world.old


def test_the_backup_root_this_run_used_survives_the_reload(world):
    world.merge()
    r = _flow(world)
    assert f"BACKUP={world.home}/launched-backup-root" in r.stdout, _out(r)


def test_shell_options_and_script_dir_come_back_for_the_exit_trap(world):
    world.merge()
    r = _flow(world, after='_do_rollback "asked"\necho "AFTER=$- DIR=$SCRIPT_DIR"\n')
    m = re.search(r"AFTER=(\S+) DIR=(\S+)", r.stdout)
    assert m, _out(r)
    assert "e" in m.group(1) and "u" in m.group(1)
    assert m.group(2) == str(world.launch)
    body_opts = re.search(r"BODY-OPTS=(\S*)", r.stdout)
    assert "e" not in body_opts.group(1) and "u" not in body_opts.group(1)


def test_the_held_checkout_lock_is_kept_then_released_once(world):
    world.merge()
    r = _flow(
        world,
        after=(
            'genesis_checkout_lock "$GENESIS_ROOT"\n'
            'echo "HELD=$GENESIS_CHECKOUT_LOCK_FD"\n'
            '_do_rollback "asked"\n'
            'echo "AFTER-LOCK=${GENESIS_CHECKOUT_LOCK_FD:-released}"\n'
        ),
    )
    assert "HELD=" in r.stdout, _out(r)
    assert "AFTER-LOCK=released" in r.stdout, _out(r)
    assert world.git("rev-parse", "HEAD") == world.old, _out(r)


# ── A fault in the merged code costs a step, not the rollback ──


def test_a_failure_while_entering_the_merged_rollback_does_not_enter_it_twice(world):
    """Between rollback entry and the body's own disarm. Called directly, as several
    update.sh failure paths do with the traps still armed: an armed ERR trap would
    start a second rollback from inside the first. (Through _on_err this cannot show:
    bash does not fire ERR inside its own handler.)"""

    def edit(lib: Path) -> None:
        head = "update_rollback_enter() {\n"
        assert lib.read_text().count(head) == 1
        lib.write_text(lib.read_text().replace(head, head + "    false\n"))

    world.merge(edit)
    r = _flow(world, after='_do_rollback "a direct call"\nexit 1\n')
    assert r.stdout.count("Using the rollback from") == 1, _out(r)
    assert r.stdout.count("UPDATE FAILED") == 1, _out(r)
    assert "BODY=merged" in r.stdout, _out(r)
    assert world.git("rev-parse", "HEAD") == world.old


def _insert_in_body(line: str):
    def edit(lib: Path) -> None:
        head = "_rollback_body() {\n"
        lib.write_text(lib.read_text().replace(head, head + f"    {line}\n"))

    return edit


@pytest.mark.parametrize(
    "fault",
    ['echo "$NEVER_SET_BY_ANYTHING"', "no_such_helper_anywhere", "false"],
    ids=["unbound-variable", "missing-helper", "failing-command"],
)
def test_a_fault_in_the_merged_body_still_restarts_the_server(world, fault):
    world.merge(_insert_in_body(fault))
    r = _flow(world)
    assert "BODY=merged" in r.stdout, _out(r)
    assert "restart-server" in world.calls_text(), _out(r)
    assert world.git("rev-parse", "HEAD") == world.old


def test_a_failed_step_under_the_merged_rollback_still_reports_incomplete(world):
    def edit(lib: Path) -> None:
        lib.write_text(lib.read_text() + "_start_genesis_server() { return 1; }\n")

    world.merge(edit)
    r = _flow(world)
    assert "ROLLBACK INCOMPLETE" in r.stdout, _out(r)
    assert "history failed" in world.calls_text()
    assert "history rolled_back" not in world.calls_text()


# ── Anything wrong with what was staged falls back, before the stop ──


def _append(text: str):
    return lambda lib: lib.write_text(lib.read_text() + text)


def _replace_abi(lib: Path) -> None:
    lib.write_text(
        lib.read_text().replace(
            "update_rollback_abi() { echo 1; }", "update_rollback_abi() { echo 99; }"
        )
    )


@pytest.mark.parametrize(
    ("edit", "why"),
    [
        (_append("if\n"), "cannot parse update_rollback.sh"),
        (lambda lib: lib.unlink(), "it has no update_rollback.sh"),
        (_append("exit 0\n"), "ended the check early"),
        (_append("exit 3\n"), "ended the check early"),
        (lambda lib: lib.write_text(""), "contract is 'none'"),
        (_replace_abi, "contract is '99'"),
        (
            lambda lib: lib.write_text(
                lib.read_text().replace("_rollback_body() {", "_some_other_body() {")
            ),
            "has no rollback",
        ),
    ],
    ids=[
        "syntax-error",
        "no-lib",
        "exit-0-when-sourced",
        "exit-3-when-sourced",
        "empty-lib",
        "wrong-abi",
        "no-body",
    ],
)
def test_a_staged_rollback_that_fails_its_check_keeps_the_launched_one(world, edit, why):
    world.merge(edit)
    r = _flow(world)
    assert "ARMED=0" in r.stdout, _out(r)
    assert why in r.stdout, _out(r)
    assert "BODY=launched" in r.stdout and "BODY=merged" not in r.stdout, _out(r)
    assert r.stdout.index("WARNING: a failure") < r.stdout.index("UPDATE FAILED")
    assert world.git("rev-parse", "HEAD") == world.old
    record = world.source_record()
    assert record["source"] == "launched" and why in record["why_launched"]


def test_a_merged_rollback_needing_what_this_run_lacks_falls_back_at_the_rollback(world):
    def edit(lib: Path) -> None:
        lib.write_text(
            lib.read_text().replace(
                "DB_FILE DB_RESTORE_OWED", "NEVER_SET_GLOBAL DB_FILE DB_RESTORE_OWED"
            )
        )

    world.merge(edit)
    r = _flow(world)
    assert "ARMED=1" in r.stdout, "the variables exist only by the time a rollback starts"
    assert "NEVER_SET_GLOBAL" in r.stdout and "BODY=launched" in r.stdout, _out(r)
    assert "NEVER_SET_GLOBAL" in world.source_record()["why_launched"]
    assert "restart-server" in world.calls_text()


def test_the_staged_copy_vanishing_after_arming_falls_back(world):
    world.merge()
    r = _flow(world, after='rm -rf -- "$_STAGED_ROLLBACK"\nfalse\n')
    assert "BODY=launched" in r.stdout, _out(r)
    assert world.git("rev-parse", "HEAD") == world.old


def test_a_staged_copy_that_cannot_be_created_keeps_the_launched_rollback(world):
    world.merge()
    (world.home / "tmp").chmod(0o500)
    try:
        r = _flow(world)
    finally:
        (world.home / "tmp").chmod(0o700)
    assert "could not create a directory" in r.stdout, _out(r)
    assert "BODY=launched" in r.stdout
    assert world.git("rev-parse", "HEAD") == world.old


def test_a_target_that_cannot_be_unpacked_keeps_the_launched_rollback(world):
    world.merge()
    r = _flow(world, deploy="0" * 40)
    assert "could not unpack" in r.stdout, _out(r)
    assert "BODY=launched" in r.stdout
    assert not list((world.home / "tmp").glob("genesis-rollback-stage.*"))


def test_a_rollback_that_never_armed_replaces_an_earlier_record(world):
    """Otherwise an earlier "merged" record reads as this rollback's."""
    (world.home / ".genesis" / "last_rollback_source.json").write_text('{"source": "merged"}\n')
    world.merge()
    r = world.run(world.script(then="false\n"))
    assert "BODY=launched" in r.stdout, _out(r)
    record = world.source_record()
    assert record["source"] == "launched" and "no merged rollback" in record["why_launched"]


def test_a_launched_fallback_keeps_the_shell_options_it_always_had(world):
    world.merge(lambda lib: lib.write_text(""))
    r = _flow(world)
    opts = re.search(r"BODY-OPTS=(\S*)", r.stdout)
    assert opts and "e" in opts.group(1) and "u" in opts.group(1), _out(r)


# ── Not armed when the staged tree is not this update's ──


def test_an_update_that_merged_nothing_does_not_arm(world):
    world.merge()
    r = _flow(world, validated=world.head)
    assert "ARMED=0" in r.stdout and "BODY=launched" in r.stdout, _out(r)
    assert "merged nothing" in world.source_record()["why_launched"]


def test_local_commits_to_the_libs_do_not_arm(world):
    target = world.merge()
    lib = world.root / "scripts" / "lib" / "venv_setup.sh"
    lib.write_text(lib.read_text() + "# a local commit\n")
    world.git("add", "-A")
    world.git("commit", "-q", "-m", "local")
    world.head = world.git("rev-parse", "HEAD")
    r = _flow(world, deploy=target)
    assert "ARMED=0" in r.stdout and "differs from" in r.stdout, _out(r)


def test_only_the_plain_merge_path_stages_or_arms():
    text = UPDATE_SH.read_text()
    stage = text.index("    _stage_merged_rollback\n")
    guard = text[text.rindex("\nif ", 0, stage) : stage]
    assert '"$POST_MERGE" == "false"' in guard and '"${ON_LIVE:-0}" != 1' in guard
    arm = text.index("# BEGIN arm-merged-rollback")
    assert arm < text.index("# BEGIN live-engine"), "arming belongs to the merge branch, not live's"


# ── Cleanup ──────────────────────────────────────────────


def test_the_staged_copy_is_gone_when_the_run_ends(world):
    world.merge()
    _flow(world)
    assert not list((world.home / "tmp").glob("genesis-rollback-stage.*"))


@pytest.mark.parametrize("anchor", ["_flush_deploy_summary' EXIT"])
def test_both_exit_traps_clean_up(anchor):
    traps = [
        ln
        for ln in UPDATE_SH.read_text().splitlines()
        if ln.startswith(("trap ", "    trap ")) and anchor in ln
    ]
    assert len(traps) == 2
    assert all('rm -rf -- "$_STAGED_ROLLBACK"' in ln for ln in traps)


def test_activation_never_loads_the_staged_rollback():
    text = UPDATE_SH.read_text()
    outside = text.replace(_block("merged-rollback"), "")
    assert "$_STAGED_ROLLBACK/" not in outside
    assert "update_rollback_enter" not in outside


# ── The package helpers ──────────────────────────────────


def _with_record(world: World) -> Path:
    lib = world.home / ".genesis" / "update-deps" / f"{TAG}.lib"
    lib.mkdir(parents=True)
    (lib / "venv_state.py").write_text("# taken before the merge\n")
    (world.home / ".genesis" / "update-deps" / f"{TAG}.json").write_text("{}\n")
    return lib


def _stub_restore(lib: Path) -> None:
    lib.write_text(
        lib.read_text()
        + 'restore_venv_state() { echo "restore-with $(head -c 40 "${3%.json}.lib/venv_state.py")" >> "$CALLS"; }\n'
    )


def test_the_restore_gets_the_merged_helpers_when_they_read_the_record(world):
    record_lib = _with_record(world)
    world.merge(_stub_restore)
    r = _flow(world, after="DEPS_STATE_TAKEN=1\nfalse\n")
    assert "BODY=merged" in r.stdout, _out(r)
    assert "taken before the merge" not in world.calls_text(), world.calls_text()
    assert "restore-with" in world.calls_text()
    assert (record_lib / "venv_missing_files.py").exists(), "every merged helper comes along"


def test_a_failed_copy_of_the_merged_helpers_leaves_the_earlier_ones_whole(world):
    """A rollback may be running because the disk is full."""
    record_lib = _with_record(world)
    shim = world.bin / "cp"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(0o755)
    world.merge(_stub_restore)
    r = _flow(world, after="DEPS_STATE_TAKEN=1\nfalse\n")
    assert "could not copy the merged package helpers" in r.stdout, _out(r)
    assert (record_lib / "venv_state.py").read_text() == "# taken before the merge\n"
    assert "taken before the merge" in world.calls_text()
    assert not list(record_lib.parent.glob("*.merged.lib")), "the trial copy must not stay"


@pytest.mark.parametrize(
    "failing", ["venv_state.py missing", "venv_state.py requirements", "venv_missing_files"]
)
def test_merged_helpers_must_do_everything_the_restore_asks(world, failing):
    _with_record(world)
    python = world.venv / "bin" / "python"
    python.write_text(f'#!/bin/sh\ncase "$*" in *"{failing}"*) exit 1 ;; esac\nexit 0\n')
    world.merge(_stub_restore)
    r = _flow(world, after="DEPS_STATE_TAKEN=1\nfalse\n")
    assert "BODY=merged" in r.stdout, _out(r)
    assert "taken before the merge" in world.calls_text()


def test_helpers_are_tried_with_a_python_that_only_fails_what_it_should(world):
    """Control for the test above: with nothing failing, the merged helpers are used."""
    _with_record(world)
    world.merge(_stub_restore)
    r = _flow(world, after="DEPS_STATE_TAKEN=1\nfalse\n")
    assert "BODY=merged" in r.stdout, _out(r)
    assert "taken before the merge" not in world.calls_text()


def test_a_merged_helper_that_cannot_read_the_record_keeps_the_earlier_one(world):
    _with_record(world)
    (world.venv / "bin" / "python").write_text(
        '#!/bin/sh\ncase "$*" in *extras*) exit 1 ;; esac\nexit 0\n'
    )
    world.merge(_stub_restore)
    r = _flow(world, after="DEPS_STATE_TAKEN=1\nfalse\n")
    assert "cannot read the package record" in r.stdout, _out(r)
    assert "taken before the merge" in world.calls_text()


# ── The contract between a launching update.sh and a merged lib ──

_FROZEN_NEEDS = {
    "1": {
        "DB_FILE",
        "DB_RESTORE_OWED",
        "DB_SNAPSHOT_TAKEN",
        "DEPLOY_HEAD",
        "DEPS_STATE_FILE",
        "DEPS_STATE_TAKEN",
        "GENESIS_ROOT",
        "MERGE_ATTEMPTED",
        "NEW_COMMIT",
        "NEW_TAG",
        "OLD_COMMIT",
        "OLD_TAG",
        "ORIGINAL_BRANCH",
        "PRE_UPDATE_DEGRADED",
        "ROLLBACK_TAG",
        "STARTED_AT",
        "STATE_FILE",
        "UPDATE_OWN_HEAD",
        "VENV_DIR",
        "WERE_RUNNING",
        "_CHECKOUT_LOCK_BUSY",
        "_LIVE_CHECKOUT_PY",
        "_SERVER_DIRECT_PID",
        "_UPDATE_LOCK_FD",
    }
}
# Read by the lib, set by it or a lib loaded with it, or read only with a default.
_NOT_NEEDED_FROM_UPDATE_SH = {
    "BASH_COMMAND",
    "BASH_REMATCH",
    "DEPLOY_BRANCH",
    "DEPLOY_MARKER_FILE",
    "EPHEMERAL_CLEAR_PATHS",
    "GENESIS_ALLOW_NON_DEPLOY_BRANCH",
    "GENESIS_BROWSER_STACK_WAIT_S",
    "GENESIS_CHECKOUT_LOCK_WAIT_S",
    "GENESIS_DEPLOY_SUMMARY",
    "_DEPLOY_MARKER_HELD",
    "_branch",
    "EPHEMERAL_BACKUP_ROOT",
    "EPHEMERAL_DIRTY_RE",
    "GENESIS_CHECKOUT_LOCK_FD",
    "GENESIS_HOME",
    "GENESIS_UPDATE_DB_RESTORE_TIMEOUT_S",
    "GH_DB_PATH",
    "HOME",
    "ON_LIVE",
    "SCRIPT_DIR",
    "SECONDS",
    "SERVER_RESTART_OWED",
    "_DB_RESTORE_LANDED",
    "_LIVE_ENGINE_RAN",
    "_LIVE_TREE_SUSPECT",
    "_SERVER_HEALTH_POLL_SECS",
    "_SERVER_HEALTH_WAIT_SECS",
    "_kept_msg",
    "_kept_rc",
    "_now_branch",
    "_now_head",
}


def _declared_needs() -> set[str]:
    m = re.search(r"for name in ([^;]*); do\n\s+declare -p", ROLLBACK_LIB.read_text())
    assert m
    return set(m.group(1).replace("\\\n", " ").split())


def _declared_functions() -> set[str]:
    m = re.search(r"for name in ([^;]*); do\n\s+declare -F", ROLLBACK_LIB.read_text())
    assert m
    return set(m.group(1).split())


def _reload_libs() -> list[Path]:
    names = re.search(r'_ROLLBACK_RELOAD="([^"]+)"', _block("merged-rollback")).group(1).split()
    return [LIB_DIR / n for n in names]


def _code_of(path: Path) -> str:
    text = re.sub(r"<<'?(\w+)'?.*?\n.*?\n\1\n", "\n", path.read_text(), flags=re.DOTALL)
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _global_reads() -> set[str]:
    text = "\n".join(_code_of(lib) for lib in _reload_libs())
    reads = set(re.findall(r"\$\{#?!?([A-Za-z_]\w*)", text)) | set(
        re.findall(r"\$([A-Za-z_]\w*)", text)
    )
    local: set[str] = set()
    for ln in re.findall(r"\blocal\s+([^\n]*)", text):
        local |= set(re.findall(r"(?:^|\s)(?:-\w+\s+)?([A-Za-z_]\w*)", ln))
    local |= set(re.findall(r"\bfor\s+(\w+)\s+in", text))
    return {n for n in reads - local if n.isupper() or n.startswith("_")}


def test_changing_what_the_lib_needs_requires_a_new_contract_number():
    abi = re.search(r"update_rollback_abi\(\) \{ echo (\d+); \}", ROLLBACK_LIB.read_text()).group(1)
    assert _FROZEN_NEEDS.get(abi) == _declared_needs(), (
        "what the lib needs changed: bump update_rollback_abi and freeze the new set here"
    )


def test_the_lib_declares_every_global_it_reads():
    undeclared = _global_reads() - _declared_needs() - _NOT_NEEDED_FROM_UPDATE_SH
    assert not undeclared, undeclared


def test_the_lib_declares_every_update_sh_function_the_loaded_libs_call():
    text = UPDATE_SH.read_text()
    own = set(re.findall(r"^([A-Za-z_]\w*)\(\)\s*\{", text, re.MULTILINE))
    code = "\n".join(_code_of(lib) for lib in _reload_libs())
    called = {n for n in own if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", code)}
    assert called == _declared_functions()


def test_no_loaded_lib_can_do_harm_when_sourced_again():
    """Each is sourced a second time, from inside a function, during a rollback."""
    risky = re.compile(
        r"^\s*(readonly|local|exit|return|set\s|trap\s|unset\s|cd\s|declare\s+-[^F])"
    )
    found = [
        f"{lib.name}: {ln.strip()}"
        for lib in _reload_libs()
        for ln in _top_level_code(lib.read_text().splitlines())
        if risky.match(ln)
    ]
    assert not found, found


def test_every_need_is_set_before_the_rollback_can_be_armed():
    """A need set after arming would make every merged rollback fall back, silently."""
    lines = UPDATE_SH.read_text().splitlines()
    arm = next(i for i, ln in enumerate(lines) if ln.startswith("# BEGIN arm-merged-rollback"))
    late = []
    for name in _declared_needs():
        assigns = re.compile(rf"^\s*(?:exec \{{{name}\}}|(?:\w+=\S*\s+)*{name}\+?=)")
        first = next(
            (
                i
                for i, ln in enumerate(lines)
                if not ln.lstrip().startswith("#") and assigns.match(ln)
            ),
            None,
        )
        if first is None or first > arm:
            late.append((name, first))
    assert not late, late


def test_update_sh_sets_everything_the_lib_declares():
    text = UPDATE_SH.read_text()
    missing = {
        n
        for n in _declared_needs()
        if not re.search(rf"(?:^|[\s;({{]){n}\+?[=}}]", text, re.MULTILINE)
    }
    assert not missing, missing


def test_the_launching_script_and_lib_agree_on_the_contract_number():
    abi = re.search(r"update_rollback_abi\(\) \{ echo (\d+); \}", ROLLBACK_LIB.read_text()).group(1)
    assert f"_ROLLBACK_ABI={abi}\n" in _block("merged-rollback")


def test_the_record_a_launching_script_wrote_stays_readable():
    """Format fixture: merged helpers read records earlier ones wrote. A change here
    is a contract change."""
    record = {
        "pinned": {"example-package": "example-package==1.2.3"},
        "versions": {"example-package": "1.2.3"},
        "editable": ["genesis-v3"],
    }
    r = subprocess.run(
        ["python3", "-P", str(LIB_DIR / "venv_state.py"), "requirements", "/dev/stdin"],
        input=json.dumps(record),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and "example-package==1.2.3" in r.stdout, r.stdout + r.stderr


def test_the_snapshot_name_is_the_one_update_sh_writes():
    text = UPDATE_SH.read_text()
    assert '".pre-update"' in text and '".peer-restore"' in text
    assert ".pre-update.peer-restore" in ROLLBACK_LIB.read_text()
