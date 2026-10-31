"""Deploy integration for the shared MCP services (MCP P7).

`scripts/lib/shared_mcp_units.sh` restarts each configured shared-MCP unit that
is installed AND enabled, and does nothing for any other. It is driven here as
a real bash process with a PATH-shimmed `systemctl` that records every call and
never reaches the user manager; the deploy wiring is driven through the
`_deploy_station` fixture, whose own systemctl shim is wrapped the same way.
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts._deploy_station import advance_upstream, alerts, run, station  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "lib" / "shared_mcp_units.sh"
SHIPPED = REPO / "config" / "mcp_shared.json"
UPDATE_SH = REPO / "scripts" / "update.sh"
DEPLOY = REPO / "scripts" / "deploy_code_only.sh"


def _shim(
    path: Path,
    log: Path,
    states: dict[str, str],
    fail_restart=frozenset(),
    unreadable=frozenset(),
    same_invocation=frozenset(),
):
    """A systemctl for *.service units, logging each argv. *states* maps a unit to
    "load:enabled:before:after" (ActiveState before and after a restart). A
    successful restart changes the InvocationID unless the unit is in
    *same_invocation*; `restart` exits 1 for *fail_restart*; `show` prints nothing
    for *unreadable* (a busy bus). Any other .service is not installed
    (LoadState=not-found). Names without .service go to $REAL_SYSTEMCTL when set."""
    lines = [
        "#!/bin/bash",
        'unit=""',
        'for a in "$@"; do case "$a" in *.service) unit="$a";; esac; done',
        f'flag="{log}.restarted.$unit"',
        'case "$unit" in',
    ]
    for unit in unreadable:
        lines += [f"  {unit})", f'    echo "$*" >> "{log}"', "    exit 0 ;;"]
    for unit, spec in states.items():
        load, enabled, before, after = spec.split(":")
        fails = "1" if unit in fail_restart else "0"
        bump = "" if unit in same_invocation else ' && touch "$flag"'
        lines += [
            f"  {unit})",
            f'    echo "$*" >> "{log}"',
            f'    act={before}; inv=inv-1; [ -f "$flag" ] && {{ act={after}; inv=inv-2; }}',
            '    if [[ " $* " == *" restart "* ]]; then',
            # A failed restart JOB can still leave a new process running: only
            # the exit code reports it.
            f'      true{bump}; [ {fails} = 0 ] || touch "$flag.tried"; exit {fails}',
            "    fi",
            f'    [ -f "$flag.tried" ] && act={after}',
            '    if [[ " $* " == *" --value "* ]]; then echo "$inv"; exit 0; fi',
            '    if [[ " $* " == *" show "* ]]; then',
            f'      echo "LoadState={load}"; echo "UnitFileState={enabled}"',
            '      echo "ActiveState=$act"; echo "InvocationID=$inv"; exit 0',
            "    fi",
            '    if [[ " $* " == *" is-active "* ]]; then [[ " $* " == *" --quiet "* ]] || echo "$act"; [ "$act" = active ]; exit; fi',
            "    exit 0 ;;",
        ]
    lines += [
        "  ?*)",
        f'    echo "$*" >> "{log}"',
        '    [[ " $* " == *" show "* ]] && { echo "LoadState=not-found"; echo "ActiveState=inactive"; }',
        "    exit 0 ;;",
        "esac",
        '[ -n "${REAL_SYSTEMCTL:-}" ] && exec "$REAL_SYSTEMCTL" "$@"',
        f'echo "$*" >> "{log}"',
        "exit 0",
    ]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _root(tmp_path, overlay=None, config=True):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    if config:
        shutil.copy(SHIPPED, root / "config" / "mcp_shared.json")
    home = tmp_path / "gh"
    (home / "config").mkdir(parents=True)
    if overlay is not None:
        (home / "config" / "mcp_shared.json").write_text(json.dumps(overlay))
    return root, home


def _run_lib(tmp_path, root, home, states, **shim):
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    log = tmp_path / "systemctl.log"
    _shim(shims / "systemctl", log, states, **shim)
    env = {
        "PATH": f"{shims}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(home),
    }
    r = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; . "{LIB}"; genesis_restart_shared_mcp_units "{root}"'],
        env=env,
        capture_output=True,
        text=True,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


ALL_ABSENT = {}
MEM = "genesis-mcp-memory.service"


def test_no_unit_installed_restarts_nothing(tmp_path):
    root, home = _root(tmp_path)
    r, calls = _run_lib(tmp_path, root, home, ALL_ABSENT)
    assert r.returncode == 0, r.stderr
    assert not [c for c in calls if "restart" in c.split()]


def _restarts(calls):
    return [c for c in calls if "restart" in c.split()]


def test_installed_but_disabled_unit_is_left_alone(tmp_path):
    root, home = _root(tmp_path)
    # Running but disabled (started by hand): enablement is the opt-in, not activity.
    states = {MEM: "loaded:disabled:active:active"}
    r, calls = _run_lib(tmp_path, root, home, states)
    assert r.returncode == 0, r.stderr
    assert not _restarts(calls)


def test_enabled_unit_is_restarted_and_checked(tmp_path):
    root, home = _root(tmp_path)
    states = {
        MEM: "loaded:enabled:active:active",
        "genesis-mcp-health.service": "loaded:disabled:inactive:inactive",
    }
    r, calls = _run_lib(tmp_path, root, home, states)
    assert r.returncode == 0, r.stderr
    assert _restarts(calls) == [f"--user restart -- {MEM}"]
    assert f"--user is-active --quiet -- {MEM}" in calls
    assert f"Restarted shared MCP unit {MEM}" in r.stdout


def test_failed_enabled_unit_is_restarted(tmp_path):
    """A unit that crashed is restarted onto the new code; only a STOPPED one is left alone."""
    root, home = _root(tmp_path)
    r, calls = _run_lib(tmp_path, root, home, {MEM: "loaded:enabled:failed:active"})
    assert r.returncode == 0, r.stderr
    assert _restarts(calls) == [f"--user restart -- {MEM}"]


def test_enabled_but_stopped_unit_stays_stopped(tmp_path):
    root, home = _root(tmp_path)
    r, calls = _run_lib(tmp_path, root, home, {MEM: "loaded:enabled:inactive:active"})
    assert r.returncode == 0, r.stderr
    assert not _restarts(calls)
    assert "left stopped" in r.stdout


def test_restart_failure_returns_nonzero_naming_the_unit(tmp_path):
    """The old process can still be active after a failed restart job: the exit
    code, not is-active, is what says the restart failed."""
    root, home = _root(tmp_path)
    states = {MEM: "loaded:enabled:active:active"}
    r, _ = _run_lib(tmp_path, root, home, states, fail_restart={MEM})
    assert r.returncode == 1
    assert f"FAILED: shared MCP unit {MEM}" in r.stdout


def test_restarted_but_not_active_is_a_failure(tmp_path):
    root, home = _root(tmp_path)
    r, _ = _run_lib(tmp_path, root, home, {MEM: "loaded:enabled:active:failed"})
    assert r.returncode == 1


def test_same_invocation_after_the_restart_is_a_failure(tmp_path):
    root, home = _root(tmp_path)
    states = {MEM: "loaded:enabled:active:active"}
    r, _ = _run_lib(tmp_path, root, home, states, same_invocation={MEM})
    assert r.returncode == 1


def test_unreadable_unit_state_is_a_failure_not_an_absence(tmp_path):
    root, home = _root(tmp_path)
    r, calls = _run_lib(tmp_path, root, home, {}, unreadable={MEM})
    assert r.returncode == 1
    assert f"cannot read the state of shared MCP unit {MEM}" in r.stdout
    assert not _restarts(calls)


def test_overlay_unit_name_is_the_one_restarted(tmp_path):
    root, home = _root(
        tmp_path, overlay={"servers": {"memory": {"unit": "genesis-mcp-custom.service"}}}
    )
    states = {
        "genesis-mcp-custom.service": "loaded:enabled:active:active",
        MEM: "loaded:enabled:active:active",
    }
    r, calls = _run_lib(tmp_path, root, home, states)
    assert r.returncode == 0, r.stderr
    assert _restarts(calls) == ["--user restart -- genesis-mcp-custom.service"]


def test_a_unit_named_twice_is_restarted_once(tmp_path):
    root, home = _root(tmp_path, overlay={"servers": {"health": {"unit": MEM}}})
    r, calls = _run_lib(tmp_path, root, home, {MEM: "loaded:enabled:active:active"})
    assert r.returncode == 0, r.stderr
    assert _restarts(calls) == [f"--user restart -- {MEM}"]


@pytest.mark.parametrize(
    "unit",
    [
        "genesis-server.service",
        "genesis-tmp-watchgod.service",
        "-H",
        "genesis-mcp-x.service; y",
        "mem",
    ],
)
def test_a_unit_name_that_is_not_a_shared_mcp_unit_is_refused(tmp_path, unit):
    root, home = _root(tmp_path, overlay={"servers": {"memory": {"unit": unit}}})
    r, calls = _run_lib(tmp_path, root, home, {})
    assert r.returncode == 1
    assert calls == []


def test_no_config_file_is_a_no_op(tmp_path):
    root, home = _root(tmp_path, config=False)
    r, calls = _run_lib(tmp_path, root, home, ALL_ABSENT)
    assert r.returncode == 0, r.stderr
    assert calls == []


def test_unreadable_config_is_reported_not_fatal(tmp_path):
    root, home = _root(tmp_path, overlay=None)
    (home / "config" / "mcp_shared.json").write_text("{not json")
    r, calls = _run_lib(tmp_path, root, home, ALL_ABSENT)
    assert r.returncode == 1
    assert "mcp_shared" in r.stdout + r.stderr
    assert calls == []


# ── Wiring ────────────────────────────────────────────────────────────


def _wrap_station_systemctl(st, states, **shim):
    shims = st["shims"]
    real = shims / "systemctl.station"
    (shims / "systemctl").rename(real)
    _shim(shims / "systemctl", st["calls"], states, **shim)
    st["env"]["REAL_SYSTEMCTL"] = str(real)


def _push_runtime_change(st, files):
    advance_upstream(st, "runtime change", files)


def test_deploy_restarts_an_enabled_shared_unit_after_the_server(station):  # noqa: F811
    st = station
    _push_runtime_change(st, {"src/m.py": "x = 1\n", "config/mcp_shared.json": SHIPPED.read_text()})
    _wrap_station_systemctl(st, {MEM: "loaded:enabled:active:active"})
    r = run(st, "deploy")
    assert r.returncode == 0, (r.stdout, r.stderr)
    calls = st["calls"].read_text().splitlines()
    server = calls.index("--user restart genesis-server")
    mcp = calls.index(f"--user restart -- {MEM}")
    assert mcp > server


def test_deploy_without_an_enabled_unit_restarts_only_the_server(station):  # noqa: F811
    st = station
    _push_runtime_change(st, {"src/m.py": "x = 1\n", "config/mcp_shared.json": SHIPPED.read_text()})
    _wrap_station_systemctl(st, {MEM: "loaded:disabled:active:active"})
    r = run(st, "deploy")
    assert r.returncode == 0, (r.stdout, r.stderr)
    restarts = [c for c in st["calls"].read_text().splitlines() if " restart " in f" {c} "]
    assert restarts == ["--user restart genesis-server"]


def test_deploy_alerts_and_fails_when_a_shared_unit_does_not_come_back(station):  # noqa: F811
    st = station
    _push_runtime_change(st, {"src/m.py": "x = 1\n", "config/mcp_shared.json": SHIPPED.read_text()})
    _wrap_station_systemctl(
        st,
        {MEM: "loaded:enabled:active:failed"},
        fail_restart={MEM},
    )
    r = run(st, "deploy")
    assert r.returncode == 1, (r.stdout, r.stderr)
    bodies = [json.loads(p.read_text()) for p in alerts(st)]
    assert any("genesis-mcp-memory.service" in json.dumps(b) for b in bodies), bodies


def test_update_sh_restarts_shared_units_on_the_success_path_only():
    """update.sh is too large to drive end to end here; pin the call's PLACE: after
    the success disarm and the watchgod restart, never on the nothing-to-do path."""
    text = UPDATE_SH.read_text()
    success = text.index("# ── Success: disarm trap")
    call = text.index("genesis_restart_shared_mcp_units", success)
    assert call > text.index("_restart_tmp_watchgod_if_stale", success)
    assert text.count("genesis_restart_shared_mcp_units") == 1
