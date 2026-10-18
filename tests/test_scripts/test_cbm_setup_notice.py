"""scripts/lib/cbm_setup_notice.sh — the installers' codebase-memory-mcp line.

Runs the real helper in bash against a tmp checkout (symlinked to this tree's
src/ and scripts/), a tmp HOME and a fake systemctl. The real ~/.genesis is never
read or written. Also pins that bootstrap/install/update call the helper and no
longer print the old unconditional line.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "scripts" / "lib" / "cbm_setup_notice.sh"


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "src").symlink_to(REPO / "src")
    (root / "scripts").symlink_to(REPO / "scripts")
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "systemctl").write_text(
        '#!/bin/sh\n[ "$FAKE_UNIT_STATE" = fail ] && exit 1\n'
        "printf 'LoadState=loaded\\nUnitFileState=%s\\n' \"$FAKE_UNIT_STATE\"\n"
    )
    (fakebin / "systemctl").chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "FAKE_UNIT_STATE": "disabled",
        "XDG_RUNTIME_DIR": str(tmp_path / "no-runtime"),
    }

    def run(
        *,
        registered=True,
        config=False,
        enabled=False,
        sentinel=False,
        python=sys.executable,
        prelude="",
    ):
        servers = {"codebase-memory-mcp": {"command": "x"}} if registered else {}
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
        cfg = home / ".genesis" / "config" / "codebase-managed.json"
        if config:
            cfg.write_text("{}")
        flag = home / ".genesis" / "codebase-memory-mcp.disabled"
        if sentinel:
            flag.write_text("")
        env["FAKE_UNIT_STATE"] = (
            enabled if isinstance(enabled, str) else ("enabled" if enabled else "disabled")
        )
        script = f'{prelude}\n. "$1"\ngenesis_cbm_setup_notice "$2" "$3" "  "\necho "rc=$?"\n'
        proc = subprocess.run(
            ["bash", "-c", script, "_", str(HELPER), str(python), str(root)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        return proc

    run.home = home
    return run


def test_registered_and_unconfigured_names_what_is_missing_and_the_fix(box):
    proc = box()
    lines = proc.stdout.splitlines()
    assert lines[-1] == "rc=0"
    (line,) = lines[:-1]
    assert line.startswith("  Codebase: NOT SET UP")
    assert "codebase-managed.json is missing" in line
    assert "genesis-cbm-query.service is not enabled (state: disabled)" in line
    assert "If you see the 'registered but unconfigured' alert" in line
    assert str(box.home / ".genesis" / "codebase-memory-mcp.disabled") in line


def test_configured_but_not_enabled_names_only_the_unit(box):
    out = box(config=True).stdout
    assert "is missing" not in out
    assert "genesis-cbm-query.service is not enabled" in out


def test_configured_and_enabled_prints_ok(box):
    assert box(config=True, enabled=True).stdout == "  Codebase: OK\nrc=0\n"


def test_opt_out_sentinel_prints_nothing(box):
    assert box(sentinel=True).stdout == "rc=0\n"


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


def test_a_failing_systemctl_cannot_hide_a_missing_config(box):
    """The missing settings file decides it; the unit probe is not needed."""
    assert "Codebase: NOT SET UP" in box(config=False, enabled="fail").stdout


def test_a_failing_systemctl_with_a_config_is_unknown_not_ok(box):
    out = box(config=True, enabled="fail").stdout
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
