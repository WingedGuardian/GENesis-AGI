"""How bootstrap.sh, install.sh and update.sh run scripts/install_browser_stack.sh.

The step downloads about 2 GB on first run, so WHERE it runs matters: update.sh
must run it only after the update is recorded done (the watchdog does not restart
a server while update_state.json says an update is in progress), and bootstrap
must defer it when update.sh is its caller, including the transition update,
whose (old) update.sh passes only GENESIS_BOOTSTRAP_ALLOW_LIVE.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def test_the_script_never_fails_its_caller_without_a_venv(tmp_path):
    env = {**os.environ, "GENESIS_VENV": str(tmp_path / "no-venv")}
    proc = subprocess.run(
        ["bash", str(_SCRIPTS / "install_browser_stack.sh")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    assert proc.stdout.splitlines()[-1].strip().startswith("browser stack: SKIPPED")


_STEP_CALL = "\n    _run_browser_stack_step\n"


def test_update_runs_the_step_after_the_update_is_recorded_done():
    text = (_SCRIPTS / "update.sh").read_text()
    bootstrap_call = text.index(
        'GENESIS_BROWSER_STACK_DEFERRED=1 "$GENESIS_ROOT/scripts/bootstrap.sh"'
    )
    done = text.index('_write_state "done"')
    step = text.index("\n_run_browser_stack_step\n", done)
    assert text.index("_run_browser_stack_step() {") < done, "defined before use"
    assert bootstrap_call < done < step


def test_an_update_with_no_new_commit_still_runs_the_step():
    """Codex (#2956): the already-up-to-date path exits early, so a step that
    was skipped or degraded (a browser open, low disk, a failed download) was
    never retried by re-running the update."""
    text = (_SCRIPTS / "update.sh").read_text()
    start = text.index('echo "  Already up to date ($NEW_COMMIT)."')
    end = text.index('echo "  Nothing to do."', start)
    block = text[start:end]
    # After the deploy state is cleared: the watchdog guards the server again.
    assert block.index("_clear_deploy_state") < block.index(_STEP_CALL)


def _bootstrap_block() -> str:
    text = (_SCRIPTS / "bootstrap.sh").read_text()
    start = text.index("# --- Browser stack ---")
    return text[start : text.index("\n# --- ", start + 1)]


@pytest.mark.parametrize(
    "deferred, allow_live, runs",
    [
        (None, None, True),
        ("1", None, False),  # update.sh from this version on
        (None, "1", False),  # the transition update: the old update.sh
        ("1", "1", False),
    ],
)
def test_bootstrap_runs_the_step_only_outside_an_update(tmp_path, deferred, allow_live, runs):
    """Run bootstrap's own block, under its own shell options, against a stub."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    marker = tmp_path / "ran"
    (root / "scripts" / "install_browser_stack.sh").write_text(f'touch "{marker}"\nexit 7\n')
    script = "set -euo pipefail\n" + _bootstrap_block() + "\necho survived\n"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GENESIS_BROWSER_STACK_DEFERRED", "GENESIS_BOOTSTRAP_ALLOW_LIVE")
    }
    env["GENESIS_ROOT"] = str(root)
    if deferred:
        env["GENESIS_BROWSER_STACK_DEFERRED"] = deferred
    if allow_live:
        env["GENESIS_BOOTSTRAP_ALLOW_LIVE"] = allow_live
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "survived" in proc.stdout, "a failed step must never stop bootstrap"
    assert marker.exists() is runs
    if runs:  # the stub exits 7; pipefail must carry that past `sed` to the warning
        assert "did not finish" in proc.stdout


def test_install_runs_the_step_and_warns_from_its_outcome_line():
    """The script always exits 0; only its last line says whether it worked."""
    text = (_SCRIPTS / "install.sh").read_text()
    call = re.search(
        r'^\s*GENESIS_VENV="\$VENV_PATH" bash "\$SCRIPT_DIR/install_browser_stack\.sh"',
        text,
        re.M,
    )
    warn = re.search(
        r"^\s*if ! tail -n 1 \"\$_bs_log\" \| grep -q 'Camoufox usable'; then\n"
        r"\s*setup_warn ",
        text,
        re.M,
    )
    assert call and warn and call.start() < warn.start()


def _install_block() -> str:
    text = (_SCRIPTS / "install.sh").read_text()
    start = text.index('            mkdir -p "$HOME/tmp"\n            _bs_log=')
    end = text.index('            rm -f "$_bs_log"\n', start) + len(
        '            rm -f "$_bs_log"\n'
    )
    return text[start:end]


@pytest.mark.parametrize(
    "outcome, warns",
    [
        (
            "browser stack: ready (engine=True, chromium=True, launch=True); Camoufox usable: x",
            False,
        ),
        # No X display during install.sh (only bootstrap sets VNC up): the
        # Chromium fallback is not the primary layer and must not fail strict CI.
        (
            "browser stack: DEGRADED (engine=True, chromium=False, launch=True); Camoufox usable: x",
            False,
        ),
        (
            "browser stack: DEGRADED (engine=False, chromium=False, launch=False); Camoufox DOWN until this is re-run: x",
            True,
        ),
        ("browser stack: SKIPPED (a browser is running); Camoufox usable: x", False),
    ],
)
def test_install_warns_only_when_camoufox_is_not_usable(tmp_path, outcome, warns):
    """Review finding on this slice: install.sh has no display step, so the
    headed Chromium check always reads DEGRADED there, and strict CI turned the
    resulting warning into a failed install."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "install_browser_stack.sh").write_text(f"echo '{outcome}'\n")
    script = (
        "set -euo pipefail\n"
        'setup_warn() { echo "WARNED: $1"; }\n'
        f'SCRIPT_DIR="{scripts}"\nVENV_PATH=/nonexistent\n' + _install_block()
    )
    env = {**os.environ, "HOME": str(tmp_path)}
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert ("WARNED:" in proc.stdout) is warns, proc.stdout


def _update_step_function() -> str:
    text = (_SCRIPTS / "update.sh").read_text()
    fn = text[text.index("_run_browser_stack_step() {") :]
    return fn[: fn.index("\n}\n") + 3]


def test_update_step_runs_the_script_and_survives_its_failure(tmp_path):
    """Review finding on this slice: the update.sh tests compared text
    positions only, and still passed with the step's body deleted."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    marker = tmp_path / "ran"
    (root / "scripts" / "install_browser_stack.sh").write_text(f'touch "{marker}"\nexit 7\n')
    script = (
        "set -Eeuo pipefail\ntrap 'echo ERR-TRAP' ERR\n"
        + _update_step_function()
        + "\n_run_browser_stack_step\necho survived\n"
    )
    env = {**os.environ, "GENESIS_ROOT": str(root)}
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0 and "survived" in proc.stdout, proc.stderr
    assert marker.exists()
    assert "did not finish" in proc.stdout and "ERR-TRAP" not in proc.stdout
