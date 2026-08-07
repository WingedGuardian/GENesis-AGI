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
#   9. Retention prune of ~/.genesis/output/retrieval_efficacy/*.md (>45d)
#      (WS2-0 retrieval-efficacy report; dated md per run — file-age prune)
#  10. Retention prune of ego_proposal_revisions (ego_reconcile config window)
#      → scripts/prune_proposal_revisions.py (PR-5 reconcile revision audit)
#  11. Retention prune of pending_issue_posts terminal rows (>30d)
#      → scripts/prune_contributor_issue_posts.py (Contributor Work-Log hold
#      store; held rows never pruned)
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

# prune_mcp_spawn DIR — remove ~/.genesis/mcp-spawn/<slot> files whose recorded
# session pid (first token) is no longer alive. These pin a CC session's MCP
# spawn commit so the dashboard can render a stale-code badge; once the session
# is gone the file is stale (a new session on the slot overwrites it, so this
# only cleans ENDED slots). Bounded by slot count regardless — this keeps it
# tidy and reclaims a slot that is never reused. Also sweeps leftover atomic-
# write temp files (.slot.XXXX) from a crashed write.
prune_mcp_spawn() {
    local dir="${1:-$HOME/.genesis/mcp-spawn}"
    [ -d "$dir" ] || return 0
    local f pid
    for f in "$dir"/*; do
        [ -f "$f" ] || continue          # literal glob on empty dir → skip
        pid="$(awk '{print $1; exit}' "$f" 2>/dev/null)"
        case "$pid" in
            ''|*[!0-9]*) rm -f "$f" 2>/dev/null ;;               # malformed
            *) kill -0 "$pid" 2>/dev/null || rm -f "$f" 2>/dev/null ;;  # dead pid
        esac
    done
    find "$dir" -maxdepth 1 -type f -name '.*' -mmin +60 -delete 2>/dev/null || true
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

    echo "--- mcp-spawn identity prune (dead-pid slots) ---"
    prune_mcp_spawn "$HOME/.genesis/mcp-spawn"

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

    echo "--- contributor work-log terminal-row prune (>30d) ---"
    "$VENV_PY" "$REPO_DIR/scripts/prune_contributor_issue_posts.py" --days 30 \
        || echo "prune_contributor_issue_posts exited $?"

    echo "--- ego proposal-revision audit retention prune (ego_reconcile config) ---"
    "$VENV_PY" "$REPO_DIR/scripts/prune_proposal_revisions.py" \
        || echo "prune_proposal_revisions exited $?"

    echo "--- retrieval-efficacy report retention prune (>45d) ---"
    # WS2-0: retrieval_efficacy_report.py writes a dated md per run; bound the
    # dir so a periodic report never slow-leaks disk on a smaller install.
    if [ -d "$HOME/.genesis/output/retrieval_efficacy" ]; then
        find "$HOME/.genesis/output/retrieval_efficacy" -maxdepth 1 -type f \
            -name '*.md' -mtime +45 -delete 2>/dev/null \
            || echo "retrieval_efficacy prune exited $?"
    fi

    echo "--- memory-reconcile ghost-export retention prune (>45d) ---"
    # The nightly reconcile lane writes a date-stamped JSONL per run day
    # (date-stamped precisely so this age prune works — an append-forever file
    # would refresh its mtime every run); d0008's one-shot export ages out the
    # same way. The exports are a recovery net, not an archive.
    if [ -d "$HOME/.genesis/output" ]; then
        find "$HOME/.genesis/output" -maxdepth 1 -type f \
            \( -name 'memory_reconcile_ghost_export-*.jsonl' -o -name 'd0008_ghost_export.jsonl' \) \
            -mtime +45 -delete 2>/dev/null \
            || echo "reconcile ghost-export prune exited $?"
    fi

    echo "=== genesis-disk-hygiene done ==="
}

# Run main only when executed directly — lets tests `source` this file to call a
# single function (e.g. prune_tmp) without running the full groom.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
