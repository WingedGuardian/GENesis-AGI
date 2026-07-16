"""Behavioral tests for host_swap_apply / host_swap_remove (scripts/lib/host_swap.sh).

The functions are sourced from the REAL lib and driven under the callers' shell
mode (``set -euo pipefail`` — install_guardian.sh and the gateway redeploy verb
both run that way) with stubbed ``sudo``/``systemctl``/``systemd-detect-virt``/
``modinfo``/``zramctl`` on PATH plus HOSTSWAP_* path-override seams. A
``__DONE__`` sentinel printed after the call proves the function returned
instead of tripping errexit — every degrade path here must never abort an
install or a redeploy.

systemctl invocations are appended to a ``SYSTEMCTL_LOG`` file (the
test_memory_resilience.py idiom) so idempotency can assert "no systemd churn"
rather than just "no error".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "host_swap.sh"
INSTALL_GUARDIAN = REPO_ROOT / "scripts" / "install_guardian.sh"
GATEWAY = REPO_ROOT / "scripts" / "guardian-gateway.sh"
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
WATCHDOG = REPO_ROOT / "src" / "genesis" / "guardian" / "watchdog.py"
DEPLOY_HEALTH = REPO_ROOT / "src" / "genesis" / "observability" / "snapshots" / "deploy_health.py"

_SUDO_PASSTHROUGH = """#!/bin/bash
if [ "$1" = "-n" ]; then shift; fi
exec "$@"
"""

# Passthrough except the privileged WRITE (tee) is refused.
_SUDO_TEE_FAIL = """#!/bin/bash
if [ "$1" = "-n" ]; then shift; fi
if [ "$1" = "tee" ]; then exit 1; fi
exec "$@"
"""

# sudo -n itself refused (no passwordless sudo).
_SUDO_N_FAIL = """#!/bin/bash
exit 1
"""

# systemctl stub: logs every invocation; `is-enabled` answers from env.
_SYSTEMCTL = """#!/bin/bash
echo "$@" >> "$SYSTEMCTL_LOG"
if [ "$1" = "is-enabled" ]; then echo "${SYSTEMCTL_IS_ENABLED:-disabled}"; exit 0; fi
exit 0
"""

# systemd-detect-virt: exit 1 = not a container (the default vantage).
_DETECT_VIRT_HOST = """#!/bin/bash
exit 1
"""
_DETECT_VIRT_CONTAINER = """#!/bin/bash
exit 0
"""

_MODINFO_OK = """#!/bin/bash
exit 0
"""
_MODINFO_FAIL = """#!/bin/bash
exit 1
"""

_ZRAMCTL = """#!/bin/bash
exit 0
"""

_MEMINFO_20G = "MemTotal:       20971520 kB\nMemFree:        1000000 kB\n"
_MEMINFO_2G = "MemTotal:       2097152 kB\nMemFree:        100000 kB\n"


def _sandbox(
    tmp_path,
    *,
    sudo=_SUDO_PASSTHROUGH,
    detect_virt=_DETECT_VIRT_HOST,
    modinfo=_MODINFO_OK,
    zramctl=_ZRAMCTL,
    meminfo=_MEMINFO_20G,
    swaps="",
    systemd_dir=True,
):
    """Build the stub bin/ + fixture files; return the env dict."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in [
        ("sudo", sudo),
        ("systemctl", _SYSTEMCTL),
        ("systemd-detect-virt", detect_virt),
        ("modinfo", modinfo),
    ]:
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    if zramctl is not None:
        p = bindir / "zramctl"
        p.write_text(zramctl)
        p.chmod(0o755)
    (tmp_path / "meminfo").write_text(meminfo)
    (tmp_path / "swaps").write_text("Filename Type Size Used Priority\n" + swaps)
    if systemd_dir:
        (tmp_path / "run-systemd").mkdir(exist_ok=True)
    (tmp_path / "systemctl.log").touch()
    return {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOSTSWAP_ETC_ROOT": str(tmp_path / "etc"),
        "HOSTSWAP_SYSTEMD_RUNTIME_DIR": str(tmp_path / "run-systemd"),
        "HOSTSWAP_MEMINFO": str(tmp_path / "meminfo"),
        "HOSTSWAP_PROC_SWAPS": str(tmp_path / "swaps"),
        "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
    }


def _run(env, fn="host_swap_apply", extra_env=None):
    """Source the real lib and invoke under the callers' `set -euo pipefail`."""
    if extra_env:
        env = {**env, **extra_env}
    script = f'set -euo pipefail; source "{LIB}"; {fn}; echo __DONE__'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def _unit(tmp_path) -> Path:
    return tmp_path / "etc" / "systemd" / "system" / "zram-swap.service"


def _log(tmp_path) -> str:
    return (tmp_path / "systemctl.log").read_text()


# ── size formula ────────────────────────────────────────────────────────────


def test_size_capped_at_4gib_on_big_host(tmp_path):
    env = _sandbox(tmp_path)  # 20 GiB host → half=10240 > cap=4096
    res = _run(env, fn="_hostswap_size_mib")
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines()[0] == "4096"


def test_size_half_ram_on_small_host(tmp_path):
    env = _sandbox(tmp_path, meminfo=_MEMINFO_2G)  # 2 GiB host → 1024 MiB
    res = _run(env, fn="_hostswap_size_mib")
    assert res.stdout.splitlines()[0] == "1024"


def test_size_cap_env_override(tmp_path):
    env = _sandbox(tmp_path)  # 20 GiB host, cap raised to 8 GiB
    res = _run(env, fn="_hostswap_size_mib", extra_env={"HOSTSWAP_CAP_GIB": "8"})
    assert res.stdout.splitlines()[0] == "8192"


# ── apply: fresh install ────────────────────────────────────────────────────


def test_fresh_apply_installs_unit_and_enables(tmp_path):
    env = _sandbox(tmp_path)
    res = _run(env)
    assert res.returncode == 0, res.stderr
    assert "__DONE__" in res.stdout
    unit = _unit(tmp_path)
    assert unit.is_file()
    content = unit.read_text()
    # Size baked in from the formula; fixed device; zstd with fallback; prio 100.
    assert "--size 4096MiB --algorithm zstd" in content
    assert "|| { zramctl --reset /dev/zram0" in content  # fallback chain
    assert "swapon -p 100 /dev/zram0" in content
    assert "$" not in content  # NO command substitution → no systemd escaping
    log = _log(tmp_path)
    assert "daemon-reload" in log
    assert "enable --now zram-swap.service" in log
    assert "zram swap unit installed" in res.stdout


def test_fresh_apply_warns_when_not_yet_active(tmp_path):
    """Stubbed systemctl can't actually start swap → the outcome check WARNs."""
    env = _sandbox(tmp_path)
    res = _run(env)
    assert "WARNING: zram swap not (yet) active" in res.stdout


def test_verify_reports_active_device(tmp_path):
    """Once /proc/swaps shows the device, re-apply reports it active."""
    env = _sandbox(tmp_path)
    _run(env)  # install the unit first (so the foreign-zram guard passes)
    (tmp_path / "swaps").write_text(
        "Filename Type Size Used Priority\n/dev/zram0 partition 4194304 0 100\n"
    )
    res = _run(env)
    assert "zram swap active" in res.stdout
    assert "prio 100" in res.stdout


# ── apply: idempotency ──────────────────────────────────────────────────────


def test_idempotent_rerun_no_systemd_churn(tmp_path):
    env = _sandbox(tmp_path)
    _run(env)
    # Mutating verbs only — the read-only `is-enabled` mask-check runs per
    # apply by design and must not count as churn.
    def _churn():
        return [
            line
            for line in _log(tmp_path).splitlines()
            if "daemon-reload" in line or "enable --now" in line
        ]

    churn_before = _churn()
    res = _run(env)
    assert res.returncode == 0
    assert "already in place" in res.stdout
    assert _churn() == churn_before  # zero new reload/enable calls


def test_ram_change_rerenders_unit(tmp_path):
    """write-if-different: a different computed size rewrites the unit."""
    env = _sandbox(tmp_path)
    _run(env)
    assert "--size 4096MiB" in _unit(tmp_path).read_text()
    (tmp_path / "meminfo").write_text(_MEMINFO_2G)
    res = _run(env)
    assert "zram swap unit installed" in res.stdout  # changed → re-applied
    assert "--size 1024MiB" in _unit(tmp_path).read_text()


# ── degrade-to-skip guards (each must return 0 under errexit) ───────────────


def test_skip_on_disable_env(tmp_path):
    env = _sandbox(tmp_path)
    res = _run(env, extra_env={"HOSTSWAP_DISABLE": "1"})
    assert res.returncode == 0
    assert "HOSTSWAP_DISABLE" in res.stdout
    assert not _unit(tmp_path).exists()


def test_skip_without_systemd(tmp_path):
    env = _sandbox(tmp_path, systemd_dir=False)
    res = _run(env)
    assert res.returncode == 0
    assert "not a systemd system" in res.stdout
    assert not _unit(tmp_path).exists()


def test_skip_in_container_vantage(tmp_path):
    """Guardian stack running inside LXC/Docker → zram needs the host kernel."""
    env = _sandbox(tmp_path, detect_virt=_DETECT_VIRT_CONTAINER)
    res = _run(env)
    assert res.returncode == 0
    assert "container vantage" in res.stdout
    assert not _unit(tmp_path).exists()


def test_skip_without_zramctl(tmp_path):
    env = _sandbox(tmp_path, zramctl=None)
    res = _run(env)
    assert res.returncode == 0
    assert "zramctl not available" in res.stdout
    assert not _unit(tmp_path).exists()


def test_skip_without_zram_module(tmp_path):
    env = _sandbox(tmp_path, modinfo=_MODINFO_FAIL)
    res = _run(env)
    assert res.returncode == 0
    assert "kernel module not available" in res.stdout
    assert not _unit(tmp_path).exists()


def test_skip_foreign_zram_not_shadowed(tmp_path):
    """zram-generator / an operator's own zram is active and OUR unit was never
    installed → leave their setup alone."""
    env = _sandbox(tmp_path, swaps="/dev/zram0 partition 8388608 0 100\n")
    res = _run(env)
    assert res.returncode == 0
    assert "external zram swap already active" in res.stdout
    assert not _unit(tmp_path).exists()


def test_skip_without_sudo(tmp_path):
    env = _sandbox(tmp_path, sudo=_SUDO_N_FAIL)
    res = _run(env)
    assert res.returncode == 0
    assert "sudo unavailable" in res.stdout
    assert "host_swap_apply" in res.stdout  # manual remedy line
    assert not _unit(tmp_path).exists()


def test_skip_when_masked(tmp_path):
    """`sudo systemctl mask zram-swap.service` is the durable operator opt-out."""
    env = _sandbox(tmp_path)
    res = _run(env, extra_env={"SYSTEMCTL_IS_ENABLED": "masked"})
    assert res.returncode == 0
    assert "operator opt-out" in res.stdout
    assert not _unit(tmp_path).exists()


def test_warn_on_unreadable_meminfo(tmp_path):
    env = _sandbox(tmp_path)
    (tmp_path / "meminfo").write_text("garbage\n")
    res = _run(env)
    assert res.returncode == 0
    assert "__DONE__" in res.stdout
    assert "cannot read MemTotal" in res.stdout
    assert not _unit(tmp_path).exists()


def test_write_failure_warns_and_skips_systemd(tmp_path):
    """A refused privileged write must warn loudly, not claim success."""
    env = _sandbox(tmp_path, sudo=_SUDO_TEE_FAIL)
    res = _run(env)
    assert res.returncode == 0
    assert "__DONE__" in res.stdout
    assert "NOT installed" in res.stdout
    log = _log(tmp_path)
    assert "daemon-reload" not in log
    assert "enable --now" not in log


# ── remove ──────────────────────────────────────────────────────────────────


def test_remove_cleans_up(tmp_path):
    env = _sandbox(tmp_path)
    _run(env)
    assert _unit(tmp_path).is_file()
    res = _run(env, fn="host_swap_remove")
    assert res.returncode == 0
    assert "__DONE__" in res.stdout
    assert not _unit(tmp_path).exists()
    assert "disable --now zram-swap.service" in _log(tmp_path)


# ── parse + wiring ──────────────────────────────────────────────────────────


def test_lib_parses_clean():
    res = subprocess.run(["bash", "-n", str(LIB)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_install_guardian_parses_clean():
    res = subprocess.run(["bash", "-n", str(INSTALL_GUARDIAN)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_gateway_parses_clean():
    res = subprocess.run(["bash", "-n", str(GATEWAY)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_install_guardian_wires_the_lib():
    """Fresh installs: install_guardian.sh must source AND call the lib."""
    text = INSTALL_GUARDIAN.read_text()
    assert 'lib/host_swap.sh"' in text
    assert "host_swap_apply" in text


def test_gateway_redeploy_wires_the_lib():
    """Existing installs only ever receive code via the redeploy verb — it must
    invoke the lib (output to stderr, best-effort) or retrofit never happens."""
    text = GATEWAY.read_text()
    assert "host_swap.sh" in text
    assert "host_swap_apply" in text
    # The verb's stdout is a parsed JSON contract — the block must redirect.
    apply_line_idx = text.index("host_swap_apply")
    window = text[apply_line_idx : apply_line_idx + 200]
    assert "1>&2" in window or ">&2" in window


def test_guardian_paths_include_the_lib_everywhere():
    """A host_swap.sh-only change must trigger a redeploy: the trigger list and
    both Python mirrors stay in lockstep."""
    assert "scripts/lib/host_swap.sh" in UPDATE_SH.read_text()
    assert "scripts/lib/host_swap.sh" in WATCHDOG.read_text()
    assert "scripts/lib/host_swap.sh" in DEPLOY_HEALTH.read_text()
