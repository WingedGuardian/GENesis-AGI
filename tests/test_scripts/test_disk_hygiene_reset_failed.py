"""Tests for the failed-transient-unit reset in scripts/disk_hygiene.sh
(``reset_failed_transient_units``).

Sources the script (functions only: ``main`` is guarded) and calls the stage with
a fake ``systemctl`` first on PATH, so no test ever reaches the real user
manager. The shim serves unit properties from one file per unit and records
every call it receives.

What the stage may reset: a FAILED unit systemd itself marks ``Transient=yes``
(a ``systemd-run`` launch, whose FragmentPath is the runtime transient directory
or empty), whose failure is older than the age floor. Never a ``genesis-*`` unit,
never a persistent unit, never a unit failed inside the floor.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

_HYGIENE = Path(__file__).resolve().parents[2] / "scripts" / "disk_hygiene.sh"
_TRANSIENT_DIR = "/run/user/4242/systemd/transient"

_SHIM = r"""#!/bin/bash
printf '%s\n' "$*" >> "$SHIM_CALLS"
[ "$1" = --user ] || exit 9
shift
case "$1" in
  list-units)
    [ -z "${SHIM_MANAGER_DOWN:-}" ] || { echo "Failed to connect to bus" >&2; exit 1; }
    for f in "$SHIM_UNITS"/*; do
      [ -f "$f" ] || continue
      grep -qx 'ActiveState=failed' "$f" && printf '%s loaded failed failed fake unit\n' "${f##*/}"
    done
    ;;
  show)
    unit="${@: -1}"
    case " $* " in
      *" --timestamp=unix "*) cat "$SHIM_UNITS/$unit" ;;
      # Without --timestamp=unix systemd prints a local wall-clock string.
      *) sed 's/^StateChangeTimestamp=@.*/StateChangeTimestamp=Sat 2026-11-07 10:00:00 UTC/' "$SHIM_UNITS/$unit" ;;
    esac
    ;;
  reset-failed)
    unit="${@: -1}"
    echo "SHIM reset-failed $unit"
    sed -i 's/^ActiveState=failed$/ActiveState=inactive/' "$SHIM_UNITS/$unit"
    ;;
  *) exit 9 ;;
esac
"""


def _unit(
    units: Path,
    name: str,
    *,
    transient: bool,
    fragment: str,
    failed_ago_s: int,
    state: str = "failed",
    result: str = "exit-code",
    timestamp: str | None = None,
) -> None:
    ts = int(time.time()) - failed_ago_s
    (units / name).write_text(
        f"Id={name}\n"
        f"Transient={'yes' if transient else 'no'}\n"
        f"FragmentPath={fragment}\n"
        f"ActiveState={state}\n"
        f"StateChangeTimestamp={timestamp if timestamp is not None else f'@{ts}'}\n"
        f"Result={result}\n"
    )


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "systemctl"
    shim.write_text(_SHIM)
    shim.chmod(0o700)
    units = tmp_path / "units"
    units.mkdir()
    calls = tmp_path / "calls"
    return shim_dir, units, calls


def _run(
    tmp_path: Path,
    shim_dir: Path,
    units: Path,
    calls: Path,
    *,
    path: str | None = None,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    script = (
        f"source '{_HYGIENE}'\n"
        'PATH="$STAGE_PATH"\n'
        "reset_failed_transient_units\n"
        'echo "rc=$?"\n'
        "echo AFTER-STAGE\n"
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "SHIM_UNITS": str(units),
        "SHIM_CALLS": str(calls),
        "STAGE_PATH": path if path is not None else f"{shim_dir}:{os.environ['PATH']}",
        **(extra_env or {}),
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def _resets(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return [
        c.split()[-1] for c in calls.read_text().splitlines() if c.split()[1:2] == ["reset-failed"]
    ]


def test_old_failed_run_u_transient_unit_is_reset_and_logged_first(tmp_path):
    shim_dir, units, calls = _setup(tmp_path)
    _unit(
        units,
        "run-u123.service",
        transient=True,
        fragment=f"{_TRANSIENT_DIR}/run-u123.service",
        failed_ago_s=2 * 86400,
    )
    r = _run(tmp_path, shim_dir, units, calls)
    assert "rc=0" in r.stdout, r.stderr
    assert _resets(calls) == ["run-u123.service"]
    lines = r.stdout.splitlines()
    logged = next(i for i, ln in enumerate(lines) if "run-u123.service" in ln and "SHIM" not in ln)
    shimmed = lines.index("SHIM reset-failed run-u123.service")
    assert logged < shimmed, "the unit must be logged BEFORE it is reset"


def test_the_reset_line_records_why_the_unit_failed(tmp_path):
    """The reset must not erase WHY a unit failed from the daily log."""
    shim_dir, units, calls = _setup(tmp_path)
    _unit(
        units,
        "run-u778.scope",
        transient=True,
        fragment=f"{_TRANSIENT_DIR}/run-u778.scope",
        failed_ago_s=2 * 86400,
        result="exit-code",
    )
    r = _run(tmp_path, shim_dir, units, calls)
    assert _resets(calls) == ["run-u778.scope"]
    assert any("run-u778.scope" in ln and "result=exit-code" in ln for ln in r.stdout.splitlines())


def test_an_oom_killed_scope_is_never_reset(tmp_path):
    """Owner ruling: Genesis's own unnamed scopes (CC sessions, code-intel) are
    in this population, and one killed by its MemoryMax is a real alarm, so it
    stays in the failed list however old it is."""
    shim_dir, units, calls = _setup(tmp_path)
    _unit(
        units,
        "run-u777.scope",
        transient=True,
        fragment=f"{_TRANSIENT_DIR}/run-u777.scope",
        failed_ago_s=30 * 86400,
        result="oom-kill",
    )
    r = _run(tmp_path, shim_dir, units, calls)
    assert "rc=0" in r.stdout, r.stderr
    assert _resets(calls) == []
    assert "kept run-u777.scope (result=oom-kill is never auto-reset)" in r.stdout


def test_a_unit_name_starting_with_a_dash_is_passed_after_double_dash(tmp_path):
    shim_dir, units, calls = _setup(tmp_path)
    _unit(units, "-lead.service", transient=True, fragment="", failed_ago_s=2 * 86400)
    r = _run(tmp_path, shim_dir, units, calls)
    assert "rc=0" in r.stdout, r.stderr
    assert _resets(calls) == ["-lead.service"]
    reset_calls = [c for c in calls.read_text().splitlines() if "reset-failed" in c]
    assert reset_calls == ["--user reset-failed -- -lead.service"]


def test_a_unit_with_no_readable_failure_time_is_kept(tmp_path):
    shim_dir, units, calls = _setup(tmp_path)
    _unit(
        units,
        "run-u5.service",
        transient=True,
        fragment=f"{_TRANSIENT_DIR}/run-u5.service",
        failed_ago_s=0,
        timestamp="",
    )
    r = _run(tmp_path, shim_dir, units, calls)
    assert _resets(calls) == []
    assert "kept run-u5.service (no readable failure time)" in r.stdout


def test_old_failed_named_transient_with_empty_fragment_is_reset(tmp_path):
    shim_dir, units, calls = _setup(tmp_path)
    _unit(units, "adhoc-probe.service", transient=True, fragment="", failed_ago_s=3 * 86400)
    r = _run(tmp_path, shim_dir, units, calls)
    assert "rc=0" in r.stdout, r.stderr
    assert _resets(calls) == ["adhoc-probe.service"]


def test_recently_failed_transient_unit_is_kept(tmp_path):
    shim_dir, units, calls = _setup(tmp_path)
    _unit(
        units,
        "run-u124.service",
        transient=True,
        fragment=f"{_TRANSIENT_DIR}/run-u124.service",
        failed_ago_s=3600,
    )
    r = _run(tmp_path, shim_dir, units, calls)
    assert "rc=0" in r.stdout, r.stderr
    assert _resets(calls) == []


def test_genesis_units_are_never_reset(tmp_path):
    shim_dir, units, calls = _setup(tmp_path)
    _unit(
        units,
        "genesis-server.service",
        transient=False,
        fragment="/etc/systemd/user/genesis-server.service",
        failed_ago_s=5 * 86400,
    )
    # Even a transient genesis-* unit is someone's alarm, not litter.
    _unit(
        units,
        "genesis-adhoc.service",
        transient=True,
        fragment=f"{_TRANSIENT_DIR}/genesis-adhoc.service",
        failed_ago_s=5 * 86400,
    )
    r = _run(tmp_path, shim_dir, units, calls)
    assert "rc=0" in r.stdout, r.stderr
    assert _resets(calls) == []


def test_persistent_unit_with_fragment_is_never_reset(tmp_path):
    shim_dir, units, calls = _setup(tmp_path)
    _unit(
        units,
        "backup-sync.service",
        transient=False,
        fragment="/etc/systemd/user/backup-sync.service",
        failed_ago_s=9 * 86400,
    )
    # A run-u name is not enough on its own: systemd must call it transient.
    _unit(
        units,
        "run-u999.service",
        transient=False,
        fragment="/etc/systemd/user/run-u999.service",
        failed_ago_s=9 * 86400,
    )
    r = _run(tmp_path, shim_dir, units, calls)
    assert "rc=0" in r.stdout, r.stderr
    assert _resets(calls) == []


def test_not_found_unit_with_empty_fragment_is_never_reset(tmp_path):
    # MEASURED on systemd 255: a unit whose file was removed reports an EMPTY
    # FragmentPath and Transient=no. Empty FragmentPath alone is not "transient".
    shim_dir, units, calls = _setup(tmp_path)
    _unit(units, "removed-timer.service", transient=False, fragment="", failed_ago_s=9 * 86400)
    r = _run(tmp_path, shim_dir, units, calls)
    assert "rc=0" in r.stdout, r.stderr
    assert _resets(calls) == []


def test_transient_unit_with_a_non_runtime_fragment_is_never_reset(tmp_path):
    shim_dir, units, calls = _setup(tmp_path)
    _unit(
        units,
        "odd.service",
        transient=True,
        fragment="/etc/systemd/user/odd.service",
        failed_ago_s=9 * 86400,
    )
    r = _run(tmp_path, shim_dir, units, calls)
    assert "rc=0" in r.stdout, r.stderr
    assert _resets(calls) == []


def test_mixed_set_resets_only_the_old_transient_one(tmp_path):
    shim_dir, units, calls = _setup(tmp_path)
    _unit(
        units,
        "run-u1.service",
        transient=True,
        fragment=f"{_TRANSIENT_DIR}/run-u1.service",
        failed_ago_s=2 * 86400,
    )
    _unit(
        units,
        "run-u2.service",
        transient=True,
        fragment=f"{_TRANSIENT_DIR}/run-u2.service",
        failed_ago_s=600,
    )
    _unit(
        units,
        "genesis-watchdog.service",
        transient=False,
        fragment="/etc/systemd/user/genesis-watchdog.service",
        failed_ago_s=2 * 86400,
    )
    _unit(
        units,
        "backup-sync.service",
        transient=False,
        fragment="/etc/systemd/user/backup-sync.service",
        failed_ago_s=2 * 86400,
    )
    r = _run(tmp_path, shim_dir, units, calls)
    assert "rc=0" in r.stdout, r.stderr
    assert _resets(calls) == ["run-u1.service"]


def test_systemctl_absent_skips_and_the_script_continues(tmp_path):
    shim_dir, units, calls = _setup(tmp_path)
    empty_bin = tmp_path / "nobin"
    empty_bin.mkdir()
    (empty_bin / "date").symlink_to("/bin/date")
    r = _run(tmp_path, shim_dir, units, calls, path=str(empty_bin))
    assert "rc=0" in r.stdout, r.stderr
    assert "AFTER-STAGE" in r.stdout
    assert "SKIPPED" in r.stdout
    assert not calls.exists()


def test_unreachable_user_manager_skips_and_the_script_continues(tmp_path):
    shim_dir, units, calls = _setup(tmp_path)
    _unit(
        units,
        "run-u5.service",
        transient=True,
        fragment=f"{_TRANSIENT_DIR}/run-u5.service",
        failed_ago_s=2 * 86400,
    )
    r = _run(tmp_path, shim_dir, units, calls, extra_env={"SHIM_MANAGER_DOWN": "1"})
    assert "rc=0" in r.stdout, r.stderr
    assert "AFTER-STAGE" in r.stdout
    assert "SKIPPED" in r.stdout
    assert _resets(calls) == []


def test_stage_runs_from_maintenance_main():
    body = _HYGIENE.read_text()
    start = body.index("\nmaintenance_main() {")
    end = body.index("\nmain() {")
    assert "reset_failed_transient_units" in body[start:end]
