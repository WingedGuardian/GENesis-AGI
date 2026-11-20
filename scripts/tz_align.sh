#!/usr/bin/env bash
#
# tz_align.sh — set the host and container clocks to the install's zone.
#
# WHY: the one zone setting (genesis.yaml `timezone`, written by the dashboard's
# Timezone control) was applied to the system clocks only at install, so the
# host clock could sit in UTC while everything else read local time. This runs
# daily (genesis-tz-align.timer, 12:20 local) and right after a dashboard zone
# change (the route starts the service).
#
# With a guardian: one gateway call, `set-timezone <zone>`, which sets the host
# clock and then the container's (through incus). Without one: the container
# clock only, through `sudo -n timedatectl`, when passwordless sudo exists.
# Host jobs carry no zone of their own, so after a move they run at their usual
# times in local time, as on a fresh install.
#
# Exit 0 for every "nothing to do or nothing reachable" case (lever off, no zone
# chosen, host unreachable, an old gateway without the verb, no sudo); exit 3
# when a reached clock could not be set, so the unit shows failed.
#
# Invoked by scripts/systemd/genesis-tz-align.{service,timer}.template.

set -u

# Resolve HOME when unset (stripped-env/systemd invocations), as the sibling
# cc_tmp_align_host.sh does.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

GENESIS_ROOT="$(unset CDPATH; cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$GENESIS_ROOT/.venv/bin/python"
GUARDIAN_CONFIG="$HOME/.genesis/guardian_remote.yaml"
SSH_KEY="$HOME/.ssh/genesis_guardian_ed25519"
# "<epoch> <zone>" of the last host move this script made. A move to a DIFFERENT
# zone waits 60 min after it: each host move can re-run daily host jobs, and the
# dashboard route that starts this unit is reachable by any dashboard caller.
LAST_MOVE="$HOME/.genesis/state/tz_align_last_move"
MOVE_COOLDOWN_S=3600
# shellcheck source=lib/timezone.sh
. "$GENESIS_ROOT/scripts/lib/timezone.sh"

log() { echo "tz_align: $*"; }

LOCKFILE="$HOME/.genesis/locks/tz_align.lock"
mkdir -p "$(dirname "$LOCKFILE")" 2>/dev/null || true
if ! exec {LOCK_FD}>"$LOCKFILE"; then
    log "cannot open lockfile $LOCKFILE — skipping"
    exit 0
fi
if ! flock -n "$LOCK_FD"; then
    log "another run is in progress — skipping"
    exit 0
fi

if [ ! -x "$VENV_PY" ]; then
    log "venv python ($VENV_PY) unavailable — cannot read the zone (skipping)"
    exit 0
fi

# One read for the lever and the zone (genesis.tz_align_config), from this
# script's own checkout rather than whichever tree the venv's install points at.
# Its warnings (a damaged lever file reads as off) go to the journal.
read_lever() {
    MODE="" ZONE=""
    read -r MODE ZONE < <(cd "$GENESIS_ROOT" && PYTHONPATH="$GENESIS_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$VENV_PY" -c \
        'from genesis.tz_align_config import chosen_zone, effective_mode; print(effective_mode(), chosen_zone() or "-")') || true
    [ "$ZONE" = - ] && ZONE=""
}

# Classifies the gateway reply (stdin): its last parseable JSON line decides.
# A reply nested deep enough to hit the recursion limit is unparseable, not fatal.
VERDICT_PY='
import json, sys
last = None
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if line.startswith("{"):
        try:
            last = json.loads(line)
        except (ValueError, RecursionError):
            continue
if not isinstance(last, dict):
    print("nojson")
elif last.get("error") == "denied":
    print("denied")
elif last.get("ok") is True and last.get("container") in ("set", "unchanged", "no-incus"):
    print("ok changed=%s container=%s" % (str(last.get("changed")).lower(), last["container"]))
else:
    print("failed")
'

# apply_zone ZONE — set the clocks once. Returns 0 when done or when nothing is
# reachable (logged), 3 when a reached clock could not be set.
apply_zone() {
    local zone="$1" now resp rc verdict
    if [ ! -f "$GUARDIAN_CONFIG" ]; then
        # No host plane: the container clock only.
        now="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
        if [ "$now" = "$zone" ]; then
            log "container clock already $zone (no guardian)"
            return 0
        fi
        if ! sudo -n true 2>/dev/null; then
            log "no guardian and no passwordless sudo — container clock left at ${now:-unknown}"
            return 0
        fi
        apply_system_timezone "$zone" "Container" sudo -n || return 3
        return 0
    fi
    if [ ! -f "$SSH_KEY" ]; then
        log "guardian SSH key $SSH_KEY missing — cannot reach the host (skipping)"
        return 0
    fi
    if [ -z "$HOST_IP" ]; then
        log "host_ip unparseable in $GUARDIAN_CONFIG — skipping"
        return 0
    fi
    # stderr is kept: the gateway reports refusals and host failures there, and
    # an old gateway's `denied` too. The verb itself is quick; 120 s bounds a
    # host that accepted the connection and then hung.
    resp="$(timeout 120 ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
        "${HOST_USER}@${HOST_IP}" "set-timezone $zone" 2>&1)"
    rc=$?
    # The reply goes in on stdin, which has no size limit (a single argv word does).
    verdict="$(printf '%s' "$resp" | "$VENV_PY" -c "$VERDICT_PY")" || verdict="nojson"
    case "$verdict" in
        ok\ *)
            log "host and container clocks at $zone (${verdict#ok })"
            if [[ "$verdict" == *"changed=true"* ]]; then
                mkdir -p "$(dirname "$LAST_MOVE")" 2>/dev/null || true
                printf '%s %s\n' "$(date +%s)" "$zone" > "$LAST_MOVE.tmp" \
                    && mv -f "$LAST_MOVE.tmp" "$LAST_MOVE"
            fi
            return 0 ;;
        denied)
            log "the host gateway predates set-timezone — clocks left alone until it is updated"
            return 0 ;;
        nojson)
            if [ "$rc" -eq 124 ]; then
                log "the host did not answer set-timezone within 120 s — clock state unknown"
                return 3
            fi
            log "host unreachable (ssh exit $rc) — clocks left alone"
            return 0 ;;
        *)
            log "could not set the clocks to $zone: $(printf '%s' "$resp" | tail -n 1)"
            return 3 ;;
    esac
}

read_lever
if [ "$MODE" != live ]; then
    log "mode is ${MODE:-unreadable} — clocks left alone"
    exit 0
fi
if [ -z "$ZONE" ]; then
    log "no zone chosen in genesis.yaml — clocks left alone"
    exit 0
fi

HOST_IP="" HOST_USER="ubuntu"
if [ -f "$GUARDIAN_CONFIG" ]; then
    HOST_IP="$("$VENV_PY" -c "import yaml, pathlib; print(yaml.safe_load(pathlib.Path('$GUARDIAN_CONFIG').read_text()).get('host_ip', ''))" 2>/dev/null || true)"
    HOST_USER="$("$VENV_PY" -c "import yaml, pathlib; print(yaml.safe_load(pathlib.Path('$GUARDIAN_CONFIG').read_text()).get('host_user', 'ubuntu'))" 2>/dev/null || echo "ubuntu")"
fi

# The host-move cooldown is checked once per run; the re-read passes below are
# bounded on their own, so one run moves the host at most 3 times.
if [ -f "$GUARDIAN_CONFIG" ] && [ -r "$LAST_MOVE" ]; then
    last_at="" last_zone=""
    read -r last_at last_zone < "$LAST_MOVE" || true
    now_s="$(date +%s)"
    # A stamp from the future (a clock stepped back, a damaged file) never holds
    # a move: nothing would rewrite it until real time caught up.
    if [[ "$last_at" =~ ^[0-9]{1,12}$ ]] && [ "$last_zone" != "$ZONE" ] \
        && [ "$last_at" -le "$now_s" ] && [ $((now_s - last_at)) -lt "$MOVE_COOLDOWN_S" ]; then
        log "the host clock moved $(((now_s - last_at) / 60)) min ago — $ZONE waits for a run after the 60-min cooldown"
        exit 0
    fi
fi

# A dashboard change made while this run is applying an earlier one starts the
# unit again, and systemd folds that start into the running job. So the zone is
# re-read after each apply, and a newer one applied too (at most 3 passes).
passes=0
while :; do
    apply_zone "$ZONE" || exit $?
    applied="$ZONE"
    read_lever
    if [ "$MODE" != live ] || [ -z "$ZONE" ] || [ "$ZONE" = "$applied" ]; then
        exit 0
    fi
    passes=$((passes + 1))
    if [ "$passes" -ge 3 ]; then
        log "the zone keeps changing ($ZONE now) — the next run applies it"
        exit 0
    fi
    log "the zone changed to $ZONE during this run — applying it"
done
