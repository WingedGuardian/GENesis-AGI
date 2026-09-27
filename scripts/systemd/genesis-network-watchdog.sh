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
# healthy. The laptop-side log showed
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
#     both and is left alone; restarting would not bring it back.
# The heal restarts tailscaled (this drops every Tailscale SSH session on the
# box, which is why it is rate-limited to once per NETWD_TS_RATE_LIMIT_SEC,
# default an hour). Each heal records what it saw and queues an owner alert.
# Operator lever NETWD_TS_MODE: live (default) | observe (record + alert, never
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
TS_STALE_SEC="${NETWD_TS_STALE_SEC:-300}"         # handshake age that makes an Active peer a suspect
TS_RATE_LIMIT_SEC="${NETWD_TS_RATE_LIMIT_SEC:-3600}"  # min seconds between tailscale heals/alerts
TS_PING_TIMEOUT="${NETWD_TS_PING_TIMEOUT:-3s}"
# Outer bounds (seconds) on each tailscale CLI call and on the restart. A ping
# answers in milliseconds or gives up at TS_PING_TIMEOUT, and `status` returns
# in well under a second, so 20s is only reached by a hung local API. The
# restart bound allows systemd's default 90s stop timeout plus the start.
TS_CALL_TIMEOUT="${NETWD_TS_CALL_TIMEOUT:-20}"
TS_RESTART_TIMEOUT="${NETWD_TS_RESTART_TIMEOUT:-150}"
TS_STAMP_FILE="${NETWD_TS_STAMP_FILE:-/run/genesis-network-watchdog-tailscale.last}"
TS_STATUS_SNAPSHOT="${NETWD_TS_STATUS_SNAPSHOT:-/run/genesis-network-watchdog-tailscale-status.json}"
# The owning user's alert queue (~/.genesis/alerts/queue), written into the
# service unit by network_resilience.sh. Unset → no owner alert (journal only).
ALERT_QUEUE="${NETWD_ALERT_QUEUE:-}"

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
# alone. action ∈ {none, suspect-unreachable, healed, observed, ratelimited,
# restart-failed, unavailable, status-unparseable, off}. `diag` is the JSON
# evidence captured at the moment of a heal/observation (or empty).
_write_ts_state() {
    local action="$1" peer="$2" diag="$3"
    NETWD_STATE_FILE="$STATE_FILE" A_NOW="$NETWD_NOW" A_ACTION="$action" \
        A_PEER="$peer" A_DIAG="$diag" python3 - <<'PY' 2>/dev/null || true
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
heal_count = int(ts.get("heal_count", 0) or 0)
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

# Evidence for one stuck peer, pulled from the saved status snapshot: both
# relay homes, the direct address, handshake, byte counters — the fields that
# told us what happened on 2026-09-26, captured at the moment rather than after.
_ts_diag() {
    local ip="$1" age="$2"
    A_SNAP="$TS_STATUS_SNAPSHOT" A_IP="$ip" A_AGE="$age" python3 -c '
import json, os
try:
    with open(os.environ["A_SNAP"]) as fh:
        status = json.load(fh)
except Exception:
    status = {}
self_node = status.get("Self") or {}
out = {
    "peer_ip": os.environ["A_IP"],
    "handshake_age_s": int(os.environ["A_AGE"]),
    "tsmp_ping": "no-reply",
    "disco_ping": "reply",
    "self_relay": self_node.get("Relay"),
    "backend_state": status.get("BackendState"),
}
for peer in (status.get("Peer") or {}).values():
    if os.environ["A_IP"] in (peer.get("TailscaleIPs") or []):
        for key in ("HostName", "Relay", "CurAddr", "LastHandshake",
                    "RxBytes", "TxBytes", "Online"):
            out["peer_" + key.lower()] = peer.get(key)
        break
print(json.dumps(out))
' 2>/dev/null
}

# Queue an owner alert into the Genesis user's durable alert queue (schema v1,
# genesis.guardian.alert.queue — the awareness tick drains it to Telegram).
# Runs as root, so the entry is chowned to the queue directory's owner BEFORE it
# is renamed into place: the drainer (that user) must be able to read and
# unlink it. A missing or root-owned queue dir means no alert, never a root-
# owned file in someone's home.
_ts_alert() {
    local title="$1" body="$2"
    [[ -n "$ALERT_QUEUE" && -d "$ALERT_QUEUE" ]] || {
        log "tailscale: no alert queue configured/present — alert is journal-only"
        return 0
    }
    A_ROOT="$ALERT_QUEUE" A_NOW="$NETWD_NOW" A_TITLE="$title" A_BODY="$body" python3 - <<'PY' 2>/dev/null || log "tailscale: alert enqueue failed"
import json, os, uuid
root = os.environ["A_ROOT"]
# The queue must be a real directory (not a symlink to one) owned by a user.
st = os.lstat(root)
import stat as _stat
if not _stat.S_ISDIR(st.st_mode) or st.st_uid == 0:
    raise SystemExit(1)  # not a user's queue; refuse to write into it
ts = float(os.environ["A_NOW"])
entry = {
    "schema": 1,
    "ts": ts,
    "severity": "warning",
    "source": "network-watchdog",
    "title": os.environ["A_TITLE"],
    "body": os.environ["A_BODY"],
    # Per-event identity: two heals an hour apart are two alerts, not a repeat.
    "dedupe_key": "network-watchdog:tailscale:%d" % int(ts),
    "meta": {},
}
# Root writing into a user-owned directory: create exclusively, never follow
# a symlink, and chown the open fd rather than a path that could be swapped.
tmp = os.path.join(root, ".%s.tmp" % uuid.uuid4().hex)
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    os.write(fd, json.dumps(entry, ensure_ascii=False).encode("utf-8"))
    os.fchown(fd, st.st_uid, st.st_gid)
finally:
    os.close(fd)
os.replace(tmp, os.path.join(root, "%.6f-%s.json" % (ts, uuid.uuid4().hex)))
PY
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

    local ip name age stuck_ip="" stuck_name="" stuck_age="" saw_unreachable=""
    while IFS=$'\t' read -r ip name age; do
        [[ -n "$ip" && "$age" =~ ^[0-9]+$ ]] || continue
        # The tunnel answers → merely idle-ish, not stuck.
        if timeout "$TS_CALL_TIMEOUT" "$TAILSCALE" ping --tsmp -c 1 --timeout "$TS_PING_TIMEOUT" -- "$ip" >/dev/null 2>&1; then
            continue
        fi
        # Tunnel dead AND the peer itself unreachable → it went away (lid shut,
        # offline). A restart here cannot help, so it is not our fault to heal.
        # --until-direct=false: a pong over a relay counts. The default (true)
        # exits 1 on a relay-only pong, which would call every peer with no
        # direct path "unreachable" and never heal it.
        if ! timeout "$TS_CALL_TIMEOUT" "$TAILSCALE" ping --until-direct=false -c 1 --timeout "$TS_PING_TIMEOUT" -- "$ip" >/dev/null 2>&1; then
            saw_unreachable=1
            continue
        fi
        stuck_ip="$ip"; stuck_name="$name"; stuck_age="$age"
        break
    done <<<"$suspects"

    if [[ -z "$stuck_ip" ]]; then
        if [[ -n "$saw_unreachable" ]]; then
            _write_ts_state "suspect-unreachable" "" ""
        else
            _write_ts_state "none" "" ""
        fi
        return 0
    fi

    # Evidence first, before anything changes: the full status snapshot
    # (root-only; it names every node on the tailnet) plus a summary for the
    # world-readable telemetry.
    ( umask 077; printf '%s' "$status" >"$TS_STATUS_SNAPSHOT" ) 2>/dev/null || true
    local diag; diag="$(_ts_diag "$stuck_ip" "$stuck_age")"
    local peer="$stuck_name ($stuck_ip)"

    local last
    last="$(_read_stamp "$TS_STAMP_FILE")"
    if (( last > 0 && NETWD_NOW - last < TS_RATE_LIMIT_SEC )); then
        log "tailscale: tunnel to $peer stuck (handshake ${stuck_age}s old) but a heal/alert fired <${TS_RATE_LIMIT_SEC}s ago — NOT acting"
        _write_ts_state "ratelimited" "$peer" "$diag"
        return 0
    fi

    local detail="No WireGuard handshake with $peer for ${stuck_age}s while traffic is wanted; the tunnel ping got no reply but the peer answers discovery pings, so the path is fine and the tunnel is stuck. Evidence: $TS_STATUS_SNAPSHOT (and 'tailscale' in $STATE_FILE)."
    if [[ "$TS_MODE" == "observe" ]]; then
        log "tailscale: OBSERVE — tunnel to $peer stuck (handshake ${stuck_age}s old); not restarting"
        echo "$NETWD_NOW" >"$TS_STAMP_FILE" 2>/dev/null || true
        _write_ts_state "observed" "$peer" "$diag"
        _ts_alert "Tailscale tunnel to $stuck_name is stuck (observe mode — not restarted)" \
            "$detail Restart it with: sudo systemctl restart tailscaled"
        return 0
    fi

    log "tailscale: HEALING — tunnel to $peer stuck (handshake ${stuck_age}s old); restarting tailscaled"
    # Arm the rate limit BEFORE the attempt: a restart that fails has still
    # dropped every session, so it spends the hour exactly as a good one does.
    echo "$NETWD_NOW" >"$TS_STAMP_FILE" 2>/dev/null || true
    if timeout "$TS_RESTART_TIMEOUT" "$SYSTEMCTL" restart tailscaled; then
        _write_ts_state "healed" "$peer" "$diag"
        _ts_alert "Tailscale tunnel to $stuck_name was stuck — restarted tailscaled" \
            "$detail Restarting tailscaled dropped the Tailscale SSH sessions on this machine; tmux sessions survive, so reconnect. Next automatic restart is allowed after $((TS_RATE_LIMIT_SEC / 60)) min."
        return 0
    else
        local rc=$?
        # Not retried: the next ticks see tailscaled down and, by design, never
        # start a stopped daemon. So the owner must hear about it now.
        log "tailscale: restart FAILED (rc=$rc) — tailscaled may be DOWN; not retrying, alert queued"
        _write_ts_state "restart-failed" "$peer" "$diag"
        _ts_alert "Tailscale restart FAILED — tailscaled may be down" \
            "$detail The watchdog tried to restart tailscaled and the restart failed (rc=$rc), so Tailscale may now be down entirely. It will not retry. Check: sudo systemctl status tailscaled; fix with: sudo systemctl restart tailscaled"
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
