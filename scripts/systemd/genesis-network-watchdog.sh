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
# A second, independent check heals a STUCK TAILSCALE TUNNEL (see
# _tailscale_check). Observed failure: the address and the path to a peer were
# both fine, but the peer stopped completing WireGuard handshakes. It kept
# sending its replies through a relay (DERP) region this node had already left,
# so every session over that tunnel timed out while everything else looked
# healthy. The peer's own log showed
# `derp-<n> does not know about peer [...], removing route` on every reply.
# Restarting tailscaled here gave this node a new disco key (observed in the
# peer's log), and the coordination server pushed the peer a fresh network map
# with the current relay. That cleared it.
# The check needs ALL of:
#   • the peer is Active (traffic wanted) with a non-zero WireGuard handshake
#     older than NETWD_TS_STALE_SEC (default 300s: WireGuard stops using a
#     session after 180s without a new handshake, and 300s spans at least two
#     2-minute ticks, so one slow tick never triggers it);
#   • a ping through the tunnel (`tailscale ping --tsmp`) gets no reply; and
#   • a discovery ping (`tailscale ping`, no --tsmp) DOES reply — the peer is
#     reachable and only the tunnel is dead. A peer that is simply gone fails
#     both and is left alone; restarting would not bring it back; and
#   • a second tunnel ping, after the discovery ping, still gets no reply (the
#     discovery ping can itself revive a cold path).
# The heal restarts tailscaled (this drops every Tailscale SSH session on the
# box, which is why it is rate-limited to once per NETWD_TS_RATE_LIMIT_SEC,
# default an hour). Each heal, observation or failed restart is recorded as
# `tailscale.last_event` in the /run telemetry file below. The watchdog writes
# nothing outside /run: the Genesis runtime, running as the owning user, reads
# that event and raises the owner alert (genesis.resilience.network_watchdog_events).
# Operator lever NETWD_TS_MODE: live (default) | observe (record, never
# restart) | off. Set it with a drop-in:
# `systemctl edit genesis-network-watchdog.service` →
# `[Service]` + `Environment=NETWD_TS_MODE=observe`.
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

# Tailscale stuck-tunnel check (all overridable; tests point TAILSCALE at a stub).
TAILSCALE="${NETWD_TAILSCALE_BIN:-tailscale}"
TS_MODE="${NETWD_TS_MODE:-live}"                  # live | observe | off
# Every numeric tailscale knob goes through _ts_knob. A drop-in typo must fall
# back to the default, never abort the check under `set -u`, disable a bound,
# or silently turn detection off. Integers only, no leading zero (bash reads
# "08" as bad octal), at most 9 digits: bash arithmetic is signed 64-bit, so a
# longer value can wrap negative or to zero and silently remove the bound it
# sets (a wrapped rate limit restarts every tick). min 1 where 0 would remove a
# safety rail: `timeout 0` never fires, a 0 rate limit restarts every tick, a 0
# stale age suspects every peer. The two scan caps allow 0 (probe nothing).
_ts_knob() {  # <env name> <default> <min: 0|1>
    local v="${!1:-$2}" re='^[1-9][0-9]{0,8}$'
    [[ "$3" == 0 ]] && re='^(0|[1-9][0-9]{0,8})$'
    if [[ "$v" =~ $re ]]; then
        printf '%s' "$v"
    else
        printf 'genesis-network-watchdog: ignoring invalid %s=%q; using %s\n' "$1" "$v" "$2" >&2
        printf '%s' "$2"
    fi
}
TS_STALE_SEC="$(_ts_knob NETWD_TS_STALE_SEC 300 1)"             # handshake age that makes an Active peer a suspect
TS_RATE_LIMIT_SEC="$(_ts_knob NETWD_TS_RATE_LIMIT_SEC 3600 1)"  # min seconds between tailscale heals/alerts
# Passed to `tailscale ping --timeout`, which needs a Go duration with a unit;
# a bare number fails every ping and would read every peer as unreachable.
TS_PING_TIMEOUT="${NETWD_TS_PING_TIMEOUT:-3s}"
if ! [[ "$TS_PING_TIMEOUT" =~ ^[1-9][0-9]{0,5}(ms|s)$ ]]; then
    printf 'genesis-network-watchdog: ignoring invalid NETWD_TS_PING_TIMEOUT=%q; using 3s\n' "$TS_PING_TIMEOUT" >&2
    TS_PING_TIMEOUT=3s
fi
# Outer bounds (seconds) on each tailscale CLI call and on the restart. A ping
# answers in milliseconds or gives up at TS_PING_TIMEOUT, and `status` returns
# in well under a second, so 20s is only reached by a hung local API. The
# restart bound allows systemd's default 90s stop timeout plus the start.
TS_CALL_TIMEOUT="$(_ts_knob NETWD_TS_CALL_TIMEOUT 20 1)"
TS_RESTART_TIMEOUT="$(_ts_knob NETWD_TS_RESTART_TIMEOUT 150 1)"
# Per-tick scan bounds. Normally there are 0 or 1 suspects (an Active peer
# with a stale handshake); 3 covers a shared outage without letting a large
# tailnet hold the oneshot for minutes: worst case 3 peers x 3 calls x 20s,
# and the budget stops starting new probes after 60s.
TS_MAX_PROBES="$(_ts_knob NETWD_TS_MAX_PROBES 3 0)"
TS_SCAN_BUDGET_SEC="$(_ts_knob NETWD_TS_SCAN_BUDGET_SEC 60 0)"
TS_STAMP_FILE="${NETWD_TS_STAMP_FILE:-/run/genesis-network-watchdog-tailscale.last}"
TS_STATUS_SNAPSHOT="${NETWD_TS_STATUS_SNAPSHOT:-/run/genesis-network-watchdog-tailscale-status.json}"

log() { printf 'genesis-network-watchdog: %s\n' "$1"; }

# A rate-limit stamp as an epoch integer. Anything else (empty, garbage,
# half-written) reads as 0 = "no recent heal": a non-numeric value in an
# arithmetic context would otherwise abort the run under `set -u`.
_read_stamp() {
    local v=""
    [[ -f "$1" ]] && v="$(cat "$1" 2>/dev/null)"
    [[ "$v" =~ ^[0-9]+$ ]] && printf '%s' "$v" || printf '0'
}

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
try:
    heal_count = max(0, int(prior.get("heal_count") or 0))
except (TypeError, ValueError):
    heal_count = 0  # a corrupt counter must never block the write
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
# The tailscale check owns its own sub-object (_write_ts_state); carry it.
if isinstance(prior.get("tailscale"), dict):
    state["tailscale"] = prior["tailscale"]
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

_networkd_check() {
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
    local last_heal
    last_heal="$(_read_stamp "$STAMP_FILE")"
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

# ── Tailscale stuck-tunnel check ──────────────────────────────────────────────

# Merge the tailscale sub-object into the state file, leaving the networkd keys
# alone. action ∈ {none, incomplete, suspect-unreachable, healed, observed,
# ratelimited, restart-failed, unverified, unavailable, status-unparseable, off}. `diag` is
# the JSON evidence captured at the moment of a heal/observation (or empty).
# healed / observed / restart-failed also become `last_event`, the record the
# Genesis runtime turns into an owner alert; its `at` is the event's identity.
# A failed write is LOUD, never swallowed: stderr reaches the journal, the
# function returns 1, and an event caller fails the unit, because a lost event
# means the owner is never told.
_write_ts_state() {
    local action="$1" peer="$2" diag="$3" rc="${4:-}"
    if ! NETWD_STATE_FILE="$STATE_FILE" A_NOW="$NETWD_NOW" A_ACTION="$action" \
        A_PEER="$peer" A_DIAG="$diag" A_RC="$rc" A_RATE="$TS_RATE_LIMIT_SEC" \
        python3 - <<'PY'
import json, os
path = os.environ["NETWD_STATE_FILE"]
now = int(os.environ["A_NOW"])
action = os.environ["A_ACTION"]
peer = os.environ["A_PEER"] or None
try:
    diag = json.loads(os.environ["A_DIAG"]) if os.environ["A_DIAG"] else None
except ValueError:
    diag = None
try:
    with open(path) as fh:
        state = json.load(fh)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}
ts = state.get("tailscale") if isinstance(state.get("tailscale"), dict) else {}
try:
    heal_count = max(0, int(ts.get("heal_count") or 0))
except (TypeError, ValueError):
    heal_count = 0  # a corrupt counter must never block recording the event
if action == "healed":
    heal_count += 1
    ts["last_heal"] = now
ts["heal_count"] = heal_count
ts["last_check"] = now
ts["last_action"] = action
if peer:
    ts["last_peer"] = peer
if diag is not None:
    ts["last_diagnostics"] = diag
if action in ("healed", "observed", "restart-failed"):
    ts["last_event"] = {
        "action": action,
        "at": now,
        "peer": peer,
        "handshake_age_s": (diag or {}).get("handshake_age_s"),
        "rc": int(os.environ["A_RC"]) if os.environ["A_RC"].isdigit() else None,
        "rate_limit_s": int(os.environ["A_RATE"]),
    }
state["tailscale"] = ts
tmp = f"{path}.tmp"
with open(tmp, "w") as fh:
    json.dump(state, fh)
os.replace(tmp, path)
try:
    os.chmod(path, 0o644)
except OSError:
    pass
PY
    then
        log "tailscale: could not record '$action' in $STATE_FILE — the owner will NOT be told of it"
        return 1
    fi
}

# Suspect peers: Active, non-zero handshake older than TS_STALE_SEC. One TSV
# line per suspect: ip, hostname, handshake-age-seconds. Reads `status --json`
# from stdin; the snapshot is also kept (root-only) as heal evidence.
_ts_suspects() {
    # Exit 2 = the status could not be read as a peer map. The caller records
    # that as its own state, never as "no suspects" (which reads as healthy).
    A_NOW="$NETWD_NOW" A_STALE="$TS_STALE_SEC" python3 -c '
import ipaddress, json, os, re, sys
from datetime import datetime
now = int(os.environ["A_NOW"])
stale = int(os.environ["A_STALE"])
try:
    status = json.load(sys.stdin)
    if not isinstance(status, dict):
        raise ValueError("status is not an object")
    # null = no peers (a legitimate empty tailnet); any other non-map shape is
    # unreadable. `or {}` would wrongly turn [] into "no peers" = healthy.
    peers = status.get("Peer")
    peers = {} if peers is None else peers
    if not isinstance(peers, dict):
        raise ValueError("Peer is not a map")
except Exception:
    sys.exit(2)
for peer in peers.values():
    if not isinstance(peer, dict) or peer.get("Active") is not True:
        continue
    raw = str(peer.get("LastHandshake") or "")
    # The CLI prints nanoseconds; Python before 3.11 only parses up to
    # microseconds, so trim the fraction to 6 digits before parsing.
    raw = re.sub(r"(\.\d{6})\d+", r"\1", raw.replace("Z", "+00:00"))
    try:
        hs = datetime.fromisoformat(raw).timestamp()
    except ValueError:
        continue
    if hs <= 0:  # 0001-01-01: never handshaked — nothing to be stuck
        continue
    age = int(now - hs)
    # Everything here except the timestamp is chosen by OTHER tailnet members,
    # and it reaches argv, log lines and an owner alert rendered as Telegram
    # HTML. So each field is reduced to what it means: the target must parse
    # as an IPv4 address, and the name keeps only hostname characters (which
    # also removes the tabs and newlines that delimit these records).
    ips = []
    for raw_ip in peer.get("TailscaleIPs") or []:
        try:
            ips.append(str(ipaddress.IPv4Address(str(raw_ip))))
        except ValueError:
            continue
    if age > stale and ips:
        name = re.sub(r"[^A-Za-z0-9._-]", "", str(peer.get("HostName") or "")) or "?"
        print(f"{ips[0]}\t{name}\t{age}")
' 2>/dev/null
}

# Evidence for one stuck peer, read from THIS tick's `status --json` on stdin
# (never the snapshot file, which may be an older incident's if this tick's
# write failed): both relay homes, the direct address, handshake, byte
# counters — the fields that told us what happened on 2026-09-26, captured at
# the moment rather than after.
_ts_diag() {
    local ip="$1" age="$2"
    A_IP="$ip" A_AGE="$age" python3 -c '
import json, os, re, sys
try:
    status = json.load(sys.stdin)
    if not isinstance(status, dict):
        status = {}
except Exception:
    status = {}
self_node = status.get("Self")
self_node = self_node if isinstance(self_node, dict) else {}
out = {
    "peer_ip": os.environ["A_IP"],
    "handshake_age_s": int(os.environ["A_AGE"]),
    "tsmp_ping": "no-reply",
    "disco_ping": "reply",
    "self_relay": self_node.get("Relay"),
    "backend_state": status.get("BackendState"),
}
peers = status.get("Peer")
for peer in (peers.values() if isinstance(peers, dict) else []):
    if not isinstance(peer, dict):
        continue
    ips = peer.get("TailscaleIPs")
    if isinstance(ips, list) and os.environ["A_IP"] in ips:
        # HostName is chosen by the peer and this file feeds INFRASTRUCTURE.md
        # and an LLM prompt: keep only hostname characters, as _ts_suspects does.
        out["peer_hostname"] = re.sub(r"[^A-Za-z0-9._-]", "", str(peer.get("HostName") or ""))
        for key in ("Relay", "CurAddr", "LastHandshake", "RxBytes", "TxBytes", "Online"):
            out["peer_" + key.lower()] = peer.get(key)
        break
print(json.dumps(out))
' 2>/dev/null
}

# When the tailscale rate limit was last spent: the LATER of tailscaled's own
# ActiveEnterTimestamp (systemd writes it as part of every start, so it cannot
# fail to record a restart that happened, and it also counts reboots and an
# operator's manual restart) and our stamp. The stamp is best-effort and only
# matters where systemd's time did not move: a restart that failed while the
# unit stayed up, and observe mode, which never restarts. A repeated restart
# therefore needs the stamp write AND the restart to fail together.
_ts_last_spent() {
    local ts started=0 stamp
    ts="$("$SYSTEMCTL" show tailscaled -p ActiveEnterTimestamp --value 2>/dev/null)"
    if [[ -n "$ts" ]]; then
        started="$(date -d "$ts" +%s 2>/dev/null || echo 0)"
        [[ "$started" =~ ^[0-9]+$ ]] || started=0
    fi
    stamp="$(_read_stamp "$TS_STAMP_FILE")"
    (( stamp > started )) && echo "$stamp" || echo "$started"
}

_tailscale_check() {
    case "$TS_MODE" in
        off) _write_ts_state "off" "" ""; return 0 ;;
        live|observe) ;;
        *) log "tailscale: unknown NETWD_TS_MODE='$TS_MODE' — treating as observe"
           TS_MODE="observe" ;;
    esac
    if ! command -v "$TAILSCALE" >/dev/null 2>&1 \
        || [[ "$("$SYSTEMCTL" is-active tailscaled 2>/dev/null || true)" != "active" ]]; then
        _write_ts_state "unavailable" "" ""
        return 0
    fi

    # Every tailscale call is bounded: the unit is a oneshot with no start
    # timeout, so one hung call to a misbehaving daemon (the situation this
    # check exists for) would wedge the unit and stop the timer, taking the
    # networkd check down with it.
    local status
    status="$(timeout "$TS_CALL_TIMEOUT" "$TAILSCALE" status --json 2>/dev/null)" || status=""
    if [[ -z "$status" ]]; then
        _write_ts_state "unavailable" "" ""
        return 0
    fi

    local suspects src=0
    suspects="$(printf '%s' "$status" | _ts_suspects)" || src=$?
    if (( src != 0 )); then
        log "tailscale: status --json could not be read as a peer map — cannot judge tunnels this tick"
        _write_ts_state "status-unparseable" "" ""
        return 0
    fi

    # Bounded scan: each suspect can cost three TS_CALL_TIMEOUTs, and the unit
    # is a oneshot sharing its timer with the networkd heal. A large tailnet
    # with many stale peers (or a hung local API) must not hold it for
    # minutes. Stop after TS_MAX_PROBES peers or TS_SCAN_BUDGET_SEC seconds,
    # and say so. The status lists peers in a FIXED order, so the start point
    # rotates each tick: otherwise the same first few (e.g. peers that went
    # offline) would be probed forever and a stuck peer behind them never.
    local -a rows=()
    local line
    while IFS= read -r line; do
        local r_ip r_age
        IFS=$'\t' read -r r_ip _ r_age <<<"$line"
        [[ -n "$r_ip" && "$r_age" =~ ^[0-9]+$ ]] && rows+=("$line")
    done <<<"$suspects"
    local n=${#rows[@]} offset=0
    (( n > 0 )) && offset=$(( (NETWD_NOW / 120) % n ))

    local ip name age stuck_ip="" stuck_name="" stuck_age="" saw_unreachable=""
    local probed=0 skipped=0 scan_start=$SECONDS k
    for (( k = 0; k < n; k++ )); do
        IFS=$'\t' read -r ip name age <<<"${rows[$(( (offset + k) % n ))]}"
        if (( probed >= TS_MAX_PROBES || SECONDS - scan_start >= TS_SCAN_BUDGET_SEC )); then
            skipped=$((skipped + 1))
            continue
        fi
        probed=$((probed + 1))
        # The tunnel answers → merely idle-ish, not stuck.
        if timeout "$TS_CALL_TIMEOUT" "$TAILSCALE" ping --tsmp -c 1 --timeout "$TS_PING_TIMEOUT" -- "$ip" >/dev/null 2>&1; then
            continue
        fi
        # Tunnel dead AND the peer itself unreachable → it went away (powered off,
        # offline). A restart here cannot help, so it is not our fault to heal.
        # --until-direct=false: a pong over a relay counts. The default (true)
        # exits 1 on a relay-only pong, which would call every peer with no
        # direct path "unreachable" and never heal it.
        if ! timeout "$TS_CALL_TIMEOUT" "$TAILSCALE" ping --until-direct=false -c 1 --timeout "$TS_PING_TIMEOUT" -- "$ip" >/dev/null 2>&1; then
            saw_unreachable=1
            continue
        fi
        # The discovery ping can itself repair a cold or stale path (it
        # refreshes endpoints), so ask the tunnel once more before calling it
        # stuck: a restart drops every session, and must not be spent on a
        # tunnel the probe just revived.
        if timeout "$TS_CALL_TIMEOUT" "$TAILSCALE" ping --tsmp -c 1 --timeout "$TS_PING_TIMEOUT" -- "$ip" >/dev/null 2>&1; then
            continue
        fi
        stuck_ip="$ip"; stuck_name="$name"; stuck_age="$age"
        break
    done
    if (( skipped > 0 )); then
        log "tailscale: probed $probed suspect peer(s); $skipped more not probed this tick (cap ${TS_MAX_PROBES} peers / ${TS_SCAN_BUDGET_SEC}s)"
    fi

    if [[ -z "$stuck_ip" ]]; then
        # A suspect the cap left unprobed could be the stuck one, so a capped
        # scan never reads as healthy.
        if (( skipped > 0 )); then
            _write_ts_state "incomplete" "" ""
        elif [[ -n "$saw_unreachable" ]]; then
            _write_ts_state "suspect-unreachable" "" ""
        else
            _write_ts_state "none" "" ""
        fi
        return 0
    fi

    # Evidence first, before anything changes: the full status snapshot
    # (root-only; it names every node on the tailnet) plus a summary for the
    # world-readable telemetry.
    # Written to a fresh 0600 file (mktemp) and renamed over the target:
    # truncating an existing file in place would keep whatever mode it had.
    local snap_tmp
    if snap_tmp="$(mktemp "$TS_STATUS_SNAPSHOT.XXXXXX" 2>/dev/null)"; then
        if ! { printf '%s' "$status" >"$snap_tmp" && mv -fT "$snap_tmp" "$TS_STATUS_SNAPSHOT"; } 2>/dev/null; then
            unlink "$snap_tmp" 2>/dev/null || true
        fi
    fi
    local diag; diag="$(printf '%s' "$status" | _ts_diag "$stuck_ip" "$stuck_age")"
    local peer="$stuck_name ($stuck_ip)"

    local last
    last="$(_ts_last_spent)"
    if (( last > 0 && NETWD_NOW - last < TS_RATE_LIMIT_SEC )); then
        log "tailscale: tunnel to $peer stuck (handshake ${stuck_age}s old) but tailscaled started, or a heal/alert fired, <${TS_RATE_LIMIT_SEC}s ago — NOT acting"
        _write_ts_state "ratelimited" "$peer" "$diag"
        return 0
    fi

    # Best-effort stamp BEFORE acting (see _ts_last_spent for why a failed
    # write is tolerable: systemd's start time bounds every restart that
    # actually happened).
    echo "$NETWD_NOW" >"$TS_STAMP_FILE" 2>/dev/null || true

    if [[ "$TS_MODE" == "observe" ]]; then
        log "tailscale: OBSERVE — tunnel to $peer stuck (handshake ${stuck_age}s old); not restarting"
        _write_ts_state "observed" "$peer" "$diag" || return 1
        return 0
    fi

    log "tailscale: HEALING — tunnel to $peer stuck (handshake ${stuck_age}s old); restarting tailscaled"
    # The stamp was written above, BEFORE the attempt: a restart that fails
    # has still dropped every session, so it spends the hour as a good one.
    # try-restart, not restart: `restart` STARTS a stopped unit, and an
    # operator may have stopped tailscaled while the scan ran. try-restart acts
    # only on a running unit and exits 0 either way, so the result is read back:
    # a new InvocationID means a restart really happened.
    local inv_before inv_after
    inv_before="$("$SYSTEMCTL" show tailscaled -p InvocationID --value 2>/dev/null)"
    if timeout "$TS_RESTART_TIMEOUT" "$SYSTEMCTL" try-restart tailscaled; then
        inv_after="$("$SYSTEMCTL" show tailscaled -p InvocationID --value 2>/dev/null)"
        if [[ -z "$inv_before" || -z "$inv_after" ]]; then
            log "tailscale: try-restart ran but its InvocationID could not be read — outcome unverified, not recorded as a heal"
            _write_ts_state "unverified" "$peer" "$diag"
            return 0
        fi
        if [[ "$inv_after" == "$inv_before" ]]; then
            # Nothing restarted: it was stopped when try-restart ran. Whether or
            # not something else has started it since, this run healed nothing.
            log "tailscale: tailscaled was stopped during the scan — not restarted"
            _write_ts_state "unavailable" "" ""
            return 0
        fi
        if [[ "$("$SYSTEMCTL" is-active tailscaled 2>/dev/null || true)" == "active" ]]; then
            _write_ts_state "healed" "$peer" "$diag" || return 1
            return 0
        fi
        # It restarted (every session dropped) and is not running now.
        log "tailscale: restarted tailscaled but it is not active — tailscaled may be DOWN; not retrying"
        _write_ts_state "restart-failed" "$peer" "$diag"
        return 1
    else
        local rc=$?
        # Not retried: the next ticks see tailscaled down and, by design, never
        # start a stopped daemon. The recorded event tells the owner now.
        log "tailscale: restart FAILED (rc=$rc) — tailscaled may be DOWN; not retrying"
        _write_ts_state "restart-failed" "$peer" "$diag" "$rc"
        return "$rc"
    fi
}

main() {
    # The two checks are independent: a networkd problem must not stop the
    # tailscale check from running, and vice versa. The unit's exit status is
    # the first failure.
    local rc=0 ts_rc=0
    _networkd_check || rc=$?
    _tailscale_check || ts_rc=$?
    (( rc != 0 )) && return "$rc"
    return "$ts_rc"
}

main "$@"
