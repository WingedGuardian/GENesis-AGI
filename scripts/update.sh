#!/bin/bash
# Update Genesis to the latest release.
# Run from inside your Genesis repo directory.
#
# Features:
#   - Pre-update backup (calls backup.sh)
#   - Rollback tag for safe revert on failure
#   - ERR trap wraps all mutating steps after rollback tag creation
#   - Idempotent bootstrap post-pull (config regen, systemd templates, hooks)
#   - Health verification with retry (fatal on failure)
#   - Migration failure is fatal and triggers rollback
#   - update_history table written on success + failure
#   - Writes failure context for CC-assisted recovery
#
# Usage: ./scripts/update.sh [--post-merge]
#   --post-merge  Skip fetch/merge (code already merged by CC conflict resolution);
#                 run only bootstrap, migrations, health check, and service restart.

set -Eeuo pipefail  # -E: the ERR trap is inherited by functions AND subshells
                    # (see _on_err's BASH_SUBSHELL guard for the subshell case)

# ── Copy-to-temp guard ──────────────────────────────────
# The update script may update itself during git merge, which would corrupt
# the running process. Industry standard (Chrome, Homebrew, Windows Update):
# copy to temp, exec from there, so the original can be safely overwritten.
if [ "${GENESIS_UPDATE_FROM_TEMP:-}" != "1" ]; then
    mkdir -p "$HOME/tmp"
    TEMP_COPY=$(mktemp "$HOME/tmp/genesis-update-XXXXXX.sh")
    cp "$0" "$TEMP_COPY"
    chmod +x "$TEMP_COPY"
    export GENESIS_UPDATE_FROM_TEMP=1
    # Pass original script dir so GENESIS_ROOT resolves correctly
    export GENESIS_UPDATE_ORIG_DIR="$(cd "$(dirname "$0")/.." && pwd)"
    exec "$TEMP_COPY" "$@"
fi
# Running from temp copy — clean up on exit
trap 'rm -f "${BASH_SOURCE[0]}" 2>/dev/null' EXIT

# ── Ensure systemctl --user works ───────────────────────
# CC sessions lack D-Bus env vars, causing systemctl --user to fail silently
# and triggering nohup fallback. Same fix as genesis.util.systemd.systemctl_env().
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

# ── Flag parsing ─────────────────────────────────────────
POST_MERGE=false
for _arg in "$@"; do
    [[ "$_arg" == "--post-merge" ]] && POST_MERGE=true
done

GENESIS_ROOT="${GENESIS_UPDATE_ORIG_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
SCRIPT_DIR="$GENESIS_ROOT/scripts"
VENV_DIR="$GENESIS_ROOT/.venv"
STARTED_AT="$(date -Iseconds)"
STATE_FILE="$HOME/.genesis/update_state.json"

# ── Update state file helper ────────────────────────────
# Written at each phase boundary so crash recovery knows where we stopped.
_write_state() {
    local phase="$1"
    mkdir -p "$HOME/.genesis"
    cat > "$STATE_FILE" << SEOF
{
    "phase": "$phase",
    "rollback_tag": "${ROLLBACK_TAG:-}",
    "old_tag": "${OLD_TAG:-}",
    "old_commit": "${OLD_COMMIT:-}",
    "started_at": "$STARTED_AT",
    "pid": $$,
    "services_stopped": [$(printf '"%s",' "${WERE_RUNNING[@]:-}" | sed 's/,$//')],
    "timestamp": "$(date -Iseconds)"
}
SEOF
}

# Signal handler for the PRE-STOP window: an interrupt before services are
# stopped has nothing to roll back (no merge, server still running), so just
# clear this run's state/marker and exit. Swapped for _on_signal (rollback)
# once the ERR trap arms. Defined here so it exists before its trap install.
_on_signal_prestop() {
    local sig="$1"
    trap - INT TERM
    echo "" >&2
    echo "  Update interrupted by SIG$sig before the merge — restoring any stopped services and cleaning up." >&2
    # The interrupt may have landed mid-stop (the stop polls up to ~10s), so a
    # service may already be down. Restart exactly what was detected running —
    # WERE_RUNNING is populated BEFORE the physical stop — so the server is never
    # left down. For genesis-server, `_start_genesis_server` runs `systemctl
    # restart`: it cleanly bounces the server if it never stopped, or starts it
    # if the interrupt already took it down — either way it ends up running. The
    # bridge uses `start`, a no-op when still running. No rollback is needed
    # here: nothing has been merged yet.
    local _svc
    for _svc in "${WERE_RUNNING[@]:-}"; do
        [ -n "$_svc" ] || continue
        if [ "$_svc" = "genesis-server" ]; then
            _start_genesis_server 2>/dev/null \
                || systemctl --user restart genesis-server.service 2>/dev/null || true
        else
            systemctl --user start "$_svc.service" 2>/dev/null || true
        fi
    done
    rm -f "$STATE_FILE" "$HOME/.genesis/update_in_progress.pid" 2>/dev/null || true
    exit 1
}

# Refuse to run from a worktree — pip install -e in bootstrap.sh would
# redirect system-wide imports and cause I/O death spiral.
if [[ "$GENESIS_ROOT" == *"/.claude/worktrees/"* ]] || \
   [[ "$GENESIS_ROOT" == *"/.worktrees/"* ]]; then
    echo "ERROR: update.sh must not run from a worktree."
    echo "       GENESIS_ROOT=$GENESIS_ROOT"
    echo "       Run from the main checkout instead."
    exit 1
fi

echo ""
echo "  Genesis Update"
echo "  ──────────────────────────────────────"

# ── Resolve upstream remote ────────────────────────────────
# Use the remote pointing to github_public_repo (e.g. 'public' for GENesis-AGI).
# Falls back to 'origin' if detection fails or genesis.env is unavailable.
_detect_update_remote() {
    local public_repo
    public_repo=$(
        "$VENV_DIR/bin/python" -c \
        "from genesis.env import github_public_repo; print(github_public_repo())" \
        2>/dev/null
    ) || public_repo="GENesis-AGI"
    local remote
    remote=$(git -C "$GENESIS_ROOT" remote -v 2>/dev/null \
        | awk "/$public_repo.*fetch/{print \$1; exit}")
    echo "${remote:-origin}"
}
UPDATE_REMOTE="$(_detect_update_remote)"
echo "  Update remote: $UPDATE_REMOTE"

# ── Current state ─────────────────────────────────────────
ORIGINAL_BRANCH=$(git -C "$GENESIS_ROOT" symbolic-ref --short HEAD 2>/dev/null || echo "main")
OLD_TAG=$(git -C "$GENESIS_ROOT" describe --tags --match 'v*' --abbrev=0 2>/dev/null || echo "untagged")
OLD_COMMIT=$(git -C "$GENESIS_ROOT" rev-parse --short HEAD)
NEW_TAG="$OLD_TAG"
NEW_COMMIT="$OLD_COMMIT"
echo "  Branch:  $ORIGINAL_BRANCH"
echo "  Current: $OLD_TAG ($OLD_COMMIT)"
echo ""

# ── Pre-update backup ────────────────────────────────────
if [ -x "$GENESIS_ROOT/scripts/backup.sh" ]; then
    echo "--- Pre-update backup ---"
    if "$GENESIS_ROOT/scripts/backup.sh" 2>&1 | tail -3; then
        echo "  Backup complete"
    else
        echo "  WARNING: backup failed (continuing anyway)"
    fi
    echo ""
fi

# ── Dirty-tree guard ─────────────────────────────────────
# Abort before touching anything if tracked files are modified.
# Untracked files (^??) are excluded — merge/reset never touches them.
# git reset --hard in _do_rollback would silently discard uncommitted work.
#
# Pure redeploy-decision helper (facts in → reason string out; no git/ssh) so
# the guardian-redeploy matrix is unit-testable. Emits a non-empty reason to
# stdout when the host guardian must be redeployed, empty when it is in sync.
#   $1 reachable          "1" if the gateway `version` op returned (host state readable)
#   $2 recognized         "1" if the host's deployed_commit resolves to a local commit
#   $3 host_commit        host's reported deployed_commit (message only)
#   $4 head_commit        local HEAD short-sha (message only)
#   $5 host_paths_differ  "1" if guardian paths differ host_commit..HEAD (meaningful iff recognized)
#   $6 pull_paths_differ  "1" if guardian paths differ OLD_COMMIT..HEAD (legacy pull-delta fallback)
_guardian_redeploy_reason() {
    local reachable="$1" recognized="$2" host_commit="$3" head_commit="$4"
    local host_paths_differ="$5" pull_paths_differ="$6"
    if [ "$reachable" != "1" ]; then
        # Host state unreadable — fall back to the legacy "did this run's pull
        # touch guardian paths" trigger (best effort; the redeploy SSH will
        # itself fail-soft if the host is genuinely down).
        if [ "$pull_paths_differ" = "1" ]; then
            echo "gateway unreachable — pull-delta: guardian paths changed this run"
        fi
        return 0
    fi
    if [ "$recognized" != "1" ]; then
        # deployed_commit is unknown/empty or a since-rebased/GC'd orphan we
        # cannot diff against — converge unconditionally onto HEAD.
        echo "host deployed_commit '${host_commit:-unknown}' unrecognized — reconciling to $head_commit"
        return 0
    fi
    if [ "$host_paths_differ" = "1" ]; then
        echo "host $host_commit vs HEAD $head_commit — guardian code drift"
    fi
    return 0
}

# ── Deploy-target sync: guardian redeploy + host Node/CC + container CC ──
# Extracted into a function so it runs on BOTH paths: the normal post-update
# path AND the "Already up to date" path. Drift healing (pin alignment on the
# host and container, guardian code redeploy) must never depend on whether THIS
# run's git merge happened to bring commits — a pin bump pulled manually, or a
# previously failed sync, still needs healing on a no-op run.
# Sets the global HOST_CC_DEGRADED (consumed by _record_update_history).
_sync_deploy_targets() {
    # Accumulates any deploy-target alignment failure (host guardian/Node/CC pin
    # AND the container's own CC pin) so it is recorded as a degraded subsystem
    # in update_history (surfaced by the dashboard) rather than silently skipped.
    # Empty = all deploy targets aligned (or no guardian configured).
    HOST_CC_DEGRADED=""

    # ── Update Guardian on host VM (if configured) ──────────
    GUARDIAN_CONFIG="$HOME/.genesis/guardian_remote.yaml"
    if [ -f "$GUARDIAN_CONFIG" ]; then
        # Single-line python -c on purpose: a multi-line body picks up the
        # shell's indentation, which is a top-level IndentationError — the
        # 2>/dev/null then silently empties HOST_IP and the ENTIRE host sync
        # (guardian redeploy + Node/CC pin healing) skips on every run.
        # That exact regression shipped once when this block was re-indented
        # into a function; test_update_host_sync.py guards the class.
        HOST_IP=$("$VENV_DIR/bin/python" -c "import yaml, pathlib; print(yaml.safe_load(pathlib.Path('$GUARDIAN_CONFIG').read_text()).get('host_ip', ''))" 2>/dev/null || true)
        HOST_USER=$("$VENV_DIR/bin/python" -c "import yaml, pathlib; print(yaml.safe_load(pathlib.Path('$GUARDIAN_CONFIG').read_text()).get('host_user', 'ubuntu'))" 2>/dev/null || echo "ubuntu")
        SSH_KEY="$HOME/.ssh/genesis_guardian_ed25519"

        if [ -n "$HOST_IP" ] && [ -f "$SSH_KEY" ]; then
            # Check if Guardian-relevant paths changed in this update. Includes
            # the systemd unit files so a unit-only change (e.g. MemoryMax,
            # TimeoutStartSec) registers as drift and triggers a redeploy — the
            # archive below ships those units and the gateway's redeploy verb
            # copies them, but none of that fires unless the drift gate sees them.
            GUARDIAN_PATHS="src/genesis/guardian src/genesis/util src/genesis/env.py src/genesis/observability src/genesis/db config/guardian-claude.md config/genesis-guardian.service config/genesis-guardian.timer config/genesis-guardian-watchman.service config/genesis-guardian-watchman.timer pyproject.toml scripts/install_guardian.sh scripts/guardian-gateway.sh scripts/lib/host_swap.sh scripts/lib/cc_tmp_volume.sh"
            DEPLOY_HASH=$(git -C "$GENESIS_ROOT" rev-parse --short HEAD)

            # ── Read host state ONCE: deployed_commit + node/cc versions ──
            # A single `version` gateway call feeds BOTH the redeploy decision
            # (deployed_commit) and the Node/CC pin sync further below. Fetched
            # up front so the redeploy trigger keys on the host's ACTUAL deployed
            # commit (observable state), NOT on whether THIS run's git pull
            # happened to touch guardian paths. The old pull-delta gate
            # ("$OLD_COMMIT"→HEAD) silently skipped the redeploy whenever the host
            # was last deployed from a since-rebased local HEAD (a no-op run has
            # OLD_COMMIT == HEAD), stranding it on an orphan commit indefinitely.
            # timeout 30: `version` is a fast JSON read. ServerAlive bounds a
            # DEAD link; `timeout` also bounds a WEDGED remote command that still
            # answers keepalives (ServerAlive alone would not). `|| true` keeps a
            # timeout/kill from aborting — an empty result is handled below.
            HOST_VER_RAW="$(timeout 30 ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
                -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
                "${HOST_USER}@${HOST_IP}" version 2>/dev/null || true)"
            HOST_DEPLOYED_COMMIT="$(printf '%s' "$HOST_VER_RAW" \
                | sed -n 's/.*"deployed_commit": "\([^"]*\)".*/\1/p' || true)"
            # F.0 capability probe: does the host gateway advertise the
            # sha-checked redeploy form? If so, a verified-form failure is a REAL
            # rejection (e.g. archive sha256 mismatch) and we must NOT retry the
            # unguarded 1-arg form (that would defeat the integrity check).
            _gv_verify_capable=0
            if printf '%s' "$HOST_VER_RAW" | grep -q '"redeploy_verify": *true'; then
                _gv_verify_capable=1
            fi

            # Compute redeploy-decision facts (no side effects) and let the pure
            # _guardian_redeploy_reason helper resolve the reason string.
            if [ -n "$HOST_VER_RAW" ]; then _gv_reachable=1; else _gv_reachable=0; fi
            # recognized = deployed_commit resolves to a real local commit (so we
            # can diff it). Empty / "unknown" / orphaned-or-GC'd shas → 0.
            if [ -n "$HOST_DEPLOYED_COMMIT" ] && [ "$HOST_DEPLOYED_COMMIT" != "unknown" ] \
               && git -C "$GENESIS_ROOT" cat-file -e "${HOST_DEPLOYED_COMMIT}^{commit}" 2>/dev/null; then
                _gv_recognized=1
            else
                _gv_recognized=0
            fi
            # host-state drift: guardian content differs between what the host
            # actually runs and HEAD (only meaningful when recognized).
            _gv_host_differ=0
            if [ "$_gv_recognized" = 1 ] \
               && ! git -C "$GENESIS_ROOT" diff --quiet "$HOST_DEPLOYED_COMMIT" HEAD -- $GUARDIAN_PATHS 2>/dev/null; then
                _gv_host_differ=1
            fi
            # legacy pull-delta (only used as the unreachable-host fallback).
            _gv_pull_differ=0
            if ! git -C "$GENESIS_ROOT" diff --quiet "$OLD_COMMIT" HEAD -- $GUARDIAN_PATHS 2>/dev/null; then
                _gv_pull_differ=1
            fi
            _gv_reason="$(_guardian_redeploy_reason "$_gv_reachable" "$_gv_recognized" \
                "$HOST_DEPLOYED_COMMIT" "$DEPLOY_HASH" "$_gv_host_differ" "$_gv_pull_differ")"

            if [ -n "$_gv_reason" ]; then
                echo "--- Guardian redeploy needed ($_gv_reason) — redeploying to host ---"
                # F.0 tree-integrity: materialize the archive to a file so we can
                # hash it (the gateway verifies the sha256 before disturbing the
                # running guardian) AND re-send the SAME bytes on the fallback
                # paths without regenerating. Large-temp discipline: never the
                # inherited TMPDIR (cc-tmp/tmpfs) — route to ~/tmp.
                # Archive excludes config/guardian.yaml (host-specific, generated by installer)
                mkdir -p "$HOME/tmp" 2>/dev/null || true
                _redeploy_ssh() {
                    # $1 = the redeploy command string; archive on stdin.
                    # ServerAlive bounds a DEAD connection (~60s of silence →
                    # ssh exits) without capping a legitimately slow redeploy:
                    # while the host runs git/pip the SSH transport stays alive
                    # and answers keepalives, so a working redeploy is never
                    # killed — only a truly hung/dead link is. timeout 600 is the
                    # backstop for a WEDGED redeploy that keeps answering
                    # keepalives: 10 min is generous for archive-unpack + git +
                    # pip on a slow host while still bounding an indefinite hang
                    # (a killed redeploy is non-fatal — recorded as degraded).
                    timeout 600 ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=30 \
                        -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
                        "${HOST_USER}@${HOST_IP}" "$1" < "$GUARDIAN_ARCHIVE" 2>/dev/null
                }
                # Guard the mktemp itself: `if ! VAR=$(...)` branches on the
                # command-substitution status, so an unwritable ~/tmp skips the
                # redeploy (non-fatal) instead of aborting the whole update run.
                if ! GUARDIAN_ARCHIVE="$(mktemp "$HOME/tmp/guardian-deploy.XXXXXX.tar" 2>/dev/null)"; then
                    echo "  Guardian archive temp unavailable (non-fatal) — skipping redeploy"
                elif git -C "$GENESIS_ROOT" archive HEAD -- \
                       src/ scripts/ pyproject.toml config/guardian-claude.md \
                       config/genesis-guardian.service config/genesis-guardian.timer \
                       config/genesis-guardian-watchman.service config/genesis-guardian-watchman.timer \
                       > "$GUARDIAN_ARCHIVE" 2>/dev/null; then
                    GUARDIAN_ARCHIVE_SHA="$(sha256sum "$GUARDIAN_ARCHIVE" | cut -d' ' -f1)"
                    if _redeploy_ssh "redeploy $DEPLOY_HASH $GUARDIAN_ARCHIVE_SHA"; then
                        echo "  Guardian redeployed ($DEPLOY_HASH, verified)"
                    elif [ "$_gv_verify_capable" = 1 ]; then
                        # The gateway advertises redeploy_verify, so the verified
                        # form's failure is a REAL rejection (archive sha256
                        # mismatch / missing files / write error) — retrying the
                        # unguarded 1-arg form would send the same bytes WITHOUT
                        # the integrity check and defeat F.0. Report non-fatal;
                        # the next run (drift keeps re-triggering) retries.
                        echo "  Guardian verified redeploy failed (non-fatal) — NOT falling back to the unguarded form (gateway is verify-capable)"
                    elif _redeploy_ssh "redeploy $DEPLOY_HASH"; then
                        # Old gateway with redeploy-but-no-sha (does NOT advertise
                        # redeploy_verify): it rejects the 2-arg command as an
                        # invalid hash — the bare 1-arg form is the only option.
                        echo "  Guardian redeployed ($DEPLOY_HASH, legacy unverified form)"
                    else
                        # Gateway too old to know 'redeploy' at all — use 'update'
                        # to install the new gateway, then retry the verified form.
                        echo "  Redeploy not available — falling back to update + retry"
                        # timeout 300: the gateway `update` verb does a host git
                        # pull + reinstall — minutes at most; bounds a wedged one.
                        if timeout 300 ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
                               -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
                               "${HOST_USER}@${HOST_IP}" update 2>&1; then
                            echo "  Guardian updated via git pull — retrying redeploy..."
                            # Gateway is a static file invoked fresh per SSH connection.
                            # After update writes the new version, next ssh uses it.
                            if _redeploy_ssh "redeploy $DEPLOY_HASH $GUARDIAN_ARCHIVE_SHA"; then
                                echo "  Guardian redeployed on retry ($DEPLOY_HASH, verified)"
                            else
                                echo "  Guardian redeploy retry failed (non-fatal)"
                            fi
                        else
                            echo "  Guardian update failed (non-fatal)"
                        fi
                    fi
                else
                    echo "  Guardian archive creation failed (non-fatal)"
                fi
                rm -f "$GUARDIAN_ARCHIVE" 2>/dev/null || true
                unset -f _redeploy_ssh
                # A redeploy just changed the host's deployed_commit — re-probe
                # so the pin sync AND the host_gateway_state.json persistence
                # (both inside cc_align_host_sync below) see post-redeploy
                # reality. Without this the state file keeps the PRE-redeploy
                # payload and deploy_health reports host_guardian_drift until
                # the nightly align timer re-probes. Best-effort: an empty
                # re-probe keeps the original payload rather than degrading
                # the pin sync.
                _post_redeploy_raw="$(timeout 30 ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
                    -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
                    "${HOST_USER}@${HOST_IP}" version 2>/dev/null || true)"
                if [ -n "$_post_redeploy_raw" ]; then
                    HOST_VER_RAW="$_post_redeploy_raw"
                fi
            else
                echo "--- Guardian in sync (host ${HOST_DEPLOYED_COMMIT:-unknown} @ HEAD $DEPLOY_HASH) — skipping host redeploy ---"
            fi

            # ── Sync host Node.js + Claude Code to the pinned versions (WS-16) ──
            # The host runs `claude -p` for Guardian's intelligent diagnosis/recovery
            # (guardian/diagnosis.py) — the highest-stakes CC call in the system: when
            # it fires, Genesis is down and, without CC's judgment, no programmatic
            # recovery is safe. So the host must carry a WORKING Claude Code at the
            # container pin, which in turn needs a compatible Node.js major (CC 2.1.198
            # needs node >=22). This block keeps BOTH aligned. Non-fatal, but NOT
            # silent: any alignment failure is recorded as a degraded subsystem
            # (guardian_host_*) in update_history so the dashboard/health surface it,
            # instead of the old misleading "gateway unreachable" skip.
            _cc_env="$SCRIPT_DIR/lib/cc_version.sh"
            if [ -f "$_cc_env" ]; then
                # Sync the host to the REPO pins, never an inherited override.
                unset CC_VERSION NODE_MAJOR
                # shellcheck source=/dev/null
                source "$_cc_env"
            else
                echo "  WARNING: $_cc_env missing — skipping host Node/CC sync"
            fi

            # HOST_VER_RAW was fetched up front (it also feeds the redeploy
            # decision above). The shared cc_align_host_sync (defined in the
            # cc_version.sh sourced just above) parses node/cc from that same
            # single `version` response and heals drift via the gateway,
            # APPENDING any failure to HOST_CC_DEGRADED. Factored so the nightly
            # genesis-cc-align timer runs the IDENTICAL logic between updates.
            # `|| true`: the function is non-fatal by contract (always returns 0),
            # but set -e is live here (ERR trap disarmed) — guard the call anyway.
            cc_align_host_sync "$HOST_USER" "$HOST_IP" "$SSH_KEY" "$HOST_VER_RAW" || true
            echo ""
        else
            # guardian_remote.yaml exists but is unusable — never skip silently:
            # a skipped sync here means guardian redeploy AND host pin healing
            # are dead, which is exactly the drift this function exists to heal.
            echo "  WARNING: guardian_remote.yaml present but host_ip unparseable or SSH key missing — host sync SKIPPED"
            HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_config_unreadable"
        fi
    fi

    # ── Sync CONTAINER Claude Code to the pinned version ──────
    # The guardian block above syncs the HOST's CC. This aligns the CONTAINER's own
    # Claude Code (update.sh runs inside the container) so a pin bump reaches the
    # container with zero user action. UNCONDITIONAL — not gated on guardian config,
    # so guardian-less installs are covered too. Non-fatal (`|| true`): `set -e` is
    # active here (ERR trap already disarmed), and a CC install hiccup must never
    # abort an update after git-pull/migrations have run.
    _cc_env="$SCRIPT_DIR/lib/cc_version.sh"
    if [ -f "$_cc_env" ]; then
        echo "--- Syncing container Claude Code to pin ---"
        unset CC_VERSION            # repo pin must win over any inherited override
        # shellcheck source=/dev/null
        source "$_cc_env"
        # Record a container CC sync failure as a degraded subsystem instead of
        # swallowing it — symmetric with the host-side pin failures accumulated
        # above, so a container left on a stale CC pin is surfaced in
        # update_history, not silently dropped.
        if ! cc_ensure_local; then
            echo "  WARNING: container Claude Code sync failed"
            HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}container_cc_sync"
        fi
        cc_shadow_scan || true
    else
        echo "  WARNING: $_cc_env missing — skipping container CC sync"
    fi
    echo ""
}

# EXCEPTION — known-ephemeral tracked files (EPHEMERAL_DIRTY_RE): tracked files
# that are routinely rewritten in place and are safe to ignore. They regenerate
# themselves, and local edits to them are discarded right before the merge
# (cleared just before the merge below) so an incoming change to one of them
# never aborts the merge with "local changes would be overwritten". Today:
#   - top-level `AGENTS.md` (GitNexus rewrites its auto-stat block)
#   - `config/procedure_triggers.yaml` (the L1 trigger cache rewrites it in
#     place). NOTE: this is now .gitignored and regenerated per-install at
#     bootstrap (`seed_procedures.py`); the entry only matters transitionally for
#     installs that still track it. AGENTS.md is the remaining tracked-ephemeral.
# These no longer block an update; REAL tracked changes still abort. Each
# alternative anchors the exact porcelain path (a single space precedes it), so
# only these exact paths are excused — e.g. `src/AGENTS.md` or
# `src/config/procedure_triggers.yaml` would still abort.
# (`.claude/settings.local.json` is install-local and belongs untracked; PR #792
# accidentally re-tracked it, which made every live install permanently dirty and
# blocked update.sh. This release de-tracks it again — the entry below excuses it
# transitionally, and the backup/restore pair around the merge preserves the live
# copy through the upstream deletion. Steady state: untracked + .gitignored.)
# (`.serena/project.yml` is the same failure mode: committed-but-machine-owned.
# Serena rewrites its comment block on version bumps, so every install where
# Serena runs went permanently dirty and update.sh required a stash dance. This
# release de-tracks it (`.serena/` is already .gitignored); the entry below plus
# its backup/restore pair carry the live copy through the upstream deletion.
# Serena autogenerates the file when missing, so fresh clones need nothing.)
EPHEMERAL_DIRTY_RE=' AGENTS\.md$| config/procedure_triggers\.yaml$| \.claude/settings\.local\.json$| \.serena/project\.yml$'
if [[ "$POST_MERGE" == "false" ]]; then
    DIRTY_FILES=$(git -C "$GENESIS_ROOT" status --porcelain 2>/dev/null \
        | grep -v "^??" \
        | grep -vE "$EPHEMERAL_DIRTY_RE" || true)
    if [[ -n "$DIRTY_FILES" ]]; then
        echo "ERROR: Working tree has uncommitted changes. Clean them up first:"
        echo "$DIRTY_FILES"
        echo ""
        # Mid-merge state (UU/AA entries) needs abort, not stash/commit
        if git -C "$GENESIS_ROOT" rev-parse --verify MERGE_HEAD &>/dev/null; then
            echo "  Repo is mid-merge. Run: git merge --abort"
        else
            echo "  git stash        # save and restore after update"
            echo "  git add -p && git commit -m 'chore: save local changes'  # commit"
        fi
        exit 1
    fi
fi

# ── Rollback tag ─────────────────────────────────────────
ROLLBACK_TAG="pre-update-$(date +%Y%m%d-%H%M%S)"
if [[ "$POST_MERGE" == "true" ]] && [ -f "$STATE_FILE" ]; then
    # In post-merge mode, reuse the rollback tag from the initial update.sh run
    # so rollback goes to pre-merge code, not the merged code.
    _saved_rt=$(
        GH_STATE_FILE="$STATE_FILE" \
        "$VENV_DIR/bin/python" -c \
        "import json, os; print(json.load(open(os.environ['GH_STATE_FILE'])).get('rollback_tag',''))" \
        2>/dev/null
    ) || _saved_rt=""
    if [ -n "$_saved_rt" ] && git -C "$GENESIS_ROOT" rev-parse "$_saved_rt" >/dev/null 2>&1; then
        ROLLBACK_TAG="$_saved_rt"
        echo "  Post-merge mode: reusing rollback tag $ROLLBACK_TAG"
    else
        git -C "$GENESIS_ROOT" tag "$ROLLBACK_TAG"
        echo "  Post-merge mode: created fallback rollback tag $ROLLBACK_TAG"
    fi
    # Recover OLD_TAG/OLD_COMMIT from state file for correct update_history.
    _saved_old_tag=$(
        GH_STATE_FILE="$STATE_FILE" \
        "$VENV_DIR/bin/python" -c \
        "import json, os; print(json.load(open(os.environ['GH_STATE_FILE'])).get('old_tag',''))" \
        2>/dev/null
    ) || true
    [ -n "${_saved_old_tag:-}" ] && OLD_TAG="$_saved_old_tag"
    _saved_old_commit=$(
        GH_STATE_FILE="$STATE_FILE" \
        "$VENV_DIR/bin/python" -c \
        "import json, os; print(json.load(open(os.environ['GH_STATE_FILE'])).get('old_commit',''))" \
        2>/dev/null
    ) || true
    [ -n "${_saved_old_commit:-}" ] && OLD_COMMIT="$_saved_old_commit"
else
    git -C "$GENESIS_ROOT" tag "$ROLLBACK_TAG"
    echo "  Rollback tag: $ROLLBACK_TAG"
fi
echo ""

# ── Fetch latest BEFORE stopping services (and before the deploy marker) ──
# Hoisted above the stop so a slow/hung fetch does NOT extend the downtime
# window, AND above `_write_state "fetching"` so env.update_in_progress() is not
# yet true while the (network, potentially-hanging) fetch runs — the watchdog's
# crash-restart guard stays fully active for the server during the fetch.
# Guarded EXPLICITLY (not via the ERR trap, which arms only after the stop): the
# trap's _do_rollback force-stops the server and restarts only WERE_RUNNING
# (empty here) — a fetch failure routed through it would leave the server DOWN.
# On failure, delete this run's freshly-created rollback tag and exit clean with
# the server untouched. POST_MERGE re-entry skips it (code already merged).
# timeout 120: bound a hung TCP fetch (accepted then silent) so it can't block
# the update forever; 120s is generous for the small repo (real fetches take
# seconds). `timeout` exits non-zero on kill → the failure branch below cleans
# up and exits with the server untouched.
if [[ "$POST_MERGE" == "false" ]]; then
    echo "--- Fetching latest ---"
    if ! timeout 120 git -C "$GENESIS_ROOT" fetch "$UPDATE_REMOTE" main; then
        echo "  Fetch failed (network/timeout?) — server NOT stopped, nothing changed."
        git -C "$GENESIS_ROOT" tag -d "$ROLLBACK_TAG" 2>/dev/null || true
        rm -f "$STATE_FILE" "$HOME/.genesis/update_in_progress.pid"
        exit 1
    fi
fi

_write_state "fetching"

# Catch an interrupt during the pre-stop window (fetch done, services still up).
# Replaced by the rollback-capable _on_signal once the ERR trap arms post-stop.
trap '_on_signal_prestop INT' INT
trap '_on_signal_prestop TERM' TERM

# ── Service stop/start helpers ───────────────────────────
# Works with both systemd and bare-process environments.
_stop_genesis_server() {
    # Try systemctl first (works when D-Bus session bus is available)
    if systemctl --user is-active --quiet genesis-server.service 2>/dev/null; then
        systemctl --user stop genesis-server.service 2>/dev/null && return 0
    fi
    # Fallback: read PID from fcntl lock file
    local lock_file="$HOME/.genesis/genesis-server.lock"
    if [ -f "$lock_file" ]; then
        local pid
        pid=$(tr -d '\0' < "$lock_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            # `|| true`: the process can exit between the kill -0 check above and
            # this signal (a race); a failed kill must not abort under set -e
            # (and, once the ERR trap is armed in P4b, must not trigger rollback).
            kill -TERM "$pid" 2>/dev/null || true
            # Wait up to 10s for graceful shutdown
            for i in $(seq 1 20); do
                kill -0 "$pid" 2>/dev/null || return 0
                sleep 0.5
            done
            kill -KILL "$pid" 2>/dev/null || true
            return 0
        fi
    fi
    # Last resort: pkill by command pattern
    pkill -TERM -f "python -m genesis serve" 2>/dev/null || true
    sleep 1
}

_ensure_server_down() {
    # Guarantee genesis-server is stopped AND stays stopped before mutating the
    # repo/DB. systemd's Restart=on-failure can resurrect the server after a
    # kill-based stop (the kill reads as a failure, arming a RestartSec timer); an
    # explicit `systemctl stop` transitions the unit to inactive/dead and DISARMS
    # that timer (on-failure only fires from active/running). Without this, an
    # auto-restarted STALE-code process runs during the merge + migration window —
    # the bug that shipped a deploy whose running process never loaded the new code.
    systemctl --user stop genesis-server.service 2>/dev/null || true
    for _ in $(seq 1 20); do
        if ! systemctl --user is-active --quiet genesis-server.service 2>/dev/null \
           && ! pgrep -f "python -m genesis serve" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    # The restart timer may have fired between the stop and the poll — stop again,
    # then hard-kill as a last resort.
    systemctl --user stop genesis-server.service 2>/dev/null || true
    pkill -KILL -f "python -m genesis serve" 2>/dev/null || true
    sleep 1
    if pgrep -f "python -m genesis serve" >/dev/null 2>&1; then
        echo "  ERROR: genesis-server could not be stopped"
        return 1
    fi
    return 0
}

_start_genesis_server() {
    # Use `restart` (NOT `start`): systemd's Restart=on-failure can resurrect a
    # STALE-code instance mid-update (a kill-based stop is seen as a failure and
    # arms a RestartSec timer). `start` is a no-op on that already-running instance
    # and would silently leave the OLD code live after the update. `restart` always
    # stop+starts from the current on-disk code, and still starts the unit cleanly
    # when it is already stopped.
    if systemctl --user restart genesis-server.service 2>&1; then
        echo "  Started genesis-server via systemd"
        return 0
    fi
    # Fallback: start directly — DEGRADED MODE
    # This bypasses systemd monitoring, so health dashboard will show red.
    echo "  WARNING: systemctl --user restart failed — falling back to direct start (degraded)"
    echo "  Health monitoring will not work correctly. Run: systemctl --user restart genesis-server.service"
    nohup "$VENV_DIR/bin/python" -m genesis serve --host 0.0.0.0 --port 5000 \
        >> "$HOME/.genesis/logs/genesis-server.log" 2>&1 &
    echo "  Started genesis-server in degraded mode (pid $!)"
    # Write marker so dashboard can detect degraded mode
    echo "nohup" > "$HOME/.genesis/server-start-mode"
}

# ── Pre-update DB snapshot ────────────────────────────────
# Flush the WAL and create a clean backup before stopping services.
# If the server is killed mid-write during the update, this backup
# enables recovery without data loss.
DB_FILE="$GENESIS_ROOT/data/genesis.db"
if [ -f "$DB_FILE" ]; then
    echo "--- Snapshotting database ---"
    sqlite3 "$DB_FILE" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
    # Use the online-backup API, not `cp`: this snapshot runs while the server
    # is still up (before the stop, to avoid extending the downtime window), and
    # a plain `cp` of a live WAL database yields a TORN copy if the server writes
    # mid-copy — which is exactly the file _do_rollback later restores. sqlite3
    # `.backup` takes a transactionally-consistent snapshot of a live database.
    if sqlite3 "$DB_FILE" ".backup '$DB_FILE.pre-update'" 2>/dev/null; then
        echo "  DB snapshot: $DB_FILE.pre-update"
    else
        echo "  WARNING: DB snapshot failed (continuing anyway)"
    fi
fi

# ── Stop services for update ──────────────────────────────
echo "--- Stopping services for update ---"
# Detect what is running BEFORE stopping anything, so the pre-stop signal handler
# can restart exactly what was running if an interrupt lands mid-stop (the stop
# polls up to ~10s) — otherwise a Ctrl-C / shutdown during the stop would leave
# the server down, the very thing the trap exists to prevent.
WERE_RUNNING=()
if systemctl --user is-active --quiet genesis-server.service 2>/dev/null || \
   pgrep -f "python -m genesis serve" >/dev/null 2>&1; then
    WERE_RUNNING+=("genesis-server")
fi
for svc in genesis-bridge; do
    if systemctl --user is-active --quiet "$svc.service" 2>/dev/null; then
        WERE_RUNNING+=("$svc")
    fi
done

# Now stop them. From here a SIG* runs _on_signal_prestop, which restarts
# WERE_RUNNING (populated above) — so an interrupt mid-stop restores the server.
if [[ " ${WERE_RUNNING[*]} " == *" genesis-server "* ]]; then
    _stop_genesis_server
    # Disarm systemd's on-failure auto-restart so a stale-code instance can't come
    # back during the merge/migration window. The ERR-trap rollback is not armed
    # yet, so aborting here leaves the repo/DB untouched — safe to exit.
    if ! _ensure_server_down; then
        echo "  Aborting update — could not stop genesis-server; refusing to merge over a live process."
        echo "  genesis-server has been stopped and was NOT restarted. Bring it back with:"
        echo "    systemctl --user restart genesis-server.service"
        # Deliberate: disarm the signal handler so it does NOT "restore" a server
        # we are intentionally leaving stopped (we could not cleanly stop it).
        trap - INT TERM
        exit 1
    fi
fi
for svc in "${WERE_RUNNING[@]}"; do
    [ "$svc" = "genesis-server" ] && continue
    systemctl --user stop "$svc.service" || true
done

[[ ${#WERE_RUNNING[@]} -gt 0 ]] && echo "  Stopped: ${WERE_RUNNING[*]}" || echo "  No services were running"
echo ""

# ── update_history helper ────────────────────────────────
# Records an entry in update_history. Silently no-ops if the table
# doesn't exist yet (first update before migration 0001 has run).
_record_update_history() {
    local status="$1"           # success | failed | rolled_back | conflicts_pending
    local reason="${2:-}"
    local degraded="${3:-}"
    local db_path="$GENESIS_ROOT/data/genesis.db"
    [ -f "$db_path" ] || return 0
    [ -x "$VENV_DIR/bin/python" ] || return 0

    # Run the insert in Python for parameterized SQL. The inline script
    # distinguishes three exit paths:
    #   0 — inserted OK
    #   2 — table missing (first update before 0001 ran) — expected, silent
    #   1 — any other error (logged to stderr + bash warns)
    # Do NOT pipe stderr to /dev/null — silencing failures is the
    # antipattern we're fixing. Only the "table missing" case is
    # allowed to be silent.
    # Note: we use `|| py_rc=$?` pattern because set -e otherwise triggers
    # on any non-zero $() assignment (including the expected rc=2 for
    # "table missing" case).
    local py_output=""
    local py_rc=0
    py_output=$(
        GH_STATUS="$status" \
        GH_REASON="$reason" \
        GH_DEGRADED="$degraded" \
        GH_DB_PATH="$db_path" \
        GH_OLD_TAG="$OLD_TAG" \
        GH_NEW_TAG="$NEW_TAG" \
        GH_OLD_COMMIT="$OLD_COMMIT" \
        GH_NEW_COMMIT="$NEW_COMMIT" \
        GH_ROLLBACK_TAG="$ROLLBACK_TAG" \
        GH_STARTED_AT="$STARTED_AT" \
        "$VENV_DIR/bin/python" - <<'PYEOF' 2>&1
import os
import sqlite3
import sys
import uuid
from datetime import UTC, datetime

try:
    con = sqlite3.connect(os.environ["GH_DB_PATH"], timeout=5.0)
    con.execute(
        "INSERT INTO update_history "
        "(id, old_tag, new_tag, old_commit, new_commit, status, rollback_tag, "
        "failure_reason, degraded_subsystems, started_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            os.environ["GH_OLD_TAG"],
            os.environ["GH_NEW_TAG"],
            os.environ["GH_OLD_COMMIT"],
            os.environ["GH_NEW_COMMIT"],
            os.environ["GH_STATUS"],
            os.environ.get("GH_ROLLBACK_TAG") or None,
            os.environ.get("GH_REASON") or None,
            os.environ.get("GH_DEGRADED") or None,
            os.environ["GH_STARTED_AT"],
            datetime.now(UTC).isoformat(),
        ),
    )
    con.commit()
    con.close()
    sys.exit(0)
except sqlite3.OperationalError as exc:
    msg = str(exc).lower()
    if "no such table" in msg:
        # Expected on the very first update — 0001 hasn't run yet.
        sys.exit(2)
    print(f"update_history insert failed (OperationalError): {exc}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"update_history insert failed ({type(exc).__name__}): {exc}", file=sys.stderr)
    sys.exit(1)
PYEOF
    ) || py_rc=$?
    case "$py_rc" in
        0) : ;;  # success
        2) : ;;  # table missing — expected
        *)
            echo "  WARNING: failed to record update_history entry:" >&2
            echo "    $py_output" >&2
            ;;
    esac
    return 0
}

# ── Rollback helper function ─────────────────────────────
_do_rollback() {
    local reason="$1"
    local degraded="${2:-}"

    # Disarm the ERR trap to prevent recursive rollback
    trap - ERR INT TERM

    echo ""
    echo "  UPDATE FAILED — $reason"
    echo "  Rolling back to $ROLLBACK_TAG..."

    # Stop any running services first. _ensure_server_down also disarms the
    # on-failure restart timer so a stale instance can't come back mid-rollback
    # (best-effort here — the rollback continues even if it can't fully stop).
    _stop_genesis_server
    _ensure_server_down || echo "  WARNING: genesis-server may still be running during rollback"
    systemctl --user stop genesis-bridge 2>/dev/null || true

    # Restore the original branch, then reset it to the rollback tag.
    # This keeps us on a named branch (not detached HEAD) at the pre-update state.
    local checkout_ok=true
    if ! git -C "$GENESIS_ROOT" checkout "$ORIGINAL_BRANCH" 2>&1; then
        echo "  CRITICAL: failed to checkout $ORIGINAL_BRANCH"
        checkout_ok=false
    fi
    if [ "$checkout_ok" = "true" ]; then
        if ! git -C "$GENESIS_ROOT" reset --hard "$ROLLBACK_TAG" 2>&1; then
            echo "  CRITICAL: failed to reset $ORIGINAL_BRANCH to $ROLLBACK_TAG"
            checkout_ok=false
        fi
    fi

    # Re-sync dependencies against the rolled-back code
    local pip_ok=true
    if ! "$VENV_DIR/bin/pip" install -e "$GENESIS_ROOT" --quiet 2>&1 | tail -1; then
        echo "  CRITICAL: pip install failed during rollback"
        pip_ok=false
    fi

    # Restart services with old code
    for svc in "${WERE_RUNNING[@]}"; do
        if [ "$svc" = "genesis-server" ]; then
            _start_genesis_server || echo "  CRITICAL: failed to restart genesis-server"
        else
            systemctl --user start "$svc.service" 2>/dev/null || \
                echo "  CRITICAL: failed to restart $svc"
        fi
    done

    if [ "$checkout_ok" = "true" ] && [ "$pip_ok" = "true" ]; then
        echo "  Rolled back to $ROLLBACK_TAG"
        _record_update_history "rolled_back" "$reason" "$degraded"
    else
        echo "  ROLLBACK INCOMPLETE — manual intervention required"
        echo "  Last known good state: $OLD_TAG ($OLD_COMMIT) on $ORIGINAL_BRANCH"
        _record_update_history "failed" "$reason (rollback incomplete)" "$degraded"
    fi

    echo ""
    echo "  To diagnose: discuss with Claude Code"
    echo "  Context: Update from $OLD_TAG to $NEW_TAG failed."
    echo "  Reason: $reason"
    [ -n "$degraded" ] && echo "  Degraded subsystems: $degraded"

    # Write failure context for CC to pick up.
    # Values passed as positional args so json.dump() handles all escaping —
    # raw git output in $reason can contain quotes and backslashes.
    mkdir -p "$HOME/.genesis"
    python3 -c "
import json, sys
data = {
    'old_tag':             sys.argv[1],
    'new_tag':             sys.argv[2],
    'old_commit':          sys.argv[3],
    'new_commit':          sys.argv[4],
    'rollback_tag':        sys.argv[5],
    'reason':              sys.argv[6],
    'degraded_subsystems': sys.argv[7],
    'original_branch':     sys.argv[8],
    'rollback_complete':   sys.argv[9] == 'true',
    'timestamp':           sys.argv[10],
}
with open(sys.argv[11], 'w') as f:
    json.dump(data, f, indent=4)
" "$OLD_TAG" "$NEW_TAG" "$OLD_COMMIT" "$NEW_COMMIT" "$ROLLBACK_TAG" \
  "$reason" "${degraded:-}" "$ORIGINAL_BRANCH" \
  "$([ "$checkout_ok" = "true" ] && [ "$pip_ok" = "true" ] && echo true || echo false)" \
  "$(date -Iseconds)" \
  "$HOME/.genesis/last_update_failure.json"

    # Clear the in-progress signal files (mirrors the success-path cleanup) so a
    # leftover entry can't suppress the watchdog's deploy-restart guard after a
    # rollback. The server is back up (above); once this invocation exits its PID
    # dies anyway, but removing the files closes the PID-reuse window proactively.
    rm -f "$STATE_FILE" "$HOME/.genesis/update_in_progress.pid"

    echo ""
    echo "  ──────────────────────────────────────"
    echo "  Rolled back: $OLD_TAG ($OLD_COMMIT) on $ORIGINAL_BRANCH"
    echo ""
}

# ── Install ERR trap — catches any unhandled failure after this point ─
# Uses $BASH_COMMAND to report which command failed.
_on_err() {
    local exit_code=$?
    # Under `set -E` the ERR trap is inherited by command substitutions and
    # subshells. Rollback must run ONLY at the top level: a failing `$(...)`
    # would otherwise run _do_rollback (force-stop server + git reset) from
    # INSIDE the subshell while the parent keeps executing. In a subshell just
    # propagate the failure — the parent's own ERR trap handles it at depth 0.
    if [ "${BASH_SUBSHELL:-0}" -ne 0 ]; then
        exit "$exit_code"
    fi
    _do_rollback "command failed (exit $exit_code): $BASH_COMMAND"
    exit 1
}
# Signal handler for the ARMED window (services stopped, mid-merge/migration):
# an interrupt here must roll back, exactly like an unhandled error — otherwise
# the server is left down. Disarm all traps first so a second signal can't
# re-enter _do_rollback. Installed alongside the ERR trap below.
_on_signal() {
    local sig="$1"
    trap - ERR INT TERM
    echo "" >&2
    echo "  Update interrupted by SIG$sig after services were stopped — rolling back." >&2
    _do_rollback "update interrupted by SIG$sig"
    exit 1
}
trap _on_err ERR
trap '_on_signal INT' INT
trap '_on_signal TERM' TERM

if [[ "$POST_MERGE" == "false" ]]; then
_write_state "merging"

# ── Merge ────────────────────────────────────────────────
# (git fetch was hoisted ABOVE the stop — see "Fetch latest BEFORE stopping
# services" — so the network round-trip is no longer inside the downtime window.)

# Clear local edits to known-ephemeral tracked files (EPHEMERAL_DIRTY_RE) before
# merging. They are rewritten in place at runtime and regenerate themselves
# (bootstrap seed / promoter / GitNexus), so an incoming change to one of them
# (e.g. this release de-tracking config/procedure_triggers.yaml) must not abort
# the merge. The pre-merge dirty guard EXCUSES these paths; clearing them here is
# the matching half so the merge actually applies.
# Transitional (settings.local.json de-track): if the file is still TRACKED
# (pre-de-track install), back up the live copy OUTSIDE the repo, then clear
# local edits so the upstream deletion merges clean. Restored (untracked)
# right after the merge join point below. Steady state (already untracked):
# ls-files fails -> no-op.
# BEGIN settings-local-premerge (extracted by tests/test_scripts/test_update_settings_local_transition.py)
SETTINGS_LOCAL=".claude/settings.local.json"
SETTINGS_LOCAL_BAK="$HOME/.genesis/settings.local.json.premerge"
if git -C "$GENESIS_ROOT" ls-files --error-unmatch "$SETTINGS_LOCAL" &>/dev/null \
   && [ -f "$GENESIS_ROOT/$SETTINGS_LOCAL" ]; then
    mkdir -p "$HOME/.genesis"
    cp "$GENESIS_ROOT/$SETTINGS_LOCAL" "$SETTINGS_LOCAL_BAK"
    git -C "$GENESIS_ROOT" checkout HEAD -- "$SETTINGS_LOCAL" 2>/dev/null \
        && echo "  (backed up live $SETTINGS_LOCAL; cleared local edits pre-merge)"
fi
# END settings-local-premerge

# Transitional (.serena/project.yml de-track): same pattern as settings.local.json
# above — Serena rewrites this machine-owned file in place, so while it is still
# TRACKED (pre-de-track install), back up the live copy outside the repo and
# clear local edits so the upstream deletion merges clean. Restored (untracked;
# `.serena/` is .gitignored) after the merge join point below. Steady state
# (already untracked): ls-files fails -> no-op.
# BEGIN serena-yml-premerge (extracted by tests/test_scripts/test_update_serena_transition.py)
SERENA_YML=".serena/project.yml"
SERENA_YML_BAK="$HOME/.genesis/serena.project.yml.premerge"
if git -C "$GENESIS_ROOT" ls-files --error-unmatch "$SERENA_YML" &>/dev/null \
   && [ -f "$GENESIS_ROOT/$SERENA_YML" ]; then
    mkdir -p "$HOME/.genesis"
    cp "$GENESIS_ROOT/$SERENA_YML" "$SERENA_YML_BAK"
    git -C "$GENESIS_ROOT" checkout HEAD -- "$SERENA_YML" 2>/dev/null \
        && echo "  (backed up live $SERENA_YML; cleared local edits pre-merge)"
fi
# END serena-yml-premerge

for _eph in AGENTS.md config/procedure_triggers.yaml; do
    if git -C "$GENESIS_ROOT" ls-files --error-unmatch "$_eph" &>/dev/null \
       && ! git -C "$GENESIS_ROOT" diff --quiet HEAD -- "$_eph" 2>/dev/null; then
        # `checkout HEAD --` (not `checkout --`) restores BOTH index and worktree
        # from HEAD, so a staged edit is cleared too — `checkout --` alone would
        # leave a staged change and the merge would still abort.
        git -C "$GENESIS_ROOT" checkout HEAD -- "$_eph" 2>/dev/null \
            && echo "  (discarded local edits to ephemeral $_eph before merge)"
    fi
done

echo "--- Merging $UPDATE_REMOTE/main ---"
MERGE_OUTPUT=""
MERGE_RC=0
MERGE_OUTPUT=$(git -C "$GENESIS_ROOT" merge "$UPDATE_REMOTE/main" --no-edit 2>&1) || MERGE_RC=$?

if [[ $MERGE_RC -ne 0 ]]; then
    # Check if this is a merge conflict (unmerged paths) vs other error
    if git -C "$GENESIS_ROOT" diff --name-only --diff-filter=U 2>/dev/null | grep -q .; then
        CONFLICTED_FILES=$(git -C "$GENESIS_ROOT" diff --name-only --diff-filter=U)
        echo "  Merge conflicts detected in:"
        echo "$CONFLICTED_FILES" | sed 's/^/    /'

        # Write conflict context for supervising CC session
        mkdir -p "$HOME/.genesis"
        # Build the context as VALID JSON via python (stdlib json). The old
        # shell heredoc only escaped `"` in merge_output, so a multi-line merge
        # message (the common case) embedded raw newlines and produced invalid
        # JSON — the dashboard consumer's json.loads then silently swallowed the
        # whole conflict context. Filenames with quotes broke the array the same
        # way. Guarded with `if !` (ERR-trap-exempt): a failure to write this
        # advisory supervisor context must NOT trip the armed rollback trap.
        _uc_target_tag="$(git -C "$GENESIS_ROOT" describe --tags --match 'v*' --abbrev=0 "$UPDATE_REMOTE/main" 2>/dev/null || echo 'untagged')"
        _uc_target_commit="$(git -C "$GENESIS_ROOT" rev-parse --short "$UPDATE_REMOTE/main" 2>/dev/null || echo 'unknown')"
        if ! UC_OLD_TAG="$OLD_TAG" UC_OLD_COMMIT="$OLD_COMMIT" \
             UC_TARGET_TAG="$_uc_target_tag" UC_TARGET_COMMIT="$_uc_target_commit" \
             UC_FILES="$CONFLICTED_FILES" UC_MERGE_OUTPUT="$MERGE_OUTPUT" \
             "$VENV_DIR/bin/python" - > "$HOME/.genesis/update_conflicts.json.tmp" <<'PYEOF'
import json
import os
from datetime import UTC, datetime

files = [f for f in os.environ.get("UC_FILES", "").splitlines() if f.strip()]
merge = "\n".join(os.environ.get("UC_MERGE_OUTPUT", "").splitlines()[:20])
data = {
    "old_tag": os.environ.get("UC_OLD_TAG", ""),
    "old_commit": os.environ.get("UC_OLD_COMMIT", ""),
    "target_tag": os.environ.get("UC_TARGET_TAG", ""),
    "target_commit": os.environ.get("UC_TARGET_COMMIT", ""),
    "conflicted_files": files,
    "merge_output": merge,
    "timestamp": datetime.now(UTC).isoformat(),
}
print(json.dumps(data, indent=2))
PYEOF
        then
            echo "  WARNING: could not write structured conflict context (advisory)"
            rm -f "$HOME/.genesis/update_conflicts.json.tmp" || true
        elif mv "$HOME/.genesis/update_conflicts.json.tmp" "$HOME/.genesis/update_conflicts.json"; then
            echo ""
            echo "  Conflict context written to ~/.genesis/update_conflicts.json"
        else
            # The rename must stay non-fatal too (disk full / perms): it is still
            # inside the armed ERR-trap window, and this is advisory supervisor
            # context — a failure here must not escalate into a full rollback.
            echo "  WARNING: could not finalize conflict context (advisory)"
            rm -f "$HOME/.genesis/update_conflicts.json.tmp" || true
        fi

        # Abort the merge — don't leave the working tree in a broken state.
        # CC will resolve conflicts in a worktree, not in the main checkout.
        echo "  Aborting merge to keep working tree clean..."
        git -C "$GENESIS_ROOT" merge --abort 2>/dev/null || true

        # Restart services with original code so the system stays operational
        echo "  Restarting services with pre-update code..."
        for svc in "${WERE_RUNNING[@]}"; do
            if [ "$svc" = "genesis-server" ]; then
                _start_genesis_server || echo "  WARNING: failed to restart genesis-server"
            else
                systemctl --user start "$svc.service" 2>/dev/null || \
                    echo "  WARNING: failed to restart $svc"
            fi
        done

        echo "  System is running on pre-update code."
        echo "  A CC session will resolve conflicts in a worktree."
        _record_update_history "conflicts_pending" \
            "merge conflicts in: $(echo "$CONFLICTED_FILES" | tr '\n' ', ' | sed 's/, $//')" ""
        trap - ERR INT TERM
        exit 2
    else
        # Not a conflict — some other merge error. Call rollback directly so the
        # DB gets a meaningful reason instead of "command failed (exit 1): false".
        echo "  Merge failed: $MERGE_OUTPUT"
        trap - ERR INT TERM
        _do_rollback "git merge failed: $(echo "$MERGE_OUTPUT" | head -3 | tr '\n' ' ')"
        exit 1
    fi
fi

NEW_TAG=$(git -C "$GENESIS_ROOT" describe --tags --match 'v*' --abbrev=0 2>/dev/null || echo "untagged")
NEW_COMMIT=$(git -C "$GENESIS_ROOT" rev-parse --short HEAD)

# BEGIN tier2-baseline-check (extracted by tests/test_scripts/test_update_tier2_baseline.py)
# Tier-2 pending vs the RECORDED baseline: bare merges may have advanced HEAD
# past the last update.sh run without activating tier-2 (units, hooks, pins).
# "No new commits from THIS pull" is then the wrong shortcut — the deploy-
# staleness alert explicitly advertises "run scripts/update.sh" as the
# recovery, so a no-op pull must still fall through to full activation
# (bootstrap + migrations + restart + a fresh update_history baseline) when
# update.sh-only paths changed since the last recorded success. Returns 0
# (pending) when the diff is non-empty OR errors (fail toward the full run);
# baseline unknown/unresolvable (pre-first-update install, rewritten history)
# returns 1 — shortcut as before, the awareness alert still covers it. Keep
# the path list in LOCKSTEP with TIER2_PATHS in
# src/genesis/observability/snapshots/deploy_health.py.
_tier2_pending_since_baseline() {
    local _baseline=""
    if [ -f "$GENESIS_ROOT/data/genesis.db" ] && [ -x "$VENV_DIR/bin/python" ]; then
        _baseline=$(GH_DB_PATH="$GENESIS_ROOT/data/genesis.db" \
            "$VENV_DIR/bin/python" - 2>/dev/null <<'PYEOF' || true
import os
import sqlite3

conn = sqlite3.connect(f"file:{os.environ['GH_DB_PATH']}?mode=ro", uri=True)
row = conn.execute(
    "SELECT new_commit FROM update_history WHERE status='success' "
    "ORDER BY datetime(completed_at) DESC LIMIT 1"
).fetchone()
print((row[0] or "").strip() if row else "")
PYEOF
        )
    fi
    [ -n "$_baseline" ] || return 1
    git -C "$GENESIS_ROOT" cat-file -e "${_baseline}^{commit}" 2>/dev/null || return 1
    ! git -C "$GENESIS_ROOT" diff --quiet "$_baseline" HEAD -- \
        scripts/systemd scripts/bootstrap.sh scripts/update.sh \
        scripts/lib/cc_version.sh scripts/hooks pyproject.toml 2>/dev/null
}
# END tier2-baseline-check

if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]] && _tier2_pending_since_baseline; then
    echo "  No new commits, but update.sh-only paths changed since the last recorded"
    echo "  update — running full activation (bootstrap + migrations + restart)."
elif [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
    echo "  Already up to date ($NEW_COMMIT)."
    # Clean up unnecessary rollback tag and disarm trap
    trap - ERR INT TERM
    git -C "$GENESIS_ROOT" tag -d "$ROLLBACK_TAG" 2>/dev/null || true
    # Restart services that we stopped (if any)
    for svc in "${WERE_RUNNING[@]}"; do
        if [ "$svc" = "genesis-server" ]; then
            _start_genesis_server || true
        else
            systemctl --user start "$svc.service" 2>/dev/null || true
        fi
    done
    # Nothing changed and no --post-merge continuation follows, so clear the
    # in-progress signals (like the success path) — a leftover must never linger.
    rm -f "$STATE_FILE" "$HOME/.genesis/update_in_progress.pid"
    # Transitional: the pre-merge step may have cleared live settings.local.json
    # or .serena/project.yml edits — put them back even though no merge landed.
    if [ -f "$SETTINGS_LOCAL_BAK" ]; then
        mkdir -p "$(dirname "$GENESIS_ROOT/$SETTINGS_LOCAL")"
        cp "$SETTINGS_LOCAL_BAK" "$GENESIS_ROOT/$SETTINGS_LOCAL"
        rm -f "$SETTINGS_LOCAL_BAK"
        echo "  (restored live $SETTINGS_LOCAL from pre-merge backup)"
    fi
    if [ -f "$SERENA_YML_BAK" ]; then
        mkdir -p "$(dirname "$GENESIS_ROOT/$SERENA_YML")"
        cp "$SERENA_YML_BAK" "$GENESIS_ROOT/$SERENA_YML"
        rm -f "$SERENA_YML_BAK"
        echo "  (restored live $SERENA_YML from pre-merge backup)"
    fi
    # Even with no repo delta, heal deploy-target drift (host/container CC + Node
    # pins): a pin bump pulled MANUALLY before this run, or an earlier failed
    # sync, must not leave drift in place just because the merge was a no-op.
    _sync_deploy_targets
    # Persist any host-side degradation the drift healing just found — this
    # path exits before the success-path recording, so without this the flag
    # would only ever reach stdout on no-op runs.
    if [ -n "$HOST_CC_DEGRADED" ]; then
        echo "  NOTE: recording degraded subsystem: $HOST_CC_DEGRADED"
        _record_update_history "success" "" "$HOST_CC_DEGRADED"
    fi
    echo ""
    echo "  Nothing to do."
    exit 0
fi
fi  # end: [[ "$POST_MERGE" == "false" ]]

# Transitional (settings.local.json de-track), both fresh-merge and --post-merge
# paths join here: if the merge removed the live file (upstream de-track applied
# to a tracked copy), restore the pre-merge backup as an UNTRACKED file. No-op
# once installs are past the transition (no backup written).
# BEGIN settings-local-restore (extracted by tests/test_scripts/test_update_settings_local_transition.py)
SETTINGS_LOCAL="${SETTINGS_LOCAL:-.claude/settings.local.json}"
SETTINGS_LOCAL_BAK="${SETTINGS_LOCAL_BAK:-$HOME/.genesis/settings.local.json.premerge}"
if [ -f "$SETTINGS_LOCAL_BAK" ]; then
    mkdir -p "$(dirname "$GENESIS_ROOT/$SETTINGS_LOCAL")"
    cp "$SETTINGS_LOCAL_BAK" "$GENESIS_ROOT/$SETTINGS_LOCAL"
    rm -f "$SETTINGS_LOCAL_BAK"
    echo "  (restored live $SETTINGS_LOCAL from pre-merge backup)"
fi
# END settings-local-restore

# Transitional (.serena/project.yml de-track): restore the pre-merge live copy
# as an UNTRACKED file after the upstream deletion applied. See the premerge
# block for the full story. No-op once installs are past the transition.
# BEGIN serena-yml-restore (extracted by tests/test_scripts/test_update_serena_transition.py)
SERENA_YML="${SERENA_YML:-.serena/project.yml}"
SERENA_YML_BAK="${SERENA_YML_BAK:-$HOME/.genesis/serena.project.yml.premerge}"
if [ -f "$SERENA_YML_BAK" ]; then
    mkdir -p "$(dirname "$GENESIS_ROOT/$SERENA_YML")"
    cp "$SERENA_YML_BAK" "$GENESIS_ROOT/$SERENA_YML"
    rm -f "$SERENA_YML_BAK"
    echo "  (restored live $SERENA_YML from pre-merge backup)"
fi
# END serena-yml-restore

NEW_TAG=$(git -C "$GENESIS_ROOT" describe --tags --match 'v*' --abbrev=0 2>/dev/null || echo "untagged")
NEW_COMMIT=$(git -C "$GENESIS_ROOT" rev-parse --short HEAD)

if [[ "$POST_MERGE" == "true" ]]; then
    echo "--- Post-merge mode: running bootstrap on conflict-resolved code ---"
    echo "  Merged: $OLD_TAG ($OLD_COMMIT) → $NEW_TAG ($NEW_COMMIT)"
fi
echo ""

# ── What changed ──────────────────────────────────────────
echo "--- Changes ---"
git -C "$GENESIS_ROOT" log "${OLD_COMMIT}..HEAD" --oneline --no-merges | head -20 || true
echo ""

_write_state "bootstrap"

# ── Run bootstrap (idempotent — handles deps, configs, hooks, systemd) ─
# GENESIS_BOOTSTRAP_ALLOW_LIVE: opt out of bootstrap's live-system guard
# (lib/live_system_guard.sh). This is the sanctioned deploy path — the server
# was stopped above, and the ERR trap is armed here, so a guard refusal would
# escalate into a full update rollback. The guard must never gate this call.
echo "--- Running bootstrap ---"
GENESIS_BOOTSTRAP_ALLOW_LIVE=1 "$GENESIS_ROOT/scripts/bootstrap.sh" 2>&1 | tail -10
echo "  Bootstrap complete"
echo ""

# ── Memory resilience — re-run VISIBLY. Bootstrap already applied it, but
# its output is eaten by the `tail -10` above, and the update path is exactly
# where an existing swapless install retrofits and must SEE the warn-only
# swap-invariant output. Idempotent: already-applied → one no-op line.
if [ -f "$SCRIPT_DIR/lib/memory_resilience.sh" ]; then
    # shellcheck source=lib/memory_resilience.sh
    . "$SCRIPT_DIR/lib/memory_resilience.sh"
    memory_resilience_apply
    echo ""
fi

# ── Network resilience — re-run VISIBLY (same rationale as memory resilience:
# bootstrap already applied it, but `tail -10` ate the output). The update path
# is where an existing install retrofits the KeepConfiguration drop-in + the
# networkd watchdog, and where a still-degraded networkd gets healed on the next
# timer tick. Idempotent: already-applied → one no-op line.
if [ -f "$SCRIPT_DIR/lib/network_resilience.sh" ]; then
    # shellcheck source=lib/network_resilience.sh
    . "$SCRIPT_DIR/lib/network_resilience.sh"
    network_resilience_apply
    echo ""
fi

# ── Verify Genesis is importable ──────────────────────────
if ! "$VENV_DIR/bin/python" -c "from genesis.runtime import GenesisRuntime" 2>/dev/null; then
    _do_rollback "Genesis not importable after bootstrap"
    exit 1
fi
echo "  Genesis importable: OK"
echo ""

_write_state "migrations"

# ── Run migrations (fatal on failure) ─────────────────────
# Distinguish a genuinely-ABSENT migrations module (a pre-migration install —
# skipping is correct) from one that FAILS to import (an update-introduced
# ImportError, e.g. a broken new migration file). The old `if import …; then`
# collapsed BOTH into a silent skip, which would run the freshly-merged code
# against an un-migrated schema. Probe exit codes: 0=present, 2=absent, 1=broken.
# `|| _mig_probe_rc=$?` keeps the non-zero probe exit from tripping the armed
# ERR trap; the `case` routes a broken module to an explicit rollback.
_mig_probe_rc=0
"$VENV_DIR/bin/python" - <<'PYEOF' || _mig_probe_rc=$?
import importlib.util
import sys

try:
    spec = importlib.util.find_spec("genesis.db.migrations")
except Exception as exc:  # a parent package (genesis / genesis.db) is broken
    print(f"migrations parent package import failed: {exc}", file=sys.stderr)
    sys.exit(1)
if spec is None:
    sys.exit(2)  # genuinely absent — pre-migration install
try:
    import genesis.db.migrations  # noqa: F401
except Exception as exc:
    print(f"migrations module import failed: {exc}", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
PYEOF
case "$_mig_probe_rc" in
    0)
        echo "--- Running migrations ---"
        if ! "$VENV_DIR/bin/python" -m genesis.db.migrations --apply 2>&1 | tail -10; then
            _do_rollback "migration runner failed"
            exit 1
        fi
        echo "  Migrations complete"
        echo ""
        ;;
    2)
        echo "  No migrations module in this build — skipping (pre-migration install)."
        echo ""
        ;;
    *)
        _do_rollback "migrations module failed to import after update (broken migration?)"
        exit 1
        ;;
esac

# ── Refresh Network Identity in user-level CLAUDE.md ────
# Always refresh ~/.claude/CLAUDE.md with current IPs (unconditional — the
# user-level file should always be current). Uses sentinel blocks.
_user_claude="$HOME/.claude/CLAUDE.md"
mkdir -p "$HOME/.claude"
# Seed file if missing (migration from an older install without user-level file)
if [ ! -f "$_user_claude" ]; then
    cat > "$_user_claude" <<'UCLSEED'
# This Genesis Install — User-Level Configuration

Install-specific overlay to the project CLAUDE.md. Populated by
scripts/host-setup.sh and refreshed by scripts/update.sh. The
<!-- begin:SECTION --> / <!-- end:SECTION --> blocks below are
managed by install scripts — edit at your own risk. The "Personal Notes"
section is safe to hand-edit; install scripts preserve it.

<!-- begin:container-specs -->
## Container
- **Specs**: (populated by Genesis on first boot — see ~/.genesis/infrastructure/INFRASTRUCTURE.md)
<!-- end:container-specs -->

<!-- begin:network-identity -->
<!-- end:network-identity -->

<!-- begin:github-config -->
## GitHub
- **Working Repo**: (set by installer)
- **Backups Repo**: (set by installer)
- **Public Distribution**: (set by installer)
- **Voice/Edge Repo**: (set by installer)
<!-- end:github-config -->

## Personal Notes

(Install scripts preserve this section. Add any machine-specific
reminders here.)
UCLSEED
fi

_write_state "health_check"

# ── Restart services ──────────────────────────────────────
# P5 GUARDRAIL: this final restart runs while the armed `_on_signal TERM` trap is
# STILL live (disarmed only at the success `trap - ERR INT TERM` below). That is
# safe TODAY only because the completing update path (the dashboard orchestrator)
# is cgroup-isolated via `systemd-run --scope`, so `_start_genesis_server`'s
# internal `systemctl stop`/`restart` does NOT signal this process. The
# `_apply_direct` path (dashboard, supervised=False) does NOT scope-isolate and
# stays in genesis-server.service's cgroup — a pre-existing bug. When P5 fixes
# `_apply_direct`, the fix MUST be scope isolation (systemd-run --scope), NOT a
# handler tweak: otherwise this restart's stop-phase would self-SIGTERM →
# _on_signal → a SPURIOUS rollback of a healthy, fully-migrated deploy.
if [[ ${#WERE_RUNNING[@]} -gt 0 ]]; then
    echo "--- Restarting services ---"

    # Reload systemd in case templates changed during bootstrap
    systemctl --user daemon-reload 2>/dev/null || true

    for svc in "${WERE_RUNNING[@]}"; do
        if [ "$svc" = "genesis-server" ]; then
            if ! _start_genesis_server; then
                _do_rollback "failed to start genesis-server after update"
                exit 1
            fi
        elif ! systemctl --user start "$svc.service"; then
            _do_rollback "failed to start $svc after update"
            exit 1
        fi
    done

    # ── Health verification with retry (FATAL on failure) ─
    echo "--- Verifying system health ---"
    HEALTH_OK=false
    DEGRADED=""

    for attempt in $(seq 1 12); do
        sleep 15
        # --max-time 20: bound a hung connection (server accepts but never
        # answers) so a single attempt can't block the update forever. Kept
        # ABOVE the health route's own ~15s internal budget so a slow-but-
        # responding check is never killed here (which would false-rollback a
        # healthy server); a 503 at 15s still fails -f correctly on its own.
        if curl -sf --max-time 20 http://localhost:5000/api/genesis/health > /dev/null 2>&1; then
            echo "  OK: Genesis health endpoint responding (attempt $attempt)"
            HEALTH_OK=true
            break
        fi
        echo "  Attempt $attempt: health endpoint not responding..."
    done

    if [ "$HEALTH_OK" = "true" ]; then
        # Check for failed subsystems
        DEGRADED=$(curl -sf --max-time 20 http://localhost:5000/api/genesis/health 2>/dev/null | \
            "$VENV_DIR/bin/python" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    failed = [k for k,v in d.get('subsystems',{}).items() if v.get('status') == 'failed']
    print(' '.join(failed))
except Exception:
    pass
" 2>/dev/null || true)
        if [ -n "$DEGRADED" ]; then
            echo "  Degraded subsystems: $DEGRADED"
            _do_rollback "subsystems failed after update: $DEGRADED" "$DEGRADED"
            exit 1
        fi
    fi

    # Verify services are active
    SVC_FAILED=()
    for svc in "${WERE_RUNNING[@]}"; do
        if systemctl --user is-active --quiet "$svc.service" 2>/dev/null; then
            echo "  OK: $svc"
        else
            echo "  FAILED: $svc — check: systemctl --user status $svc.service"
            SVC_FAILED+=("$svc")
        fi
    done

    if [[ ${#SVC_FAILED[@]} -gt 0 ]]; then
        _do_rollback "${#SVC_FAILED[@]} service(s) failed to start: ${SVC_FAILED[*]}" "$DEGRADED"
        exit 1
    fi

    if [ "$HEALTH_OK" = "false" ]; then
        _do_rollback "health endpoint did not respond after 12 attempts (3 minutes)"
        exit 1
    fi
    echo ""
fi

# ── Success: disarm trap ──────────────────────────────────
trap - ERR INT TERM

_sync_deploy_targets

# ── Refresh CLAUDE.md blocks (hoisted OUT of the downtime window) ──────────
# Pure-local regeneration (local ifaces / a local yaml / the last-collected
# profile; no network, no server import). Relocated to AFTER the restart so it
# no longer runs in the stop→restart window, but BEFORE _write_state "done"
# (below) so the restarted server's infra_profile collector is still
# update_in_progress-gated (infra_profile/claude_md.py) and cannot race this
# write. COSMETIC + post-success + past `trap - ERR`: a hiccup here must NEVER
# abort an already-healthy, already-restarted deploy — that would strand
# update_state.json at "health_check" (deferring the watchdog restart guard for
# hours) and skip the success record. errexit is live here and the multi-step
# network-identity refresh (lib source + sed-based write) is not otherwise
# guarded, so bracket the whole section with `set +e`.
set +e
echo "--- Refreshing Network Identity in ~/.claude/CLAUDE.md ---"
# Block format + detection helpers are shared with host-setup.sh via the
# lib — the two writers previously drifted (Tailscale line lost, container
# IP mis-detected as the tailscale0 address).
# shellcheck source=lib/claude_md_blocks.sh
. "$SCRIPT_DIR/lib/claude_md_blocks.sh"
_c_ip=$(detect_container_lan_ip)
_c_ipv6=$(detect_container_lan_ipv6)
_ts_ip=$(detect_tailscale_ip)
_host_ip=$("$VENV_DIR/bin/python" -c "
import yaml, pathlib
p = pathlib.Path.home() / '.genesis' / 'guardian_remote.yaml'
if p.exists():
    cfg = yaml.safe_load(p.read_text())
    print(cfg.get('host_ip', ''))
" 2>/dev/null || true)
[ -z "$_host_ip" ] && _host_ip=$(ip route | grep default | awk '{print $3}' || true)

if build_network_identity_block "$_c_ip" "$_c_ipv6" "$_host_ip" "" "$_ts_ip" \
    | write_sentinel_block "$_user_claude" "network-identity"; then
    echo "  Network identity updated in ~/.claude/CLAUDE.md"
else
    echo "  WARNING: network-identity refresh failed (non-fatal)"
fi
echo ""

# Refresh the container-specs block from the LAST collected infrastructure
# profile (content owner: genesis.infra_profile.claude_md — no collection, no
# runtime import; safe while the server is down). Skips gracefully pre-first-
# collection or on older checkouts without the module.
# Capture stderr rather than discarding it: the old `2>/dev/null || echo "no
# profile yet"` reported EVERY failure (module crash, write error) as the benign
# "no profile yet", masking real errors. Show the actual reason instead.
if _specs_err=$("$VENV_DIR/bin/python" -m genesis.infra_profile --claude-md-block 2>&1); then
    echo "  Container specs refreshed in ~/.claude/CLAUDE.md"
else
    echo "  Container specs refresh skipped: ${_specs_err:-no profile yet}"
fi
echo ""
set -e

# ── Clear update failure file on success ──────────────────
if [ -f "$HOME/.genesis/last_update_failure.json" ]; then
    rm -f "$HOME/.genesis/last_update_failure.json"
    echo "  Cleared previous update failure context"
fi

# ── Record success in update_history ─────────────────────
# Container update succeeded; $HOST_CC_DEGRADED (if set) flags a host-side
# Node/CC alignment gap as a degraded subsystem so it is surfaced, not silent.
if [ -n "$HOST_CC_DEGRADED" ]; then
    echo "  NOTE: recording degraded subsystem: $HOST_CC_DEGRADED"
fi
_record_update_history "success" "" "$HOST_CC_DEGRADED"

_write_state "done"

# Clean up state files — successful update, nothing to recover
rm -f "$STATE_FILE"
rm -f "$HOME/.genesis/update_conflicts.json"
rm -f "$HOME/.genesis/last_update_summary.txt"
# Clean up PID file
rm -f "$HOME/.genesis/update_in_progress.pid"

# ── Done ──────────────────────────────────────────────────
echo "  ──────────────────────────────────────"
echo "  Updated: $OLD_TAG ($OLD_COMMIT) → $NEW_TAG ($NEW_COMMIT)"
echo ""
