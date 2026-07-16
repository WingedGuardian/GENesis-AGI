#!/usr/bin/env bash
# genesis-disk-hygiene — daily disk grooming entrypoint.
#
# Run by the genesis-disk-hygiene.timer systemd unit (also runnable by hand).
# Best-effort steps — one failing must not skip the others:
#   1. Reap merged/inactive git worktrees  → scripts/worktree_lifecycle.py
#      (trash-bin with 7-day recovery; frees space when trash purges)
#   2. Reclaim regenerable caches          → scripts/disk_reclaim.py
#      (cheap tier always; medium/reindex tier only when disk >= 90%)
#   3. Reap orphaned background-CC sandboxes (~/tmp/bg-cc-sessions, 24h)
#   4. Age-prune ~/tmp direct children (>7d, excluding bg-cc-sessions)
#   5. Label-aware attention-snapshot GC   → scripts/attention_snapshot_gc.py
#      (home >60d / OMI >14d, but NEVER a snapshot a labeled event references)
#   6. Retention prune of immunity_shadow_events (>45d) → scripts/prune_immunity_shadow.py
#      (WS-3 B1 observe-only gate log; bounds the shadow store)
#   7. Retention prune of capability_shadow_events (>45d) → scripts/prune_capability_shadow.py
#      (WS-5 Discord observe-only gate log; bounds the shadow store)
#   8. Retention prune of session_ledger_shadow_* (>45d) → scripts/prune_ledger_shadow.py
#      (session-manager PR-3 ambient extractor shadow store; runs + events)
#
# Note: run under a hardened systemd sandbox (NoNewPrivileges, ProtectSystem=
# strict), so disk_reclaim's --system (/var, sudo) path is intentionally NOT
# passed here — it would no-op anyway. /var reclaim is the reactive path's job.
#
# Structured as functions + a guarded main() so tests can `source` this file to
# exercise a single step (e.g. prune_tmp) without running the whole groom.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

VENV_PY="$REPO_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    VENV_PY="$(command -v python3 || true)"
fi

# prune_tmp DIR — delete direct children of DIR older than 7d, EXCLUDING
# bg-cc-sessions (reaped at 24h below). Direct-children-only so a fresh file
# deep inside a kept dir can't be orphaned; whole one-off job dirs go atomically.
# CLAUDE.md: large one-off jobs legitimately live in ~/tmp, so be conservative —
# a >7d entry is safely dead (backup.sh's mktemp files self-clean well before).
prune_tmp() {
    local tmp_dir="${1:-$HOME/tmp}"
    [ -d "$tmp_dir" ] || return 0
    find "$tmp_dir" -mindepth 1 -maxdepth 1 \
        ! -name bg-cc-sessions \
        -mtime +7 \
        -exec rm -rf {} + 2>/dev/null || echo "tmp prune exited $?"
}

main() {
    if [ -z "$VENV_PY" ]; then
        echo "disk_hygiene: no python interpreter found" >&2
        exit 1
    fi

    echo "=== genesis-disk-hygiene $(date -u +%FT%TZ) ==="

    echo "--- worktree reaping ---"
    "$VENV_PY" "$REPO_DIR/scripts/worktree_lifecycle.py" || echo "worktree_lifecycle exited $?"

    echo "--- cache reclamation ---"
    "$VENV_PY" "$REPO_DIR/scripts/disk_reclaim.py" --apply --if-above 90 || echo "disk_reclaim exited $?"

    # Reap orphaned per-session background-CC sandboxes (~/tmp/bg-cc-sessions/<id>).
    # direct_session._run_session removes these in a finally on normal completion;
    # this catches orphans left when a session is hard-SIGKILLed (skips finally).
    # 24h is well past any live session: the Genesis-controlled max timeout is
    # 7200s/2h (CCInvocation.timeout_s); DirectSessionRequest defaults to 3600s/1h.
    echo "--- background CC sandbox reaping ---"
    BG_CC_SANDBOX_DIR="$HOME/tmp/bg-cc-sessions"
    if [ -d "$BG_CC_SANDBOX_DIR" ]; then
        find "$BG_CC_SANDBOX_DIR" -mindepth 1 -maxdepth 1 -type d -mmin +1440 \
            -exec rm -rf {} + 2>/dev/null || echo "bg-cc-sandbox reap exited $?"
    fi

    echo "--- ~/tmp age prune (>7d) ---"
    prune_tmp "$HOME/tmp"

    echo "--- attention snapshot GC (label-aware) ---"
    "$VENV_PY" "$REPO_DIR/scripts/attention_snapshot_gc.py" --home-days 60 --omi-days 14 \
        || echo "attention_snapshot_gc exited $?"

    echo "--- immunity shadow retention prune (>45d) ---"
    "$VENV_PY" "$REPO_DIR/scripts/prune_immunity_shadow.py" --days 45 \
        || echo "prune_immunity_shadow exited $?"

    echo "--- capability shadow retention prune (>45d) ---"
    "$VENV_PY" "$REPO_DIR/scripts/prune_capability_shadow.py" --days 45 \
        || echo "prune_capability_shadow exited $?"

    echo "--- ledger shadow retention prune (>45d) ---"
    "$VENV_PY" "$REPO_DIR/scripts/prune_ledger_shadow.py" --days 45 \
        || echo "prune_ledger_shadow exited $?"

    echo "--- repo pulse retention prune (>45d) ---"
    "$VENV_PY" "$REPO_DIR/scripts/prune_repo_pulse.py" --days 45 \
        || echo "prune_repo_pulse exited $?"

    echo "=== genesis-disk-hygiene done ==="
}

# Run main only when executed directly — lets tests `source` this file to call a
# single function (e.g. prune_tmp) without running the full groom.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
