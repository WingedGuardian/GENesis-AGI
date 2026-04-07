#!/bin/bash
# Genesis v3 — Clean Uninstall
# Removes Genesis and/or Guardian from a system, handling the
# bidirectional monitoring loop that would otherwise fight removal.
#
# Usage:
#   ./scripts/uninstall.sh                 # Remove Genesis + Guardian, keep container
#   ./scripts/uninstall.sh --genesis-only  # Remove Genesis inside container only
#   ./scripts/uninstall.sh --guardian-only # Remove Guardian from host only
#   ./scripts/uninstall.sh --full          # Remove everything including container
#   ./scripts/uninstall.sh --dry-run       # Show what would be removed
#
# Flags:
#   --genesis-only      Only remove Genesis (inside container)
#   --guardian-only      Only remove Guardian (host-side)
#   --full              Remove everything including the Incus container
#   --dry-run           Show removal plan without making changes
#   --non-interactive   Skip all confirmation prompts
#   --container-name N  Container name (default: genesis)
#
# IMPORTANT: Run this script on the HOST, not inside the container.
# If run inside a container, it auto-downgrades to --genesis-only.

set -euo pipefail

# ── Globals ──────────────────────────────────────────────────
MODE="default"          # default | genesis-only | guardian-only | full
DRY_RUN=false
INTERACTIVE=true
CONTAINER_NAME="genesis"
CONTAINER_USER="ubuntu"
IN_CONTAINER=false

# Tracking what was removed for summary
REMOVED=()
SKIPPED=()
KEPT=()

# ── CLI flags ────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --genesis-only)     MODE="genesis-only" ;;
        --guardian-only)    MODE="guardian-only" ;;
        --full)             MODE="full" ;;
        --dry-run)          DRY_RUN=true ;;
        --non-interactive)  INTERACTIVE=false ;;
        --container-name)   shift; CONTAINER_NAME="$1" ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# \?//; p }' "$0"
            exit 0
            ;;
        *) echo "  Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# ── Helpers ──────────────────────────────────────────────────

log()  { echo "  $*"; }
warn() { echo "  WARNING: $*"; }
info() { echo "    . $*"; }
ok()   { echo "    + $*"; }
skip() { echo "    - $* (not found, skipping)"; SKIPPED+=("$*"); }

confirm() {
    local prompt="$1"
    if [ "$INTERACTIVE" = false ]; then return 0; fi
    if [ "$DRY_RUN" = true ]; then return 0; fi
    echo ""
    printf "  %s [y/N] " "$prompt"
    read -r answer
    case "$answer" in
        [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

# Remove a file or directory if it exists
safe_remove() {
    local path="$1"
    local label="${2:-$1}"
    if [ -e "$path" ] || [ -L "$path" ]; then
        if [ "$DRY_RUN" = true ]; then
            echo "    [DRY RUN] Would remove: $label"
        else
            rm -rf "$path"
            ok "Removed $label"
        fi
        REMOVED+=("$label")
    else
        skip "$label"
    fi
}

# Stop and disable a systemd user service/timer if it exists
safe_disable_service() {
    local unit="$1"
    local found=false
    if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
        found=true
        if [ "$DRY_RUN" = true ]; then
            echo "    [DRY RUN] Would stop: $unit"
        else
            systemctl --user stop "$unit" 2>/dev/null || true
        fi
    fi
    if systemctl --user is-enabled --quiet "$unit" 2>/dev/null; then
        found=true
        if [ "$DRY_RUN" = true ]; then
            echo "    [DRY RUN] Would disable: $unit"
        else
            systemctl --user disable "$unit" 2>/dev/null || true
        fi
    fi
    if [ "$found" = true ]; then
        ok "Stopped $unit"
    else
        skip "$unit"
    fi
}

# Run a command inside the container (from host). Tolerates container issues.
container_exec() {
    local cmd="$1"
    if [ "$DRY_RUN" = true ]; then
        echo "    [DRY RUN] Would run in container: ${cmd:0:80}..."
        return 0
    fi
    incus exec "$CONTAINER_NAME" -- su - "$CONTAINER_USER" -c "$cmd" 2>/dev/null || true
}

# Remove a line matching a pattern from a file
remove_line_containing() {
    local file="$1"
    local pattern="$2"
    local label="${3:-line matching '$pattern' from $file}"
    if [ -f "$file" ] && grep -q "$pattern" "$file" 2>/dev/null; then
        if [ "$DRY_RUN" = true ]; then
            echo "    [DRY RUN] Would remove: $label"
        else
            grep -v "$pattern" "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
            ok "Removed $label"
        fi
        REMOVED+=("$label")
    else
        skip "$label"
    fi
}

# ── Phase 0: Detect Environment ─────────────────────────────

echo ""
echo "  Genesis Uninstall"
echo "  ──────────────────────────────────────"
echo ""

# Detect if running inside a container
if [ -f /run/host/container-manager ] || \
   grep -q "lxc" /proc/1/environ 2>/dev/null || \
   [ -f /.dockerenv ] || \
   systemd-detect-virt -c -q 2>/dev/null; then
    IN_CONTAINER=true
    if [ "$MODE" != "genesis-only" ]; then
        warn "Running inside a container — downgrading to --genesis-only"
        warn "To remove Guardian, run this script on the host."
        MODE="genesis-only"
    fi
fi

# Detect what exists
HAS_GUARDIAN=false
HAS_GENESIS=false
HAS_CONTAINER=false

if [ "$IN_CONTAINER" = false ]; then
    [ -d "$HOME/.local/share/genesis-guardian" ] && HAS_GUARDIAN=true
    if command -v incus &>/dev/null && incus info "$CONTAINER_NAME" &>/dev/null 2>&1; then
        HAS_CONTAINER=true
        if incus exec "$CONTAINER_NAME" -- test -d "/home/$CONTAINER_USER/genesis" 2>/dev/null; then
            HAS_GENESIS=true
        fi
    fi
else
    [ -d "$HOME/genesis" ] && HAS_GENESIS=true
fi

log "Mode:      $MODE"
log "Dry run:   $DRY_RUN"
log "Container: $CONTAINER_NAME (exists: $HAS_CONTAINER)"
log "Guardian:  $HAS_GUARDIAN"
log "Genesis:   $HAS_GENESIS"

if [ "$HAS_GUARDIAN" = false ] && [ "$HAS_GENESIS" = false ]; then
    log ""
    log "Nothing to uninstall."
    exit 0
fi

# ── Phase 1: Neutralize Monitoring Loop ──────────────────────

echo ""
echo "  [1/6] Neutralizing monitoring loop..."

# Stop Guardian timers first (host-side) — prevents Guardian from
# restarting Genesis while we're removing it
if [ "$IN_CONTAINER" = false ] && [ "$HAS_GUARDIAN" = true ] && [ "$MODE" != "genesis-only" ]; then
    # Write pause file so any in-flight Guardian cycle sees it
    GUARDIAN_STATE_DIR="$HOME/.local/state/genesis-guardian/state"
    if [ -d "$GUARDIAN_STATE_DIR" ] && [ "$DRY_RUN" = false ]; then
        echo "{\"reason\":\"uninstall\",\"ts\":\"$(date -Is)\"}" > "$GUARDIAN_STATE_DIR/paused.json"
        info "Paused Guardian via state file"
    fi

    # Stop both timers AND services — watchman would restart the guardian timer
    for unit in genesis-guardian-watchman.timer genesis-guardian-watchman.service \
                genesis-guardian.timer genesis-guardian.service; do
        if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
            if [ "$DRY_RUN" = false ]; then
                systemctl --user stop "$unit" 2>/dev/null || true
            fi
            info "Stopped $unit"
        fi
    done
fi

# Stop Genesis watchdog (container-side) — prevents it from
# SSH-restarting Guardian while we're removing it
if [ "$MODE" != "guardian-only" ] && [ "$HAS_GENESIS" = true ]; then
    if [ "$IN_CONTAINER" = true ]; then
        for unit in genesis-watchdog.timer genesis-watchdog.service; do
            if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
                if [ "$DRY_RUN" = false ]; then
                    systemctl --user stop "$unit" 2>/dev/null || true
                fi
                info "Stopped $unit"
            fi
        done
    elif [ "$HAS_CONTAINER" = true ]; then
        container_exec "systemctl --user stop genesis-watchdog.timer genesis-watchdog.service 2>/dev/null || true"
        info "Stopped watchdog inside container"
    fi
fi

# Wait for any in-flight monitoring cycle to complete
if [ "$DRY_RUN" = false ]; then
    sleep 3
    info "Waited for in-flight cycles"
fi

# ── Phase 2: Offer Backup ───────────────────────────────────

echo ""
echo "  [2/6] Backup check..."

if [ "$MODE" != "guardian-only" ] && [ "$HAS_GENESIS" = true ]; then
    BACKUP_AVAILABLE=false
    if [ "$IN_CONTAINER" = true ]; then
        [ -f "$HOME/genesis/scripts/backup.sh" ] && BACKUP_AVAILABLE=true
    elif [ "$HAS_CONTAINER" = true ]; then
        incus exec "$CONTAINER_NAME" -- test -f "/home/$CONTAINER_USER/genesis/scripts/backup.sh" 2>/dev/null && \
            BACKUP_AVAILABLE=true
    fi

    if [ "$BACKUP_AVAILABLE" = true ]; then
        if [ "$INTERACTIVE" = true ] && [ "$DRY_RUN" = false ]; then
            if confirm "Run backup before uninstall?"; then
                if [ "$IN_CONTAINER" = true ]; then
                    bash "$HOME/genesis/scripts/backup.sh"
                else
                    container_exec "cd ~/genesis && bash scripts/backup.sh"
                fi
                ok "Backup complete"
            else
                info "Skipping backup"
            fi
        else
            warn "Skipping backup prompt (non-interactive or dry-run mode)"
        fi
    else
        info "No backup script found"
    fi
else
    info "N/A (not removing Genesis)"
fi

# ── Phase 3: Show Removal Plan ──────────────────────────────

echo ""
echo "  [3/6] Removal plan:"
echo ""

if [ "$MODE" != "genesis-only" ] && [ "$HAS_GUARDIAN" = true ]; then
    echo "    Guardian (host-side):"
    echo "      - Systemd units: genesis-guardian.{service,timer}, genesis-guardian-watchman.{service,timer}"
    echo "      - Code: ~/.local/share/genesis-guardian/"
    echo "      - State: ~/.local/state/genesis-guardian/"
    echo "      - Gateway: ~/.local/bin/guardian-gateway.sh"
    echo "      - SSH key: genesis-guardian-control entry in ~/.ssh/authorized_keys"
    [ "$HAS_CONTAINER" = true ] && echo "      - Incus device: guardian-shared on $CONTAINER_NAME"
    echo ""
fi

if [ "$MODE" != "guardian-only" ] && [ "$HAS_GENESIS" = true ]; then
    echo "    Genesis (container-side):"
    echo "      - Systemd units: genesis-server, genesis-bridge, genesis-watchdog, qdrant"
    echo "      - Repository: ~/genesis/"
    echo "      - Runtime state: ~/.genesis/"
    echo "      - Database: ~/data/"
    echo "      - Vector DB: ~/.qdrant/ + qdrant binary"
    echo "      - Cron entries: backup.sh, inbox_sync.sh (if any)"
    echo "      - CC config: .claude/settings.json, .claude/settings.local.json, .mcp.json, .claude/hooks/"
    echo "      - bashrc: DISABLE_INSTALLATION_CHECKS line"
    echo ""
fi

if [ "$MODE" = "full" ] && [ "$HAS_CONTAINER" = true ]; then
    echo "    Container:"
    echo "      - Incus container: $CONTAINER_NAME (DESTROYED — all data inside lost)"
    echo ""
fi

echo "    Preserved:"
echo "      - Claude Code CLI (npm global)"
echo "      - ~/.claude/ global config"
echo "      - Python, Node.js, system packages"
if [ "$MODE" != "full" ] && [ "$HAS_CONTAINER" = true ]; then
    echo "      - Incus container shell ($CONTAINER_NAME)"
fi
echo ""

if [ "$DRY_RUN" = true ]; then
    log "Dry run complete. No changes made."
    exit 0
fi

echo ""
echo "  ⚠  This is destructive and cannot be undone."
echo ""
if [ "$INTERACTIVE" = false ]; then
    warn "Non-interactive mode — skipping confirmation"
elif [ "$DRY_RUN" = false ]; then
    printf "  Type UNINSTALL to confirm: "
    read -r answer
    if [ "$answer" != "UNINSTALL" ]; then
        log "Aborted."
        exit 0
    fi
fi

# ── Phase 4: Remove Guardian (host-side) ─────────────────────

if [ "$MODE" != "genesis-only" ] && [ "$HAS_GUARDIAN" = true ]; then
    echo ""
    echo "  [4/6] Removing Guardian..."

    SYSTEMD_DIR="$HOME/.config/systemd/user"

    # Stop and disable all Guardian systemd units
    for unit in genesis-guardian.timer genesis-guardian.service \
                genesis-guardian-watchman.timer genesis-guardian-watchman.service; do
        safe_disable_service "$unit"
    done

    # Remove unit files
    for unit in genesis-guardian.service genesis-guardian.timer \
                genesis-guardian-watchman.service genesis-guardian-watchman.timer; do
        safe_remove "$SYSTEMD_DIR/$unit" "systemd/$unit"
    done
    systemctl --user daemon-reload 2>/dev/null || true

    # Remove Guardian code, state, and gateway
    safe_remove "$HOME/.local/share/genesis-guardian" "Guardian code (~/.local/share/genesis-guardian/)"
    safe_remove "$HOME/.local/state/genesis-guardian" "Guardian state (~/.local/state/genesis-guardian/)"
    safe_remove "$HOME/.local/bin/guardian-gateway.sh" "Guardian gateway"

    # Clean SSH authorized_keys — remove only the genesis-guardian-control entry
    remove_line_containing "$HOME/.ssh/authorized_keys" "genesis-guardian-control" \
        "Guardian SSH key from authorized_keys"

    # Remove Incus disk device
    if [ "$HAS_CONTAINER" = true ]; then
        if incus config device get "$CONTAINER_NAME" guardian-shared source &>/dev/null 2>&1; then
            incus config device remove "$CONTAINER_NAME" guardian-shared
            ok "Removed Incus disk device 'guardian-shared'"
            REMOVED+=("Incus device guardian-shared")
        else
            skip "Incus device guardian-shared"
        fi
    fi

    ok "Guardian removal complete"
else
    echo ""
    echo "  [4/6] Guardian removal... (skipped)"
fi

# ── Phase 5: Remove Genesis (container-side) ─────────────────

if [ "$MODE" != "guardian-only" ] && [ "$HAS_GENESIS" = true ]; then
    echo ""
    echo "  [5/6] Removing Genesis..."

    if [ "$IN_CONTAINER" = true ]; then
        # Running inside the container — direct operations

        # Stop all Genesis services (timer first, then service, to prevent restart)
        for unit in genesis-watchdog.timer genesis-watchdog.service \
                    genesis-server.service genesis-bridge.service \
                    qdrant.service; do
            safe_disable_service "$unit"
        done

        # Wait for genesis-server port to close
        for _i in $(seq 1 10); do
            if ! ss -tlnp 2>/dev/null | grep -q ':5000 '; then break; fi
            sleep 1
        done

        # Remove systemd unit files
        SYSTEMD_DIR="$HOME/.config/systemd/user"
        for f in "$SYSTEMD_DIR"/genesis-*.service "$SYSTEMD_DIR"/genesis-*.timer "$SYSTEMD_DIR/qdrant.service"; do
            [ -e "$f" ] && safe_remove "$f" "systemd/$(basename "$f")"
        done
        systemctl --user daemon-reload 2>/dev/null || true

        # Remove cron entries (graceful — crontab may be empty or not installed)
        if command -v crontab &>/dev/null && crontab -l 2>/dev/null | grep -qE 'backup\.sh|inbox_sync\.sh'; then
            crontab -l 2>/dev/null | grep -vE 'backup\.sh|inbox_sync\.sh' | crontab - 2>/dev/null || true
            ok "Removed cron entries"
            REMOVED+=("cron entries")
        else
            skip "cron entries"
        fi

        # Remove Genesis directories
        GENESIS_ROOT="$HOME/genesis"
        safe_remove "$GENESIS_ROOT" "Genesis repo ($GENESIS_ROOT)"
        safe_remove "$HOME/.genesis" "Runtime state (~/.genesis/)"
        safe_remove "$HOME/data" "Database (~/data/)"
        safe_remove "$HOME/.qdrant" "Qdrant data (~/.qdrant/)"

        # Remove Qdrant binary
        if [ -f /usr/local/bin/qdrant ]; then
            sudo rm -f /usr/local/bin/qdrant 2>/dev/null || rm -f /usr/local/bin/qdrant 2>/dev/null || true
            ok "Removed /usr/local/bin/qdrant"
            REMOVED+=("qdrant binary")
        elif [ -f "$HOME/.local/bin/qdrant" ]; then
            safe_remove "$HOME/.local/bin/qdrant" "qdrant binary"
        else
            skip "qdrant binary"
        fi

        # Clean Claude Code config (NOT Claude Code itself or ~/.claude/ global)
        for f in .claude/settings.json .claude/settings.local.json .mcp.json; do
            [ -e "$HOME/genesis/$f" ] && safe_remove "$HOME/genesis/$f" "CC config: $f"
        done
        safe_remove "$HOME/genesis/.claude/hooks" "CC hooks (.claude/hooks/)"

        # Clean .bashrc
        remove_line_containing "$HOME/.bashrc" "DISABLE_INSTALLATION_CHECKS" \
            "DISABLE_INSTALLATION_CHECKS from .bashrc"

        ok "Genesis removal complete (direct)"
    else
        # Running on host — reach into container via incus exec
        if [ "$MODE" = "full" ]; then
            # Container will be deleted in phase 6 — no need to clean inside
            info "Skipping container-side cleanup (container will be deleted in next phase)"
        else
            log "Cleaning Genesis inside container '$CONTAINER_NAME'..."

            # Stop all services (timers first to prevent restart races)
            container_exec "
                systemctl --user stop genesis-watchdog.timer genesis-watchdog.service 2>/dev/null || true;
                systemctl --user stop genesis-server.service genesis-bridge.service qdrant.service 2>/dev/null || true;
                systemctl --user disable genesis-server.service genesis-bridge.service \
                    genesis-watchdog.timer genesis-watchdog.service qdrant.service 2>/dev/null || true
            "
            ok "Stopped Genesis services"

            # Wait for port 5000 to close inside container
            for _i in $(seq 1 10); do
                if ! incus exec "$CONTAINER_NAME" -- ss -tlnp 2>/dev/null | grep -q ':5000 '; then break; fi
                sleep 1
            done

            # Remove systemd unit files
            container_exec "
                rm -f ~/.config/systemd/user/genesis-*.service \
                      ~/.config/systemd/user/genesis-*.timer \
                      ~/.config/systemd/user/qdrant.service 2>/dev/null;
                systemctl --user daemon-reload 2>/dev/null || true
            "
            ok "Removed systemd unit files"

            # Remove cron entries
            container_exec "
                if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -qE 'backup\.sh|inbox_sync\.sh'; then
                    crontab -l 2>/dev/null | grep -vE 'backup\.sh|inbox_sync\.sh' | crontab - 2>/dev/null || true;
                fi
            "
            ok "Cleaned cron entries"

            # Remove Genesis directories and data
            container_exec "rm -rf ~/genesis/ ~/.genesis/ ~/data/ ~/.qdrant/ 2>/dev/null || true"
            ok "Removed Genesis directories (repo, state, data, qdrant)"
            REMOVED+=("Genesis repo" "\$HOME/.genesis/" "\$HOME/data/" "\$HOME/.qdrant/")

            # Remove Qdrant binary
            container_exec "sudo rm -f /usr/local/bin/qdrant 2>/dev/null; rm -f ~/.local/bin/qdrant 2>/dev/null || true"
            ok "Removed qdrant binary"

            # Clean .bashrc
            container_exec "
                if grep -q 'DISABLE_INSTALLATION_CHECKS' ~/.bashrc 2>/dev/null; then
                    grep -v 'DISABLE_INSTALLATION_CHECKS' ~/.bashrc > ~/.bashrc.tmp && mv ~/.bashrc.tmp ~/.bashrc;
                fi
            "
            ok "Cleaned .bashrc"

            ok "Genesis removal complete (via incus exec)"
        fi
    fi
else
    echo ""
    echo "  [5/6] Genesis removal... (skipped)"
fi

# ── Phase 6: Delete Container (--full only) ──────────────────

if [ "$MODE" = "full" ] && [ "$HAS_CONTAINER" = true ]; then
    echo ""
    echo "  [6/6] Deleting Incus container..."
    echo ""
    warn "This will DESTROY container '$CONTAINER_NAME' and ALL data inside it."
    warn "This cannot be undone."

    if confirm "Delete container '$CONTAINER_NAME'?"; then
        incus delete "$CONTAINER_NAME" --force
        ok "Container '$CONTAINER_NAME' deleted"
        REMOVED+=("Incus container $CONTAINER_NAME")
    else
        info "Container preserved"
        KEPT+=("Incus container $CONTAINER_NAME")
    fi
else
    echo ""
    echo "  [6/6] Container deletion... (skipped)"
fi

# ── Phase 7: Summary ─────────────────────────────────────────

echo ""
echo "  ──────────────────────────────────────"
echo "  Uninstall Summary"
echo "  ──────────────────────────────────────"

if [ ${#REMOVED[@]} -gt 0 ]; then
    echo ""
    echo "  Removed:"
    for item in "${REMOVED[@]}"; do
        echo "    - $item"
    done
fi

if [ ${#SKIPPED[@]} -gt 0 ]; then
    echo ""
    echo "  Not found (already clean):"
    for item in "${SKIPPED[@]}"; do
        echo "    - $item"
    done
fi

echo ""
echo "  Preserved:"
echo "    - Claude Code CLI"
echo "    - ~/.claude/ global config"
echo "    - Python, Node.js, system packages"
if [ "$MODE" != "full" ] && [ "$HAS_CONTAINER" = true ]; then
    echo "    - Incus container '$CONTAINER_NAME' (empty shell)"
fi

echo ""
echo "  To reinstall:"
if [ "$MODE" = "full" ] || [ "$IN_CONTAINER" = true ]; then
    echo "    git clone https://github.com/WingedGuardian/GENesis-AGI.git genesis"
    echo "    cd genesis && ./scripts/bootstrap.sh"
else
    echo "    # Inside container:"
    echo "    incus exec $CONTAINER_NAME --user 1000 --env HOME=/home/$CONTAINER_USER -- bash"
    echo "    git clone https://github.com/WingedGuardian/GENesis-AGI.git genesis"
    echo "    cd genesis && ./scripts/install.sh"
    echo ""
    echo "    # Guardian (on this host):"
    echo "    cd <genesis-repo> && ./scripts/install_guardian.sh"
fi
echo ""
