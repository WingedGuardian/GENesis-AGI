"""scripts/lib/cbm_setup_notice.sh — the installers' codebase-memory-mcp line.

Runs the real helper in bash against a tmp checkout (this tree's src/ and the
real kill-switch resolver, with a fake `codebase_managed.py status`), a tmp HOME
and a systemctl that always fails. The real ~/.genesis is never read or written.
Also pins that bootstrap/install/update call the helper, that bootstrap leaves
the line to update.sh when update.sh drives it, and that the old unconditional
line is gone.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "scripts" / "lib" / "cbm_setup_notice.sh"
FAKE_STATUS = """import os, sys
if os.environ.get("FAKE_STATUS_MODE") == "crash":
    sys.exit(2)
print(os.environ["FAKE_STATUS"])
"""


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "checkout"
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "src").symlink_to(REPO / "src")
    shutil.copy(REPO / "scripts/lib/cbm_disable_file.sh", root / "scripts/lib/")
    (root / "scripts" / "codebase_managed.py").write_text(FAKE_STATUS)
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "systemctl").write_text("#!/bin/sh\nexit 1\n")
    (fakebin / "systemctl").chmod(0o755)
    flag = home / ".genesis" / "codebase-memory-mcp.disabled"

    def run(
        *,
        registered=True,
        config=False,
        unit="disabled",
        sentinel=False,
        python=sys.executable,
        prelude="",
    ):
        servers = {"codebase-memory-mcp": {"command": "x"}} if registered else {}
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
        if config:
            (home / ".genesis" / "config" / "codebase-managed.json").write_text("{}")
        if sentinel:
            flag.write_text("")
        service = None if unit == "unreachable" else {"LoadState": "loaded", "UnitFileState": unit}
        status = {
            "settings": {"version": 2, "sentinel": str(flag)},
            "settings_error": None,
            "service": service,
            "manager_error": "unreachable" if service is None else None,
        }
        env = {
            "HOME": str(home),
            "PATH": f"{fakebin}:/usr/bin:/bin",
            "XDG_RUNTIME_DIR": str(tmp_path / "no-runtime"),
            "FAKE_STATUS": json.dumps(status),
        }
        script = f'{prelude}\n. "$1"\ngenesis_cbm_setup_notice "$2" "$3" "  "\necho "rc=$?"\n'
        return subprocess.run(
            ["bash", "-c", script, "_", str(HELPER), str(python), str(root)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )

    run.home = home
    return run


def test_registered_and_unconfigured_names_what_is_missing_and_the_fix(box):
    lines = box().stdout.splitlines()
    assert lines[-1] == "rc=0"
    (line,) = lines[:-1]
    assert line.startswith("  Codebase: NOT SET UP")
    assert "codebase-managed.json is missing" in line
    assert "If you see the 'registered but unconfigured' alert" in line
    assert str(box.home / ".genesis" / "codebase-memory-mcp.disabled") in line


def test_configured_but_not_enabled_names_only_the_unit(box):
    out = box(config=True).stdout
    assert "is missing" not in out
    assert "genesis-cbm-query.service is not enabled (state: disabled)" in out


def test_configured_and_enabled_prints_ok(box):
    assert box(config=True, unit="enabled").stdout == "  Codebase: OK\nrc=0\n"


def test_opt_out_sentinel_prints_nothing(box):
    assert box(sentinel=True).stdout == "rc=0\n"
    assert box(config=True, unit="unreachable", sentinel=True).stdout == "rc=0\n"


def test_not_registered_prints_nothing(box):
    assert box(registered=False).stdout == "rc=0\n"


def test_a_failing_check_says_so_and_never_trips_errexit_or_err_trap(box, tmp_path):
    bad = tmp_path / "python"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    proc = box(python=bad, prelude="set -Eeuo pipefail\ntrap 'echo ERRTRAP' ERR")
    assert "Codebase: setup state could not be checked" in proc.stdout
    assert "ERRTRAP" not in proc.stdout
    assert proc.stdout.endswith("rc=0\n")


def test_an_unreachable_manager_cannot_hide_a_missing_config(box):
    """The missing settings file decides it; the unit state is not needed."""
    assert "Codebase: NOT SET UP" in box(config=False, unit="unreachable").stdout


def test_an_unreachable_manager_with_a_config_is_unknown_not_ok(box):
    out = box(config=True, unit="unreachable").stdout
    assert "could not be checked" in out
    assert "OK" not in out


def test_no_interpreter_says_so(box, tmp_path):
    proc = box(python=tmp_path / "absent")
    assert "could not be checked" in proc.stdout
    assert proc.stdout.endswith("rc=0\n")


@pytest.mark.parametrize("script", ["bootstrap.sh", "install.sh", "update.sh"])
def test_installers_call_the_shared_helper_not_the_old_line(script):
    text = (REPO / "scripts" / script).read_text()
    assert "explicit pinned setup required" not in text
    assert "genesis_cbm_setup_notice " in text
    assert "lib/cbm_setup_notice.sh" in text


def test_bootstrap_leaves_the_line_to_update_sh():
    """update.sh prints the line in its own summary; bootstrap under update.sh
    (which hands it the summary file) must not print it a second time."""
    text = (REPO / "scripts" / "bootstrap.sh").read_text()
    call = text.index("genesis_cbm_setup_notice ")
    guard = text.rindex('if [[ "${_bootstrap_owns_summary:-0}" = "1" ]]; then', 0, call)
    assert "fi" not in text[guard:call].split("then", 1)[1]
