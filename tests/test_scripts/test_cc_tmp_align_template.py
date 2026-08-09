"""Structural tests for the genesis-cc-tmp-align systemd units + their enablement.

The cold-start leg is a SERVICE (WantedBy=default.target) ordered before
genesis-server — the one deterministically CC-quiet window. Because the generic
render/enable loops only *enable* ``*.timer.template``, this service needs an
EXPLICIT enable in BOTH install.sh (fresh) and bootstrap.sh (existing installs),
or it renders but never activates. These tests pin that contract.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SVC = REPO_ROOT / "scripts" / "systemd" / "genesis-cc-tmp-align.service.template"
TIMER = REPO_ROOT / "scripts" / "systemd" / "genesis-cc-tmp-align.timer.template"
INSTALL = REPO_ROOT / "scripts" / "install.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"


def test_service_runs_in_the_quiet_cold_start_window():
    t = SVC.read_text()
    assert "Before=genesis-server.service" in t  # quiet window: before ego/bg CC spawn
    assert "Type=oneshot" in t
    assert "cc_tmp_align_host.sh" in t
    assert "WantedBy=default.target" in t  # cold-start leg pulled in at boot


def test_service_is_hardened_and_bounded():
    t = SVC.read_text()
    assert "NoNewPrivileges=yes" in t
    assert "ProtectSystem=strict" in t
    assert "ReadWritePaths=%h" in t
    assert "TimeoutStartSec=" in t  # bounds boot delay if the host is slow/unreachable


def test_timer_is_periodic_and_persistent():
    t = TIMER.read_text()
    assert "OnUnitInactiveSec=" in t  # keep retrying on a busy install
    assert "Persistent=true" in t
    assert "WantedBy=timers.target" in t


def test_service_explicitly_enabled_on_fresh_and_existing_installs():
    for f in (INSTALL, BOOTSTRAP):
        assert "enable genesis-cc-tmp-align.service" in f.read_text(), (
            f"{f.name} must EXPLICITLY enable the cold-start service — the timer "
            f"loop does not cover a WantedBy=default.target service"
        )


def test_only_supported_render_placeholders():
    supported = {"__HOME__", "__VENV__", "__REPO_DIR__", "__CC_BIN_DIR__"}
    for tmpl in (SVC, TIMER):
        for ph in re.findall(r"__[A-Z_]+__", tmpl.read_text()):
            assert ph in supported, f"unsupported render placeholder {ph} in {tmpl.name}"


def test_uninstall_stops_and_disables_the_new_units():
    # Both container-side uninstall paths (direct + remote) must stop/disable the
    # new service AND timer, or an uninstall leaves dangling enabled links under
    # default.target.wants / timers.target.wants (enable != file-glob removal).
    txt = (REPO_ROOT / "scripts" / "uninstall.sh").read_text()
    assert txt.count("genesis-cc-tmp-align.timer") >= 2
    assert txt.count("genesis-cc-tmp-align.service") >= 2
