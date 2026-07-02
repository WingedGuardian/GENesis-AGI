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
#   redeploy <hash> — receive tar archive on stdin, deploy to install dir
#   update-cc <ver> — install a pinned Claude Code version (validated semver)
#   update-node <N> — install a pinned Node.js major via NodeSource (validated)
#   test-approval   — E2E test the keyword-reply approval gate (no recovery)
#   ping            — liveness check
#
# SSH authorized_keys entry (replace CONTAINER_IP with your container's IP):
#   command="~/.local/bin/guardian-gateway.sh",from="CONTAINER_IP" ssh-ed25519 ...
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
        DEPLOYED=$(python3 -c "import json; print(json.load(open('$STATE_DIR/deploy_state.json')).get('deployed_commit','unknown'))" 2>/dev/null || echo "unknown")
        # sha256 of the DEPLOYED gateway script — lets the container detect a
        # stale/frozen gateway whose self-update silently failed (the install-dir
        # code can be current while ~/.local/bin/guardian-gateway.sh lags).
        GW_SHA=$(sha256sum "$HOME/.local/bin/guardian-gateway.sh" 2>/dev/null | cut -d' ' -f1 || echo "unknown")
        [ -n "$GW_SHA" ] || GW_SHA="unknown"
        printf '{"cc_version": "%s", "node_version": "%s", "code_version": "%s", "code_date": "%s", "deployed_commit": "%s", "gateway_sha": "%s"}\n' \
            "$CC_VER" "$NODE_VER" "$CODE_VER" "$CODE_DATE" "$DEPLOYED" "$GW_SHA"
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
        # Push-based redeploy: container sends tar archive on stdin.
        # Usage: tar ... | ssh host "redeploy <commit_hash>"
        # The container is the source of truth — no git pull needed.
        COMMIT_HASH="${SSH_ORIGINAL_COMMAND#redeploy }"
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        BACKUP_DIR="${STATE_DIR}/deploy-backup"

        # Validate commit hash (must be 7-40 hex chars — defense in depth)
        if ! echo "$COMMIT_HASH" | grep -qE '^[0-9a-f]{7,40}$'; then
            echo '{"ok": false, "action": "redeploy", "error": "invalid commit hash"}' >&2
            exit 1
        fi

        # Stop timer during extraction to prevent running on partial state
        systemctl --user stop genesis-guardian.timer 2>/dev/null || true

        # Backup current installation for rollback
        rm -rf "$BACKUP_DIR"
        if [ -d "$INSTALL_DIR/src" ]; then
            cp -a "$INSTALL_DIR" "$BACKUP_DIR"
        fi

        # Extract archive from stdin into install dir
        mkdir -p "$INSTALL_DIR"
        if ! tar -xf - -C "$INSTALL_DIR" 2>/dev/null; then
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

        # Self-update gateway script (atomic rename — safe mid-execution)
        if [ -f "$INSTALL_DIR/scripts/guardian-gateway.sh" ]; then
            cp "$INSTALL_DIR/scripts/guardian-gateway.sh" "$HOME/.local/bin/guardian-gateway.sh.new"
            chmod +x "$HOME/.local/bin/guardian-gateway.sh.new"
            mv "$HOME/.local/bin/guardian-gateway.sh.new" "$HOME/.local/bin/guardian-gateway.sh"
        fi

        # Regenerate CLAUDE.md from template (never use repo version on host).
        # Shared host/container facts live in the user-level ~/.claude/CLAUDE.md
        # (D16), so nothing is appended here.
        if [ -f "$INSTALL_DIR/config/guardian-claude.md" ]; then
            cp "$INSTALL_DIR/config/guardian-claude.md" "$INSTALL_DIR/CLAUDE.md"
        fi

        # Record deployed commit (separate file — state.json is overwritten by Guardian ticks)
        mkdir -p "$STATE_DIR"
        python3 << PYEOF
import json
from datetime import datetime, timezone
sf = "$STATE_DIR/deploy_state.json"
d = {"deployed_commit": "$COMMIT_HASH", "deployed_at": datetime.now(timezone.utc).isoformat()}
with open(sf, "w") as f:
    json.dump(d, f, indent=2)
print(json.dumps({"ok": True, "action": "redeploy", "commit": "$COMMIT_HASH"}))
PYEOF

        # Restart timer so new code takes effect immediately
        systemctl --user restart genesis-guardian.timer 2>/dev/null || true

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
        if [ "$INSTALLED_VER" = "$VERSION" ]; then
            printf '{"ok": true, "action": "update-cc", "version": "%s", "installed": "%s"}\n' "$VERSION" "$INSTALLED"
        else
            printf '{"ok": false, "action": "update-cc", "error": "version mismatch after install", "requested": "%s", "installed": "%s"}\n' "$VERSION" "$INSTALLED" >&2
            exit 1
        fi
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
    ping)
        echo '{"ok": true, "action": "ping"}'
        ;;
    *)
        echo '{"ok": false, "error": "denied"}' >&2
        exit 1
        ;;
esac
