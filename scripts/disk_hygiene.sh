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
#   8b. Retention prune of ~/.genesis/sessions/<id>/ (>60d, whole dirs)
#      (per-session state + SessionStart context mirrors; age far exceeds any
#      live session, which rewrites last_prompt_time every prompt)
#      COUPLING: observability/snapshots/context_injection.py uses a NON-EMPTY
#      ~/.genesis/sessions as its proof that CC has ever run here — the guard
#      that stops a blind scan reporting a false all-clear. Emptying this
#      directory silently disarms that guard, so 60d is load-bearing for the
#      injection watcher too, not only for disk. Read that code before lowering.
#   9. Retention prune of ~/.genesis/output/retrieval_efficacy/*.md (>45d)
#      (WS2-0 retrieval-efficacy report; dated md per run — file-age prune)
#  10. Retention prune of ego_proposal_revisions (ego_reconcile config window)
#      → scripts/prune_proposal_revisions.py (PR-5 reconcile revision audit)
#  11. Retention prune of pending_issue_posts terminal rows (>30d)
#      → scripts/prune_contributor_issue_posts.py (Contributor Work-Log hold
#      store; held rows never pruned)
#  12. Retention prune of entity_merge_journal (>180d) → scripts/prune_entity_merge_journal.py
#      (reversibility snapshot store; generous window so unmerge_entity outlives
#      the mis-merge discovery horizon)
#
# Note: run under a hardened systemd sandbox (NoNewPrivileges, ProtectSystem=
# strict), so disk_reclaim's --system (/var, sudo) path is intentionally NOT
# passed here — it would no-op anyway. /var reclaim is the reactive path's job.
#
# Structured as functions + a guarded main() so tests can `source` this file to
# exercise a single step (e.g. prune_tmp) without running the whole groom.
set -uo pipefail

# Resolve HOME when unset: stripped-env/systemd/sandbox invocations can leave
# HOME unset, which under `set -u` aborts at the first ${HOME} use. Fall back
# to the passwd entry for the current uid (same source Path.home() uses); fail
# closed if unresolvable. See CC memory sandbox_shell_no_home.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

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

    # BEFORE the reaper, deliberately. This is the wall-clock floor for the
    # stranded-work sweep — the detector is normally spawned at session
    # boundaries, so a box that starts no sessions for days would answer "what
    # fell through the cracks?" from a stale board. But this run only happens at
    # all on a box quiet enough that the 60-minute debounce did not already
    # no-op it, which is exactly the idle box where the reaper below is most
    # likely to be deleting stale worktrees — and its restore path does not
    # reconstruct uncommitted state. Observing the world after the reaper had
    # cleared it would mean the one daily look never saw what was lost.
    echo "--- zero-drop stranded-work sweep ---"
    "$VENV_PY" "$REPO_DIR/scripts/zero_drop_worker.py" --trigger hygiene \
        || echo "zero_drop_worker exited $?"

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

    echo "--- zero-drop findings retention prune (resolved only, >45d) ---"
    "$VENV_PY" "$REPO_DIR/scripts/prune_zero_drop.py" --days 45 \
        || echo "prune_zero_drop exited $?"

    echo "--- contributor work-log terminal-row prune (>30d) ---"
    "$VENV_PY" "$REPO_DIR/scripts/prune_contributor_issue_posts.py" --days 30 \
        || echo "prune_contributor_issue_posts exited $?"

    echo "--- ego proposal-revision audit retention prune (ego_reconcile config) ---"
    "$VENV_PY" "$REPO_DIR/scripts/prune_proposal_revisions.py" \
        || echo "prune_proposal_revisions exited $?"

    echo "--- session state retention prune (>60d) ---"
    # ~/.genesis/sessions/<id>/ holds per-session state (charter.md,
    # last_prompt_time, cursors) and now the SessionStart context mirrors — up to
    # four per session, so this store grew from kilobytes to tens of kilobytes
    # per session and had no prune at all (MEASURED 2026-08-31: 588 dirs, 17 MB,
    # oldest from April).
    #
    # Whole directories, by DIRECTORY mtime, at 60 days.
    #
    # Be precise about what that predicate measures, because the obvious claim
    # is false: a directory's mtime tracks entry creation/removal, NOT in-place
    # rewrites of the files inside it. `last_prompt_time` is written with
    # Path.write_text (truncate-in-place), so a live session does NOT keep
    # bumping its directory's mtime. "It cannot take a running session because
    # the dir mtime is minutes old" would be wrong.
    #
    # What actually makes this safe is the MARGIN, measured on the live store:
    # dir mtime trails the newest contained file by at most ~1 day (588 dirs
    # sampled), against a 60-day threshold — ~57x the worst observed skew. Of
    # the 161 dirs older than 60d, none held a file newer than 60d.
    #
    # That margin is a SNAPSHOT, though, and it is not what the safety should
    # rest on: a session resumed after a long dormancy has an old directory
    # mtime and a brand-new last_prompt_time, and deleting it takes a LIVE
    # session's state. So take the predicate the paragraph above prescribes
    # instead of the margin that made it unnecessary — a directory is pruned
    # only when it contains NO file modified inside the window. Costs one extra
    # stat pass over ~160 candidate dirs, once a day.
    if [ -d "$HOME/.genesis/sessions" ]; then
        find "$HOME/.genesis/sessions" -mindepth 1 -maxdepth 1 -type d -mtime +60 2>/dev/null |
            while IFS= read -r _sess_dir; do
                # -print -quit: stop at the FIRST recent file; no need to walk
                # the rest of the directory to know it must be kept.
                if [ -n "$(find "$_sess_dir" -type f -mtime -60 -print -quit 2>/dev/null)" ]; then
                    continue
                fi
                rm -rf "$_sess_dir" || echo "sessions prune failed for $_sess_dir"
            done
    fi
    echo "--- entity merge-journal reversibility retention prune (>180d) ---"
    "$VENV_PY" "$REPO_DIR/scripts/prune_entity_merge_journal.py" --days 180 \
        || echo "prune_entity_merge_journal exited $?"

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
