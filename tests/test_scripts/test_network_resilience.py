"""Behavioral tests for network resilience (scripts/lib/network_resilience.sh
and scripts/systemd/genesis-network-watchdog.sh).

Both are driven from the REAL shipped files with stubbed
``sudo``/``systemctl``/``networkctl``/``ip`` on PATH plus NETRES_*/NETWD_*
overrides, so the logic under test is the shipped code: adaptive
KeepConfiguration drop-in + watchdog install (idempotent, graceful degradation
without systemd/networkd/networkctl/sudo), and the watchdog's detect→heal
decision (grace window, rate limit, telemetry).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "network_resilience.sh"
WATCHDOG = REPO_ROOT / "scripts" / "systemd" / "genesis-network-watchdog.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
UPDATE = REPO_ROOT / "scripts" / "update.sh"

# ── stubs for the lib (network_resilience_apply) ──────────────────────────────

_SUDO_STUB = """#!/bin/bash
if [ "$1" = "-n" ] && [ "$2" = "true" ]; then exit "${SUDO_N_RC:-0}"; fi
exec "$@"
"""

_SYSTEMCTL_STUB = """#!/bin/bash
# Stateful stub for genesis-network-watchdog.timer, modelling real systemd:
# `enable`->enabled, `start`->active, `mask`->(enable/start fail until `unmask`).
# is-active / is-enabled are keyed on the UNIT ($2) and SILENT (never log — a
# probe must not churn); tests can force states via WATCHDOG_TIMER_ACTIVE_RC /
# WATCHDOG_TIMER_ENABLED_RC (or by deleting the .timer{started,enabled} state
# files). systemd-networkd keeps the shared NETWORKD_* vars.
_b="${SYSTEMCTL_LOG%.log}"
_T=genesis-network-watchdog.timer
_TU="${NETRES_ETC_ROOT:-/nonexistent}/systemd/system/$_T"
_masked() { [ -L "$_TU" ]; }   # real systemd: a masked unit path is a symlink to /dev/null
if [ "$1" = "is-active" ]; then
    if [ "$2" = "$_T" ]; then
        [ -n "${WATCHDOG_TIMER_ACTIVE_RC:-}" ] && exit "$WATCHDOG_TIMER_ACTIVE_RC"
        { _masked || [ ! -f "$_b.timerstarted" ]; } && exit 3 || exit 0
    fi
    exit "${NETWORKD_ACTIVE_RC:-0}"
fi
if [ "$1" = "is-enabled" ]; then
    if [ "$2" = "$_T" ]; then
        [ -n "${WATCHDOG_TIMER_ENABLED_RC:-}" ] && exit "$WATCHDOG_TIMER_ENABLED_RC"
        _masked && { printf 'masked'; exit 1; }
        [ -f "$_b.timerenabled" ] && exit 0 || exit 1
    fi
    printf '%s' "${NETWORKD_ENABLED-enabled}"; exit 0
fi
[ "$1" = "unmask" ] && _masked && rm -f "$_TU"   # real unmask removes the /dev/null symlink
if [ "$1" = "enable" ] && [ "$2" = "$_T" ]; then
    _masked && { echo "$@" >> "$SYSTEMCTL_LOG"; exit 1; }
    : > "$_b.timerenabled"
fi
if [ "$1" = "start" ] && [ "$2" = "$_T" ]; then
    _masked && { echo "$@" >> "$SYSTEMCTL_LOG"; exit 1; }
    : > "$_b.timerstarted"
fi
echo "$@" >> "$SYSTEMCTL_LOG"
exit 0
"""

# NB: stubs never embed JSON (or any `}`) in a bash ${VAR-default} — the `}`
# prematurely closes the parameter expansion. Complex/default values are set
# from Python (proper quoting) into the env; stubs just echo the env var.
_NETWORKCTL_STUB = """#!/bin/bash
case "$1" in
    status) printf 'Network File: %s\\n' "${NETFILE:-/run/systemd/network/10-netplan-eth0.network}" ;;
    reload) echo "reload" >> "$SYSTEMCTL_LOG" ;;
esac
exit 0
"""

_IP_STUB = """#!/bin/bash
printf '%s' "${IP_ROUTE_OUT:-}"
"""


def _stage(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("sudo", _SUDO_STUB),
        ("systemctl", _SYSTEMCTL_STUB),
        ("networkctl", _NETWORKCTL_STUB),
        ("ip", _IP_STUB),
    ):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        "NETRES_ETC_ROOT": str(tmp_path / "etc"),
        "NETRES_SYSTEMD_RUNTIME_DIR": "/run/systemd/system",
        "NETRES_LIBEXEC_DIR": str(tmp_path / "libexec"),
        "NETRES_WATCHDOG_SRC": str(WATCHDOG),
        "IP_ROUTE_OUT": json.dumps([{"dev": "eth0"}]),
    }


def _run_apply(env_overlay: dict) -> subprocess.CompletedProcess:
    # Absolute bash path so the child never needs PATH to find its shell — lets a
    # test restrict PATH to the stub dir alone (e.g. to hide the real networkctl).
    return subprocess.run(
        ["/bin/bash", "-c", f'set -euo pipefail; source "{LIB}"; network_resilience_apply'],
        capture_output=True,
        text=True,
        timeout=30,
        env={"HOME": env_overlay.get("NETRES_ETC_ROOT", "/tmp"), **env_overlay},
    )


def _paths(env: dict) -> dict[str, Path]:
    etc = Path(env["NETRES_ETC_ROOT"])
    libexec = Path(env["NETRES_LIBEXEC_DIR"])
    return {
        "keepconf": etc / "systemd/network/10-netplan-eth0.network.d/genesis-keep-config.conf",
        "service": etc / "systemd/system/genesis-network-watchdog.service",
        "timer": etc / "systemd/system/genesis-network-watchdog.timer",
        "script": libexec / "network-watchdog.sh",
    }


def test_fresh_apply_writes_dropin_units_and_reloads(tmp_path):
    env = _stage(tmp_path)
    result = _run_apply(env)
    assert result.returncode == 0, result.stderr
    assert "Network resilience applied" in result.stdout

    p = _paths(env)
    # KeepConfiguration=true (superset of dhcp) — not =dhcp, not a percentage.
    assert p["keepconf"].read_text() == "[Network]\nKeepConfiguration=true\n"
    assert p["timer"].exists()
    # ExecStart tracks the (overridden) install dir, not a hardcoded path.
    service = p["service"].read_text()
    assert "Type=oneshot" in service
    assert f"ExecStart={p['script']}" in service
    assert p["script"].read_text() == WATCHDOG.read_text()

    calls = Path(env["SYSTEMCTL_LOG"]).read_text()
    assert "reload" in calls  # networkctl reload for the drop-in
    assert "daemon-reload" in calls
    assert "enable genesis-network-watchdog.timer" in calls
    assert "start genesis-network-watchdog.timer" in calls


def test_second_run_is_a_noop(tmp_path):
    """A re-run with a HEALTHY (active) timer stays churn-free: the self-heal
    probe (`is-active`) is silent, so no reload/enable/start is logged. NR1's
    self-heal must not turn a healthy re-run into churn."""
    env = _stage(tmp_path)
    _run_apply(env)  # fresh install: timer written + started → now active
    Path(env["SYSTEMCTL_LOG"]).write_text("")

    result = _run_apply(env)
    assert result.returncode == 0
    assert "already in place" in result.stdout
    # The silent is-active probe finds the timer active → no reload/enable/start.
    assert Path(env["SYSTEMCTL_LOG"]).read_text() == ""


def test_second_run_heals_disabled_timer(tmp_path):
    """NR1 + Codex P2: an active-but-DISABLED timer (e.g. `systemctl disable`
    without --now — won't persist across reboot) must heal, even though
    `is-active` alone would report it fine. The self-heal probes enablement too."""
    env = _stage(tmp_path)
    _run_apply(env)  # fresh install: active + enabled
    Path(env["SYSTEMCTL_LOG"]).write_text("")

    Path(env["SYSTEMCTL_LOG"][:-4] + ".timerenabled").unlink()  # externally disabled
    result = _run_apply(env)
    assert result.returncode == 0, result.stderr
    calls = Path(env["SYSTEMCTL_LOG"]).read_text()
    assert "enable genesis-network-watchdog.timer" in calls  # re-enabled for persistence
    assert "re-enabled a stopped/disabled watchdog timer" in result.stdout


def test_masked_timer_unit_is_recreated_before_enable(tmp_path):
    """Codex P2: a masked timer's unit path is a symlink to /dev/null, and the
    unit writes use `tee` (which follows the symlink). The self-heal must UNMASK
    (remove the symlink) BEFORE rewriting the unit — else the write goes to
    /dev/null and enable has no unit file. After apply the unit must be a REAL
    file (not the symlink), unmask must precede enable, and the heal must NOT
    report failure."""
    env = _stage(tmp_path)
    _run_apply(env)  # fresh: real unit files + active/enabled
    timer = _paths(env)["timer"]
    assert not timer.is_symlink()  # sanity: fresh install wrote a real file

    # Simulate `systemctl mask`: the unit path becomes a /dev/null symlink, and
    # the unit is thereby stopped + disabled.
    timer.unlink()
    timer.symlink_to("/dev/null")
    base = env["SYSTEMCTL_LOG"][:-4]
    Path(base + ".timerstarted").unlink(missing_ok=True)
    Path(base + ".timerenabled").unlink(missing_ok=True)
    Path(env["SYSTEMCTL_LOG"]).write_text("")

    result = _run_apply(env)
    assert result.returncode == 0, result.stderr
    # Unmasked + rewritten as a real file (the write did NOT go to /dev/null).
    assert not timer.is_symlink(), "masked unit must be unmasked+rewritten as a real file"
    assert "[Timer]" in timer.read_text()
    calls = Path(env["SYSTEMCTL_LOG"]).read_text()
    assert "unmask genesis-network-watchdog.timer" in calls
    assert calls.index("unmask") < calls.index("enable genesis-network-watchdog.timer")
    assert "could not be re-enabled" not in result.stdout  # genuinely healed


def test_unhealable_timer_reports_failure_not_false_heal(tmp_path):
    """code-reviewer SHOULD-FIX: if enable/start don't take (broken/permanently
    down unit), the heal must VERIFY and report failure — never a false
    're-enabled' success on a state that's still broken."""
    env = _stage(tmp_path)
    _run_apply(env)
    Path(env["SYSTEMCTL_LOG"]).write_text("")

    env["WATCHDOG_TIMER_ACTIVE_RC"] = "3"  # stays down through the post-heal verify
    result = _run_apply(env)
    assert result.returncode == 0, result.stderr
    assert "could not be re-enabled" in result.stdout  # honest WARNING
    assert "re-enabled a stopped/disabled watchdog timer" not in result.stdout
    assert "NOT fully applied" in result.stdout  # routed to the _NETRES_FAILED path


def test_no_systemd_skips_cleanly(tmp_path):
    env = _stage(tmp_path)
    env["NETRES_SYSTEMD_RUNTIME_DIR"] = str(tmp_path / "does-not-exist")
    result = _run_apply(env)
    assert result.returncode == 0
    assert "not a systemd system" in result.stdout
    assert not _paths(env)["keepconf"].exists()


def test_no_networkctl_skips_cleanly(tmp_path):
    env = _stage(tmp_path)
    bin_dir = Path(env["PATH"].split(":")[0])
    (bin_dir / "networkctl").unlink()
    # Restrict PATH to the stub dir ONLY, so `command -v networkctl` cannot fall
    # through to the host's real /usr/bin/networkctl. The networkctl guard runs
    # before any external binary is needed, so the stub dir alone is sufficient.
    env["PATH"] = str(bin_dir)
    result = _run_apply(env)
    assert result.returncode == 0
    assert "networkctl not present" in result.stdout
    assert not _paths(env)["keepconf"].exists()


def test_networkd_disabled_and_inactive_skips_cleanly(tmp_path):
    # Genuine other-manager host: networkd inactive AND not enabled -> skip.
    env = _stage(tmp_path)
    env["NETWORKD_ACTIVE_RC"] = "1"
    env["NETWORKD_ENABLED"] = "disabled"
    result = _run_apply(env)
    assert result.returncode == 0
    assert "not active or enabled" in result.stdout
    assert not _paths(env)["keepconf"].exists()


def test_networkd_inactive_but_enabled_still_installs_watchdog(tmp_path):
    # The crashed-but-ours case: networkd is inactive (exactly what the watchdog
    # heals) yet enabled — must NOT be skipped, or the machine that most needs
    # the watchdog never gets it (Codex P2).
    env = _stage(tmp_path)
    env["NETWORKD_ACTIVE_RC"] = "1"
    env["NETWORKD_ENABLED"] = "enabled"
    result = _run_apply(env)
    assert result.returncode == 0
    assert "not active or enabled" not in result.stdout
    assert _paths(env)["timer"].exists()


def test_no_noninteractive_sudo_skips_with_remediation(tmp_path):
    env = _stage(tmp_path)
    env["SUDO_N_RC"] = "1"
    result = _run_apply(env)
    assert result.returncode == 0
    assert "sudo unavailable" in result.stdout
    assert "network_resilience_apply" in result.stdout
    assert not _paths(env)["keepconf"].exists()


def test_no_default_route_skips_keepconfig_but_installs_watchdog(tmp_path):
    env = _stage(tmp_path)
    env["IP_ROUTE_OUT"] = "[]"  # no IPv4 default route
    result = _run_apply(env)
    assert result.returncode == 0
    assert "no IPv4 default route" in result.stdout
    p = _paths(env)
    assert not p["keepconf"].exists()  # nothing to protect
    assert p["timer"].exists()  # watchdog still worthwhile


def test_unresolved_network_file_skips_keepconfig_but_installs_watchdog(tmp_path):
    env = _stage(tmp_path)
    env["NETFILE"] = "n/a"  # link has no governing .network unit
    result = _run_apply(env)
    assert result.returncode == 0
    assert "no .network unit resolved" in result.stdout
    p = _paths(env)
    assert not p["keepconf"].exists()
    assert p["timer"].exists()


def test_watchdog_source_missing_warns_but_keepconfig_still_applies(tmp_path):
    env = _stage(tmp_path)
    env["NETRES_WATCHDOG_SRC"] = str(tmp_path / "no-such-watchdog.sh")
    result = _run_apply(env)
    assert result.returncode == 0
    assert "watchdog source missing" in result.stdout
    assert "NOT fully applied" in result.stdout
    p = _paths(env)
    assert p["keepconf"].exists()  # Part A independent of Part B
    assert not p["script"].exists()


def test_failed_write_warns_instead_of_claiming_already_in_place(tmp_path):
    env = _stage(tmp_path)
    etc = Path(env["NETRES_ETC_ROOT"])
    etc.mkdir()
    etc.chmod(0o555)  # unwritable -> tee fails
    try:
        result = _run_apply(env)
    finally:
        etc.chmod(0o755)
    assert result.returncode == 0
    assert "could not write" in result.stdout
    assert "already in place" not in result.stdout


def test_keepconfig_is_true_not_dhcp_or_percentage(tmp_path):
    # Guards the deliberate choice: =true is the netplan `critical: true`
    # superset (retains DHCP + static/foreign), delivering both hand-applied
    # protections through one mechanism.
    env = _stage(tmp_path)
    _run_apply(env)
    body = _paths(env)["keepconf"].read_text()
    assert "KeepConfiguration=true" in body
    assert "=dhcp" not in body


def test_bootstrap_wires_the_lib():
    text = BOOTSTRAP.read_text()
    assert 'source "$SCRIPT_DIR/lib/network_resilience.sh"' in text
    assert "network_resilience_apply" in text


def test_update_sh_wires_the_lib_visibly():
    text = UPDATE.read_text()
    assert "lib/network_resilience.sh" in text
    assert text.count("network_resilience_apply") >= 1


# ── stubs + harness for the watchdog script (detect→heal) ─────────────────────

_WD_SYSTEMCTL_STUB = """#!/bin/bash
case "$1" in
    is-enabled) printf '%s' "${WD_ENABLED-enabled}" ;;
    is-active)  printf '%s' "${WD_ACTIVE-active}" ;;
    show)       printf '@%s' "${WD_START_EPOCH-0}" ;;
    restart)    echo "restart $2" >> "$WD_RESTART_LOG"; exit "${WD_RESTART_RC:-0}" ;;
esac
exit 0
"""

_WD_NETWORKCTL_STUB = """#!/bin/bash
# only `--json=short list` is used
printf '%s' "${WD_LINKS_JSON:-}"
"""

_WD_IP_STUB = """#!/bin/bash
# `ip route show default`
printf '%s' "${WD_ROUTE_OUT:-}"
"""


def _stage_wd(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "wdbin"
    bin_dir.mkdir()
    (bin_dir / "networkctl").write_text(_WD_NETWORKCTL_STUB)
    (bin_dir / "ip").write_text(_WD_IP_STUB)
    sysctl = bin_dir / "sysctl-stub"
    sysctl.write_text(_WD_SYSTEMCTL_STUB)
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "NETWD_SYSTEMCTL": str(sysctl),
        "NETWD_STATE_FILE": str(tmp_path / "state.json"),
        "NETWD_STAMP_FILE": str(tmp_path / "stamp"),
        "NETWD_NOW": "100000",
        "NETWD_RATE_LIMIT_SEC": "600",
        "NETWD_GRACE_SEC": "120",
        "WD_RESTART_LOG": str(tmp_path / "restart.log"),
        # Healthy defaults (Python-quoted, no JSON-in-bash-default); individual
        # tests override these to drive each trigger.
        "WD_LINKS_JSON": json.dumps(
            {"Interfaces": [{"Name": "eth0", "AdministrativeState": "configured"}]}
        ),
        "WD_ROUTE_OUT": "default via 10.0.0.1 dev eth0",
    }


def _run_wd(env_overlay: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(WATCHDOG)],
        capture_output=True,
        text=True,
        timeout=30,
        env={"HOME": "/tmp", **env_overlay},
    )


def _restarted(env: dict) -> bool:
    log = Path(env["WD_RESTART_LOG"])
    return log.exists() and "systemd-networkd" in log.read_text()


def _state(env: dict) -> dict:
    return json.loads(Path(env["NETWD_STATE_FILE"]).read_text())


def test_watchdog_healthy_does_not_restart(tmp_path):
    env = _stage_wd(tmp_path)  # active, route present, no failed link
    result = _run_wd(env)
    assert result.returncode == 0
    assert not _restarted(env)
    st = _state(env)
    assert st["last_action"] == "none"
    assert st["heal_count"] == 0
    assert st["last_check"] == 100000


def test_watchdog_failed_link_heals(tmp_path):
    env = _stage_wd(tmp_path)
    env["WD_LINKS_JSON"] = json.dumps(
        {"Interfaces": [{"Name": "eth0", "AdministrativeState": "failed"}]}
    )
    result = _run_wd(env)
    assert result.returncode == 0
    assert _restarted(env)
    st = _state(env)
    assert st["last_action"] == "healed"
    assert st["heal_count"] == 1
    assert st["last_trigger"] == "failed-link:eth0"


def test_watchdog_networkd_inactive_heals(tmp_path):
    env = _stage_wd(tmp_path)
    env["WD_ACTIVE"] = "inactive"
    _run_wd(env)
    assert _restarted(env)
    assert _state(env)["last_trigger"] == "networkd-inactive"


def test_watchdog_masked_never_heals(tmp_path):
    env = _stage_wd(tmp_path)
    env["WD_ENABLED"] = "masked"
    env["WD_ACTIVE"] = "inactive"  # even though inactive, mask = operator intent
    _run_wd(env)
    assert not _restarted(env)
    assert _state(env)["last_action"] == "none"


def test_watchdog_no_default_route_heals(tmp_path):
    env = _stage_wd(tmp_path)
    env["WD_ROUTE_OUT"] = ""  # no default route
    _run_wd(env)
    assert _restarted(env)
    assert _state(env)["last_trigger"] == "no-default-route"


def test_watchdog_grace_window_suppresses_heal(tmp_path):
    env = _stage_wd(tmp_path)
    env["WD_LINKS_JSON"] = json.dumps(
        {"Interfaces": [{"Name": "eth0", "AdministrativeState": "failed"}]}
    )
    env["WD_START_EPOCH"] = "99950"  # started 50s ago < 120s grace
    _run_wd(env)
    assert not _restarted(env)  # settling, don't fight it
    assert _state(env)["last_action"] == "none"


def test_watchdog_rate_limit_suppresses_repeat_heal(tmp_path):
    env = _stage_wd(tmp_path)
    env["WD_LINKS_JSON"] = json.dumps(
        {"Interfaces": [{"Name": "eth0", "AdministrativeState": "failed"}]}
    )
    Path(env["NETWD_STAMP_FILE"]).write_text("99500")  # healed 500s ago < 600s
    _run_wd(env)
    assert not _restarted(env)
    st = _state(env)
    assert st["last_action"] == "ratelimited"
    assert st["last_trigger"] == "failed-link:eth0"  # trigger recorded even so


def test_watchdog_failed_restart_not_recorded_as_healed(tmp_path):
    # A restart that exits nonzero must NOT claim a heal or arm the rate limit
    # (Codex P2): telemetry says restart-failed, heal_count stays 0, and no
    # stamp is written so the next tick retries.
    env = _stage_wd(tmp_path)
    env["WD_ACTIVE"] = "inactive"
    env["WD_RESTART_RC"] = "1"
    result = _run_wd(env)
    assert result.returncode != 0  # surfaces as a failed oneshot unit
    assert _restarted(env)  # it DID attempt the restart
    st = _state(env)
    assert st["last_action"] == "restart-failed"
    assert st["heal_count"] == 0
    assert not Path(env["NETWD_STAMP_FILE"]).exists()  # rate limit not armed


def test_watchdog_heal_count_accumulates_across_runs(tmp_path):
    env = _stage_wd(tmp_path)
    env["WD_ACTIVE"] = "inactive"
    _run_wd(env)
    assert _state(env)["heal_count"] == 1
    # second heal must clear the rate-limit window (advance NOW past it)
    env["NETWD_NOW"] = "101000"  # 1000s later > 600s rate limit
    _run_wd(env)
    assert _state(env)["heal_count"] == 2


def test_watchdog_state_file_is_world_readable(tmp_path):
    env = _stage_wd(tmp_path)
    _run_wd(env)
    mode = Path(env["NETWD_STATE_FILE"]).stat().st_mode & 0o777
    assert mode & 0o044  # infra collector reads it non-root
