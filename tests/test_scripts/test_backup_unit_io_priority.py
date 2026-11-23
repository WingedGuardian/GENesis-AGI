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

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "scripts" / "systemd" / "genesis-backup.service.template"


def _rendered_service(tmp_path: Path) -> dict[str, list[str]]:
    """The [Service] section as systemd reads it: each key maps to every value
    it is assigned, in order, since keys such as Environment= and ExecStartPre=
    may repeat (configparser's strict mode refuses a repeat, and its lax mode
    keeps only the last)."""
    # Substitute every __TOKEN__ the way the renderers do (a path per token), so
    # the parse reads the unit as installed rather than the raw template.
    rendered = re.sub(r"__[A-Z0-9][A-Z0-9_]*__", str(tmp_path / "x"), TEMPLATE.read_text())
    assert not re.search(r"__[A-Z0-9_]+__", rendered)  # every placeholder substituted
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for raw in rendered.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        assert current is not None and "=" in line, f"unparseable unit line: {raw!r}"
        key, _, value = line.partition("=")  # systemd keys are case-sensitive
        current.setdefault(key.strip(), []).append(value.strip())
    return sections["Service"]


def test_backup_unit_sets_best_effort_lowest_io_priority(tmp_path):
    service = _rendered_service(tmp_path)
    assert service.get("IOSchedulingClass") == ["best-effort"]
    assert service.get("IOSchedulingPriority") == ["7"]


def test_backup_unit_waits_for_verify_memory_before_the_backup(tmp_path):
    """The ExecStartPre wait is "-"-prefixed (a failure never stops the run),
    names a script that exists, and its cap lives in this unit's Environment."""
    service = _rendered_service(tmp_path)
    pre = service.get("ExecStartPre", [])
    assert len(pre) == 1, pre
    assert pre[0].startswith("-/bin/bash "), pre
    script = pre[0].rsplit("/scripts/", 1)[1]
    assert (ROOT / "scripts" / script).is_file(), script
    assert "GENESIS_BACKUP_VERIFY_WAIT_MAX_S=3600" in service.get("Environment", [])


def test_backup_unit_timeout_covers_the_wait_plus_the_run(tmp_path):
    """TimeoutStartSec bounds ExecStartPre as well as ExecStart, so it must hold
    the whole wait cap plus the 2 h the run itself is sized for."""
    service = _rendered_service(tmp_path)
    cap = next(
        int(v.split("=", 1)[1])
        for v in service["Environment"]
        if v.startswith("GENESIS_BACKUP_VERIFY_WAIT_MAX_S=")
    )
    (timeout,) = service["TimeoutStartSec"]
    assert int(timeout) >= cap + 7200, (timeout, cap)


def test_backup_unit_does_not_lower_cpu_priority(tmp_path):
    # Measured: a run uses under one core with CPU pressure near 1%, so the
    # change is I/O only. A Nice/CPUWeight here would be an unmeasured change.
    service = _rendered_service(tmp_path)
    for key in ("Nice", "CPUWeight", "CPUSchedulingPolicy"):
        assert key not in service
