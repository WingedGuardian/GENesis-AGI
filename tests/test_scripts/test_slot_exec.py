"""scripts/slot_exec.sh: run an interactive command in a systemd scope of its own.

systemd-run is shimmed on PATH, so these run on any install (no user manager
needed). The shim records each call and, for the real launch, execs the command
after `--` the way `systemd-run --scope` does.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "slot_exec.sh"

_SHIM = """#!/usr/bin/env bash
echo "XDG=${XDG_RUNTIME_DIR:-unset} ARGS=$*" >> "$SHIM_LOG"
last=""
for a in "$@"; do last="$a"; done
if [ "$last" = "true" ]; then
    [ -n "${SHIM_PROBE_ERR:-}" ] && echo "$SHIM_PROBE_ERR" >&2
    exit "${SHIM_PROBE_RC:-0}"
fi
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
INVOCATION_ID=deadbeef exec "$@"
"""


@pytest.fixture()
def shim(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    s = bindir / "systemd-run"
    s.write_text(_SHIM)
    s.chmod(0o755)
    log = tmp_path / "shim.log"

    def run(*args: str, env_extra: dict | None = None):
        neutral = tmp_path / "cgroup-neutral"
        neutral.write_text("0::/user.slice/user-1000.slice/session-1.scope\n")
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "SHIM_LOG": str(log),
               "SLOT_EXEC_CGROUP_FILE": str(neutral)}
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", str(_SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30
        )

    run.log = log
    return run


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_probe_then_scoped_launch(shim):
    r = shim("7", "--", "bash", "-c", "echo ran; exit 0")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ran"
    calls = _calls(shim.log)
    assert len(calls) == 2, calls
    assert "--slice=genesis-slots.slice" in calls[0] and calls[0].endswith(" true")
    assert "--unit=genesis-slot-7-" in calls[1]
    assert "-- env -u INVOCATION_ID" in calls[1]


@pytest.mark.parametrize("code", [0, 3, 137])
def test_exit_status_passes_through(shim, code):
    r = shim("1", "--", "bash", "-c", f"exit {code}")
    assert r.returncode == code


def test_systemd_injected_invocation_id_is_removed(shim):
    r = shim("1", "--", "bash", "-c", 'echo "INV=${INVOCATION_ID:-unset}"')
    assert "INV=unset" in r.stdout


def test_missing_runtime_dir_goes_to_systemd_run_only(shim, tmp_path):
    rt = tmp_path / "run-user"
    rt.mkdir()
    r = shim("1", "--", "bash", "-c", 'echo "XDG=${XDG_RUNTIME_DIR:-unset}"',
             env_extra={"SLOT_EXEC_RUNTIME_DIR": str(rt)})
    assert "XDG=unset" in r.stdout, "the command's environment gained XDG_RUNTIME_DIR"
    calls = _calls(shim.log)
    assert len(calls) == 2 and all(c.startswith(f"XDG={rt} ") for c in calls), calls


def test_absent_runtime_dir_is_not_invented(shim, tmp_path):
    r = shim("1", "--", "true", env_extra={"SLOT_EXEC_RUNTIME_DIR": str(tmp_path / "nope")})
    assert r.returncode == 0
    assert all(c.startswith("XDG=unset ") for c in _calls(shim.log))


def test_existing_runtime_dir_is_left_alone(shim, tmp_path):
    xdg = str(tmp_path)
    r = shim(
        "1",
        "--",
        "bash",
        "-c",
        'echo "XDG=${XDG_RUNTIME_DIR:-unset}"',
        env_extra={"XDG_RUNTIME_DIR": xdg},
    )
    assert f"XDG={xdg}" in r.stdout
    assert "-u XDG_RUNTIME_DIR" not in "\n".join(_calls(shim.log))


def test_refused_probe_runs_the_command_unscoped_once(shim):
    r = shim(
        "1",
        "--",
        "bash",
        "-c",
        "echo ran; exit 5",
        env_extra={"SHIM_PROBE_RC": "1", "SHIM_PROBE_ERR": "Failed to connect to bus"},
    )
    assert r.returncode == 5
    assert r.stdout.count("ran") == 1
    assert "could not create a systemd scope (Failed to connect to bus)" in r.stderr
    assert len(_calls(shim.log)) == 1, "a refused probe must not be followed by a scoped launch"


def test_no_systemd_run_runs_the_command_unscoped(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    bindir = tmp_path / "empty"
    bindir.mkdir()
    env["PATH"] = f"{bindir}"
    r = subprocess.run(
        ["/bin/bash", str(_SCRIPT), "1", "--", "/bin/bash", "-c", "echo ran; exit 4"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if "systemd-run not found" not in r.stderr:
        pytest.skip("systemd-run is reachable without PATH on this box")
    assert r.returncode == 4 and "ran" in r.stdout


def test_omit_and_custom_prefix_and_slice(shim):
    r = shim(
        "--unit-prefix", "genesis-tmux", "--slice", "app.slice", "--omit", "server", "--", "true"
    )
    assert r.returncode == 0, r.stderr
    calls = _calls(shim.log)
    assert all("--slice=app.slice" in c and "-p ManagedOOMPreference=omit" in c for c in calls), (
        calls
    )
    assert "--unit=genesis-tmux-server-" in calls[1]


def test_unsafe_label_is_replaced_not_passed(shim):
    r = shim("../x y", "--", "true")
    assert r.returncode == 0, r.stderr
    assert "--unit=genesis-slot-x-" in _calls(shim.log)[1]


@pytest.mark.parametrize("args", [["1", "true"], ["1"], [], ["--bogus", "1", "--", "true"], ["--slice", "x.slice"]])
def test_usage_errors_exit_64(shim, args):
    assert shim(*args).returncode == 64


def test_nested_call_inside_a_slot_scope_does_not_wrap_again(shim, tmp_path):
    cg = tmp_path / "cgroup"
    cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/genesis.slice/genesis-slots.slice/genesis-slot-3-ab.scope\n")
    r = shim("3", "--", "bash", "-c", "echo ran", env_extra={"SLOT_EXEC_CGROUP_FILE": str(cg)})
    assert r.stdout.strip() == "ran"
    assert not shim.log.exists(), "a command already in a slot scope was wrapped again"

    # A claude left in the tmux SERVER's scope must still be wrapped: that scope
    # carries an omit mark, and the claude must not inherit it.
    cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/genesis-tmux-server-ab.scope\n")
    r = shim("3", "--", "bash", "-c", "echo ran", env_extra={"SLOT_EXEC_CGROUP_FILE": str(cg)})
    assert r.stdout.strip() == "ran"
    assert len(_calls(shim.log)) == 2, _calls(shim.log)


def test_no_task_cap_and_no_argv_in_the_description(shim):
    r = shim("4", "--", "bash", "-c", "echo secret-prompt-text")
    assert r.returncode == 0, r.stderr
    probe, launch = _calls(shim.log)
    assert "-p TasksMax=infinity" in probe and "-p TasksMax=infinity" in launch
    assert "--description=Genesis genesis-slot 4" in launch
    assert "secret-prompt-text" not in launch.split(" -- ", 1)[0], "argv leaked into the unit description"


def test_callers_own_invocation_id_is_kept(shim):
    r = shim("1", "--", "bash", "-c", 'echo "INV=${INVOCATION_ID:-unset}"', env_extra={"INVOCATION_ID": "callers-own"})
    assert "INV=callers-own" in r.stdout


def test_callers_invocation_id_survives_with_runtime_dir_unset(shim, tmp_path):
    """`env` reads -u options before any NAME=value: both must coexist."""
    rt = tmp_path / "rt"
    rt.mkdir()
    r = shim("1", "--", "bash", "-c", 'echo "INV=${INVOCATION_ID:-unset} XDG=${XDG_RUNTIME_DIR:-unset}"',
             env_extra={"INVOCATION_ID": "callers-own", "SLOT_EXEC_RUNTIME_DIR": str(rt)})
    assert r.returncode == 0, r.stderr
    assert "INV=callers-own XDG=unset" in r.stdout


@pytest.mark.parametrize("flag", ["--slice", "--unit-prefix"])
def test_option_without_a_value_is_refused_not_looped(shim, flag):
    assert shim(flag).returncode == 64


def test_script_is_executable_and_parses():
    assert os.access(_SCRIPT, os.X_OK)
    subprocess.run(["bash", "-n", str(_SCRIPT)], check=True, timeout=10)


@pytest.mark.parametrize("bad", ["-.slice", "x;y.slice", "a b.slice", "nodot"])
def test_unsafe_slice_falls_back_to_the_default(shim, bad):
    r = shim("--slice", bad, "1", "--", "true")
    assert r.returncode == 0, r.stderr
    assert all("--slice=genesis-slots.slice" in c for c in _calls(shim.log)), _calls(shim.log)

