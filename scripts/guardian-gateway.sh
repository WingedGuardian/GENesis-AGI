#!/usr/bin/env bash
# Guardian gateway — SSH command dispatcher.
#
# Restricts container->host SSH to exactly these operations:
#   restart-timer  — restart the guardian timer
#   pause          — pause guardian checks
#   resume         — resume guardian checks
#   status         — get current guardian state
#   reset-state    — reset stuck state machine to healthy
#   version        — report CC, code, and Node versions
#   update         — pull latest code + self-update gateway script
#   sync-gateway    — redeploy gateway script from install dir (no pull; recovery)
#   redeploy <hash> [sha256] — receive tar archive on stdin, deploy to install
#                     dir; when the optional sha256 of the tar stream is given
#                     it is verified before the running guardian is disturbed
#   update-cc <ver> — install a pinned Claude Code version (validated semver)
#   update-node <N> — install a pinned Node.js major via NodeSource (validated)
#   test-approval   — E2E test the keyword-reply approval gate (no recovery)
#   disk-status     — read-only storage-pool + snapshot JSON (Genesis's window
#                     into host capacity: lvs data%/metadata%, VG free, snapshots)
#   host-profile    — read-only host body-schema JSON (meminfo/nproc/kernel,
#                     storage pool, incus version + container limits.*) for the
#                     container's infra_profile host plane
#   bundle-status   — read-only offline repo-bundle archive JSON (host-only
#                     archived `git bundle` copies + newest stamp; F.4 lifeline)
#   provision-status          — read-only Proxmox host capacity (audit token)
#   provision-grow-disk <disk> <GiB> — EXECUTE a pre-approved VM disk grow +
#                     absorb (execute-only: NO Telegram gate; caller approves)
#   provision-grow-memory <MiB>      — EXECUTE a pre-approved VM memory grow
#   storage-expand            — absorb an already-grown disk into the storage
#                     pool (LVM-thin: pvresize; btrfs-on-LVM: + lvextend/resize)
#   configure-provisioning <k=v ...> — land host provisioning config as a
#                     state-dir override (survives redeploys; no secrets)
#   reharden-key    — rewrite the guardian authorized_keys line to the canonical
#                     hardened options with a self-proving from= (dead-man's-
#                     switch restores the previous file unless a fresh
#                     connection confirms the rewrite works)
#   ping            — liveness check
#
# SSH authorized_keys entry (replace CONTAINER_IP with the container's
# host-facing source IP — install_guardian.sh derives and installs this):
#   from="CONTAINER_IP",command="~/.local/bin/guardian-gateway.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 ...
#
# This gives Genesis a fixed allowlist of operations on the host. Nothing else.

set -euo pipefail

STATE_DIR="${HOME}/.local/state/genesis-guardian"

case "${SSH_ORIGINAL_COMMAND:-}" in
    restart-timer)
        systemctl --user restart genesis-guardian.timer
        echo '{"ok": true, "action": "restart-timer"}'
        ;;
    pause)
        mkdir -p "$STATE_DIR"
        printf '{"paused": true, "since": "%s"}' "$(date -Is)" > "$STATE_DIR/paused.json"
        echo '{"ok": true, "action": "pause"}'
        ;;
    resume)
        rm -f "$STATE_DIR/paused.json"
        echo '{"ok": true, "action": "resume"}'
        ;;
    status)
        cat "$STATE_DIR/state.json" 2>/dev/null || echo '{"current_state": "unknown"}'
        ;;
    reset-state)
        # Reset Guardian state to HEALTHY when stuck in confirmed_dead/recovering/recovered.
        # Safety: refuses to reset from active investigation states (healthy, confirming, surveying).
        STATE_FILE="$STATE_DIR/state.json"
        if [ ! -f "$STATE_FILE" ]; then
            echo '{"ok": false, "error": "no state file"}' >&2
            exit 1
        fi
        CURRENT=$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('current_state','unknown'))" 2>/dev/null || echo "unknown")
        case "$CURRENT" in
            confirmed_dead|recovering|recovered)
                # Read-modify-write with atomic rename (matches state_machine.py save_state)
                python3 << PYEOF
import json, os, tempfile
from datetime import datetime, timezone
sf = "$STATE_FILE"
with open(sf) as f:
    d = json.load(f)
prev = d.get("current_state", "unknown")
now = datetime.now(timezone.utc).isoformat()
d.update(current_state="healthy", consecutive_failures=0, recheck_count=0,
         first_failure_at=None, recovery_attempts=0, last_healthy_at=now,
         last_check_at=now, auto_reset_count=0, dialogue_sent_at=None,
         dialogue_eta_s=0, dialogue_action=None, cc_unavailable_since=None,
         last_cc_unavailable_alert_at=None, io_triage_attempts=0,
         sentinel_state="")
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(sf), suffix=".tmp")
try:
    os.write(fd, json.dumps(d, indent=2).encode())
    os.fsync(fd)
finally:
    os.close(fd)
os.rename(tmp, sf)
print(json.dumps({"ok": True, "action": "reset-state", "previous_state": prev}))
PYEOF
                ;;
            *)
                printf '{"ok": false, "error": "state is %s, not stuck"}\n' "$CURRENT" >&2
                exit 1
                ;;
        esac
        ;;
    version)
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        CC_VER=$(claude --version 2>/dev/null || echo "unavailable")
        NODE_VER=$(node --version 2>/dev/null || echo "unavailable")
        CODE_VER=$(cd "$INSTALL_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
        CODE_DATE=$(cd "$INSTALL_DIR" && git log -1 --format=%ci 2>/dev/null || echo "unknown")
        # Read deployed_commit AND the F.0 tree_sha256 in ONE python3 spawn —
        # `version` runs every 5-min awareness tick, so keep spawns off the hot
        # path (host OOM history). Line 1 = commit, line 2 = tree sha ("" legacy).
        _DEPLOY_INFO=$(python3 -c "import json; d=json.load(open('$STATE_DIR/deploy_state.json')); print(d.get('deployed_commit','unknown')); print(d.get('tree_sha256',''))" 2>/dev/null || printf 'unknown\n\n')
        DEPLOYED=$(printf '%s\n' "$_DEPLOY_INFO" | sed -n '1p')
        DEPLOYED_TREE_SHA=$(printf '%s\n' "$_DEPLOY_INFO" | sed -n '2p')
        [ -n "$DEPLOYED" ] || DEPLOYED="unknown"
        # sha256 of the DEPLOYED gateway script — lets the container detect a
        # stale/frozen gateway whose self-update silently failed (the install-dir
        # code can be current while ~/.local/bin/guardian-gateway.sh lags).
        GW_SHA=$(sha256sum "$HOME/.local/bin/guardian-gateway.sh" 2>/dev/null | cut -d' ' -f1 || echo "unknown")
        [ -n "$GW_SHA" ] || GW_SHA="unknown"
        # --- authorized_keys hardening indicators (consumed by the container's
        # _check_authkey_hardening reconciler; healed via `reharden-key`).
        # Least disclosure: booleans + sha256 hashes only — never the raw key
        # blob, the stored from= literal, or the observed source address.
        AK_NO_PTY=false; AK_HAS_FROM=false; AK_FROM_MATCHES=false
        AK_OPTS_HASH=""; AK_SRC_HASH=""
        AK_SRC=$(printf '%s' "${SSH_CONNECTION:-}" | awk '{print $1}')
        if [ -n "$AK_SRC" ]; then
            AK_SRC_HASH=$(printf '%s' "$AK_SRC" | sha256sum | cut -d' ' -f1)
        fi
        AK_LINE=$(grep -F "genesis-guardian-control" "$HOME/.ssh/authorized_keys" 2>/dev/null | head -1 || true)
        if [ -n "$AK_LINE" ]; then
            # Options = the line minus the keytype/blob/comment tail. The
            # keytype anchor is robust against options containing spaces.
            AK_KEYPART=$(printf '%s\n' "$AK_LINE" | grep -oE '(ssh|ecdsa|sk)-[A-Za-z0-9@.-]+ [A-Za-z0-9+/=]+( .*)?$' || true)
            AK_OPTS="${AK_LINE%"$AK_KEYPART"}"
            AK_OPTS="${AK_OPTS% }"
            case ",$AK_OPTS," in *,no-pty,*) AK_NO_PTY=true;; esac
            if [ -n "$AK_OPTS" ]; then
                AK_OPTS_HASH=$(printf '%s' "$AK_OPTS" | tr ',' '\n' | sort | sha256sum | cut -d' ' -f1)
            fi
            AK_FROM=$(printf '%s\n' "$AK_OPTS" | grep -oE 'from="[^"]*"' | head -1 | sed 's/^from="//;s/"$//' || true)
            if [ -n "$AK_FROM" ]; then
                AK_HAS_FROM=true
                if [ -n "$AK_SRC" ]; then
                    # sshd-style match: comma-separated fnmatch patterns.
                    # Negated (!) patterns are skipped — never report an
                    # operator's manual negation as a mismatch to "fix".
                    IFS=',' read -ra _AK_PATS <<< "$AK_FROM"
                    for _pat in "${_AK_PATS[@]}"; do
                        case "$_pat" in "!"*) continue;; esac
                        # shellcheck disable=SC2254  # glob match is the point (sshd fnmatch semantics)
                        case "$AK_SRC" in $_pat) AK_FROM_MATCHES=true; break;; esac
                    done
                fi
            fi
        fi
        # --- CC recovery-brain auth-health indicators (consumed by the
        # container's _check_cc_auth reconciler). Least disclosure: emit ONLY
        # loggedIn (tri-state), token presence, and age — NEVER token material,
        # and NEVER the account email/orgId that `claude auth status` also prints.
        # cc_logged_in is a JSON literal true/false/null (null = auth status
        # unavailable/unparseable, so an old CC or a transient failure never
        # false-alarms the reconciler).
        #
        # The loggedIn probe spawns `claude` (~1s, ~290MB transient RSS, no MCP),
        # and `version` runs on EVERY 5-min awareness tick — so cache it host-side
        # with a 1h TTL to keep that spawn off the hot path (host OOM history).
        # loggedIn here is an early-warning SIGNAL, not an incident-time decision
        # (the diagnosis path probes fresh, uncached), so ≤1h staleness is fine.
        # Token presence/age are cheap stats → per-tick, uncached.
        CC_NOW_EPOCH=$(date +%s)
        CC_LOGGED_IN=null
        CC_PROBE_CACHE="$STATE_DIR/cc_auth_probe.json"
        CC_PROBE_TTL=3600
        CC_PROBE_FRESH=false
        if [ -f "$CC_PROBE_CACHE" ]; then
            CC_CACHED=$(python3 -c '
import sys, json
try:
    d = json.load(open(sys.argv[1]))
    ts = int(d.get("checked_at", 0)); li = d.get("logged_in")
    now = int(sys.argv[2]); ttl = int(sys.argv[3])
    if 0 <= now - ts <= ttl and li in ("true", "false", "null"):
        print(li)
except Exception:
    pass
' "$CC_PROBE_CACHE" "$CC_NOW_EPOCH" "$CC_PROBE_TTL" 2>/dev/null || true)
            if [ -n "$CC_CACHED" ]; then CC_LOGGED_IN="$CC_CACHED"; CC_PROBE_FRESH=true; fi
        fi
        if [ "$CC_PROBE_FRESH" = false ]; then
            # timeout 15: node startup + a network round-trip, bounded so the
            # version verb can't hang the awareness tick on a wedged claude.
            CC_AUTH_JSON=$(timeout 15 claude auth status --json 2>/dev/null || true)
            if [ -n "$CC_AUTH_JSON" ]; then
                CC_LOGGED_IN=$(printf '%s' "$CC_AUTH_JSON" | python3 -c '
import sys, json
try:
    v = json.load(sys.stdin).get("loggedIn")
    print("true" if v is True else "false" if v is False else "null")
except Exception:
    print("null")
' 2>/dev/null || echo null)
            fi
            [ -n "$CC_LOGGED_IN" ] || CC_LOGGED_IN=null
            # Cache best-effort; a write failure just means we re-probe next tick.
            # We DELIBERATELY cache `null` too: the cache exists to bound the
            # ~290MB claude spawn on this OOM-history host, and a broken/hung
            # claude returns null every time — caching it keeps that at 1/hr
            # instead of 1/tick. The cost is ≤1h latency to notice a recovered
            # login (an early-warning signal only; the diagnosis path always
            # probes fresh at incident time), which is an acceptable trade.
            mkdir -p "$STATE_DIR" 2>/dev/null || true
            if printf '{"logged_in": "%s", "checked_at": %s}\n' "$CC_LOGGED_IN" "$CC_NOW_EPOCH" \
                    > "$CC_PROBE_CACHE.tmp" 2>/dev/null; then
                mv "$CC_PROBE_CACHE.tmp" "$CC_PROBE_CACHE" 2>/dev/null || true
            fi
        fi
        # Synced setup-token fallback: presence + age (from the shared mount the
        # credential-bridge writes to). Age drives a pre-expiry warning; -1 =
        # unknown. Never read or emit the token value itself.
        CC_TOKEN_FILE="$STATE_DIR/shared/guardian/cc_oauth_token.env"
        CC_TOKEN_PRESENT=false
        CC_TOKEN_AGE_DAYS=-1
        if [ -f "$CC_TOKEN_FILE" ]; then
            if grep -qE '^CLAUDE_CODE_OAUTH_TOKEN=.+' "$CC_TOKEN_FILE" 2>/dev/null; then
                CC_TOKEN_PRESENT=true
            fi
            CC_TOKEN_CREATED=$(grep '^GENESIS_CC_TOKEN_CREATED_AT=' "$CC_TOKEN_FILE" 2>/dev/null \
                | head -1 | cut -d= -f2- | tr -d '"' || true)
            if printf '%s' "$CC_TOKEN_CREATED" | grep -qE '^[0-9]+$'; then
                # 10# forces base-10: a corrupt leading-zero created_at (e.g.
                # 08…) would otherwise be read as octal → arithmetic error.
                CC_TOKEN_AGE_DAYS=$(( (CC_NOW_EPOCH - 10#$CC_TOKEN_CREATED) / 86400 ))
                # A future-dated created_at (clock skew / bad write) → clamp to 0,
                # never a negative that the reconciler would misread as unknown.
                if [ "$CC_TOKEN_AGE_DAYS" -lt 0 ]; then CC_TOKEN_AGE_DAYS=0; fi
            else
                CC_MTIME=$(stat -c %Y "$CC_TOKEN_FILE" 2>/dev/null || echo "")
                if printf '%s' "$CC_MTIME" | grep -qE '^[0-9]+$'; then
                    CC_TOKEN_AGE_DAYS=$(( (CC_NOW_EPOCH - 10#$CC_MTIME) / 86400 ))
                    if [ "$CC_TOKEN_AGE_DAYS" -lt 0 ]; then CC_TOKEN_AGE_DAYS=0; fi
                fi
            fi
        fi
        # --- systemd linger health (consumed by the container's _check_linger
        # host leg). Linger keeps this user's timers/services alive after logout;
        # if it is ever disabled the guardian's user timers die silently on the
        # next logout. Fail-safe: emit a JSON literal true/false/null — `null`
        # (NOT false) on any probe error, so a transient loginctl failure can
        # never false-alarm as "linger disabled". Only an explicit `Linger=no`
        # yields false. System-bus call (logind) — no PATH/login-shell dependence.
        HOST_LINGER=null
        _LINGER_RAW=$(loginctl show-user "$(id -un)" --property=Linger 2>/dev/null || true)
        case "$_LINGER_RAW" in
            Linger=yes) HOST_LINGER=true ;;
            Linger=no)  HOST_LINGER=false ;;
        esac
        # Surface the deployed tree_sha256 (F.0, read above) so the container can
        # tell a verified deploy from a legacy one, and advertise redeploy_verify
        # so a newer update.sh knows this gateway understands the sha-checked form.
        printf '{"cc_version": "%s", "node_version": "%s", "code_version": "%s", "code_date": "%s", "deployed_commit": "%s", "deployed_tree_sha256": "%s", "redeploy_verify": true, "gateway_sha": "%s", "authkey_no_pty": %s, "authkey_has_from": %s, "authkey_from_matches": %s, "authkey_observed_src_hash": "%s", "authkey_opts_hash": "%s", "cc_logged_in": %s, "cc_token_present": %s, "cc_token_age_days": %s, "host_linger": %s}\n' \
            "$CC_VER" "$NODE_VER" "$CODE_VER" "$CODE_DATE" "$DEPLOYED" "$DEPLOYED_TREE_SHA" "$GW_SHA" \
            "$AK_NO_PTY" "$AK_HAS_FROM" "$AK_FROM_MATCHES" "$AK_SRC_HASH" "$AK_OPTS_HASH" \
            "$CC_LOGGED_IN" "$CC_TOKEN_PRESENT" "$CC_TOKEN_AGE_DAYS" "$HOST_LINGER"
        ;;
    update)
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        if [ -d "$INSTALL_DIR/.git" ]; then
            cd "$INSTALL_DIR"
            OLD=$(git rev-parse --short HEAD 2>/dev/null)
            # Discard CLAUDE.md before pull — it's a generated file that the
            # repo's version would overwrite with the wrong content (container
            # dev instructions vs Guardian diagnostic instructions). We
            # regenerate it from config/guardian-claude.md after every pull.
            # Clear skip-worktree first: legacy installs marked CLAUDE.md
            # --skip-worktree, which wedges `git pull` ("local changes would be
            # overwritten") the moment upstream touches the tracked CLAUDE.md.
            git update-index --no-skip-worktree CLAUDE.md 2>/dev/null || true
            git checkout -- CLAUDE.md >/dev/null 2>&1 || true
            # Stash remaining local config changes (container_ip, etc.)
            STASHED=0
            if ! git diff --quiet 2>/dev/null; then
                git stash --quiet >/dev/null 2>&1 && STASHED=1
            fi
            # NOTE: every git command in this verb redirects stdout to /dev/null.
            # The verb's contract is "JSON on stdout ONLY" (the container parses the
            # whole stdout with json.loads); git's diffstat / "Updating.."/ conflict
            # chatter on stdout would otherwise corrupt the response into a parse
            # failure (a successful update misread as {"ok": false}).
            if git pull --ff-only >/dev/null 2>&1; then
                # Restore local config changes
                CONFIG_RESET=0
                if [ "$STASHED" -eq 1 ]; then
                    if ! git stash pop --quiet >/dev/null 2>&1; then
                        # Conflict applying local config onto the freshly pulled
                        # tree. Reset the worktree to a clean post-pull state so the
                        # gateway stays functional, but DO NOT `git stash drop` — the
                        # local config (container_ip, etc.) stays recoverable via
                        # `git stash list`. Dropping it here silently destroyed the
                        # operator's config (the bug this fixes). Stashes may
                        # accumulate across repeated conflicts — that is benign and
                        # recoverable, unlike silent loss.
                        git reset --hard --quiet HEAD >/dev/null 2>&1 || git checkout -- . >/dev/null 2>&1 || true
                        CONFIG_RESET=1
                    fi
                fi
                # Self-update FIRST (before any best-effort step): copy the freshly
                # pulled gateway to ~/.local/bin so nothing below can leave the
                # deployed gateway stale. Atomic rename is safe mid-execution —
                # Linux preserves the old inode for this running process's fd.
                # Guarded so `set -e` can't abort the whole update if it fails.
                if [ -f "$INSTALL_DIR/scripts/guardian-gateway.sh" ]; then
                    cp "$INSTALL_DIR/scripts/guardian-gateway.sh" "$HOME/.local/bin/guardian-gateway.sh.new" \
                        && chmod +x "$HOME/.local/bin/guardian-gateway.sh.new" \
                        && mv "$HOME/.local/bin/guardian-gateway.sh.new" "$HOME/.local/bin/guardian-gateway.sh" \
                        || true
                fi
                # Regenerate Guardian CLAUDE.md from template (never use repo
                # version). Shared host/container facts live in the user-level
                # ~/.claude/CLAUDE.md (D16), so nothing is appended here.
                if [ -f "$INSTALL_DIR/config/guardian-claude.md" ]; then
                    cp "$INSTALL_DIR/config/guardian-claude.md" "$INSTALL_DIR/CLAUDE.md" || true
                fi
                # --- BEST-EFFORT host tuning below: every command is guarded so a
                # sudo/tee/cp failure can NEVER abort the update or suppress the
                # success JSON (Bug A — froze the deployed gateway for ~2 months
                # on a passwordless-sudo host where an unguarded sudo tee failed). ---
                # Update systemd units from repo (picks up MemoryMax, OOMScoreAdjust, etc.)
                SYSTEMD_DIR="$HOME/.config/systemd/user"
                mkdir -p "$SYSTEMD_DIR"
                for unit in genesis-guardian.service genesis-guardian.timer \
                            genesis-guardian-watchman.service genesis-guardian-watchman.timer; do
                    if [ -f "$INSTALL_DIR/config/$unit" ]; then
                        cp "$INSTALL_DIR/config/$unit" "$SYSTEMD_DIR/$unit" || true
                    fi
                done
                systemctl --user daemon-reload 2>/dev/null || true
                # Refresh host sysctl/udev configs (I/O tuning, BFQ scheduler)
                if sudo -n true 2>/dev/null; then
                    if [ -f "$INSTALL_DIR/config/60-ioscheduler.rules" ]; then
                        sudo cp "$INSTALL_DIR/config/60-ioscheduler.rules" /etc/udev/rules.d/ 2>/dev/null || true
                        sudo udevadm control --reload-rules 2>/dev/null || true
                    fi
                    # Regenerate I/O sysctl from canonical values
                    sudo tee /etc/sysctl.d/99-genesis-io-tuning.conf > /dev/null 2>&1 << 'IOSYSCTL' || true
vm.swappiness = 10
vm.dirty_ratio = 10
vm.dirty_background_ratio = 3
vm.vfs_cache_pressure = 50
IOSYSCTL
                    # Regenerate OOM sysctl (same formula as install_guardian.sh Step 9).
                    # Guard the arithmetic: empty/odd /proc/meminfo would make
                    # $(( _HOST_RAM_KB / 100 )) a syntax error that aborts under set -e.
                    _HOST_RAM_KB=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}' || echo "")
                    if [ -n "$_HOST_RAM_KB" ]; then
                        _MIN_FREE=$(( _HOST_RAM_KB / 100 ))
                        # if-form (not `[ ] && x`): clearer + avoids any set -e
                        # ambiguity when the clamp condition is false.
                        if [ "$_MIN_FREE" -lt 131072 ]; then _MIN_FREE=131072; fi
                        if [ "$_MIN_FREE" -gt 1048576 ]; then _MIN_FREE=1048576; fi
                        sudo tee /etc/sysctl.d/99-genesis-oom-tuning.conf > /dev/null 2>&1 << OOMSYSCTL || true
vm.min_free_kbytes = $_MIN_FREE
vm.watermark_scale_factor = 50
vm.oom_kill_allocating_task = 1
OOMSYSCTL
                    fi
                    sudo sysctl --system > /dev/null 2>&1 || true
                fi
                # Restart timer so new check.py code takes effect immediately
                systemctl --user restart genesis-guardian.timer 2>/dev/null || true
                NEW=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
                # Record the deployed commit so the watchdog's drift detection works
                # for pull-based installs (previously only `redeploy` wrote this →
                # drift detection silently skipped on a "unknown" deployed_commit).
                mkdir -p "$STATE_DIR"
                python3 << PYEOF 2>/dev/null || true
import json
from datetime import datetime, timezone
sf = "$STATE_DIR/deploy_state.json"
try:
    with open(sf) as f:
        d = json.load(f)
except Exception:
    d = {}
d["deployed_commit"] = "$NEW"
d["deployed_at"] = datetime.now(timezone.utc).isoformat()
with open(sf, "w") as f:
    json.dump(d, f, indent=2)
PYEOF
                if [ "$CONFIG_RESET" -eq 1 ]; then
                    printf '{"ok": true, "action": "update", "old": "%s", "new": "%s", "warning": "local config changes conflicted with the update and were preserved in git stash (recover with: git stash list); or re-run install_guardian.sh to regenerate"}\n' "$OLD" "$NEW"
                else
                    printf '{"ok": true, "action": "update", "old": "%s", "new": "%s"}\n' "$OLD" "$NEW"
                fi
            else
                [ "$STASHED" -eq 1 ] && git stash pop --quiet >/dev/null 2>&1 || true
                printf '{"ok": false, "action": "update", "error": "git pull failed"}\n' >&2
                exit 1
            fi
        else
            echo '{"ok": false, "action": "update", "error": "not a git repo"}' >&2
            exit 1
        fi
        ;;
    redeploy\ *)
        # Push-based redeploy: container sends a tar archive on stdin.
        # Usage: tar ... | ssh host "redeploy <commit_hash> [tar_sha256]"
        # The container is the source of truth — no git pull needed.
        #
        # F.0 tree-integrity: the ONLY gate here used to be tar's exit code, so
        # a truncated/corrupt stream (or an archive built with the wrong
        # pathspec) could overwrite a healthy install and record a good-looking
        # deploy_state — which every downstream drift check then trusts. Two
        # guards close that:
        #   1. When the optional 2nd arg (sha256 of the whole tar stream) is
        #      present, the stream is spooled and verified BEFORE the running
        #      guardian is disturbed (timer still up, no backup churn).
        #   2. A membership gate checks the tar CONTAINS the required files
        #      (tar -tf, also pre-extraction) on BOTH forms — catching a
        #      wrong-pathspec / partial archive.
        # Older update.sh clients that send only <hash> still work (sha skipped).
        REDEPLOY_ARGS="${SSH_ORIGINAL_COMMAND#redeploy }"
        COMMIT_HASH="${REDEPLOY_ARGS%% *}"
        TREE_SHA=""
        if [ "$REDEPLOY_ARGS" != "$COMMIT_HASH" ]; then
            TREE_SHA="${REDEPLOY_ARGS#* }"
        fi
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        BACKUP_DIR="${STATE_DIR}/deploy-backup"

        # Validate commit hash (7-40 hex). Use bash [[ =~ ]] NOT `echo | grep`:
        # grep is line-oriented, so a multiline arg would pass on its first line
        # and pollute deployed_commit (the update-cc verb was hardened the same
        # way). [[ =~ ]] anchors against the WHOLE string.
        if ! [[ "$COMMIT_HASH" =~ ^[0-9a-f]{7,40}$ ]]; then
            echo '{"ok": false, "action": "redeploy", "error": "invalid commit hash"}' >&2
            exit 1
        fi
        # Validate the optional archive sha256 (exactly 64 hex when present)
        if [ -n "$TREE_SHA" ] && ! [[ "$TREE_SHA" =~ ^[0-9a-f]{64}$ ]]; then
            echo '{"ok": false, "action": "redeploy", "error": "invalid archive sha256"}' >&2
            exit 1
        fi

        # Spool the stdin tar to a temp file so we can (a) verify its sha256
        # before touching the running install and (b) extract deterministically.
        # Single-purpose process (one SSH command per invocation) → an EXIT trap
        # is the safe place to (1) always remove the spool AND (2) guarantee the
        # guardian timer comes back up if any post-stop step aborts under set -e
        # (a failed self-update/cp/write must never leave the guardian DOWN).
        mkdir -p "$STATE_DIR"
        SPOOL="$(mktemp "${STATE_DIR}/redeploy.XXXXXX.tar")"
        _TIMER_STOPPED=0
        trap 'rm -f "$SPOOL" 2>/dev/null || true; [ "$_TIMER_STOPPED" = 1 ] && systemctl --user start genesis-guardian.timer 2>/dev/null; true' EXIT
        if ! cat > "$SPOOL"; then
            echo '{"ok": false, "action": "redeploy", "error": "failed to receive archive"}' >&2
            exit 1
        fi

        # Verify the stream sha256 BEFORE stopping the timer / taking a backup —
        # a bad transfer must not disturb the healthy running guardian at all.
        if [ -n "$TREE_SHA" ]; then
            ACTUAL_SHA="$(sha256sum "$SPOOL" | cut -d' ' -f1)"
            if [ "$ACTUAL_SHA" != "$TREE_SHA" ]; then
                echo '{"ok": false, "action": "redeploy", "error": "archive sha256 mismatch"}' >&2
                exit 1
            fi
        fi

        # Required-file gate — check the ARCHIVE CONTENTS (not the extracted
        # tree): the install is extracted as a MERGE onto the existing dir (so
        # host-specific config/guardian.yaml + secrets.env, which are NOT in the
        # archive, survive), which means a post-extract check would be masked by
        # stale files. Verifying membership in the tar catches an archive built
        # with the wrong pathspec, or a partial one that lost whole paths, and —
        # like the sha check — runs BEFORE the running guardian is disturbed.
        TAR_LIST="$(tar -tf "$SPOOL" 2>/dev/null || true)"
        REDEPLOY_TREE_OK=true
        # Whole-line membership via a PURE-BASH substring match — deliberately
        # NOT `printf '%s\n' "$TAR_LIST" | grep -qxF`. Under `set -o pipefail`,
        # grep -q short-circuits on a match and the writer (printf) then gets
        # SIGPIPE, so the pipeline exits non-zero and a PRESENT file reads as
        # missing — but only once the listing exceeds the ~64 KB pipe buffer
        # (a real archive's `tar -tf` is ~86 KB). Wrapping the list in newlines
        # and matching `\n<req>\n` as a substring has no subprocess, no pipe,
        # and no SIGPIPE, so it is correct at any size.
        _tar_wrapped=$'\n'"${TAR_LIST}"$'\n'
        for _req in src/genesis/guardian/check.py scripts/guardian-gateway.sh pyproject.toml; do
            case "$_tar_wrapped" in
                *$'\n'"$_req"$'\n'*) : ;;   # present
                *) REDEPLOY_TREE_OK=false; break ;;
            esac
        done
        if [ "$REDEPLOY_TREE_OK" != true ]; then
            echo '{"ok": false, "action": "redeploy", "error": "archive missing required files"}' >&2
            exit 1
        fi

        # Stop timer during extraction to prevent running on partial state.
        # From here on the EXIT trap guarantees the timer is restarted even if a
        # later best-effort step aborts under set -e.
        systemctl --user stop genesis-guardian.timer 2>/dev/null || true
        _TIMER_STOPPED=1

        # Backup current installation for rollback
        rm -rf "$BACKUP_DIR"
        if [ -d "$INSTALL_DIR/src" ]; then
            cp -a "$INSTALL_DIR" "$BACKUP_DIR"
        fi

        # Extract archive from the verified spool into install dir (merge — see
        # the required-file gate note above on why we don't wipe first).
        mkdir -p "$INSTALL_DIR"
        if ! tar -xf "$SPOOL" -C "$INSTALL_DIR" 2>/dev/null; then
            # Rollback on extraction failure
            if [ -d "$BACKUP_DIR" ]; then
                rm -rf "$INSTALL_DIR"
                mv "$BACKUP_DIR" "$INSTALL_DIR"
            fi
            # Restart timer with old code
            systemctl --user start genesis-guardian.timer 2>/dev/null || true
            echo '{"ok": false, "action": "redeploy", "error": "tar extraction failed"}' >&2
            exit 1
        fi

        # Self-update gateway script (atomic rename — safe mid-execution).
        # Best-effort: guarded so a failure here can't abort under set -e and
        # strand the guardian with its timer stopped (the sibling `update` verb
        # guards the identical lines the same way).
        if [ -f "$INSTALL_DIR/scripts/guardian-gateway.sh" ]; then
            cp "$INSTALL_DIR/scripts/guardian-gateway.sh" "$HOME/.local/bin/guardian-gateway.sh.new" \
                && chmod +x "$HOME/.local/bin/guardian-gateway.sh.new" \
                && mv "$HOME/.local/bin/guardian-gateway.sh.new" "$HOME/.local/bin/guardian-gateway.sh" \
                || true
        fi

        # Regenerate CLAUDE.md from template (never use repo version on host).
        # Shared host/container facts live in the user-level ~/.claude/CLAUDE.md
        # (D16), so nothing is appended here. Best-effort (see note above).
        if [ -f "$INSTALL_DIR/config/guardian-claude.md" ]; then
            cp "$INSTALL_DIR/config/guardian-claude.md" "$INSTALL_DIR/CLAUDE.md" || true
        fi

        # Refresh systemd units from the archived repo config (picks up
        # MemoryMax, OOMScoreAdjust, TimeoutStartSec, etc.) — mirrors the `update`
        # verb, which was the ONLY path that refreshed units, so push-redeploys
        # (the path update.sh actually uses) left host units frozen at install
        # time. Copy-if-present: an older client whose archive lacks the unit
        # files simply leaves the installed units untouched — the redeploy
        # required-file gate deliberately does NOT demand them (backward-compat).
        # Best-effort/guarded so a cp or daemon-reload failure can never abort the
        # redeploy under set -e and strand the Guardian with its timer stopped.
        SYSTEMD_DIR="$HOME/.config/systemd/user"
        mkdir -p "$SYSTEMD_DIR" 2>/dev/null || true
        for unit in genesis-guardian.service genesis-guardian.timer \
                    genesis-guardian-watchman.service genesis-guardian-watchman.timer; do
            if [ -f "$INSTALL_DIR/config/$unit" ]; then
                cp "$INSTALL_DIR/config/$unit" "$SYSTEMD_DIR/$unit" 2>/dev/null || true
            fi
        done
        systemctl --user daemon-reload 2>/dev/null || true

        # Record deployed commit + the verified tree sha (separate file —
        # state.json is overwritten by Guardian ticks). Values are passed via
        # the environment (not string-interpolated into the heredoc), and the
        # delimiter is quoted, so nothing in them can break out of the script —
        # defense in depth even though both are regex-validated above.
        # tree_sha256 is "" for a legacy no-sha deploy.
        mkdir -p "$STATE_DIR"
        GENESIS_COMMIT_HASH="$COMMIT_HASH" GENESIS_TREE_SHA="$TREE_SHA" GENESIS_SF="$STATE_DIR/deploy_state.json" \
            python3 << 'PYEOF'
import json
import os
from datetime import datetime, timezone
sf = os.environ["GENESIS_SF"]
commit = os.environ["GENESIS_COMMIT_HASH"]
tree_sha = os.environ.get("GENESIS_TREE_SHA", "")
d = {
    "deployed_commit": commit,
    "deployed_at": datetime.now(timezone.utc).isoformat(),
    "tree_sha256": tree_sha,
}
with open(sf, "w") as f:
    json.dump(d, f, indent=2)
print(json.dumps({
    "ok": True, "action": "redeploy", "commit": commit,
    "verified": bool(tree_sha),
}))
PYEOF

        # Restart timer so new code takes effect immediately
        systemctl --user restart genesis-guardian.timer 2>/dev/null || true
        _TIMER_STOPPED=0  # cleanly restarted — don't let the EXIT trap re-start

        # Clean up backup on success
        rm -rf "$BACKUP_DIR"
        ;;
    update-cc\ *)
        # Controlled Claude Code version update on the host (WS-16).
        # Installs a single pinned version using the SAME npm that owns the
        # in-use `claude`, so the global prefix matches the binary that Guardian's
        # baked path (guardian.yaml, set by install_guardian.sh via
        # `command -v claude`) resolves — then verifies the result.
        VERSION="${SSH_ORIGINAL_COMMAND#update-cc }"
        # Strict allowlist on the arg: it is interpolated into a privileged
        # `npm install` under sudo, so accept ONLY a bare semver X.Y.Z
        # (anchored — mirrors the `redeploy` hash check above).
        # Whole-string anchor via bash regex — NOT `grep` (line-oriented: a
        # `1.2.3\n<payload>` would pass its `^…$`). SSH_ORIGINAL_COMMAND is
        # untrusted, so validate the entire value.
        if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo '{"ok": false, "action": "update-cc", "error": "invalid version (expected X.Y.Z)"}' >&2
            exit 1
        fi
        # Resolve npm next to the in-use claude (fall back to PATH npm).
        CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
        if [ -n "$CLAUDE_BIN" ] && [ -x "$(dirname "$CLAUDE_BIN")/npm" ]; then
            NPM_BIN="$(dirname "$CLAUDE_BIN")/npm"
        else
            NPM_BIN="$(command -v npm 2>/dev/null || true)"
        fi
        if [ -z "$NPM_BIN" ]; then
            echo '{"ok": false, "action": "update-cc", "error": "npm not found"}' >&2
            exit 1
        fi
        if ! sudo -n true 2>/dev/null; then
            echo '{"ok": false, "action": "update-cc", "error": "passwordless sudo unavailable"}' >&2
            exit 1
        fi
        # Package name is hardcoded — never derived from input. PATH is passed
        # through sudo because npm is often nvm-managed (matches host-setup.sh).
        if ! sudo -n env "PATH=$PATH" "$NPM_BIN" install -g "@anthropic-ai/claude-code@${VERSION}" >/dev/null 2>&1; then
            echo '{"ok": false, "action": "update-cc", "error": "npm install failed"}' >&2
            exit 1
        fi
        # Verify: the in-use claude must now report exactly the requested version.
        INSTALLED="$(claude --version 2>/dev/null || echo unknown)"
        INSTALLED_VER="$(printf '%s' "$INSTALLED" | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+' || true)"
        if [ "$INSTALLED_VER" != "$VERSION" ]; then
            printf '{"ok": false, "action": "update-cc", "error": "version mismatch after install", "requested": "%s", "installed": "%s"}\n' "$VERSION" "$INSTALLED" >&2
            exit 1
        fi
        # One-canonical-copy sweep — compact mirror of cc_shadow_scan in
        # scripts/lib/cc_version.sh (this script must stay hermetic; it is the
        # path that manages host CC when the install dir may be broken).
        # Runs ONLY after the verify above proved the resolved claude is at
        # the requested pin — the fail-safe: no proven-good copy, no removal.
        # Every host shadow incident to date was a USER-dir copy (nvm tree,
        # native installer symlink + version blobs), so this sweep is
        # user-dir-only — the gateway never sudo-removes files. Silent on
        # stdout (consumers parse the JSON line below); count in the JSON.
        SHADOWS_REMOVED=0
        CANON="$(readlink -f "$(command -v claude 2>/dev/null)" 2>/dev/null || true)"
        CANON_PKG=""
        case "$CANON" in
            */@anthropic-ai/claude-code/*)
                CANON_PKG="$(readlink -f "${CANON%%/@anthropic-ai/claude-code/*}/@anthropic-ai/claude-code" 2>/dev/null || true)"
                ;;
        esac
        if [ -n "$CANON" ] && [ "${CC_SHADOW_SCAN:-1}" != "0" ]; then
            for SHADOW in "$HOME"/.nvm/versions/node/*/bin/claude \
                          "$HOME/.local/bin/claude" \
                          "$HOME/.claude/local/claude" \
                          "$HOME/.npm-global/bin/claude"; do
                { [ -e "$SHADOW" ] || [ -L "$SHADOW" ]; } || continue
                [ "$(readlink -f "$SHADOW" 2>/dev/null)" = "$CANON" ] && continue
                T="$(readlink "$SHADOW" 2>/dev/null || true)"
                case "$T" in
                    *@anthropic-ai/claude-code*)
                        PKG="$(cd "$(dirname "$SHADOW")" 2>/dev/null && cd "$(dirname "$T")" 2>/dev/null && pwd || true)"
                        PKG="${PKG%%/@anthropic-ai/claude-code*}/@anthropic-ai/claude-code"
                        rm -f "$SHADOW"
                        case "$PKG" in
                            */@anthropic-ai/claude-code)
                                # Never rm the CANONICAL's package via a stale
                                # second link into it — the link alone goes.
                                if [ -d "$PKG" ] && [ "$(readlink -f "$PKG" 2>/dev/null)" != "$CANON_PKG" ]; then
                                    rm -rf "$PKG"
                                fi
                                ;;
                        esac
                        SHADOWS_REMOVED=$((SHADOWS_REMOVED + 1))
                        ;;
                    */.local/share/claude/*)
                        rm -f "$SHADOW"
                        SHADOWS_REMOVED=$((SHADOWS_REMOVED + 1))
                        ;;
                esac
            done
            # Native version blobs — unless the canonical IS a native install.
            case "$CANON" in
                "$HOME/.local/share/claude/"*) : ;;
                *)
                    if [ -d "$HOME/.local/share/claude/versions" ]; then
                        rm -rf "$HOME/.local/share/claude/versions"
                        SHADOWS_REMOVED=$((SHADOWS_REMOVED + 1))
                    fi
                    ;;
            esac
        fi
        printf '{"ok": true, "action": "update-cc", "version": "%s", "installed": "%s", "shadows_removed": %s}\n' "$VERSION" "$INSTALLED" "$SHADOWS_REMOVED"
        ;;
    update-node\ *)
        # Controlled Node.js major upgrade on the host (WS-16).
        # The host runs `claude -p` for Guardian's intelligent diagnosis/recovery,
        # and Claude Code only runs on the Node major its pin requires (e.g. CC
        # 2.1.198 needs node >=22). This installs a pinned major via NodeSource —
        # the SAME mechanism host-setup.sh uses — so the host stays runnable.
        # Mirrors `update-cc`: strict arg allowlist, passwordless-sudo guard, and
        # a post-install verify by `node --version` (never trust the apt exit
        # code — dpkg can "succeed" while leaving the old binary first on PATH).
        MAJOR="${SSH_ORIGINAL_COMMAND#update-node }"
        # Strict allowlist: interpolated into a privileged NodeSource URL + a
        # package-manager install under sudo, so accept ONLY a bare 1-2 digit
        # major. Whole-string bash regex, NOT `grep` (line-oriented: a
        # `22\n<payload>` would pass its `^…$`); SSH_ORIGINAL_COMMAND is untrusted.
        if [[ ! "$MAJOR" =~ ^[0-9]{1,2}$ ]]; then
            echo '{"ok": false, "action": "update-node", "error": "invalid major (expected NN)"}' >&2
            exit 1
        fi
        if ! sudo -n true 2>/dev/null; then
            echo '{"ok": false, "action": "update-node", "error": "passwordless sudo unavailable"}' >&2
            exit 1
        fi
        # Idempotent: already on the requested major → no-op.
        CUR_MAJOR="$(node --version 2>/dev/null | grep -oE '^v[0-9]+' | tr -d 'v' || true)"
        if [ "$CUR_MAJOR" = "$MAJOR" ]; then
            printf '{"ok": true, "action": "update-node", "major": "%s", "installed": "%s", "note": "already-current"}\n' \
                "$MAJOR" "$(node --version 2>/dev/null)"
            exit 0
        fi
        # Add the NodeSource repo for the requested major, then install. The URL
        # is built ONLY from the validated major — no injection surface.
        if command -v apt-get >/dev/null 2>&1; then
            if ! curl -fsSL "https://deb.nodesource.com/setup_${MAJOR}.x" | sudo -n -E bash - >/dev/null 2>&1; then
                echo '{"ok": false, "action": "update-node", "error": "nodesource repo setup failed"}' >&2
                exit 1
            fi
            # A distro `nodejs` (not from NodeSource — the state that stranded an
            # earlier host on Node 18) causes a dpkg conflict on install. If the
            # straight install fails, purge the distro package and retry once.
            # TRADEOFF: the purge is destructive — if the retry then fails (e.g.
            # transient NodeSource unavailability) the host is left with no node.
            # Bounded acceptable: host node is CC-only, and the failure is
            # reported (mismatch JSON) → update.sh records it as a degraded
            # subsystem rather than swallowing it.
            if ! sudo -n apt-get install -y nodejs >/dev/null 2>&1; then
                sudo -n apt-get remove -y nodejs libnode-dev >/dev/null 2>&1 || true
                sudo -n apt-get autoremove -y >/dev/null 2>&1 || true
                if ! sudo -n apt-get install -y nodejs >/dev/null 2>&1; then
                    echo '{"ok": false, "action": "update-node", "error": "apt install nodejs failed (dpkg conflict?)"}' >&2
                    exit 1
                fi
            fi
        elif command -v dnf >/dev/null 2>&1; then
            if ! curl -fsSL "https://rpm.nodesource.com/setup_${MAJOR}.x" | sudo -n bash - >/dev/null 2>&1; then
                echo '{"ok": false, "action": "update-node", "error": "nodesource repo setup failed"}' >&2
                exit 1
            fi
            if ! sudo -n dnf install -y nodejs >/dev/null 2>&1; then
                echo '{"ok": false, "action": "update-node", "error": "dnf install nodejs failed"}' >&2
                exit 1
            fi
        else
            echo '{"ok": false, "action": "update-node", "error": "no supported package manager (apt/dnf)"}' >&2
            exit 1
        fi
        # Verify by node major — NOT apt's exit code.
        hash -r 2>/dev/null || true
        NEW_MAJOR="$(node --version 2>/dev/null | grep -oE '^v[0-9]+' | tr -d 'v' || true)"
        if [ "$NEW_MAJOR" = "$MAJOR" ]; then
            printf '{"ok": true, "action": "update-node", "major": "%s", "installed": "%s"}\n' \
                "$MAJOR" "$(node --version 2>/dev/null)"
        else
            printf '{"ok": false, "action": "update-node", "error": "version mismatch after install", "requested": "%s", "installed": "%s"}\n' \
                "$MAJOR" "$(node --version 2>/dev/null || echo unknown)" >&2
            exit 1
        fi
        ;;
    test-approval)
        # E2E self-test of the keyword-reply approval gate. Sends a test
        # prompt and polls getUpdates for an APPROVE/DENY reply (~120s).
        # No recovery is performed. Timeout-guarded just above the self-test's
        # internal 120s budget so a hung poll can't wedge this SSH session.
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "test-approval", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
        GUARDIAN_SECRETS="$INSTALL_DIR/secrets.env" \
            timeout 130 "$VENV_PY" -m genesis.guardian --test-approval
        ;;
    disk-status)
        # Read-only: print storage-pool + snapshot JSON. Genesis's programmatic
        # window into host capacity (thin-pool data%/metadata%, VG free extents,
        # guardian snapshot list). No mutation, no secrets needed.
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "disk-status", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
            timeout 30 "$VENV_PY" -m genesis.guardian --disk-status
        ;;
    ram-status)
        # Read-only: print the guardian's RAM view JSON (container cgroup +
        # host-VM axes + worst-of tier). Genesis's window onto the out-of-band
        # RAM alert. No mutation, no secrets, no sudo.
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "ram-status", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        # Skew guard (see host-profile): sync-gateway redeploys THIS script
        # independently of the src tree. An old checkout has no --ram-status
        # branch — the flag would fall through main()'s if-chain into run_check(),
        # i.e. a FULL guardian recovery cycle triggered by a routine read-only poll.
        if [ ! -f "$INSTALL_DIR/src/genesis/guardian/memory_watch.py" ]; then
            echo '{"ok": false, "action": "ram-status", "error": "guardian src predates ram-status — run update to redeploy"}' >&2
            exit 1
        fi
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
            timeout 30 "$VENV_PY" -m genesis.guardian --ram-status
        ;;
    host-profile)
        # Read-only: print the host body-schema JSON (system identity, storage
        # pool, virtualization stack + this container's limits.*). Consumed by
        # the container's infra_profile host-plane collector. No mutation, no
        # secrets, no sudo.
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "host-profile", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        # Skew guard: sync-gateway redeploys THIS script independently of the
        # src tree. An old checkout has no --host-profile branch — the flag
        # would fall through main()'s if-chain into run_check(), i.e. a FULL
        # guardian recovery cycle triggered by a routine read-only poll.
        if [ ! -f "$INSTALL_DIR/src/genesis/guardian/host_profile.py" ]; then
            echo '{"ok": false, "action": "host-profile", "error": "guardian src predates host-profile — run update to redeploy"}' >&2
            exit 1
        fi
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
            timeout 45 "$VENV_PY" -m genesis.guardian --host-profile
        ;;
    bundle-status)
        # Read-only: print the offline repo-bundle archive JSON (host-only
        # archived `git bundle` copies + the newest stamp). The container's window
        # onto its offline re-clone lifeline. No mutation, no secrets, no sudo.
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "bundle-status", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        # Skew guard (see host-profile): sync-gateway redeploys THIS script
        # independently of the src tree. An old checkout has no --bundle-status
        # branch — the flag would fall through main()'s if-chain into run_check(),
        # i.e. a FULL guardian recovery cycle triggered by a routine read-only poll.
        if [ ! -f "$INSTALL_DIR/src/genesis/guardian/bundle_watch.py" ]; then
            echo '{"ok": false, "action": "bundle-status", "error": "guardian src predates bundle-status — run update to redeploy"}' >&2
            exit 1
        fi
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
            timeout 30 "$VENV_PY" -m genesis.guardian --bundle-status
        ;;
    provision-status)
        # Read-only host capacity via the Proxmox AUDIT token (VM cores/RAM,
        # per-disk sizes, storage + node-RAM headroom). No mutation, no sudo.
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "provision-status", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
        GUARDIAN_SECRETS="$INSTALL_DIR/secrets.env" \
            timeout 60 "$VENV_PY" -m genesis.guardian --provision-status
        ;;
    provision-grow-disk\ *)
        # EXECUTE-ONLY (approval is the CALLER's job — the container approves via
        # its own bot BEFORE calling this). Runs the execute-core: fresh
        # due-diligence re-check + rate cap + ONE resize attempt + verify +
        # ledger + storage-expand. Whole-string bash regex (NOT grep — grep is
        # line-oriented; SSH_ORIGINAL_COMMAND is untrusted) rejects anything but
        # exactly "<disk> <GiB>".
        ARGS="${SSH_ORIGINAL_COMMAND#provision-grow-disk }"
        PROV_DISK_RE='^(scsi|virtio|sata)[0-9]{1,2} [1-9][0-9]{0,2}$'
        if [[ ! "$ARGS" =~ $PROV_DISK_RE ]]; then
            echo '{"ok": false, "action": "provision-grow-disk", "error": "invalid args (expected <disk> <GiB 1-999>)"}' >&2
            exit 1
        fi
        DISK="${ARGS%% *}"
        GIB="${ARGS##* }"
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "provision-grow-disk", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
        GUARDIAN_SECRETS="$INSTALL_DIR/secrets.env" \
            timeout 600 "$VENV_PY" -m genesis.guardian --provision-grow-disk "$DISK" "$GIB"
        ;;
    provision-grow-memory\ *)
        # EXECUTE-ONLY (see provision-grow-disk). Grows configured VM memory;
        # takes effect only after a VM reboot (scheduled downtime).
        MIB="${SSH_ORIGINAL_COMMAND#provision-grow-memory }"
        if [[ ! "$MIB" =~ ^[1-9][0-9]{2,5}$ ]]; then
            echo '{"ok": false, "action": "provision-grow-memory", "error": "invalid MiB (100-999999)"}' >&2
            exit 1
        fi
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "provision-grow-memory", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
        GUARDIAN_SECRETS="$INSTALL_DIR/secrets.env" \
            timeout 120 "$VENV_PY" -m genesis.guardian --provision-grow-memory "$MIB"
        ;;
    storage-expand)
        # Absorb an already-grown virtual disk into the LVM-thin pool
        # (pvresize → autoextend profile → verify). Strictly additive LVM ops
        # (they use sudo -n internally), so guard passwordless sudo up front.
        if ! sudo -n true 2>/dev/null; then
            echo '{"ok": false, "action": "storage-expand", "error": "passwordless sudo unavailable"}' >&2
            exit 1
        fi
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "storage-expand", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
            timeout 600 "$VENV_PY" -m genesis.guardian --storage-expand
        ;;
    grow-root\ *)
        # EXECUTE-ONLY (approval is the CALLER's job — the container approves via
        # its own bot BEFORE calling). Local incus op: grow the container root
        # device to <GB> total (incus resizes the thin LV + filesystem online).
        # Whole-string bash regex (NOT grep) rejects anything but exactly "<GB>";
        # a digits-only arg can never word-split into a flag-shaped argv token.
        GB="${SSH_ORIGINAL_COMMAND#grow-root }"
        if [[ ! "$GB" =~ ^[1-9][0-9]{0,3}$ ]]; then
            echo '{"ok": false, "action": "grow-root", "error": "invalid arg (expected <GB total 1-9999>)"}' >&2
            exit 1
        fi
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "grow-root", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        # Skew guard (see host-profile): an old checkout has no --grow-root branch
        # — the flag would fall through main()'s if-chain into run_check().
        if [ ! -f "$INSTALL_DIR/src/genesis/guardian/grow_capacity.py" ]; then
            echo '{"ok": false, "action": "grow-root", "error": "guardian src predates grow-root — run update to redeploy"}' >&2
            exit 1
        fi
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
            timeout 300 "$VENV_PY" -m genesis.guardian --grow-root "$GB"
        ;;
    set-container-limits\ *)
        # EXECUTE-ONLY (see grow-root). Raise the container cgroup caps
        # (limits.memory / limits.cpu), grow-only, applied live. Whole-string
        # regex: two tokens, each digits-or-'-' (no flag-shaped token can match).
        ARGS="${SSH_ORIGINAL_COMMAND#set-container-limits }"
        if [[ ! "$ARGS" =~ ^([1-9][0-9]{2,6}|-)\ ([1-9][0-9]{0,2}|-)$ ]]; then
            echo '{"ok": false, "action": "set-container-limits", "error": "invalid args (expected <mem_mib|-> <cpu|->)"}' >&2
            exit 1
        fi
        MEM="${ARGS%% *}"
        CPU="${ARGS##* }"
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "set-container-limits", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        if [ ! -f "$INSTALL_DIR/src/genesis/guardian/grow_capacity.py" ]; then
            echo '{"ok": false, "action": "set-container-limits", "error": "guardian src predates set-container-limits — run update to redeploy"}' >&2
            exit 1
        fi
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
            timeout 60 "$VENV_PY" -m genesis.guardian --set-container-limits "$MEM" "$CPU"
        ;;
    configure-provisioning\ *)
        # Land/refresh host provisioning config as a state-dir override
        # (provisioning.local.yaml — survives redeploys, never edits the tracked
        # guardian.yaml). key=value args only; strict charset guards the untrusted
        # SSH_ORIGINAL_COMMAND (no shell metacharacters). NO secrets here — the two
        # Proxmox tokens cross the credential bridge, not this verb.
        ARGS="${SSH_ORIGINAL_COMMAND#configure-provisioning }"
        # Require EVERY token to be key=value — a bare flag-shaped token (e.g.
        # "--storage-expand") would otherwise word-split into argv and hijack
        # main()'s `if "--x" in sys.argv` dispatch to a different guardian verb.
        # key = lowercase field name; value = [-A-Za-z0-9_.] (covers IPs,
        # local-lvm, scsiN, true/false, ints). No leading "-" token can match.
        PROV_KV_RE='^[a-z_][a-z0-9_]*=[-A-Za-z0-9_.]*( [a-z_][a-z0-9_]*=[-A-Za-z0-9_.]*)*$'
        if [[ ! "$ARGS" =~ $PROV_KV_RE ]]; then
            echo '{"ok": false, "action": "configure-provisioning", "error": "invalid args (expected key=value tokens; key [a-z_], value [-A-Za-z0-9_.])"}' >&2
            exit 1
        fi
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        VENV_PY="$INSTALL_DIR/.venv/bin/python"
        if [ ! -x "$VENV_PY" ]; then
            echo '{"ok": false, "action": "configure-provisioning", "error": "guardian venv not found"}' >&2
            exit 1
        fi
        # ARGS is charset-validated above; intentional word-split into argv tokens.
        # shellcheck disable=SC2086
        PYTHONPATH="$INSTALL_DIR/src" \
        GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
        GUARDIAN_SECRETS="$INSTALL_DIR/secrets.env" \
            timeout 30 "$VENV_PY" -m genesis.guardian --configure-provisioning $ARGS
        ;;
    sync-gateway)
        # Recovery: redeploy the gateway script from the (already-pulled) install
        # dir to ~/.local/bin, WITHOUT a git pull. Recovers a stale/frozen deployed
        # gateway when the `update` self-update path is unavailable (e.g. a gateway
        # too old to have this verb is bootstrapped by a one-time host-local copy).
        # No git, no sudo — just an idempotent copy of a repo-tracked file.
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        SRC="$INSTALL_DIR/scripts/guardian-gateway.sh"
        DEST="$HOME/.local/bin/guardian-gateway.sh"
        if [ ! -f "$SRC" ]; then
            echo '{"ok": false, "action": "sync-gateway", "error": "install-dir gateway not found"}' >&2
            exit 1
        fi
        OLD_SHA=$(sha256sum "$DEST" 2>/dev/null | cut -d' ' -f1 || echo "none")
        [ -n "$OLD_SHA" ] || OLD_SHA="none"
        mkdir -p "$(dirname "$DEST")"
        # Guard the copy so a write failure reports a clean JSON error rather
        # than a bare set -e abort with no output.
        if cp "$SRC" "$DEST.new" && chmod +x "$DEST.new" && mv "$DEST.new" "$DEST"; then
            NEW_SHA=$(sha256sum "$DEST" 2>/dev/null | cut -d' ' -f1 || echo "unknown")
            printf '{"ok": true, "action": "sync-gateway", "old_sha": "%s", "new_sha": "%s"}\n' \
                "$OLD_SHA" "$NEW_SHA"
        else
            rm -f "$DEST.new" 2>/dev/null || true
            echo '{"ok": false, "action": "sync-gateway", "error": "failed to deploy gateway"}' >&2
            exit 1
        fi
        ;;
    reharden-key)
        # Rewrite the guardian authorized_keys line to the canonical hardened
        # options, deriving from= from THIS connection's source address —
        # self-proving: sshd authenticated this very connection from that
        # address, and the container always re-dials the same host_ip, so the
        # next connection presents the same source. Zero input is taken from
        # the caller; key material comes from the FILE, options are hardcoded.
        #
        # Un-brickable by construction: the previous file is snapshotted and a
        # systemd dead-man's-switch restores it after 120s unless a fresh
        # connection arrives to cancel it. Any arrival at this verb
        # authenticated against the CURRENT file is living proof the file
        # works — so the container's second call doubles as the confirm.
        AK="$HOME/.ssh/authorized_keys"
        AK_BAK="$AK.guardian-bak"
        AK_MARKER="genesis-guardian-control"
        # Canonical hardened options. Deliberately duplicated from
        # install_guardian.sh Step 11 (this script must stay hermetic — it is
        # the recovery path when the install dir is broken); byte-identity is
        # enforced by tests/test_guardian/test_gateway_reharden.py
        # (TestOptionsDivergenceGuardrail).
        GUARD_BASE_OPTS="command=\"$HOME/.local/bin/guardian-gateway.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty"

        # A connection reaching this verb proves the current file works →
        # cancel any pending restore (the confirm path after a rewrite).
        systemctl --user stop genesis-authkey-restore.timer 2>/dev/null || true
        systemctl --user stop genesis-authkey-restore.service 2>/dev/null || true

        COUNT=$(grep -cF "$AK_MARKER" "$AK" 2>/dev/null) || COUNT=0
        if [ "$COUNT" -ne 1 ]; then
            # 0 = nothing safe to rewrite; >1 = ambiguous/tampered. Refuse —
            # a human must resolve this, the reconciler will escalate.
            printf '{"ok": false, "action": "reharden-key", "error": "expected exactly 1 guardian line, found %s"}\n' "$COUNT" >&2
            exit 1
        fi
        AK_LINE=$(grep -F "$AK_MARKER" "$AK")
        # keytype anchor tolerates options containing spaces; blob + comment
        # are preserved exactly as stored.
        KEYPART=$(printf '%s\n' "$AK_LINE" | grep -oE '(ssh|ecdsa|sk)-[A-Za-z0-9@.-]+ [A-Za-z0-9+/=]+( .*)?$' || true)
        BLOB=$(printf '%s\n' "$KEYPART" | awk '{print $2}')
        if [ -z "$KEYPART" ] || [ -z "$BLOB" ]; then
            echo '{"ok": false, "action": "reharden-key", "error": "cannot parse guardian key line"}' >&2
            exit 1
        fi
        SRC=$(printf '%s' "${SSH_CONNECTION:-}" | awk '{print $1}')
        HAS_FROM=false
        if [ -n "$SRC" ]; then
            NEW_LINE="from=\"${SRC}\",${GUARD_BASE_OPTS} ${KEYPART}"
            HAS_FROM=true
        else
            # Never guess a source. Hardened-without-from matches the
            # installer's fallback and cannot lock the container out.
            NEW_LINE="${GUARD_BASE_OPTS} ${KEYPART}"
        fi
        if [ "$NEW_LINE" = "$AK_LINE" ]; then
            printf '{"ok": true, "action": "reharden-key", "changed": false, "has_from": %s}\n' "$HAS_FROM"
            exit 0
        fi
        # Validate the rebuilt line parses as an authorized_keys entry BEFORE
        # touching the real file. mktemp beside the target (tiny file; also
        # keeps everything on one filesystem).
        CHECK_TMP=$(mktemp "$AK.check.XXXXXX")
        printf '%s\n' "$NEW_LINE" > "$CHECK_TMP"
        if ! ssh-keygen -l -f "$CHECK_TMP" >/dev/null 2>&1; then
            rm -f "$CHECK_TMP"
            echo '{"ok": false, "action": "reharden-key", "error": "rebuilt line failed ssh-keygen validation"}' >&2
            exit 1
        fi
        rm -f "$CHECK_TMP"
        # Snapshot + arm the dead-man's-switch BEFORE any write. No armed
        # switch → no safety net → refuse to modify the file at all. The
        # fixed unit name doubles as a concurrency lock (a second concurrent
        # reharden's systemd-run fails while one is pending).
        cp "$AK" "$AK_BAK"
        if ! systemd-run --user --collect --on-active=120 \
                --unit=genesis-authkey-restore \
                /bin/cp "$AK_BAK" "$AK" >/dev/null 2>&1; then
            rm -f "$AK_BAK"
            echo '{"ok": false, "action": "reharden-key", "error": "cannot arm restore switch (systemd-run failed); refusing to modify authorized_keys"}' >&2
            exit 1
        fi
        AK_TMP=$(mktemp "$AK.XXXXXX")
        # Drop the guardian line by its blob (unique key material — immune to
        # option/comment drift), keep every other line untouched, append the
        # rebuilt line.
        grep -vF "$BLOB" "$AK" > "$AK_TMP" || true
        printf '%s\n' "$NEW_LINE" >> "$AK_TMP"
        OLD_N=$(grep -c '' "$AK") || OLD_N=0
        NEW_N=$(grep -c '' "$AK_TMP") || NEW_N=0
        if [ "$OLD_N" -ne "$NEW_N" ]; then
            # Aborting WITHOUT writing → the file is unchanged, so disarm the
            # switch we armed above (keep "armed iff we wrote" symmetry). The
            # restore would be a harmless self-copy either way, but leaving a
            # pending timer around is untidy.
            rm -f "$AK_TMP"
            systemctl --user stop genesis-authkey-restore.timer 2>/dev/null || true
            printf '{"ok": false, "action": "reharden-key", "error": "line-count invariant violated (%s -> %s); file untouched"}\n' "$OLD_N" "$NEW_N" >&2
            exit 1
        fi
        chmod 600 "$AK_TMP"
        mv "$AK_TMP" "$AK"
        printf '{"ok": true, "action": "reharden-key", "changed": true, "has_from": %s}\n' "$HAS_FROM"
        ;;
    ping)
        echo '{"ok": true, "action": "ping"}'
        ;;
    *)
        echo '{"ok": false, "error": "denied"}' >&2
        exit 1
        ;;
esac
