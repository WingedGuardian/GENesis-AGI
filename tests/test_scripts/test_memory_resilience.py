"""Behavioral tests for memory_resilience_apply (scripts/lib/memory_resilience.sh).

The function is sourced from the REAL lib and driven with stubbed
``sudo``/``systemctl``/``systemd-detect-virt``/``swapon`` on PATH plus
MEMRES_* path overrides, so the logic under test is the shipped code:
adaptive systemd-oomd pressure-kill setup (idempotent drop-ins, graceful
degradation without systemd/oomd/PSI/sudo) and the warn-only swap
invariant check (vantage-aware: container vs bare/VM).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "memory_resilience.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
INSTALL = REPO_ROOT / "scripts" / "install.sh"

_SUDO_STUB = """#!/bin/bash
# Passthrough sudo: "-n true" honors $SUDO_N_RC (0 = passwordless ok).
if [ "$1" = "-n" ] && [ "$2" = "true" ]; then exit "${SUDO_N_RC:-0}"; fi
exec "$@"
"""

_SYSTEMCTL_STUB = """#!/bin/bash
if [ "$1" = "list-unit-files" ]; then
    printf '%s' "${OOMD_UNIT_LINE-systemd-oomd.service enabled enabled}"
    exit 0
fi
# Reads of the user manager's root slice: answered from the environment, never
# logged (a read is not systemd churn).
if [ "$1" = "--user" ] && [ "$2" = "show" ]; then
    case "$*" in
        *ManagedOOMMemoryPressureLimit*) printf '%s\\n' "${UM_ROOT_LIMIT-2576980377}" ;;
        *ManagedOOMMemoryPressure*) printf '%s\\n' "${UM_ROOT_MODE-}" ;;
        *ManagedOOMPreference*init.scope*) printf '%s\\n' "${UM_INIT_PREF-omit}" ;;
        *ManagedOOMPreference*dbus.service*) printf '%s\\n' "${UM_DBUS_PREF-avoid}" ;;
        *ControlGroup*) printf '%s\\n' "${UM_ROOT_CG-/user.slice/user-1000.slice/user@1000.service}" ;;
    esac
    exit 0
fi
# PID 1's view of user.slice and oomd's state: reads, answered from the
# environment and never logged.
if [ "$1" = "show" ] && [ "${*: -1}" = "user.slice" ]; then
    printf '%s\\n%s\\n' "${SYS_SLICE_MODE-kill}" "${SYS_SLICE_LIMIT-3435973836}"
    exit 0
fi
if [ "$1" = "is-active" ]; then exit "${OOMD_ACTIVE_RC:-0}"; fi
echo "$@" >> "$SYSTEMCTL_LOG"
if [ "$1" = "--user" ] && [ "$2" = "daemon-reload" ]; then exit "${USER_RELOAD_RC:-0}"; fi
exit 0
"""

# oomctl: prints a Memory Pressure Monitored list naming $OOMCTL_PATHS
# (space-separated); OOMCTL_RC makes it fail; OOMCTL_FLAP_FILE lists the paths
# on the first call only (a report that is erased right after).
_OOMCTL_STUB = """#!/bin/bash
[ -n "${OOMCTL_RC:-}" ] && exit "$OOMCTL_RC"
if [ -n "${OOMCTL_FLAP_FILE:-}" ]; then
    if [ -e "$OOMCTL_FLAP_FILE" ]; then OOMCTL_PATHS=/user.slice; else : > "$OOMCTL_FLAP_FILE"; fi
fi
echo "Dry Run: no"
echo "Swap Monitored CGroups:"
echo "Memory Pressure Monitored CGroups:"
for p in ${OOMCTL_PATHS-/user.slice}; do printf '\\tPath: %s\\n' "$p"; done
"""

_ID_STUB = """#!/bin/bash
if [ "$1" = "-u" ]; then echo "${FAKE_UID:-1000}"; exit 0; fi
exec /usr/bin/id "$@"
"""

_DETECT_VIRT_STUB = """#!/bin/bash
# --container: rc 0 = we are a container, rc 1 = bare/VM.
exit "${DETECT_VIRT_RC:-0}"
"""

_SWAPON_STUB = """#!/bin/bash
printf '%s' "${SWAPON_OUT-}"
"""


def _stamped(body: str) -> str:
    """What scripts/lib/managed_units.py stamp writes for <body>."""
    body = body.rstrip("\n") + "\n"
    return body + f"# genesis-managed v1 sha256={hashlib.sha256(body.encode()).hexdigest()}\n"


def _stage(tmp_path: Path) -> dict:
    """Stub bin dir + fake roots; returns the env overlay for _run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("sudo", _SUDO_STUB),
        ("systemctl", _SYSTEMCTL_STUB),
        ("systemd-detect-virt", _DETECT_VIRT_STUB),
        ("swapon", _SWAPON_STUB),
        ("oomctl", _OOMCTL_STUB),
        ("id", _ID_STUB),
    ):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)

    psi = tmp_path / "pressure" / "memory"
    psi.parent.mkdir()
    psi.write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    swap_max = tmp_path / "cgroup" / "memory.swap.max"
    swap_max.parent.mkdir()
    swap_max.write_text("max\n")

    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        "MEMRES_ETC_ROOT": str(tmp_path / "etc"),
        # Real path: the test host runs systemd, so this guard passes there;
        # the no-systemd test overrides it with a missing dir.
        "MEMRES_SYSTEMD_RUNTIME_DIR": "/run/systemd/system",
        "MEMRES_PSI_FILE": str(psi),
        "MEMRES_CGROUP_SWAP_MAX": str(swap_max),
        "MEMRES_USER_UNIT_DIR": str(tmp_path / "user-units"),
        "MEMRES_OOMCTL_POLL_S": "0",
        "UM_ROOT_MODE": "kill",
        "MEMRES_OOMCTL_CONFIRM_S": "0",
    }


def _run(env_overlay: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; memory_resilience_apply'],
        capture_output=True,
        text=True,
        timeout=30,
        env={"HOME": env_overlay.get("MEMRES_ETC_ROOT", "/tmp"), **env_overlay},
    )


def _dropins(env: dict) -> dict[str, str]:
    etc = Path(env["MEMRES_ETC_ROOT"])
    return {
        "user_slice": (etc / "systemd/system/user.slice.d/genesis-oomd.conf"),
        "user_service": (etc / "systemd/system/user@.service.d/genesis-oomd.conf"),
        "oomd_conf": (etc / "systemd/oomd.conf.d/genesis.conf"),
    }


@pytest.mark.parametrize("partial", [False, True])
def test_failed_metadata_migration_reports_actual_memory_policy_state(tmp_path, partial):
    env = _stage(tmp_path)
    assert _run(env).returncode == 0
    originals = {}
    for path in _dropins(env).values():
        legacy = path.read_text().split("# genesis-managed v1 sha256=", 1)[0]
        path.write_text(legacy)
        originals[path] = legacy.encode()
    log = Path(env["SYSTEMCTL_LOG"])
    log.write_text("")
    fault = 'head -c 7 > "$2"; ' if partial else ''
    sudo = tmp_path / "bin/sudo"
    sudo.write_text(_SUDO_STUB.replace('exec "$@"',
        f'if [ "$1" = tee ]; then {fault}exit 1; fi\nexec "$@"'))
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("NOT fully applied" in result.stdout) == partial
    assert ("policy already in place" in result.stdout) == (not partial)
    for path, original in originals.items():
        assert path.read_bytes() == (original[:7] if partial else original)
    calls = log.read_text()
    assert "daemon-reload" not in calls and "restart" not in calls


def test_fresh_apply_writes_dropins_and_reloads(tmp_path):
    env = _stage(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "policy applied" in result.stdout

    files = _dropins(env)
    assert files["user_slice"].read_text() == _stamped(
        "[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=80%\n"
    )
    assert files["user_service"].read_text() == _stamped(
        "[Service]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=60%\n"
    )
    assert files["oomd_conf"].read_text() == _stamped(
        "[OOM]\nSwapUsedLimit=90%\nDefaultMemoryPressureLimit=60%\n"
        "DefaultMemoryPressureDurationSec=20s\n"
    )
    # Pressure thresholds are percentages only — the adaptive contract.
    for body in (f.read_text() for f in files.values()):
        for line in body.splitlines():
            if "Limit" in line and "Sec" not in line:
                assert line.endswith("%"), f"non-percentage limit: {line}"

    calls = Path(env["SYSTEMCTL_LOG"]).read_text()
    assert "daemon-reload" in calls
    assert "enable systemd-oomd" in calls  # no --now: restart below does the single start
    assert "restart systemd-oomd" in calls
    # The user half verifies AFTER the /etc half's oomd restart.
    assert calls.index("restart systemd-oomd") < calls.index("--user daemon-reload")


def test_second_run_is_a_noop(tmp_path):
    env = _stage(tmp_path)
    _run(env)
    Path(env["SYSTEMCTL_LOG"]).write_text("")

    result = _run(env)
    assert result.returncode == 0
    assert "already in place" in result.stdout
    assert Path(env["SYSTEMCTL_LOG"]).read_text() == ""  # no systemd churn


def test_no_systemd_skips_cleanly(tmp_path):
    env = _stage(tmp_path)
    env["MEMRES_SYSTEMD_RUNTIME_DIR"] = str(tmp_path / "does-not-exist")
    result = _run(env)
    assert result.returncode == 0
    assert "not a systemd system" in result.stdout
    assert not _dropins(env)["user_slice"].exists()


def test_no_oomd_unit_skips_cleanly(tmp_path):
    env = _stage(tmp_path)
    env["OOMD_UNIT_LINE"] = ""  # list-unit-files finds nothing
    result = _run(env)
    assert result.returncode == 0
    assert "systemd-oomd not available" in result.stdout
    assert not _dropins(env)["user_slice"].exists()


def test_no_psi_skips_cleanly(tmp_path):
    env = _stage(tmp_path)
    env["MEMRES_PSI_FILE"] = str(tmp_path / "no-psi-here")
    result = _run(env)
    assert result.returncode == 0
    assert "PSI not available" in result.stdout
    assert not _dropins(env)["user_slice"].exists()


def test_no_noninteractive_sudo_skips_with_remediation(tmp_path):
    env = _stage(tmp_path)
    env["SUDO_N_RC"] = "1"
    result = _run(env)
    assert result.returncode == 0
    assert "sudo unavailable" in result.stdout
    assert "memory_resilience_apply" in result.stdout  # manual remediation line
    assert not _dropins(env)["user_slice"].exists()


def test_swap_max_zero_container_vantage_names_host_remediation(tmp_path):
    env = _stage(tmp_path)
    Path(env["MEMRES_CGROUP_SWAP_MAX"]).write_text("0\n")
    env["DETECT_VIRT_RC"] = "0"  # container
    result = _run(env)
    assert result.returncode == 0
    assert "memory.swap.max is 0" in result.stdout
    assert "limits.memory.swap true" in result.stdout  # host-side knob named


def test_swap_max_zero_bare_vantage_generic_remediation(tmp_path):
    env = _stage(tmp_path)
    Path(env["MEMRES_CGROUP_SWAP_MAX"]).write_text("0\n")
    env["DETECT_VIRT_RC"] = "1"  # bare/VM
    result = _run(env)
    assert result.returncode == 0
    assert "memory.swap.max is 0" in result.stdout
    assert "limits.memory.swap" not in result.stdout  # container knob NOT suggested


def test_bare_vantage_without_swap_device_warns(tmp_path):
    env = _stage(tmp_path)
    env["DETECT_VIRT_RC"] = "1"  # bare/VM
    env["SWAPON_OUT"] = ""  # no swap devices
    result = _run(env)
    assert result.returncode == 0
    assert "no swap device configured" in result.stdout


def test_container_vantage_ignores_missing_swap_device(tmp_path):
    # Inside a container swapon shows nothing even when host swap exists —
    # the device check must not fire there (the cgroup knob is the signal).
    env = _stage(tmp_path)
    env["DETECT_VIRT_RC"] = "0"  # container
    env["SWAPON_OUT"] = ""
    result = _run(env)
    assert result.returncode == 0
    assert "no swap device" not in result.stdout


def test_failed_write_warns_instead_of_claiming_already_in_place(tmp_path):
    env = _stage(tmp_path)
    # Existing etc content differs AND the target dir is unwritable via the
    # stub (make the etc root a read-only dir) -> tee fails.
    etc = Path(env["MEMRES_ETC_ROOT"])
    etc.mkdir()
    etc.chmod(0o555)
    try:
        result = _run(env)
    finally:
        etc.chmod(0o755)
    assert result.returncode == 0
    assert "could not write" in result.stdout
    assert "already in place" not in result.stdout


def test_later_dropin_reverting_policy_not_detected_as_protected():
    # Companion to the collector fact: the lib writes genesis-oomd.conf, but
    # systemd applies drop-ins lexicographically with last-assignment-wins.
    # This is covered collector-side (test_collectors_container) — here we
    # only pin that the lib's own drop-in name sorts BEFORE a conventional
    # zz-*.conf operator override, so operators can override us, not vice versa.
    assert "genesis-oomd.conf" < "zz-local.conf"


def test_bootstrap_wires_the_lib():
    text = BOOTSTRAP.read_text()
    assert 'source "$SCRIPT_DIR/lib/memory_resilience.sh"' in text
    assert "memory_resilience_apply" in text


def test_bootstrap_provisions_the_oomd_package():
    """The oomd layer is dead unless bootstrap installs the systemd-oomd package.

    memory_resilience_apply SKIPS pressure-kill setup when systemd-oomd is
    absent (a separate apt/dnf package on minimal installs), leaving the box
    silently unprotected — the exact gap a sibling install exposed. bootstrap
    must provision it, and BEFORE it applies the lib (install → apply order),
    so the same run that would otherwise skip now succeeds.
    """
    text = BOOTSTRAP.read_text()
    assert "install_pkg systemd-oomd" in text
    # Ordering: the install must precede sourcing/applying the lib, or the first
    # run still skips. Anchor to the `source` code line (unambiguous — the string
    # "memory_resilience_apply" also appears in the explanatory comment above).
    source_line = 'source "$SCRIPT_DIR/lib/memory_resilience.sh"'
    assert text.index("install_pkg systemd-oomd") < text.index(source_line)


def test_update_sh_wires_the_lib_visibly():
    # update.sh pipes bootstrap through `tail -10`, which eats the swap
    # warning — the retrofit path re-runs the (idempotent) apply unpiped.
    text = (REPO_ROOT / "scripts" / "update.sh").read_text()
    assert "lib/memory_resilience.sh" in text
    assert text.count("memory_resilience_apply") >= 1


def test_install_wires_the_lib():
    # install.sh (the fresh-container path) must apply memory-resilience too —
    # bootstrap.sh and update.sh did, but install.sh did not, so a fresh
    # container install sat unprotected against the OOM/fork wedge with NO
    # automatic trigger to run update.sh (the salvaged red-team gap).
    text = INSTALL.read_text()
    assert 'source "$SCRIPT_DIR/lib/memory_resilience.sh"' in text
    assert "memory_resilience_apply" in text
    assert "pid_budget_apply" in text


def test_install_provisions_the_oomd_package_with_its_own_idiom():
    """install.sh must provision systemd-oomd BEFORE applying the lib — using
    install.sh's OWN package helper (``_install_pkg``), never bootstrap's bare
    ``install_pkg``.

    install.sh defines ``_install_pkg``/``_PKG_MGR`` (underscore-prefixed); it
    has no ``install_pkg``. A verbatim copy of bootstrap's block would call an
    undefined function and hard-abort a fresh install under ``set -euo
    pipefail``. Lock the idiom and the install→apply ordering.
    """
    text = INSTALL.read_text()
    assert "_install_pkg systemd-oomd" in text
    # No BARE install_pkg call (not preceded by the underscore) — that function
    # is undefined in install.sh and would abort the run.
    assert re.search(r"(?<!_)install_pkg systemd-oomd", text) is None
    # Ordering: provision must precede sourcing/applying the lib, or the first
    # run still skips pressure-kill setup (oomd-absent → silent skip).
    source_line = 'source "$SCRIPT_DIR/lib/memory_resilience.sh"'
    assert text.index("_install_pkg systemd-oomd") < text.index(source_line)


def test_install_guards_the_lib_source():
    # The block must be guarded so a tree missing the lib degrades (echoes a
    # warning) instead of aborting install.sh — mirrors bootstrap's guard and
    # relies on the lib's own never-abort contract.
    text = INSTALL.read_text()
    assert '[[ -f "$SCRIPT_DIR/lib/memory_resilience.sh" ]]' in text


# ── pid_budget_apply (per-user-slice TasksMax=60% provisioning) ──────────────


def _run_pid(env_overlay: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; pid_budget_apply'],
        capture_output=True,
        text=True,
        timeout=30,
        env={"HOME": env_overlay.get("MEMRES_ETC_ROOT", "/tmp"), **env_overlay},
    )


def _tasksmax_dropin(env: dict) -> Path:
    return Path(env["MEMRES_ETC_ROOT"]) / "systemd/system/user-.slice.d/90-genesis-tasksmax.conf"


def test_pid_budget_apply_writes_percentage_dropin_and_reloads(tmp_path):
    env = _stage(tmp_path)
    result = _run_pid(env)
    assert result.returncode == 0, result.stderr
    assert "PID ceiling raised" in result.stdout
    # A PERCENTAGE (self-calibrating), never an absolute count — the whole point.
    assert _tasksmax_dropin(env).read_text() == _stamped("[Slice]\nTasksMax=60%\n")
    assert "daemon-reload" in Path(env["SYSTEMCTL_LOG"]).read_text()


def test_pid_budget_apply_is_idempotent(tmp_path):
    env = _stage(tmp_path)
    _run_pid(env)
    Path(env["SYSTEMCTL_LOG"]).write_text("")
    result = _run_pid(env)
    assert result.returncode == 0
    assert "already provisioned" in result.stdout
    assert Path(env["SYSTEMCTL_LOG"]).read_text() == ""  # no systemd churn


def test_pid_budget_no_sudo_skips_with_posture_surface(tmp_path):
    env = _stage(tmp_path)
    env["SUDO_N_RC"] = "1"
    result = _run_pid(env)
    assert result.returncode == 0
    assert "sudo unavailable" in result.stdout
    assert "posture check" in result.stdout  # surfaces instead of silently capping
    assert not _tasksmax_dropin(env).exists()


def test_pid_budget_no_systemd_skips_cleanly(tmp_path):
    env = _stage(tmp_path)
    env["MEMRES_SYSTEMD_RUNTIME_DIR"] = str(tmp_path / "does-not-exist")
    result = _run_pid(env)
    assert result.returncode == 0
    assert "not a systemd system" in result.stdout
    assert not _tasksmax_dropin(env).exists()


def test_pid_budget_dropin_wins_over_systemd_default():
    # 90- must sort AFTER systemd's stock 10-defaults.conf so last-assignment-wins
    # makes our 60% the effective cap, not the 33% default.
    assert "10-defaults.conf" < "90-genesis-tasksmax.conf"


def test_bootstrap_and_update_wire_pid_budget_apply():
    assert "pid_budget_apply" in BOOTSTRAP.read_text()
    assert "pid_budget_apply" in (REPO_ROOT / "scripts" / "update.sh").read_text()


# ── Hand edits are kept, not overwritten (scripts/lib/managed_etc.sh) ────────


_SLICE = "[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=80%\n"
_SLICE_V1 = "[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=60%\n"
_SERVICE = "[Service]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=60%\n"


def test_a_drop_in_edited_by_hand_is_kept_and_named(tmp_path):
    env = _stage(tmp_path)
    files = _dropins(env)
    files["user_slice"].parent.mkdir(parents=True)
    edited = "[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=70%\n"
    files["user_slice"].write_text(edited)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert files["user_slice"].read_text() == edited
    assert "was edited by hand" in result.stdout and str(files["user_slice"]) in result.stdout
    assert "a drop-in edited by hand was kept" in result.stdout
    # The other two still land, and they changed, so oomd restarts for them.
    assert files["oomd_conf"].read_text().startswith("[OOM]")
    assert "restart systemd-oomd" in Path(env["SYSTEMCTL_LOG"]).read_text()


def test_an_unstamped_genesis_drop_in_is_stamped_without_restarting_oomd(tmp_path):
    """Every install from before stamps holds the bare content. Adding the stamp
    changes only a comment: reload, but never restart the OOM killer for it."""
    env = _stage(tmp_path)
    bare = {
        "user_slice": _SLICE,
        "user_service": _SERVICE,
        "oomd_conf": "[OOM]\nSwapUsedLimit=90%\nDefaultMemoryPressureLimit=60%\n"
        "DefaultMemoryPressureDurationSec=20s\n",
    }
    for key, path in _dropins(env).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(bare[key])
    result = _run(env)
    assert result.returncode == 0, result.stderr
    for key, path in _dropins(env).items():
        assert path.read_text() == _stamped(bare[key])
    calls = Path(env["SYSTEMCTL_LOG"]).read_text()
    assert "daemon-reload" in calls
    assert "restart systemd-oomd" not in calls


def test_a_drop_in_with_an_older_genesis_stamp_is_rewritten(tmp_path):
    env = _stage(tmp_path)
    path = _dropins(env)["user_slice"]
    path.parent.mkdir(parents=True)
    path.write_text(
        _stamped("[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=55%\n")
    )
    _run(env)
    assert path.read_text() == _stamped(_SLICE)


def test_a_taken_drop_in_is_backed_up_then_written(tmp_path):
    env = _stage(tmp_path)
    path = _dropins(env)["user_slice"]
    path.parent.mkdir(parents=True)
    path.write_text("[Slice]\nMine=1\n")
    backups = tmp_path / "backups"
    env["GENESIS_TAKE_TEMPLATES"] = str(path)
    env["GENESIS_MU_BACKUP_DIR"] = str(backups)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert path.read_text() == _stamped(_SLICE)
    saved = backups / str(path).lstrip("/")
    assert saved.read_text() == "[Slice]\nMine=1\n"
    assert saved.stat().st_mode & 0o777 == 0o600


def test_a_bare_star_takes_units_only_never_etc_files(tmp_path):
    env = _stage(tmp_path)
    path = _dropins(env)["user_slice"]
    path.parent.mkdir(parents=True)
    path.write_text("[Slice]\nMine=1\n")
    env["GENESIS_TAKE_TEMPLATES"] = "*"
    _run(env)
    assert path.read_text() == "[Slice]\nMine=1\n"
    env["GENESIS_TAKE_TEMPLATES"] = "etc:*"
    env["GENESIS_MU_BACKUP_DIR"] = str(tmp_path / "b")
    _run(env)
    assert path.read_text() == _stamped(_SLICE)


def test_a_symlinked_drop_in_is_kept(tmp_path):
    env = _stage(tmp_path)
    path = _dropins(env)["user_slice"]
    path.parent.mkdir(parents=True)
    target = tmp_path / "elsewhere.conf"
    target.write_text("[Slice]\nMine=1\n")
    path.symlink_to(target)
    result = _run(env)
    assert result.returncode == 0
    assert path.is_symlink() and target.read_text() == "[Slice]\nMine=1\n"
    assert "is a symlink (a masked unit?)" in result.stdout
    assert "NOT fully applied" in result.stdout  # a masked drop-in means the policy is off


def test_the_run_summary_names_a_kept_file_without_counting_it_as_a_unit(tmp_path):
    env = _stage(tmp_path)
    path = _dropins(env)["user_slice"]
    path.parent.mkdir(parents=True)
    path.write_text("[Slice]\nMine=1\n")
    summary = tmp_path / "summary.txt"
    env["GENESIS_DEPLOY_SUMMARY"] = str(summary)
    env["GENESIS_ETC_SUMMARY_OK"] = "1"
    _run(env)
    lines = summary.read_text().splitlines()
    assert any(line.startswith(f"kept-etc {path}: edited by hand") for line in lines)
    assert not any(line.startswith("kept ") for line in lines)  # bootstrap's unit count


# --- the user-manager monitor and the user.slice decision (systemd #43174) ---

_UM_CG = "/user.slice/user-1000.slice/user@1000.service"
_UM_ROOT = "[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=60%\n"
_UM_INIT = "[Scope]\nManagedOOMPreference=omit\n"
_UM_DBUS = "[Service]\nManagedOOMPreference=avoid\n"


def _user_dropins(env: dict) -> dict[str, Path]:
    d = Path(env["MEMRES_USER_UNIT_DIR"])
    return {
        "root": d / "-.slice.d/genesis-oomd.conf",
        "init": d / "init.scope.d/genesis-oomd.conf",
        "dbus": d / "dbus.service.d/genesis-oomd.conf",
    }


def _live(env: dict) -> dict:
    """The user manager reports kill for its root slice and oomd lists it."""
    env.update({"UM_ROOT_MODE": "kill", "OOMCTL_PATHS": f"/user.slice {_UM_CG}"})
    return env


def _slice_text(env: dict) -> str:
    return _dropins(env)["user_slice"].read_text()


def test_user_half_writes_the_three_drop_ins_and_reloads_once(tmp_path):
    env = _stage(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    files = _user_dropins(env)
    assert files["root"].read_text() == _stamped(_UM_ROOT)
    assert files["init"].read_text() == _stamped(_UM_INIT)
    assert files["dbus"].read_text() == _stamped(_UM_DBUS)
    assert Path(env["SYSTEMCTL_LOG"]).read_text().count("--user daemon-reload") == 1

    Path(env["SYSTEMCTL_LOG"]).write_text("")
    _run(env)
    assert "--user daemon-reload" not in Path(env["SYSTEMCTL_LOG"]).read_text()


def test_backstop_is_kill_at_80_whatever_the_user_monitor_reports(tmp_path):
    """user.slice is never switched off: it is the backstop at 80%, above the
    user-manager monitor's 60%, verified or not."""
    for live in (True, False):
        (tmp_path / str(live)).mkdir()
        env = _stage(tmp_path / str(live))
        if live:
            _live(env)
        result = _run(env)
        assert result.returncode == 0, result.stderr
        assert _slice_text(env) == _stamped(_SLICE)
        assert "user.slice backstop kill @80% PSI" in result.stdout
        assert ("verified live" in result.stdout) is live


@pytest.mark.parametrize("breakage", ["reload_fails", "root_mode_auto", "root_dropin_edited",
                                      "oomctl_missing_path", "oomctl_fails", "oomctl_flaps"])
def test_each_failed_check_reports_the_user_monitor_unverified(tmp_path, breakage):
    env = _live(_stage(tmp_path))
    if breakage == "reload_fails":
        env["USER_RELOAD_RC"] = "1"
    elif breakage == "root_mode_auto":
        env["UM_ROOT_MODE"] = "auto"
    elif breakage == "root_dropin_edited":
        root = _user_dropins(env)["root"]
        root.parent.mkdir(parents=True)
        root.write_text("[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=40%\n")
    elif breakage == "oomctl_missing_path":
        env["OOMCTL_PATHS"] = "/user.slice"
    elif breakage == "oomctl_fails":
        env["OOMCTL_RC"] = "1"
    elif breakage == "oomctl_flaps":
        env["OOMCTL_FLAP_FILE"] = str(tmp_path / "flap")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "User-manager monitor not verified" in result.stdout
    assert "user manager NOT verified" in result.stdout
    assert _slice_text(env) == _stamped(_SLICE)


def test_unreported_drop_in_gets_a_retry_reload(tmp_path):
    """The drop-in is already in place (no change this run) but the manager
    does not report kill: an earlier reload failed. The run reloads again."""
    env = _stage(tmp_path)
    _run(env)  # writes the user drop-ins
    Path(env["SYSTEMCTL_LOG"]).write_text("")
    env["UM_ROOT_MODE"] = "auto"
    _run(env)
    assert Path(env["SYSTEMCTL_LOG"]).read_text().count("--user daemon-reload") == 1


def test_running_as_root_skips_the_user_half(tmp_path):
    env = _live(_stage(tmp_path))
    env["FAKE_UID"] = "0"
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "running as root" in result.stdout
    assert not _user_dropins(env)["root"].exists()
    assert _slice_text(env) == _stamped(_SLICE)


def test_user_half_runs_even_without_sudo(tmp_path):
    env = _live(_stage(tmp_path))
    env["SUDO_N_RC"] = "1"
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert _user_dropins(env)["root"].read_text() == _stamped(_UM_ROOT)
    assert "sudo unavailable" in result.stdout
    assert not _dropins(env)["user_slice"].exists()


@pytest.mark.parametrize(("key", "old", "new"), [
    ("user_service", "[Service]\nManagedOOMMemoryPressure=kill\n", _SERVICE),
    ("user_slice", _SLICE_V1, _SLICE),
])
def test_legacy_unstamped_genesis_content_is_replaced_not_kept(tmp_path, key, old, new):
    """An install that never updated since stamps holds an earlier Genesis
    content bare; it is Genesis's, so it is replaced, not kept as an edit."""
    env = _stage(tmp_path)
    path = _dropins(env)[key]
    path.parent.mkdir(parents=True)
    path.write_text(old)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert path.read_text() == _stamped(new)
    assert "edited by hand" not in result.stdout


def test_stamped_older_slice_limit_is_rewritten_to_80(tmp_path):
    env = _stage(tmp_path)
    path = _dropins(env)["user_slice"]
    path.parent.mkdir(parents=True)
    path.write_text(_stamped(_SLICE_V1))
    _run(env)
    assert _slice_text(env) == _stamped(_SLICE)



# --- round 1: every claim in the summary comes from a read ---------------------


@pytest.mark.parametrize(
    ("env_extra", "why"),
    [
        ({"UM_ROOT_LIMIT": "3006477107"}, "limit '3006477107'"),
        ({"UM_INIT_PREF": "none"}, "'none' for init.scope, not omit"),
        ({"UM_DBUS_PREF": ""}, "'nothing' for dbus.service, not avoid"),
    ],
)
def test_monitor_is_not_live_until_limit_and_marks_read_back(tmp_path, env_extra, why):
    env = _stage(tmp_path)
    env.update(env_extra)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "User-manager monitor not verified" in result.stdout and why in result.stdout, result.stdout
    assert "verified live" not in result.stdout


@pytest.mark.parametrize("which", ["init", "dbus"])
def test_a_hand_edited_mark_drop_in_blocks_live(tmp_path, which):
    env = _stage(tmp_path)
    path = _user_dropins(env)[which]
    path.parent.mkdir(parents=True)
    path.write_text("[Service]\nManagedOOMPreference=none\n")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    unit = {"init": "init.scope", "dbus": "dbus.service"}[which]
    assert f"its {unit} drop-in was kept-edited" in result.stdout, result.stdout
    assert "verified live" not in result.stdout


@pytest.mark.parametrize("second_run", [False, True])
def test_a_backstop_systemd_does_not_report_is_never_called_in_force(tmp_path, second_run):
    """The summary reads PID 1's view of user.slice. A reload that failed on an
    earlier run leaves the old limit in systemd; the 'already in place' run
    that follows must say so, not repeat the drop-in's 80%."""
    env = _live(_stage(tmp_path))
    if second_run:
        _run(env)
    env["SYS_SLICE_LIMIT"] = "2576980377"
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "user.slice backstop NOT confirmed (systemd reports 'kill 2576980377')" in result.stdout, result.stdout
    assert "backstop kill @80% PSI" not in result.stdout


def test_oomd_not_running_overrides_every_claim(tmp_path):
    env = _live(_stage(tmp_path))
    env["OOMD_ACTIVE_RC"] = "3"
    result = _run(env)
    assert "systemd-oomd is not running, so no monitor is in force" in result.stdout, result.stdout
    assert "verified live;" not in result.stdout and "kill @80% PSI" not in result.stdout


def test_the_healthy_summary_names_every_read(tmp_path):
    env = _live(_stage(tmp_path))
    result = _run(env)
    assert (
        "user manager kill @60% PSI, init.scope omit, dbus avoid, verified live; "
        "user.slice backstop kill @80% PSI" in result.stdout
    ), result.stdout


def test_uninstall_removes_the_user_manager_drop_ins():
    """The drop-ins sit on non-Genesis unit names, so the genesis-* glob in
    uninstall.sh misses them; each must be named."""
    text = (Path(__file__).resolve().parents[2] / "scripts" / "uninstall.sh").read_text()
    loop = text[text.index("for d in -.slice.d init.scope.d dbus.service.d; do"):]
    # The same root the writer uses (MEMRES_USER_UNIT_DIR follows XDG_CONFIG_HOME).
    assert 'f="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$d/genesis-oomd.conf"' in loop.split("done", 1)[0]
    assert "safe_remove" in loop.split("done", 1)[0]
