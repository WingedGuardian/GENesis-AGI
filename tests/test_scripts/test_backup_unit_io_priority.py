"""The backup unit runs at a low per-process I/O priority.

A backup run's footprint is disk I/O (SQLite dump, GPG, Qdrant snapshots), not
CPU, so the unit lowers its I/O scheduling class. It uses
``IOSchedulingClass``/``IOSchedulingPriority`` rather than ``IOWeight``: the
first two set the process's own I/O priority (inherited by every child the
script starts), while ``IOWeight`` is a cgroup setting that a user unit can use
only where the io controller is delegated to the user manager, and many hosts
delegate only cpu, memory and pids.

bootstrap.sh renders this template into the user unit directory on install and
on every update, so the keys reach the installed unit through that render.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "scripts" / "systemd" / "genesis-backup.service.template"


def _rendered_service(tmp_path: Path) -> configparser.SectionProxy:
    # Substitute every __TOKEN__ the way the renderers do (a path per token), so
    # the parse reads the unit as installed rather than the raw template.
    rendered = re.sub(r"__[A-Z0-9][A-Z0-9_]*__", str(tmp_path / "x"), TEMPLATE.read_text())
    assert not re.search(r"__[A-Z0-9_]+__", rendered)  # every placeholder substituted
    unit = configparser.ConfigParser(interpolation=None, strict=True)
    unit.optionxform = str  # systemd keys are case-sensitive
    unit.read_string(rendered)
    return unit["Service"]


def test_backup_unit_sets_best_effort_lowest_io_priority(tmp_path):
    service = _rendered_service(tmp_path)
    assert service.get("IOSchedulingClass") == "best-effort"
    assert service.get("IOSchedulingPriority") == "7"


def test_backup_unit_does_not_lower_cpu_priority(tmp_path):
    # Measured: a run uses under one core with CPU pressure near 1%, so the
    # change is I/O only. A Nice/CPUWeight here would be an unmeasured change.
    service = _rendered_service(tmp_path)
    for key in ("Nice", "CPUWeight", "CPUSchedulingPolicy"):
        assert key not in service
