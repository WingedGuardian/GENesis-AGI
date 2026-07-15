#!/usr/bin/env bash
#
# genesis-network-watchdog — detect a wedged systemd-networkd and heal it by
# restarting the daemon. Codifies the manual `systemctl restart systemd-networkd`
# recovery used twice during the 2026-07 eth0 rtnetlink-timeout incidents
# (`eth0: Could not set route: Connection timed out` → `eth0: Failed`, leaving
# the link in AdministrativeState=failed with renewals dead). Installed to
# /usr/local/lib/genesis/network-watchdog.sh and run as root by
# genesis-network-watchdog.timer (~every 2 min) via network_resilience.sh.
#
# The restart is address-preserving: with KeepConfiguration=true on the link
# (network_resilience.sh lays that down), the kept address survives the daemon
# bounce — networkd logs "considered critical, ignoring request to reconfigure".
#
# Triggers (any → heal, subject to grace + rate limit):
#   1. systemd-networkd inactive — but NOT masked (mask = deliberate operator
#      intent; we must not fight it).
#   2. A managed link in AdministrativeState=failed — the live fingerprint:
#      OperationalState stays "routable" (address held) while link SETUP failed.
#   3. No IPv4 default route.
#
# Healthy path exits 0 silently (no per-tick journal spam). Every run rewrites a
# /run telemetry file the infra profile reads as network metrics, so heals are
# visible in INFRASTRUCTURE.md / the dashboard instead of buried in root logs.
set -uo pipefail

STATE_FILE="${NETWD_STATE_FILE:-/run/genesis-network-watchdog.json}"
STAMP_FILE="${NETWD_STAMP_FILE:-/run/genesis-network-watchdog.last}"
RATE_LIMIT_SEC="${NETWD_RATE_LIMIT_SEC:-600}"   # min seconds between heals
GRACE_SEC="${NETWD_GRACE_SEC:-120}"             # skip if networkd just (re)started
SYSTEMCTL="${NETWD_SYSTEMCTL:-systemctl}"
NETWD_NOW="${NETWD_NOW:-$(date +%s)}"           # overridable for tests

log() { printf 'genesis-network-watchdog: %s\n' "$1"; }

# Persist telemetry. action ∈ {none, ratelimited, healed}. heal_count and
# last_heal carry forward from the prior file; heal_count only increments on a
# real heal. Written atomically, 0644 so the non-root infra collector can read.
_write_state() {
    local action="$1" trigger="$2"
    NETWD_STATE_FILE="$STATE_FILE" A_NOW="$NETWD_NOW" A_ACTION="$action" \
        A_TRIGGER="$trigger" python3 - <<'PY' 2>/dev/null || true
import json, os
path = os.environ["NETWD_STATE_FILE"]
now = int(os.environ["A_NOW"])
action = os.environ["A_ACTION"]
trigger = os.environ["A_TRIGGER"] or None
try:
    with open(path) as fh:
        prior = json.load(fh)
    if not isinstance(prior, dict):
        prior = {}
except Exception:
    prior = {}
heal_count = int(prior.get("heal_count", 0) or 0)
last_heal = prior.get("last_heal")
last_trigger = prior.get("last_trigger")
if action == "healed":
    heal_count += 1
    last_heal = now
    last_trigger = trigger
elif trigger:
    last_trigger = trigger  # record the observed trigger even when rate-limited
state = {
    "last_check": now,
    "last_heal": last_heal,
    "last_trigger": last_trigger,
    "heal_count": heal_count,
    "last_action": action or "none",
}
tmp = f"{path}.tmp"
with open(tmp, "w") as fh:
    json.dump(state, fh)
os.replace(tmp, path)
try:
    os.chmod(path, 0o644)
except OSError:
    pass
PY
}

_networkd_start_epoch() {
    local ts
    ts="$("$SYSTEMCTL" show systemd-networkd -p ActiveEnterTimestamp --value 2>/dev/null)"
    [[ -z "$ts" ]] && { echo 0; return 0; }
    date -d "$ts" +%s 2>/dev/null || echo 0
}

# First managed link stuck in AdministrativeState=failed, or empty.
_first_failed_link() {
    networkctl --json=short list 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for iface in data.get("Interfaces", []):
    if iface.get("AdministrativeState") == "failed":
        print(iface.get("Name", "?"))
        break
' 2>/dev/null
}

_has_default_route() {
    [[ -n "$(ip route show default 2>/dev/null)" ]]
}

main() {
    # Masked = operator intent; never fight it.
    if [[ "$("$SYSTEMCTL" is-enabled systemd-networkd 2>/dev/null || true)" == "masked" ]]; then
        _write_state "none" ""
        return 0
    fi

    local trigger=""
    if [[ "$("$SYSTEMCTL" is-active systemd-networkd 2>/dev/null || true)" != "active" ]]; then
        trigger="networkd-inactive"
    else
        # Active: give a fresh (re)start time to reconfigure before we judge its
        # links failed — otherwise we could ping-pong a settling daemon.
        local started; started="$(_networkd_start_epoch)"
        if (( started > 0 && NETWD_NOW - started < GRACE_SEC )); then
            _write_state "none" ""
            return 0
        fi
        local failed; failed="$(_first_failed_link)"
        if [[ -n "$failed" ]]; then
            trigger="failed-link:$failed"
        elif ! _has_default_route; then
            trigger="no-default-route"
        fi
    fi

    if [[ -z "$trigger" ]]; then
        _write_state "none" ""
        return 0
    fi

    # A trigger fired — heal unless we healed too recently (a persistent fault
    # should page a human via loud logs, not flap-restart every 2 min).
    local last_heal=0
    [[ -f "$STAMP_FILE" ]] && last_heal="$(cat "$STAMP_FILE" 2>/dev/null || echo 0)"
    if (( last_heal > 0 && NETWD_NOW - last_heal < RATE_LIMIT_SEC )); then
        log "trigger=$trigger but a heal fired <${RATE_LIMIT_SEC}s ago — NOT restarting; networkd may need manual attention"
        _write_state "ratelimited" "$trigger"
        return 0
    fi

    log "healing: restarting systemd-networkd (trigger=$trigger)"
    if "$SYSTEMCTL" restart systemd-networkd; then
        # Only a SUCCESSFUL restart counts: stamp the rate-limit window and
        # record the heal. A failed restart must NOT claim a heal in telemetry
        # and must NOT arm the rate limit (so the next tick retries promptly).
        echo "$NETWD_NOW" >"$STAMP_FILE" 2>/dev/null || true
        _write_state "healed" "$trigger"
        return 0
    else
        # $? here is the failed restart's exit code (after `fi` it would be the
        # if-compound's 0). Don't stamp; leave the rate limit disarmed to retry.
        local rc=$?
        log "restart FAILED (rc=$rc, trigger=$trigger) — will retry next tick"
        _write_state "restart-failed" "$trigger"
        return "$rc"
    fi
}

main "$@"
