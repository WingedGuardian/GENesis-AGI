#!/usr/bin/env bash
# genesis-disk-hygiene — daily disk grooming entrypoint.
#
# Run by the genesis-disk-hygiene.timer systemd unit (also runnable by hand).
# Best-effort steps — one failing must not skip the others:
#   1. Reap merged/inactive git worktrees  → scripts/worktree_lifecycle.py
#      (archives into ~/.genesis/worktree-trash; first releases session-claim
#      locks whose process is gone — GENESIS_WORKTREE_STALE_CLAIM_RELEASE=0
#      disables that. Archive retention (--expire-trash) is deliberately NOT
#      run here: it stays off until it has a content-verified predicate, #2504)
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
#  13. Size trim of the hook audit stores (>5MB each) → scripts/prune_hook_audit_logs.py
#      (merge-override + git-discard records, one file per flush — oldest whole
#      files dropped; an age prune cannot bound an append-forever store)
#  14. Retention prune of ~/.genesis/output/guard-corpus.jsonl (>45d)
#      (the guard replay corpus and any temp an interrupted rebuild left; it is
#      regenerable, and it holds verbatim command lines — see prune_guard_corpus)
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

# shellcheck source=scripts/lib/tmp_liveness.sh
. "$SCRIPT_DIR/lib/tmp_liveness.sh"

# prune_tmp DIR [AGE_MIN] — delete direct children of DIR not modified for more
# than AGE_MIN minutes (default: the historical `-mtime +7`), EXCLUDING
# bg-cc-sessions (reaped at 24h below). Direct-children-only so a fresh file
# deep inside a kept dir can't be orphaned; whole one-off job dirs go atomically.
# CLAUDE.md: large one-off jobs legitimately live in ~/tmp, so be conservative —
# a >7d entry is safely dead (backup.sh's mktemp files self-clean well before).
#
# A child some process still holds OPEN is spared whatever its age. Age alone
# cannot see a download or unpack that started days ago and is still running,
# and the pressure mode below prunes at 2 days, where that stops being
# hypothetical. The liveness signal and its limits (same-uid only, and a writer
# that closes between files is invisible) are documented in tmp_liveness.sh.
prune_tmp() {
    local tmp_dir="${1:-$HOME/tmp}" age_min="${2:-}"
    [ -d "$tmp_dir" ] || return 0
    # CANONICAL: /proc reports resolved paths (see sweep_cc_tmp).
    tmp_dir="$(cd -P -- "$tmp_dir" 2>/dev/null && pwd -P)" || return 0
    local -a age_pred=(-mtime +7) recent_pred=(-mtime -8)
    if [[ "$age_min" =~ ^[0-9]+$ ]]; then
        age_pred=(-mmin "+$age_min")
        recent_pred=(-mmin "-$age_min")
    fi
    local snap child mounts
    if ! liveness_visible; then
        echo "tmp prune SKIPPED: no other process is visible in /proc, so nothing can be proven unused"
        return 0
    fi
    if ! mounts="$(mount_targets "$tmp_dir")"; then
        echo "tmp prune SKIPPED: the mount table (/proc/self/mountinfo) is unreadable, so no tree can be proven free of mounts"
        return 0
    fi
    snap="$(live_open_paths)"
    while IFS= read -r -d '' child; do
        # A child that is, or holds, a mount (a separately mounted or
        # bind-mounted ~/tmp/downloads) frees nothing here; never recurse into
        # it (#2521 item 6). The table lists every mount, same device or not.
        if tree_holds_mount "$child" "$mounts" 1; then
            echo "tmp prune: sparing $child (a separate filesystem)"
            continue
        fi
        if [ -d "$child" ] && dir_has_live_writer "$child" "$snap"; then
            echo "tmp prune: sparing $child (held open by a live process)"
            continue
        fi
        if [ ! -d "$child" ] && path_is_held "$child" "$snap"; then
            echo "tmp prune: sparing $child (held open by a live process)"
            continue
        fi
        # A directory's OWN mtime moves only when direct children are added or
        # removed, so a tree written deep inside (a session scratchpad under
        # ~/tmp/claude-<uid>/) looks old while it is in daily use. Judge it by
        # the newest thing anywhere inside it.
        if [ -d "$child" ] && [ -n "$(find "$child" -mindepth 1 "${recent_pred[@]}" -print -quit 2>/dev/null)" ]; then
            echo "tmp prune: sparing $child (modified inside the window)"
            continue
        fi
        # The ONLY way a tree is removed: remove_tree_one_fs re-checks the
        # table this pass read at its start (not a fresh read).
        remove_tree_one_fs "$child" "$mounts" 1 || echo "tmp prune failed or spared $child"
    done < <(find "$tmp_dir" -mindepth 1 -maxdepth 1 \
                ! -name bg-cc-sessions "${age_pred[@]}" -print0 2>/dev/null)
}

# reap_bg_sandboxes DIR — remove per-session background-CC sandboxes (DIR/<id>)
# untouched for 24 h. 24 h is well past any live session (the Genesis-controlled
# max timeout is 2 h), but a surviving child of an ended session can still run
# in or hold one, so each is spared when a live process holds it or uses it as
# its cwd — and the whole reap is refused when liveness is blind, like every
# other deleter here (review finding).
reap_bg_sandboxes() {
    local dir="$1" snap d mounts
    [ -d "$dir" ] || return 0
    dir="$(cd -P -- "$dir" 2>/dev/null && pwd -P)" || return 0
    if ! liveness_visible; then
        echo "bg-cc sandbox reap SKIPPED: no other process is visible in /proc"
        return 0
    fi
    if ! mounts="$(mount_targets "$dir")"; then
        echo "bg-cc sandbox reap SKIPPED: the mount table (/proc/self/mountinfo) is unreadable, so no tree can be proven free of mounts"
        return 0
    fi
    snap="$(live_open_paths)"
    while IFS= read -r -d '' d; do
        if tree_holds_mount "$d" "$mounts" 1; then
            echo "bg-cc sandbox reap: sparing $d (a separate filesystem)"
            continue
        fi
        if dir_has_live_writer "$d" "$snap"; then
            echo "bg-cc sandbox reap: sparing $d (held open or in use by a live process)"
            continue
        fi
        remove_tree_one_fs "$d" "$mounts" 1 || echo "bg-cc sandbox reap failed or spared $d"
    done < <(find "$dir" -mindepth 1 -maxdepth 1 -type d -mmin +1440 -print0 2>/dev/null)
}

# reclaim_lock / reclaim_unlock — serialize the steps that DELETE (cache
# reclamation, sandbox reaping, the ~/tmp prune) across the daily groom and
# both pressure instances. systemd serializes starts of ONE unit only, so
# pressure@standard, pressure@last-resort and the daily unit could otherwise
# traverse and delete the same trees at once (review finding).
#
# reclaim_lock WAIT_S waits at most WAIT_S seconds: 0 = held (or the lock file
# cannot even be opened because the disk is full — then reclaim runs
# unserialized, since freeing space beats ordering); 1 = another reclaim still
# holds it. Every wait is bounded so it fits inside the caller's systemd
# timeout; each caller decides what a timeout means (pressure_main, main).
RECLAIM_LOCK="${RECLAIM_LOCK:-$HOME/.genesis/disk_hygiene_reclaim.lock}"
reclaim_lock() {
    local wait_s="${1:-60}"
    if ! { exec 8>"$RECLAIM_LOCK"; } 2>/dev/null; then
        echo "reclaim lock unavailable ($RECLAIM_LOCK) — running unserialized"
        return 0
    fi
    flock -w "$wait_s" 8 && return 0
    return 1
}
reclaim_unlock() { { exec 8>&-; } 2>/dev/null || true; }

# pressure_main [--last-resort] — the reclaim subset the tmp watchgod runs when
# the disk itself is in trouble (its ORANGE tier; RED adds --last-resort). Only
# the steps that FREE space, and each one more aggressive than the daily run:
# caches regardless of the usage gate, ~/tmp at 2 days instead of 7, and at RED
# the code-intel indexes too. Deliberately NOT the worktree reaper (it moves
# worktrees to a trash bin, which frees nothing until the purge) nor any of the
# database retention prunes (they free megabytes and take the DB lock).
#
# Started as a systemd template unit (genesis-disk-hygiene-pressure@<tier>)
# so it runs under the same sandbox as the daily groom; reclaim_lock keeps it
# from overlapping the other instance or the daily run. The watchgod never
# deletes anything itself.
pressure_main() {
    local last_resort="${1:-}"
    # The standard (ORANGE) pass pins --last-resort-above past 100 %. That is
    # disk_reclaim.py's own default now (#2567), but it stays explicit: an
    # ORANGE pass that inherited the old 95 % default deleted the code-intel
    # indexes before RED ever fired (review finding, #2521 item 1). Only the
    # last-resort (RED) pass clears them.
    local -a reclaim=(--apply --if-above 0 --fail-above 101 --last-resort-above 101)
    # "last-resort" is the systemd instance name (%i); "--last-resort" the CLI form.
    if [ "$last_resort" = "--last-resort" ] || [ "$last_resort" = "last-resort" ]; then
        reclaim=(--apply --if-above 0 --fail-above 101 --last-resort-above 0)
    fi
    echo "=== genesis-disk-hygiene PRESSURE ${last_resort:-} $(date -u +%FT%TZ) ==="
    # Standard waits up to one watchgod re-trigger interval and then yields —
    # the reclaim already running is doing this work. Last-resort (RED) waits
    # briefly and then runs regardless: at RED, freeing space beats ordering.
    if [ "$last_resort" = "--last-resort" ] || [ "$last_resort" = "last-resort" ]; then
        reclaim_lock "${RECLAIM_WAIT_S:-120}" \
            || echo "reclaim lock still held after ${RECLAIM_WAIT_S:-120}s — last-resort runs unserialized"
    elif ! reclaim_lock "${RECLAIM_WAIT_S:-600}"; then
        echo "another reclaim is still running after ${RECLAIM_WAIT_S:-600}s — skipping this standard pass"
        echo "=== genesis-disk-hygiene PRESSURE done ==="
        return 0
    fi
    echo "--- cache reclamation (usage gate off) ---"
    "$VENV_PY" "$REPO_DIR/scripts/disk_reclaim.py" "${reclaim[@]}" \
        || echo "disk_reclaim exited $?"
    echo "--- background CC sandbox reaping ---"
    reap_bg_sandboxes "$HOME/tmp/bg-cc-sessions"
    echo "--- ~/tmp age prune (>2d, live writers spared) ---"
    prune_tmp "$HOME/tmp" 2880
    reclaim_unlock
    echo "=== genesis-disk-hygiene PRESSURE done ==="
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

prune_guard_corpus() {
    # scripts/replay_guard_corpus.py caches every distinct Bash (command, cwd)
    # pair from this install's transcripts so it can replay them through a guard.
    # That cache is REGENERABLE — a missing one costs a rebuild, nothing else —
    # and it holds verbatim command lines, which demonstrably include secrets
    # passed in argv. It is written 0600 for that reason. Left alone it is a
    # tens-of-megabytes file that no longer has a reader between measurements, so
    # it ages out here rather than living forever by default. Size is stated
    # relatively because it tracks the transcript tree, which only grows:
    # MEASURED 2026-09-10 it was 67.6 MB for 140,293 rows, against the ~30 MB
    # this comment claimed five days earlier.
    #
    # The temps are swept too, and that is not incidental: the rebuild writes
    # through mkstemp (guard-corpus.jsonl.XXXXXX.tmp) and unlinks its own temp on
    # failure, but a SIGKILL mid-write leaves one behind holding the same
    # commands with none of the value.
    local out_dir="${1:-$HOME/.genesis/output}"
    [ -d "$out_dir" ] || return 0
    find "$out_dir" -maxdepth 1 -type f \
        \( -name 'guard-corpus.jsonl' -o -name 'guard-corpus.jsonl.*.tmp' \) \
        -mtime +45 -delete 2>/dev/null \
        || echo "guard-corpus prune exited $?"
}

main() {
    local disk_reclaim_rc=0
    if [ -z "$VENV_PY" ]; then
        echo "disk_hygiene: no python interpreter found" >&2
        exit 1
    fi

    case "${1:-}" in
        --pressure) pressure_main "${2:-}"; return 0 ;;
        "") ;;
        *) echo "disk_hygiene: unknown argument '$1' (usage: disk_hygiene.sh [--pressure [--last-resort]])" >&2
           return 2 ;;
    esac

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

    # Stale claims BEFORE the reaper, so a worktree whose claiming session died
    # is judged on its merits tonight rather than pinned for another day. The
    # claim module decides staleness; a lock it did not write is never touched.
    echo "--- stale worktree-claim release ---"
    "$VENV_PY" "$REPO_DIR/scripts/worktree_lifecycle.py" --release-stale-claims \
        || echo "worktree_lifecycle --release-stale-claims exited $?"

    echo "--- worktree reaping ---"
    "$VENV_PY" "$REPO_DIR/scripts/worktree_lifecycle.py" || echo "worktree_lifecycle exited $?"

    # The deleting steps take the shared reclaim lock. A pressure run holding
    # it is already reclaiming, so after a bounded wait (inside this unit's
    # 1200s timeout) the daily pass skips them rather than time out and lose
    # every retention prune below.
    local reclaim_locked=1
    if ! reclaim_lock "${RECLAIM_WAIT_S:-180}"; then
        reclaim_locked=0
        echo "--- reclaim steps SKIPPED: a pressure reclaim still holds the lock ---"
    fi
    if [ "$reclaim_locked" -eq 1 ]; then
    echo "--- cache reclamation ---"
    # --last-resort-above 101: the code-intel indexes are cleared only by the
    # guardian's RED (last-resort) pass, never by the daily groom on a disk
    # that happens to be at 95 % (review finding on #2521 item 1).
    "$VENV_PY" "$REPO_DIR/scripts/disk_reclaim.py" --apply --if-above 90 \
        --fail-above 95 --last-resort-above 101 || disk_reclaim_rc=$?
    if [ "$disk_reclaim_rc" -ne 0 ]; then
        echo "disk_reclaim exited $disk_reclaim_rc"
    fi

    # Reap orphaned per-session background-CC sandboxes (~/tmp/bg-cc-sessions/<id>).
    # direct_session._run_session removes these in a finally on normal completion;
    # this catches orphans left when a session is hard-SIGKILLed (skips finally).
    # 24h is well past any live session: the Genesis-controlled max timeout is
    # 7200s/2h (CCInvocation.timeout_s); DirectSessionRequest defaults to 3600s/1h.
    echo "--- background CC sandbox reaping ---"
    reap_bg_sandboxes "$HOME/tmp/bg-cc-sessions"

    echo "--- ~/tmp age prune (>7d) ---"
    prune_tmp "$HOME/tmp"
    reclaim_unlock
    fi

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
    # Same guarded removal as every other recursive deleter: a session
    # directory that is, or holds, a mount is spared (review finding on #2570
    # -- --one-file-system alone misses a same-device bind mount). CANONICAL
    # root, like prune_tmp: the mount table holds resolved paths, so a
    # symlinked ancestor would hide a mount below a session directory.
    if ! _sess_root="$(cd -P -- "$HOME/.genesis/sessions" 2>/dev/null && pwd -P)"; then
        :
    elif ! _sess_mounts="$(mount_targets "$_sess_root")"; then
        echo "sessions prune SKIPPED: the mount table (/proc/self/mountinfo) is unreadable, so no tree can be proven free of mounts"
    else
        while IFS= read -r -d '' _sess_dir; do
            # -print -quit: stop at the FIRST recent file; no need to walk
            # the rest of the directory to know it must be kept.
            if [ -n "$(find "$_sess_dir" -type f -mtime -60 -print -quit 2>/dev/null)" ]; then
                continue
            fi
            remove_tree_one_fs "$_sess_dir" "$_sess_mounts" 1 \
                || echo "sessions prune failed or spared $_sess_dir"
        done < <(find "$_sess_root" -mindepth 1 -maxdepth 1 -type d -mtime +60 -print0 2>/dev/null)
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

    echo "--- update.sh ephemeral-file backup retention prune (>45d) ---"
    # update.sh saves local edits to the tracked ephemeral files it discards before
    # its merge (AGENTS.md, config/procedure_triggers.yaml) under one directory per
    # run. Written once and never touched again, so a directory's mtime is its age.
    # Same guarded removal as every other recursive deleter (remove_tree_one_fs,
    # which spares a tree that is or holds a mount), over the CANONICAL root.
    if ! _pmb_root="$(cd -P -- "$HOME/.genesis/premerge-backups" 2>/dev/null && pwd -P)"; then
        :
    elif ! _pmb_mounts="$(mount_targets "$_pmb_root")"; then
        echo "premerge-backups prune SKIPPED: the mount table (/proc/self/mountinfo) is unreadable, so no tree can be proven free of mounts"
    else
        while IFS= read -r -d '' _pmb_dir; do
            remove_tree_one_fs "$_pmb_dir" "$_pmb_mounts" 1 \
                || echo "premerge-backups prune failed or spared $_pmb_dir"
        done < <(find "$_pmb_root" -mindepth 1 -maxdepth 1 -type d -mtime +45 -print0 2>/dev/null)
    fi

    echo "--- hook audit store size trim (>5MB per store, newest kept) ---"
    # The two store knobs, read BY NAME out of secrets.env.
    #
    # The writers see them because genesis-server loads that file; this unit does
    # not, so without this the trim resolved the DEFAULT directories on an install
    # that had configured custom ones — the real store growing with no retention
    # while the timer reported success on an empty default (Codex P2, PR #1609).
    #
    # Two variables rather than `EnvironmentFile=` on the unit, for two reasons.
    # MEASURED on systemd 255: an EnvironmentFile overrides `Environment=`
    # regardless of directive order, so loading secrets.env would silently replace
    # the unit's deliberately-pinned gh/git PATH on any install whose secrets.env
    # sets PATH. And this oneshot runs `rm -rf` and `find -delete`; it has no
    # business holding provider keys or the backup passphrase.
    #
    # No `eval` and no `source`: both execute file content, and this file is the
    # one place on the box that holds every credential.
    _load_store_knob() {
        local key="$1"
        local line=""
        local val
        [ -f "$REPO_DIR/secrets.env" ] || return 0
        # Last assignment wins, which is how systemd reads these files too.
        line="$(grep -aE "^${key}=" "$REPO_DIR/secrets.env" | tail -1)" || line=""
        [ -n "$line" ] || return 0
        val="${line#*=}"
        case "$val" in
            \"*\") val="${val#\"}"; val="${val%\"}" ;;
            \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac
        [ -n "$val" ] && export "$key=$val"
        return 0
    }
    _load_store_knob GENESIS_MERGE_OVERRIDE_DIR
    _load_store_knob GENESIS_DISCARD_SNAPSHOT_DIR
    # One file per hook flush, so the oldest whole files are deleted past the byte
    # bound. This is the shape the ghost-export note above explains an age prune
    # cannot handle for an append-forever file. Retention lives here, never on the
    # hook path: a guard returning a security verdict must not also groom a store.
    "$VENV_PY" "$REPO_DIR/scripts/prune_hook_audit_logs.py" --max-bytes 5000000 \
        || echo "prune_hook_audit_logs exited $?"

    echo "--- guard replay corpus retention prune (>45d) ---"
    prune_guard_corpus "$HOME/.genesis/output"

    echo "=== genesis-disk-hygiene done ==="
    return "$disk_reclaim_rc"
}

# Run main only when executed directly — lets tests `source` this file to call a
# single function (e.g. prune_tmp) without running the full groom.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
