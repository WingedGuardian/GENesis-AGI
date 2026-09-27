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
from datetime import UTC, datetime
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
# `enable`->enabled, `start`->active, mask (unit path = /dev/null symlink) ->
# enable/start fail until `unmask`. is-enabled echoes the real state string
# (enabled / enabled-runtime / disabled / masked); is-active/is-enabled are
# keyed on the UNIT ($2) and SILENT (never log — a probe must not churn). Tests
# drive state via the .timer{started,enabled,enabledruntime} files, the /dev/null
# symlink, or WATCHDOG_TIMER_ACTIVE_RC. systemd-networkd keeps the NETWORKD_* vars.
_b="${SYSTEMCTL_LOG%.log}"
_T=genesis-network-watchdog.timer
_TU="${NETRES_ETC_ROOT:-/nonexistent}/systemd/system/$_T"
_masked() { [ -L "$_TU" ]; }                   # persistent mask: /etc unit path is a /dev/null symlink
_rmasked() { [ -f "$_b.timermaskruntime" ]; }  # runtime mask (`mask --runtime`): /run shadow, modeled as a flag
if [ "$1" = "is-active" ]; then
    if [ "$2" = "$_T" ]; then
        [ -n "${WATCHDOG_TIMER_ACTIVE_RC:-}" ] && exit "$WATCHDOG_TIMER_ACTIVE_RC"
        { _masked || _rmasked || [ ! -f "$_b.timerstarted" ]; } && exit 3 || exit 0
    fi
    exit "${NETWORKD_ACTIVE_RC:-0}"
fi
if [ "$1" = "is-enabled" ]; then
    if [ "$2" = "$_T" ]; then
        _masked && { printf 'masked'; exit 1; }
        _rmasked && { printf 'masked-runtime'; exit 1; }
        [ -f "$_b.timerenabled" ] && { printf 'enabled'; exit 0; }
        [ -f "$_b.timerenabledruntime" ] && { printf 'enabled-runtime'; exit 0; }
        printf 'disabled'; exit 1
    fi
    printf '%s' "${NETWORKD_ENABLED-enabled}"; exit 0
fi
if [ "$1" = "unmask" ]; then _masked && rm -f "$_TU"; rm -f "$_b.timermaskruntime"; fi  # clears both variants
if [ "$1" = "enable" ] && [ "$2" = "$_T" ]; then
    { _masked || _rmasked; } && { echo "$@" >> "$SYSTEMCTL_LOG"; exit 1; }
    : > "$_b.timerenabled"
fi
if [ "$1" = "start" ] && [ "$2" = "$_T" ]; then
    { _masked || _rmasked; } && { echo "$@" >> "$SYSTEMCTL_LOG"; exit 1; }
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
    # The root watchdog is told where the owning user's alert queue is (it runs
    # with HOME=/root and could not find it), and the queue dir exists, created
    # as that user rather than by root.
    queue = Path(env["NETRES_ETC_ROOT"]) / ".genesis" / "alerts" / "queue"
    assert f'Environment="NETWD_ALERT_QUEUE={queue}"' in service
    assert queue.is_dir()
    assert service.index("Environment=") < service.index("ExecStart=")

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


def test_runtime_enabled_timer_is_re_enabled_persistently(tmp_path):
    """Codex P2: `enabled-runtime` (systemctl enable --runtime) is transient — it
    lives under /run and vanishes on reboot, yet `is-enabled` exits 0 for it. An
    exit-code check would wrongly treat it as persistent and skip enable; the heal
    keys on stdout == exactly 'enabled', so a runtime-only enablement re-enables
    persistently."""
    env = _stage(tmp_path)
    _run_apply(env)  # fresh: persistently enabled + active
    Path(env["SYSTEMCTL_LOG"]).write_text("")

    base = env["SYSTEMCTL_LOG"][:-4]
    Path(base + ".timerenabled").unlink(missing_ok=True)  # not persistently enabled …
    Path(base + ".timerenabledruntime").write_text("")  # … only transiently enabled
    result = _run_apply(env)
    assert result.returncode == 0, result.stderr
    calls = Path(env["SYSTEMCTL_LOG"]).read_text()
    assert "enable genesis-network-watchdog.timer" in calls  # re-enabled persistently
    assert "could not be re-enabled" not in result.stdout  # and it took


def test_runtime_masked_timer_is_unmasked_and_healed(tmp_path):
    """Codex P2: `mask --runtime` reports is-enabled 'masked-runtime' (a /run
    shadow), not 'masked'. The unmask guard must match BOTH variants, else a
    runtime-masked timer is never unmasked and the heal fails until reboot."""
    env = _stage(tmp_path)
    _run_apply(env)
    Path(env["SYSTEMCTL_LOG"]).write_text("")

    base = env["SYSTEMCTL_LOG"][:-4]
    Path(base + ".timermaskruntime").write_text("")  # `systemctl mask --runtime`
    Path(base + ".timerstarted").unlink(missing_ok=True)
    Path(base + ".timerenabled").unlink(missing_ok=True)
    result = _run_apply(env)
    assert result.returncode == 0, result.stderr
    calls = Path(env["SYSTEMCTL_LOG"]).read_text()
    assert "unmask genesis-network-watchdog.timer" in calls  # runtime mask detected + cleared
    assert "enable genesis-network-watchdog.timer" in calls
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
    is-active)
        if [ "$2" = "tailscaled" ]; then printf '%s' "${WD_TS_ACTIVE-active}"
        else printf '%s' "${WD_ACTIVE-active}"; fi ;;
    show)
        if [ "$2" = "tailscaled" ] && [ -n "${WD_TS_START_RAW:-}" ]; then printf '%s' "$WD_TS_START_RAW"
        elif [ "$2" = "tailscaled" ]; then printf '@%s' "${WD_TS_START_EPOCH-0}"
        else printf '@%s' "${WD_START_EPOCH-0}"; fi ;;
    restart)
        echo "restart $2" >> "$WD_RESTART_LOG"
        if [ "$2" = "tailscaled" ]; then exit "${WD_TS_RESTART_RC:-0}"; fi
        exit "${WD_RESTART_RC:-0}" ;;
esac
exit 0
"""

# `tailscale status --json` echoes WD_TS_STATUS; `tailscale ping` logs its argv
# and answers per kind: a --tsmp ping (through the tunnel) exits WD_TS_TSMP_RC,
# a plain discovery ping exits WD_TS_DISCO_RC. 0 = pong, 1 = "no reply" — the
# exit codes measured from the real CLI. WD_TS_RELAY_ONLY=1 models a peer whose
# discovery pong arrives only over a relay: the real CLI's --until-direct
# (default true) then exits 1 ("direct connection not established"), so only
# a ping carrying --until-direct=false succeeds.
_WD_TAILSCALE_STUB = """#!/bin/bash
if [ "$1" = "status" ]; then printf '%s' "$WD_TS_STATUS"; exit 0; fi
if [ "$1" = "ping" ]; then
    echo "$*" >> "$WD_TS_PING_LOG"
    case " $* " in
        *" --tsmp "*) exit "${WD_TS_TSMP_RC:-0}" ;;
    esac
    if [ "${WD_TS_RELAY_ONLY:-0}" = "1" ]; then
        case " $* " in *" --until-direct=false "*) ;; *) exit 1 ;; esac
    fi
    exit "${WD_TS_DISCO_RC:-0}"
fi
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
    tailscale = bin_dir / "tailscale-stub"
    tailscale.write_text(_WD_TAILSCALE_STUB)
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    queue = tmp_path / "alerts" / "queue"
    queue.mkdir(parents=True)
    return {
        # Always the stub, never the real CLI on the test machine's PATH.
        "NETWD_TAILSCALE_BIN": str(tailscale),
        "NETWD_TS_STAMP_FILE": str(tmp_path / "ts-stamp"),
        "NETWD_TS_STATUS_SNAPSHOT": str(tmp_path / "ts-status.json"),
        "NETWD_ALERT_QUEUE": str(queue),
        "WD_TS_PING_LOG": str(tmp_path / "ts-ping.log"),
        # No active peers: the tailscale check has nothing to look at.
        "WD_TS_STATUS": json.dumps({"Self": {"Relay": "r1"}, "Peer": {}}),
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


# ── Tailscale stuck-tunnel check ──────────────────────────────────────────────
#
# The replay below is an observed incident's shape, with synthetic names, times
# and addresses: the peer was Active, its last WireGuard handshake stopped
# advancing, tunnel pings got no reply, and discovery pings still answered. The watchdog ticks every ~2 min; 300s after the stall is the first
# tick that should act.

_STALL = datetime(2030, 1, 15, 12, 0, 0, tzinfo=UTC)
_PEER_IP = "100.64.0.7"


def _ts_status(*, handshake: str, active: bool = True) -> str:
    return json.dumps(
        {
            "BackendState": "Running",
            "Self": {"HostName": "node", "Relay": "r1"},
            "Peer": {
                "nodekey:aa": {
                    "HostName": "laptop",
                    "TailscaleIPs": [_PEER_IP, "2001:db8::7"],  # IPv6 doc range
                    "Active": active,
                    "Online": True,
                    "Relay": "r2",
                    "CurAddr": "198.51.100.7:41641",
                    "LastHandshake": handshake,
                    "RxBytes": 15434076,
                    "TxBytes": 39340372,
                },
                "nodekey:bb": {  # idle, never handshaked — never a suspect
                    "HostName": "idle-peer",
                    "TailscaleIPs": ["100.64.0.8"],
                    "Active": False,
                    "LastHandshake": "0001-01-01T00:00:00Z",
                },
            },
        }
    )


def _stage_incident(tmp_path: Path, *, seconds_after_stall: int = 326) -> dict:
    env = _stage_wd(tmp_path)
    # Real-CLI timestamp shape: nanoseconds + a local offset.
    env["WD_TS_STATUS"] = _ts_status(handshake="2030-01-15T14:00:00.577514622+02:00")
    env["NETWD_NOW"] = str(int(_STALL.timestamp()) + seconds_after_stall)
    env["WD_TS_TSMP_RC"] = "1"  # tunnel: no reply
    env["WD_TS_DISCO_RC"] = "0"  # path: fine
    return env


def _ts_restarted(env: dict) -> bool:
    log = Path(env["WD_RESTART_LOG"])
    return log.exists() and "restart tailscaled" in log.read_text()


def _pings(env: dict) -> list[str]:
    log = Path(env["WD_TS_PING_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def _alerts(env: dict) -> list[dict]:
    from genesis.guardian.alert.queue import list_queued

    return [entry for _, entry in list_queued(env["NETWD_ALERT_QUEUE"])]


def test_tailscale_incident_replay_heals_and_alerts(tmp_path):
    env = _stage_incident(tmp_path)
    result = _run_wd(env)
    assert result.returncode == 0, result.stderr
    assert _ts_restarted(env)
    assert not _restarted(env)  # networkd was healthy — untouched
    ts = _state(env)["tailscale"]
    assert ts["last_action"] == "healed"
    assert ts["heal_count"] == 1
    assert ts["last_peer"] == f"laptop ({_PEER_IP})"
    diag = ts["last_diagnostics"]
    # The evidence that explained the incident, captured at the moment.
    assert diag["handshake_age_s"] == 325  # 326s minus the .577s fraction, floored
    assert diag["self_relay"] == "r1"
    assert diag["peer_relay"] == "r2"
    assert diag["peer_curaddr"] == "198.51.100.7:41641"
    assert diag["tsmp_ping"] == "no-reply" and diag["disco_ping"] == "reply"
    snapshot = Path(env["NETWD_TS_STATUS_SNAPSHOT"])
    assert json.loads(snapshot.read_text())["Peer"]["nodekey:aa"]["HostName"] == "laptop"
    assert snapshot.stat().st_mode & 0o077 == 0  # names the whole tailnet: root-only
    # The owner alert parses with the REAL drain-side reader.
    alerts = _alerts(env)
    assert len(alerts) == 1
    assert alerts[0]["source"] == "network-watchdog"
    assert "restarted tailscaled" in alerts[0]["title"]
    assert "laptop" in alerts[0]["title"]
    assert Path(env["NETWD_TS_STAMP_FILE"]).read_text().strip() == env["NETWD_NOW"]


def test_tailscale_stall_younger_than_threshold_is_left_alone(tmp_path):
    # 226s after the stall: past WireGuard's 180s reject point but inside one
    # watchdog tick of it — not yet confirmed across two ticks.
    env = _stage_incident(tmp_path, seconds_after_stall=226)
    _run_wd(env)
    assert not _ts_restarted(env)
    assert _pings(env) == []  # not even a suspect
    assert _state(env)["tailscale"]["last_action"] == "none"


def test_tailscale_tunnel_that_answers_is_not_stuck(tmp_path):
    env = _stage_incident(tmp_path)
    env["WD_TS_TSMP_RC"] = "0"
    _run_wd(env)
    assert not _ts_restarted(env)
    assert _alerts(env) == []
    assert _state(env)["tailscale"]["last_action"] == "none"


def test_tailscale_peer_that_went_away_is_not_healed(tmp_path):
    # Tunnel AND discovery both silent: the peer is gone (lid shut, offline).
    # Restarting our daemon cannot bring it back.
    env = _stage_incident(tmp_path)
    env["WD_TS_DISCO_RC"] = "1"
    _run_wd(env)
    assert not _ts_restarted(env)
    assert _alerts(env) == []
    assert _state(env)["tailscale"]["last_action"] == "suspect-unreachable"


def test_tailscale_inactive_peer_with_old_handshake_is_not_pinged(tmp_path):
    env = _stage_incident(tmp_path)
    env["WD_TS_STATUS"] = _ts_status(handshake="2030-01-15T14:00:00.577514622+02:00", active=False)
    _run_wd(env)
    assert _pings(env) == []
    assert not _ts_restarted(env)


def test_tailscale_only_the_tunnel_ping_decides_order(tmp_path):
    # The tunnel ping runs first; a discovery ping is only spent on a peer whose
    # tunnel did not answer.
    env = _stage_incident(tmp_path)
    _run_wd(env)
    pings = _pings(env)
    assert len(pings) == 2
    assert "--tsmp" in pings[0] and _PEER_IP in pings[0]
    assert "--tsmp" not in pings[1] and _PEER_IP in pings[1]


def test_tailscale_rate_limit_is_an_hour(tmp_path):
    env = _stage_incident(tmp_path)
    Path(env["NETWD_TS_STAMP_FILE"]).write_text(str(int(env["NETWD_NOW"]) - 1800))
    _run_wd(env)
    assert not _ts_restarted(env)
    assert _alerts(env) == []
    ts = _state(env)["tailscale"]
    assert ts["last_action"] == "ratelimited"
    assert ts["last_diagnostics"]["peer_relay"] == "r2"  # evidence kept anyway


def test_tailscale_heals_again_after_the_hour(tmp_path):
    env = _stage_incident(tmp_path)
    Path(env["NETWD_TS_STAMP_FILE"]).write_text(str(int(env["NETWD_NOW"]) - 3601))
    _run_wd(env)
    assert _ts_restarted(env)


def test_tailscale_networkd_rate_limit_does_not_gate_tailscale(tmp_path):
    env = _stage_incident(tmp_path)
    Path(env["NETWD_STAMP_FILE"]).write_text(env["NETWD_NOW"])  # networkd just healed
    _run_wd(env)
    assert _ts_restarted(env)


def test_tailscale_observe_mode_alerts_without_restarting(tmp_path):
    env = _stage_incident(tmp_path)
    env["NETWD_TS_MODE"] = "observe"
    _run_wd(env)
    assert not _ts_restarted(env)
    assert _state(env)["tailscale"]["last_action"] == "observed"
    alerts = _alerts(env)
    assert len(alerts) == 1 and "not restarted" in alerts[0]["title"]
    assert Path(env["NETWD_TS_STAMP_FILE"]).exists()  # an hour before the next alert


def test_tailscale_unknown_mode_degrades_to_observe_not_live(tmp_path):
    env = _stage_incident(tmp_path)
    env["NETWD_TS_MODE"] = "yes-please"
    _run_wd(env)
    assert not _ts_restarted(env)
    assert _state(env)["tailscale"]["last_action"] == "observed"


def test_tailscale_off_mode_does_nothing(tmp_path):
    env = _stage_incident(tmp_path)
    env["NETWD_TS_MODE"] = "off"
    _run_wd(env)
    assert _pings(env) == []
    assert not _ts_restarted(env)
    assert _state(env)["tailscale"]["last_action"] == "off"


def test_tailscale_daemon_not_running_is_unavailable(tmp_path):
    env = _stage_incident(tmp_path)
    env["WD_TS_ACTIVE"] = "inactive"
    _run_wd(env)
    assert _pings(env) == []
    assert not _ts_restarted(env)  # never starts a daemon someone stopped
    assert _state(env)["tailscale"]["last_action"] == "unavailable"


def test_tailscale_not_installed_is_unavailable(tmp_path):
    env = _stage_incident(tmp_path)
    env["NETWD_TAILSCALE_BIN"] = str(tmp_path / "no-such-tailscale")
    result = _run_wd(env)
    assert result.returncode == 0
    assert _state(env)["tailscale"]["last_action"] == "unavailable"


def test_tailscale_failed_restart_alerts_and_spends_the_hour(tmp_path):
    # A failed restart has still dropped every session, and the next ticks will
    # see tailscaled down and (by design) not start it. So: no heal claimed, the
    # owner is told NOW, and the hour is spent so it cannot repeat every tick.
    env = _stage_incident(tmp_path)
    env["WD_TS_RESTART_RC"] = "1"
    result = _run_wd(env)
    assert result.returncode != 0  # surfaces as a failed oneshot
    assert _ts_restarted(env)  # attempted
    ts = _state(env)["tailscale"]
    assert ts["last_action"] == "restart-failed"
    assert ts["heal_count"] == 0
    assert Path(env["NETWD_TS_STAMP_FILE"]).read_text().strip() == env["NETWD_NOW"]
    alerts = _alerts(env)
    assert len(alerts) == 1 and "FAILED" in alerts[0]["title"]
    # Next tick, restart still failing and the unit still up: no second attempt.
    Path(env["WD_RESTART_LOG"]).write_text("")
    env["NETWD_NOW"] = str(int(env["NETWD_NOW"]) + 120)
    _run_wd(env)
    assert not _ts_restarted(env)
    assert _state(env)["tailscale"]["last_action"] == "ratelimited"


def test_tailscale_missing_queue_still_heals(tmp_path):
    env = _stage_incident(tmp_path)
    env["NETWD_ALERT_QUEUE"] = str(tmp_path / "absent")
    result = _run_wd(env)
    assert result.returncode == 0
    assert _ts_restarted(env)
    assert not (tmp_path / "absent").exists()  # root must never create it
    assert "journal-only" in result.stdout


def test_both_checks_run_and_both_heal(tmp_path):
    env = _stage_incident(tmp_path)
    env["WD_LINKS_JSON"] = json.dumps(
        {"Interfaces": [{"Name": "eth0", "AdministrativeState": "failed"}]}
    )
    _run_wd(env)
    assert _restarted(env) and _ts_restarted(env)
    st = _state(env)
    assert st["last_trigger"] == "failed-link:eth0"
    assert st["tailscale"]["last_action"] == "healed"


def test_networkd_write_keeps_the_tailscale_record(tmp_path):
    env = _stage_incident(tmp_path)
    _run_wd(env)  # heal
    env["WD_TS_TSMP_RC"] = "0"  # next tick: tunnel is fine again
    env["NETWD_NOW"] = str(int(env["NETWD_NOW"]) + 120)
    _run_wd(env)
    st = _state(env)
    assert st["tailscale"]["heal_count"] == 1
    assert st["tailscale"]["last_heal"] == int(env["NETWD_NOW"]) - 120
    assert st["heal_count"] == 0  # networkd's own counter untouched


def test_tailscale_relay_only_peer_is_still_healed(tmp_path):
    # No direct path to the peer: its discovery pong arrives over a relay. That
    # peer IS reachable, so a stuck tunnel to it must heal, not be written off
    # as "unreachable".
    env = _stage_incident(tmp_path)
    env["WD_TS_RELAY_ONLY"] = "1"
    _run_wd(env)
    assert _ts_restarted(env)
    disco = [p for p in _pings(env) if "--tsmp" not in p]
    assert disco and "--until-direct=false" in disco[0]


def test_tailscale_unparseable_status_is_not_healthy(tmp_path):
    for i, bad in enumerate(("not json at all", json.dumps({"Peer": []}))):
        case_dir = tmp_path / f"case{i}"
        case_dir.mkdir()
        env = _stage_incident(case_dir)
        env["WD_TS_STATUS"] = bad
        _run_wd(env)
        assert not _ts_restarted(env)
        assert _state(env)["tailscale"]["last_action"] == "status-unparseable"


def test_garbage_stamps_do_not_abort_either_check(tmp_path):
    env = _stage_incident(tmp_path)
    env["WD_LINKS_JSON"] = json.dumps(
        {"Interfaces": [{"Name": "eth0", "AdministrativeState": "failed"}]}
    )
    Path(env["NETWD_STAMP_FILE"]).write_text("abc")
    Path(env["NETWD_TS_STAMP_FILE"]).write_text("12x")
    result = _run_wd(env)
    assert "unbound variable" not in result.stderr
    assert _restarted(env) and _ts_restarted(env)  # garbage reads as "no recent heal"


def test_alert_queue_path_is_escaped_for_systemd(tmp_path):
    env = _stage(tmp_path)
    env["NETRES_ALERT_QUEUE"] = '/srv/a%b "q"/queue'
    _run_apply(env)
    service = _paths(env)["service"].read_text()
    assert 'Environment="NETWD_ALERT_QUEUE=/srv/a%%b \\"q\\"/queue"' in service


def test_tailscale_hostile_peer_name_cannot_forge_records_or_markup(tmp_path):
    # HostName is chosen by the peer. A newline would split its record into
    # forged ones (one carrying a flag-shaped "ip" into `tailscale ping`), and
    # markup would render in the owner's Telegram alert (HTML parse mode).
    env = _stage_incident(tmp_path)
    status = json.loads(env["WD_TS_STATUS"])
    status["Peer"]["nodekey:aa"]["HostName"] = (
        'x\n--socks5-server=127.0.0.1:1\t\n<a href="http://evil.example">y</a>'
    )
    env["WD_TS_STATUS"] = json.dumps(status)
    _run_wd(env)
    pings = _pings(env)
    assert len(pings) == 2  # one tunnel + one discovery ping, no forged record
    assert all(p.endswith(f"-- {_PEER_IP}") for p in pings)
    title = _alerts(env)[0]["title"]
    assert "<" not in title and ">" not in title and "\n" not in title
    assert _state(env)["tailscale"]["last_peer"].startswith("x--socks5-server127.0.0.11ahref")


def test_tailscale_non_ip_target_is_never_pinged(tmp_path):
    env = _stage_incident(tmp_path)
    status = json.loads(env["WD_TS_STATUS"])
    status["Peer"]["nodekey:aa"]["TailscaleIPs"] = ["--help", "not-an-ip"]
    env["WD_TS_STATUS"] = json.dumps(status)
    _run_wd(env)
    assert _pings(env) == []
    assert not _ts_restarted(env)


def test_tailscale_alert_refuses_a_symlinked_queue(tmp_path):
    env = _stage_incident(tmp_path)
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "linkq"
    link.symlink_to(real)
    env["NETWD_ALERT_QUEUE"] = str(link)
    result = _run_wd(env)
    assert _ts_restarted(env)  # the heal itself still happens
    assert list(real.iterdir()) == []  # nothing written through the link
    assert "alert enqueue failed" in result.stdout


def test_tailscale_recent_daemon_start_holds_the_limit(tmp_path):
    # tailscaled's own start time bounds the rate: a start 10 minutes ago
    # (a reboot, a manual restart, or our last heal) holds off another restart
    # even with no stamp at all.
    env = _stage_incident(tmp_path)
    env["WD_TS_START_EPOCH"] = str(int(env["NETWD_NOW"]) - 600)
    _run_wd(env)
    assert not _ts_restarted(env)
    assert _state(env)["tailscale"]["last_action"] == "ratelimited"


def test_tailscale_unwritable_stamp_still_heals_only_once(tmp_path):
    # The stamp cannot be written. The heal still happens; on the next tick,
    # tailscaled's new start time (which systemd records) holds the limit.
    env = _stage_incident(tmp_path)
    env["NETWD_TS_STAMP_FILE"] = str(tmp_path / "no-such-dir" / "stamp")
    _run_wd(env)
    assert _ts_restarted(env)
    Path(env["WD_RESTART_LOG"]).write_text("")
    env["WD_TS_START_EPOCH"] = env["NETWD_NOW"]  # what the restart did
    env["NETWD_NOW"] = str(int(env["NETWD_NOW"]) + 120)
    _run_wd(env)
    assert not _ts_restarted(env)
    assert _state(env)["tailscale"]["last_action"] == "ratelimited"


def test_tailscale_scan_rotates_to_a_stuck_peer_behind_the_cap(tmp_path):
    # Three offline peers sort before the stuck one. The start point rotates
    # each tick, so the stuck peer is reached within a few ticks, not never.
    env = _stage_incident(tmp_path)
    status = json.loads(env["WD_TS_STATUS"])
    template = status["Peer"]["nodekey:aa"]
    status["Peer"] = {
        f"nodekey:{i}": {**template, "HostName": f"p{i}", "TailscaleIPs": [f"100.64.1.{i}"]}
        for i in range(4)
    }
    env["WD_TS_STATUS"] = json.dumps(status)
    # Per-IP stub: 100.64.1.0-2 are gone (both pings fail); 100.64.1.3 is stuck.
    stub = Path(env["NETWD_TAILSCALE_BIN"])
    stub.write_text(
        '#!/bin/bash\nif [ "$1" = "status" ]; then printf \'%s\' "$WD_TS_STATUS"; exit 0; fi\n'
        'echo "$*" >> "$WD_TS_PING_LOG"\n'
        'case " $* " in *" --tsmp "*) exit 1 ;; esac\n'
        'case " $* " in *" 100.64.1.3 "*) exit 0 ;; esac\nexit 1\n'
    )
    base = int(env["NETWD_NOW"])
    healed_at = None
    for tick in range(4):
        env["NETWD_NOW"] = str(base + 120 * tick)
        _run_wd(env)
        if _ts_restarted(env):
            healed_at = tick
            break
    assert healed_at is not None, _pings(env)
    assert any("100.64.1.3" in p for p in _pings(env))


def test_tailscale_observe_alert_is_one_queued_entry_per_peer(tmp_path):
    # With the stamp unwritable, observe mode fires every tick; a stable
    # per-peer key keeps ONE queued entry, as enqueue_alert does.
    env = _stage_incident(tmp_path)
    env["NETWD_TS_MODE"] = "observe"
    env["NETWD_TS_STAMP_FILE"] = str(tmp_path / "no-such-dir" / "stamp")
    for tick in range(3):
        env["NETWD_NOW"] = str(int(env["NETWD_NOW"]) + 120 * tick)
        _run_wd(env)
    alerts = _alerts(env)
    assert len(alerts) == 1
    assert alerts[0]["dedupe_key"] == f"network-watchdog:tailscale:observe:{_PEER_IP}"


def test_tailscale_bad_scan_knobs_fall_back_to_defaults(tmp_path):
    env = _stage_incident(tmp_path)
    env["NETWD_TS_MAX_PROBES"] = "abc"
    env["NETWD_TS_SCAN_BUDGET_SEC"] = "3x"
    result = _run_wd(env)
    assert "unbound variable" not in result.stderr
    assert _ts_restarted(env)


def test_tailscale_zero_padded_knob_falls_back_too(tmp_path):
    # "08" is bad octal in bash arithmetic: accepting it would make the cap
    # comparison fail silently and probe every peer.
    env = _stage_incident(tmp_path)
    env["WD_TS_DISCO_RC"] = "1"
    status = json.loads(env["WD_TS_STATUS"])
    template = status["Peer"]["nodekey:aa"]
    status["Peer"] = {
        f"nodekey:{i}": {**template, "HostName": f"p{i}", "TailscaleIPs": [f"100.64.1.{i}"]}
        for i in range(5)
    }
    env["WD_TS_STATUS"] = json.dumps(status)
    env["NETWD_TS_MAX_PROBES"] = "08"
    result = _run_wd(env)
    assert "value too great" not in result.stderr
    assert len(_pings(env)) == 3 * 2  # the default cap of 3 applied


def test_tailscale_real_systemd_timestamp_format_is_parsed(tmp_path):
    # systemd prints e.g. "Tue 2030-01-15 12:03:00 UTC", not an @epoch.
    env = _stage_incident(tmp_path)
    started = datetime.fromtimestamp(int(env["NETWD_NOW"]) - 300, UTC)
    env["WD_TS_START_RAW"] = started.strftime("%a %Y-%m-%d %H:%M:%S UTC")
    _run_wd(env)
    assert not _ts_restarted(env)  # started 5 min ago: inside the hour
    assert _state(env)["tailscale"]["last_action"] == "ratelimited"


def test_tailscale_hostile_queue_entries_cannot_hang_or_break_the_alert(tmp_path):
    # The stable-key dedupe scan reads the user-owned queue as root. A symlink
    # to /dev/zero, a FIFO and a non-object JSON file must not exhaust memory,
    # block, or stop the alert from being queued.
    import os

    env = _stage_incident(tmp_path)
    env["NETWD_TS_MODE"] = "observe"
    queue = Path(env["NETWD_ALERT_QUEUE"])
    (queue / "zero.json").symlink_to("/dev/zero")
    os.mkfifo(queue / "fifo.json")
    (queue / "list.json").write_text("[]")
    result = _run_wd(env)  # _run_wd has a 30s timeout: a hang fails here
    assert result.returncode == 0
    assert "alert enqueue failed" not in result.stdout
    keyed = [
        f
        for f in queue.glob("*.json")
        if f.is_file() and not f.is_symlink() and f.name != "list.json"
    ]
    assert len(keyed) == 1


def test_tailscale_scan_is_capped_per_tick(tmp_path):
    # Many stale Active peers, all gone (tunnel and discovery both silent):
    # only TS_MAX_PROBES of them are probed this tick, and the log says so.
    env = _stage_incident(tmp_path)
    env["WD_TS_DISCO_RC"] = "1"
    status = json.loads(env["WD_TS_STATUS"])
    template = status["Peer"]["nodekey:aa"]
    status["Peer"] = {
        f"nodekey:{i}": {**template, "HostName": f"p{i}", "TailscaleIPs": [f"100.64.1.{i}"]}
        for i in range(5)
    }
    env["WD_TS_STATUS"] = json.dumps(status)
    result = _run_wd(env)
    assert len(_pings(env)) == 3 * 2  # 3 peers, a tunnel + a discovery ping each
    assert "probed 3 suspect peer(s); 2 more not probed" in result.stdout
    assert not _ts_restarted(env)


def test_tailscale_scan_budget_stops_new_probes(tmp_path):
    env = _stage_incident(tmp_path)
    env["WD_TS_DISCO_RC"] = "1"
    env["NETWD_TS_SCAN_BUDGET_SEC"] = "0"  # budget already spent at the first check
    result = _run_wd(env)
    assert _pings(env) == []
    assert "1 more not probed" in result.stdout


def test_installer_under_sudo_honours_genesis_home(tmp_path):
    # Run as root via sudo, the queue follows GENESIS_HOME (what the drainer
    # reads), not the invoking user's default ~/.genesis.
    env = _stage(tmp_path)
    bin_dir = Path(env["PATH"].split(":")[0])
    (bin_dir / "id").write_text(
        '#!/bin/bash\n[ "$1" = "-u" ] && { echo 0; exit 0; }\nexec /usr/bin/id "$@"\n'
    )
    (bin_dir / "getent").write_text(
        '#!/bin/bash\necho "alice:x:1000:1000::/home/alice:/bin/bash"\n'
    )
    # sudo -u USER cmd... -> run cmd (the stub cannot switch users)
    (bin_dir / "sudo").write_text(
        '#!/bin/bash\nif [ "$1" = "-n" ] && [ "$2" = "true" ]; then exit 0; fi\n'
        'if [ "$1" = "-u" ]; then shift 2; fi\nexec "$@"\n'
    )
    for f in ("id", "getent", "sudo"):
        (bin_dir / f).chmod(0o755)
    custom = tmp_path / "srv-genesis"
    env["SUDO_USER"] = "alice"
    env["GENESIS_HOME"] = str(custom)
    result = _run_apply(env)
    assert result.returncode == 0, result.stderr
    service = _paths(env)["service"].read_text()
    assert f'Environment="NETWD_ALERT_QUEUE={custom}/alerts/queue"' in service
    assert (custom / "alerts" / "queue").is_dir()
