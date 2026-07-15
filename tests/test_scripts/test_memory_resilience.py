"""Behavioral tests for memory_resilience_apply (scripts/lib/memory_resilience.sh).

The function is sourced from the REAL lib and driven with stubbed
``sudo``/``systemctl``/``systemd-detect-virt``/``swapon`` on PATH plus
MEMRES_* path overrides, so the logic under test is the shipped code:
adaptive systemd-oomd pressure-kill setup (idempotent drop-ins, graceful
degradation without systemd/oomd/PSI/sudo) and the warn-only swap
invariant check (vantage-aware: container vs bare/VM).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "memory_resilience.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"

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
echo "$@" >> "$SYSTEMCTL_LOG"
exit 0
"""

_DETECT_VIRT_STUB = """#!/bin/bash
# --container: rc 0 = we are a container, rc 1 = bare/VM.
exit "${DETECT_VIRT_RC:-0}"
"""

_SWAPON_STUB = """#!/bin/bash
printf '%s' "${SWAPON_OUT-}"
"""


def _stage(tmp_path: Path) -> dict:
    """Stub bin dir + fake roots; returns the env overlay for _run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("sudo", _SUDO_STUB),
        ("systemctl", _SYSTEMCTL_STUB),
        ("systemd-detect-virt", _DETECT_VIRT_STUB),
        ("swapon", _SWAPON_STUB),
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


def test_fresh_apply_writes_dropins_and_reloads(tmp_path):
    env = _stage(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "policy applied" in result.stdout

    files = _dropins(env)
    assert (
        files["user_slice"].read_text()
        == "[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=60%\n"
    )
    assert files["user_service"].read_text() == "[Service]\nManagedOOMMemoryPressure=kill\n"
    assert files["oomd_conf"].read_text() == (
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
    assert "enable --now systemd-oomd" in calls
    assert "restart systemd-oomd" in calls


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


def test_bootstrap_wires_the_lib():
    text = BOOTSTRAP.read_text()
    assert 'source "$SCRIPT_DIR/lib/memory_resilience.sh"' in text
    assert "memory_resilience_apply" in text
