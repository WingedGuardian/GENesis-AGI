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
case " $* " in *" --version "*) exit "${SHIM_LITERAL_RC:-0}" ;; esac
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
        # Pin the runtime-dir seam to an absent path so no test depends on whether
        # the host has /run/user/<uid> (CI runners do, the local sandbox hides it);
        # the XDG tests pass their own.
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "SHIM_LOG": str(log),
               "SLOT_EXEC_CGROUP_FILE": str(neutral),
               "SLOT_EXEC_RUNTIME_DIR": str(tmp_path / "no-runtime-dir")}
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


@pytest.mark.parametrize(
    ("answer", "kept"),
    [("0", True), ("1", False), ("2", True), ("127", True)],
)
def test_only_a_definite_refusal_drops_the_literal_option(shim, answer, kept):
    r = shim("7", "--", "bash", "-c", "echo ran", env_extra={"SHIM_LITERAL_RC": answer})
    assert r.stdout.strip() == "ran", r.stderr
    calls = _calls(shim.log)
    assert len(calls) == 2, calls
    for call in calls:
        head = call.split(" -- ", 1)[0]
        assert ("--expand-environment=no" in head) is kept, call


def test_the_probe_runs_under_gnu_timeout_where_the_system_one_is_uutils(shim, tmp_path):
    """uutils' kill reaches the direct child alone, and a function cannot run under env."""
    bindir = tmp_path / "bin"
    log = tmp_path / "gnu.log"
    (bindir / "timeout").write_text(
        '#!/bin/sh\n[ "$1" = --version ] && { echo "timeout (uutils coreutils) 0.8.0"; exit 0; }\n'
        'echo "system timeout $*" >> "$GNU_LOG"\nshift 3\nexec "$@"\n'
    )
    (bindir / "gnutimeout").write_text(
        '#!/bin/sh\necho "gnutimeout $*" >> "$GNU_LOG"\nshift 3\nexec "$@"\n'
    )
    (bindir / "timeout").chmod(0o755)
    (bindir / "gnutimeout").chmod(0o755)
    r = shim("7", "--", "bash", "-c", "echo ran", env_extra={"GNU_LOG": str(log)})
    assert r.stdout.strip() == "ran", r.stderr
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith("gnutimeout -k 2 15 env "), lines


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
    # A failure that names no property ends the ladder at the first probe,
    # and is never followed by a scoped launch.
    calls = _calls(shim.log)
    assert len(calls) == 1 and calls[0].endswith(" true"), calls


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


def test_a_tmux_server_request_from_inside_a_slot_still_gets_its_own_scope(shim, tmp_path):
    """The nesting skip covers slot requests only. A tmux server started from a
    slot shell must not live, and die, with that slot."""
    cg = tmp_path / "cgroup"
    cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/genesis.slice/genesis-slots.slice/genesis-slot-3-ab.scope\n")
    r = shim("--unit-prefix", "genesis-tmux", "--slice", "app.slice", "--omit", "server", "--",
             "bash", "-c", "echo ran", env_extra={"SLOT_EXEC_CGROUP_FILE": str(cg)})
    assert r.stdout.strip() == "ran", r.stderr
    calls = _calls(shim.log)
    assert len(calls) == 2 and "--unit=genesis-tmux-server-" in calls[1], calls


def test_a_slot_scope_asks_for_the_memory_and_swap_cap(shim):
    r = shim("5", "--", "true")
    assert r.returncode == 0, r.stderr
    probe, launch = _calls(shim.log)
    for call in (probe, launch):  # the probe proves systemd accepts both properties
        assert "-p MemoryMax=55% -p MemorySwapMax=5% -p OOMPolicy=continue" in call, call


def test_the_tmux_server_scope_has_no_memory_cap(shim):
    shim("--unit-prefix", "genesis-tmux", "--slice", "app.slice", "--omit", "server", "--", "true")
    assert not any("MemoryMax" in c or "MemorySwapMax" in c or "OOMPolicy" in c
                   for c in _calls(shim.log))


@pytest.mark.parametrize(("value", "expected"), [
    ("20G", "20G"), ("40%", "40%"), ("infinity", "infinity"),
    ("0%", "55%"), ("100%", "55%"), ("20g", "55%"), ("1G;x", "55%"), ("", "55%"),
])
def test_memory_cap_override_is_validated(shim, value, expected):
    shim("5", "--", "true", env_extra={"SLOT_EXEC_MEMORY_MAX": value})
    assert len(_calls(shim.log)) == 2, _calls(shim.log)
    assert all(f"-p MemoryMax={expected} " in c for c in _calls(shim.log)), _calls(shim.log)


@pytest.mark.parametrize(("value", "expected"), [
    ("2G", "2G"), ("0", "0"), ("10%", "10%"), ("infinity", "infinity"),
    ("-1", "5%"), ("2g", "5%"), ("1G x", "5%"), ("", "5%"),
])
def test_swap_cap_override_is_validated(shim, value, expected):
    shim("5", "--", "true", env_extra={"SLOT_EXEC_SWAP_MAX": value})
    assert len(_calls(shim.log)) == 2, _calls(shim.log)
    assert all(f"-p MemorySwapMax={expected} " in c for c in _calls(shim.log)), _calls(shim.log)


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



_REFUSING_SHIM = """#!/usr/bin/env bash
case " $* " in *" --version "*) exit "${SHIM_LITERAL_RC:-0}" ;; esac
echo "ARGS=$*" >> "$SHIM_LOG"
for a in "$@"; do
    for bad in ${SHIM_REFUSE:-}; do
        case "$a" in "$bad"*) echo "Unknown assignment: $a" >&2; exit 1 ;; esac
    done
done
[ "${SHIM_TIMEOUT:-}" = 1 ] && exit 124
if [ -n "${SHIM_FAIL_FIRST:-}" ] && [ ! -e "$SHIM_FAIL_FIRST" ]; then
    : > "$SHIM_FAIL_FIRST"
    echo "Failed to connect to bus: Resource temporarily unavailable" >&2
    exit 1
fi
last=""
for a in "$@"; do last="$a"; done
[ "$last" = "true" ] && exit 0
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
exec "$@"
"""


@pytest.fixture()
def refusing(tmp_path):
    bindir = tmp_path / "rbin"
    bindir.mkdir()
    s = bindir / "systemd-run"
    s.write_text(_REFUSING_SHIM)
    s.chmod(0o755)
    log = tmp_path / "refusing.log"
    neutral = tmp_path / "cg"
    neutral.write_text("0::/user.slice/user-1000.slice/session-1.scope\n")

    def run(refuse: str, **extra):
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "SHIM_LOG": str(log),
               "SLOT_EXEC_CGROUP_FILE": str(neutral), "SLOT_EXEC_RUNTIME_DIR": str(tmp_path / "none"),
               "SHIM_REFUSE": refuse, **extra}
        return subprocess.run(["bash", str(_SCRIPT), "5", "--", "bash", "-c", "echo ran"],
                              env=env, capture_output=True, text=True, timeout=30)

    run.log = log
    return run


def test_older_systemd_without_scope_oompolicy_keeps_the_scope_and_the_caps(refusing):
    """systemd before 253 refuses OOMPolicy on a scope: the slot must still get
    its scope and its caps, silently (the property was a no-op there)."""
    r = refusing("OOMPolicy=")
    assert r.stdout.strip() == "ran" and r.stderr == "", r.stderr
    launch = _calls(refusing.log)[-1]
    assert "--unit=genesis-slot-5-" in launch
    assert "MemoryMax=55%" in launch and "OOMPolicy" not in launch


def test_a_refused_memory_cap_keeps_the_scope_and_says_so(refusing):
    r = refusing("OOMPolicy= MemoryMax=")
    assert r.stdout.strip() == "ran"
    assert "refused the session memory cap (" in r.stderr
    assert "); starting in a scope without it" in r.stderr
    launch = _calls(refusing.log)[-1]
    assert "--unit=genesis-slot-5-" in launch and "MemoryMax" not in launch


def test_a_failure_that_names_no_property_never_drops_oompolicy(refusing, tmp_path):
    """A bus hiccup is not a refusal: stepping down would silently drop
    OOMPolicy=continue on a systemd that supports it, so the first OOM kill
    would stop the whole session. Such a failure ends the ladder."""
    first = tmp_path / "first-call-done"
    r = refusing("", SHIM_FAIL_FIRST=str(first))
    assert r.stdout.strip() == "ran"
    calls = _calls(refusing.log)
    assert len(calls) == 1, calls
    assert "could not create a systemd scope (Failed to connect to bus" in r.stderr, r.stderr


def test_a_refused_cap_names_systemds_reason(refusing):
    r = refusing("OOMPolicy= MemoryMax=")
    assert "refused the session memory cap (Unknown assignment: MemoryMax=" in r.stderr, r.stderr


def test_a_probe_timeout_is_not_retried(refusing):
    r = refusing("", SHIM_TIMEOUT="1")
    assert r.stdout.strip() == "ran"
    assert "could not create a systemd scope (timed out after 15 s)" in r.stderr
    assert len(_calls(refusing.log)) == 1, _calls(refusing.log)
