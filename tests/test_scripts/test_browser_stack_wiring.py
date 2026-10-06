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


def test_update_runs_the_step_after_the_update_is_recorded_done():
    text = (_SCRIPTS / "update.sh").read_text()
    bootstrap_call = text.index('GENESIS_BROWSER_STACK_DEFERRED=1 "$GENESIS_ROOT/scripts/bootstrap.sh"')
    done = text.index('_write_state "done"')
    step = text.index('bash "$GENESIS_ROOT/scripts/install_browser_stack.sh"')
    assert bootstrap_call < done < step


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
        r"^\s*if ! tail -n 1 \"\$_bs_log\" \| grep -q 'browser stack: ready'; then\n"
        r"\s*setup_warn ",
        text,
        re.M,
    )
    assert call and warn and call.start() < warn.start()
