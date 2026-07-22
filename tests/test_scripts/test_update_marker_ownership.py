"""Owner-checked marker deletes in scripts/update.sh (deploy-audit P5-B, part 4).

update.sh must NOT unconditionally delete ~/.genesis/update_in_progress.pid: on
the supervised path the orchestrator holds it with ITS pid across tiers, and
scripts/restore.sh holds it with its own pid while rebuilding the DB — stripping
a live foreign holder's marker reopens the watchdog-revives-mid-op hazard. The
`_clear_deploy_state` helper deletes the marker only if WE own it ($$) OR its
holder is dead. The state file is always removed (update.sh wrote it).

These drive the ACTUAL shipped `_clear_deploy_state` function against each marker
state.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return UPDATE_SH.read_text()


def _extract_func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}$", text, re.DOTALL | re.MULTILINE)
    assert m, f"{name} not found"
    return f"{name}() {{\n{m.group(1)}\n}}"


def test_no_unconditional_marker_rm_remains(text: str) -> None:
    """Every marker delete must go through the owner-checked helper — no raw
    `rm -f ... update_in_progress.pid` outside `_clear_deploy_state`."""
    body = _extract_func(text, "_clear_deploy_state")
    outside = text.replace(body, "")
    # No raw DELETE of the marker may survive outside the helper.
    assert not re.search(r"rm -f[^\n]*update_in_progress\.pid", outside), (
        "a raw marker rm survives outside the helper"
    )
    assert text.count("_clear_deploy_state\n") >= 5, "helper must be called at every cleanup site"
    # The direct path no longer adopts/writes a marker (it signals via the state
    # file), so update.sh must NOT reference the marker outside the helper at all.
    assert "update_in_progress.pid" not in outside, (
        "no marker use should survive outside the helper"
    )


def _run_clear(tmp_path: Path, text: str, marker_pid: str | None) -> tuple[bool, bool]:
    """Run the shipped _clear_deploy_state with a given marker state.
    Returns (marker_still_exists, state_still_exists)."""
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    marker = home / ".genesis" / "update_in_progress.pid"
    state = tmp_path / "update_state.json"
    state.write_text("{}")
    if marker_pid is not None:
        marker.write_text(marker_pid)
    harness = f"""#!/bin/bash
set -Eeuo pipefail
STATE_FILE="{state}"
{_extract_func(text, "_clear_deploy_state")}
_clear_deploy_state
"""
    script = tmp_path / "h.sh"
    script.write_text(harness)
    subprocess.run(
        ["bash", str(script)], env={**os.environ, "HOME": str(home)}, timeout=10, check=True
    )
    return marker.exists(), state.exists()


def test_deletes_marker_we_own(tmp_path: Path, text: str) -> None:
    # A helper subshell's $$ differs from any pid we write, so use the harness's
    # own pid by writing "$$" — emulate ownership by writing the bash pid at run.
    # Simpler: a marker holding THIS python process's pid is a live FOREIGN pid;
    # to test "owned", we write the shell's own $$ from inside the harness.
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    state = tmp_path / "s.json"
    state.write_text("{}")
    marker = home / ".genesis" / "update_in_progress.pid"
    harness = f"""#!/bin/bash
set -Eeuo pipefail
STATE_FILE="{state}"
echo "$$" > "{marker}"      # marker holds OUR pid → owned
{_extract_func(text, "_clear_deploy_state")}
_clear_deploy_state
"""
    (tmp_path / "h.sh").write_text(harness)
    subprocess.run(
        ["bash", str(tmp_path / "h.sh")],
        env={**os.environ, "HOME": str(home)},
        timeout=10,
        check=True,
    )
    assert not marker.exists(), "an owned marker must be deleted"
    assert not state.exists(), "the state file is always removed"


def test_keeps_live_foreign_marker(tmp_path: Path, text: str) -> None:
    """A LIVE foreign holder's marker (e.g. a concurrent restore) must survive."""
    # os.getpid() is this pytest process — alive and NOT the harness's $$.
    marker_exists, state_exists = _run_clear(tmp_path, text, str(os.getpid()))
    assert marker_exists, "a live foreign marker must NOT be stripped"
    assert not state_exists, "the state file is still removed"


def test_deletes_dead_marker(tmp_path: Path, text: str) -> None:
    """A marker whose holder is dead (a stale direct-run systemd-run pid) is cleaned."""
    # PID 2^31-ish is not a live process.
    marker_exists, _ = _run_clear(tmp_path, text, "2147480000")
    assert not marker_exists, "a dead-holder marker must be cleaned"


def test_no_marker_is_safe(tmp_path: Path, text: str) -> None:
    marker_exists, state_exists = _run_clear(tmp_path, text, None)
    assert not marker_exists and not state_exists
