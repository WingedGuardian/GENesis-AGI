# shellcheck shell=bash
# deploy_lock.sh — the deploy-station lock + deployed-SHA receipts (issue #1699).
#
# ONE lock file serializes the runtime + deploy pipeline across every path that
# mutates or measures the live tree:
#
#   * scripts/update.sh          — EXCLUSIVE, non-blocking (its historical
#                                  hard-fail contract is deliberately kept: a
#                                  queued full deploy firing minutes later on a
#                                  box nobody is watching is a surprise deploy;
#                                  update.sh keeps its own inline flock and
#                                  shares only the PATH constant below)
#   * scripts/deploy_code_only.sh — EXCLUSIVE, queues (bounded wait)
#   * validation / E2E runs       — SHARED, queue (scripts/run_under_deploy_lock.sh)
#
# Reader/writer semantics come from flock(2) itself: `-s` holders coexist with
# each other and exclude `-x`, so a deploy waits for in-flight validations and
# a validation waits for an in-flight deploy. There is no lock FILE to go stale:
# the kernel releases the lock when the last copy of the fd closes, so a crashed
# or SIGKILLed holder needs no cleanup — the same property pytest_lock.py
# documents for the test lock.
#
# That is NOT the same as "no stale hold is possible", and the earlier wording
# here said so wrongly. An fd is inherited by children, and the lock survives
# until EVERY copy closes — so a process that outlives its parent keeps the hold.
# MEASURED 2026-09-06: a backgrounded grandchild held the lock after the
# acquiring script exited, and a new acquirer was refused. Two consequences the
# callers handle rather than the lib: run_under_deploy_lock.sh closes the fd in
# the wrapped command, and the guardian renewer's residue is bounded (below).
#
# RECEIPTS: every deploy/validation appends one JSON line to
# ~/.genesis/deploy_receipts.jsonl — {ts, status, sha, path, by, note?} —
# so "validated" becomes an attributable claim about a specific serving SHA
# instead of a race (#1699's second ask). status: deployed | deploy_failed |
# health_failed | deployed_not_started (update.sh honouring an operator stop) |
# validated. update_history is deliberately NOT
# reused: it is tag-update-shaped (old_tag/new_tag rows driving the dashboard
# update view); code-only restarts and validation holds are a different kind of
# event, and this ordered cross-path ledger needs no migration. Retention:
# disk_hygiene.sh prunes the file to its newest _DEPLOY_RECEIPTS_KEEP lines
# daily (an unbounded store ships its prune path in the same PR).
#
# Sourced by deploy_code_only.sh, run_under_deploy_lock.sh, update.sh
# (receipts only), and disk_hygiene.sh (retention constant). Bash 4+.

GENESIS_DEPLOY_LOCK="${GENESIS_DEPLOY_LOCK:-${GENESIS_HOME:-$HOME/.genesis}/locks/update.lock}"
GENESIS_DEPLOY_RECEIPTS="${GENESIS_DEPLOY_RECEIPTS:-$HOME/.genesis/deploy_receipts.jsonl}"

# Exit code for "the wait timed out with the lock still held" — mirrors
# pytest_lock.py's EXIT_LOCK_HELD so operators meet one convention.
DEPLOY_LOCK_HELD_RC=200

# Retention: newest N receipt lines survive the daily disk-hygiene groom.
# Compatibility bound, not safety: ~150 bytes/line → ~300 KB ceiling, and at
# this install's observed deploy cadence (a few/day) that is years of history.
_DEPLOY_RECEIPTS_KEEP=2000

# _acquire_deploy_lock <-x|-s> <wait_seconds>
#   Opens the lock fd (kept in _DEPLOY_LOCK_FD; kernel-released when the LAST
#   copy of the fd closes — the acquiring process's own copy is what holds it
#   for the duration, so a caller that runs a child does NOT need the child to
#   inherit it, and should close it in the child: see the leak note below)
#   and blocks up to <wait_seconds> for the requested mode. Returns 0 holding
#   the lock, DEPLOY_LOCK_HELD_RC on timeout, 1 on setup failure.
#   Callers pick the wait: deploys use a short bound (a stuck deploy should
#   surface, not queue forever); validations use the 2h project floor.
_acquire_deploy_lock() {
    local mode="$1" wait_s="$2"
    mkdir -p "$(dirname "$GENESIS_DEPLOY_LOCK")" || return 1
    # Append-mode open: never truncates, and works for shared holders too.
    exec {_DEPLOY_LOCK_FD}>>"$GENESIS_DEPLOY_LOCK" || return 1
    local flock_rc=0
    flock "$mode" -w "$wait_s" "$_DEPLOY_LOCK_FD" || flock_rc=$?
    if [ "$flock_rc" -ne 0 ]; then
        exec {_DEPLOY_LOCK_FD}>&-
        # flock exits 1 when the timeout expires — that, and only that, is
        # contention. Anything else (a usage error, EMFILE, a signal) is a SETUP
        # failure, and reporting it as "the lock is held" sends the operator
        # hunting for a holder that does not exist (Kimi P3, 2026-09-06).
        [ "$flock_rc" -eq 1 ] && return "$DEPLOY_LOCK_HELD_RC"
        return 1
    fi
    return 0
}

acquire_deploy_lock_ex() { _acquire_deploy_lock -x "$1"; }
acquire_deploy_lock_sh() { _acquire_deploy_lock -s "$1"; }

# deploy_tree_is_linked_worktree <root>
#   True when <root> is a LINKED worktree (git worktree add) rather than the
#   main checkout: a linked worktree's git dir lives under the main repo's
#   .git/worktrees/, so --git-dir differs from --git-common-dir. A
#   path-SUBSTRING test misses worktrees at arbitrary locations
#   (`git worktree add /workspace/feature`), and pip install -e from one
#   redirects the live server's editable installation at it — the exact
#   hazard the callers' worktree refusals exist to prevent (Codex P2, #1804).
#   A non-git <root> answers FALSE: the callers' later rev-parse under set -e
#   already rejects that, and the test seam's fixture trees are plain repos.
deploy_tree_is_linked_worktree() {
    local root="$1" gd gcd
    gd="$(git -C "$root" rev-parse --absolute-git-dir 2>/dev/null)" || return 1
    gcd="$(cd "$root" 2>/dev/null && git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 1
    [ -n "$gd" ] && [ -n "$gcd" ] && [ "$gd" != "$gcd" ]
}

# deploy_tree_serving_diff <root>
#   Porcelain lines for everything that changes what an editable install
#   serves or a wrapped validation runs: every TRACKED change in the tree,
#   PLUS untracked files under code-bearing paths (src/, scripts/, tests/).
#   An untracked module under src/genesis is importable code — a bare
#   `--untracked-files=no` probe attributes it to HEAD, which the receipts
#   exist to prevent (Codex P2, #1804). Untracked files OUTSIDE those paths
#   stay excluded: the main checkout legitimately carries local scratch
#   (.local/, output dirs). Prints nothing on a clean tree; returns nonzero
#   when a git probe fails — callers must fail closed.
deploy_tree_serving_diff() {
    local root="$1"
    git -C "$root" status --porcelain --untracked-files=no || return
    # ls-files, not `status -- <paths>`: a pathspec with no match aborts
    # status, while ls-files --others simply lists nothing — the code-bearing
    # dirs may legitimately not exist in a fixture or sparse checkout.
    git -C "$root" ls-files --others --exclude-standard -- src scripts tests | sed 's/^/?? /' || return
}

# append_deploy_receipt <status> <sha> <path> [note]
#   status: deployed | deploy_failed | health_failed | deployed_not_started | validated
#   path: code-only | update.sh | validation
#   Values cross via the environment (never interpolated into code), matching
#   alert_queue.sh's injection-safe convention. Best-effort: a receipt failure
#   must never abort a deploy — but it says so on stderr rather than
#   disappearing (a silently missing receipt reads as "nothing happened").
append_deploy_receipt() {
    local status="$1" sha="$2" dpath="$3" note="${4:-}"
    # Make the write self-sufficient: append mode raises FileNotFoundError when the
    # parent directory is absent, and this function only WARNS on failure — so the
    # row would be dropped with a stderr line nobody reads. The deploy path happens
    # to mkdir ~/.genesis earlier via its state write, but a validation hold writes
    # no state, and an operator-set GENESIS_DEPLOY_RECEIPTS can name a fresh
    # directory. The ledger is the point of #1699 (CodeRabbit Minor, 2026-09-06).
    mkdir -p "$(dirname "$GENESIS_DEPLOY_RECEIPTS")" 2>/dev/null || true
    if ! RECEIPT_OUT="$GENESIS_DEPLOY_RECEIPTS" RECEIPT_STATUS="$status" \
         RECEIPT_SHA="$sha" RECEIPT_PATH="$dpath" RECEIPT_NOTE="$note" \
         python3 - <<'PY'
import datetime
import json
import os

# timezone.utc, not datetime.UTC: this runs under the SYSTEM python3 (like
# alert_queue.sh), and datetime.UTC needs >=3.11 — an install with system 3.10
# would fail every append and the ledger would never accumulate a line.
row = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "status": os.environ["RECEIPT_STATUS"],
    "sha": os.environ["RECEIPT_SHA"],
    "path": os.environ["RECEIPT_PATH"],
    "by": os.environ.get("USER") or "",
}
note = os.environ.get("RECEIPT_NOTE")
if note:
    row["note"] = note
# O_APPEND single write: atomic at this size on every POSIX filesystem we run.
with open(os.environ["RECEIPT_OUT"], "a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False) + "\n")
PY
    then
        echo "  WARNING: deploy receipt not written ($GENESIS_DEPLOY_RECEIPTS)" >&2
    fi
    return 0
}

# deploy_receipt_serving_ok <sha>
#   True when the ledger's newest DEPLOY-OUTCOME row for <sha> is `deployed` —
#   post-restart and health-checked, so the serving process provably loaded
#   that SHA. Tree identity alone cannot prove process identity: a pull that
#   advanced HEAD to B then failed before restart leaves the OLD process
#   serving while clean HEAD reads B (Devin #1804), so a `validated` receipt
#   gated only on HEAD falsely marks undeployed code. deploy_failed,
#   health_failed, deployed_not_started, and no deploy row at all all fail
#   closed. Validation rows are skipped — they describe what was tested, not
#   what is serving.
deploy_receipt_serving_ok() {
    local sha="$1"
    [ -f "$GENESIS_DEPLOY_RECEIPTS" ] || return 1
    RECEIPT_FILE="$GENESIS_DEPLOY_RECEIPTS" WANT_SHA="$sha" python3 - <<'PY'
import json
import os
import sys

latest = None
with open(os.environ["RECEIPT_FILE"], encoding="utf-8") as f:
    for line in f:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("sha") != os.environ["WANT_SHA"]:
            continue
        if row.get("status") in (
            "deployed", "deploy_failed", "health_failed", "deployed_not_started",
        ):
            latest = row["status"]
sys.exit(0 if latest == "deployed" else 1)
PY
}

# prune_deploy_receipts
#   Trim the receipts ledger to the newest _DEPLOY_RECEIPTS_KEEP lines. Lives
#   HERE, beside the append it bounds, rather than inline in disk_hygiene.sh:
#   the unbounded-growth it prevents is this file's own, the cap constant is
#   this file's, and inline in the groom script it was unreachable by any test —
#   so a drift in the tail/wc logic would silently stop pruning and nothing
#   would notice until the ledger was huge (Kimi P3, 2026-09-06).
#   Takes the lock EXCLUSIVE (nonblocking) so the rewrite cannot interleave with
#   an append: a busy station skips today's prune and bounded growth resumes
#   tomorrow, which is the right trade for a daily groom — queueing it behind a
#   2h validation hold would be backwards.
#   Returns 0 when it pruned or had nothing to do, 2 when the station was busy.
#   A lock SETUP failure (unusable lock path, flock operational error) propagates
#   the acquirer's 1 unchanged — mapping it to 2 would report a persistent fault
#   as a benign busy-station skip on every daily run, and the ledger would never
#   be pruned (Codex P3 / CodeRabbit Minor, #1804).
prune_deploy_receipts() {
    [ -f "$GENESIS_DEPLOY_RECEIPTS" ] || return 0
    local lock_rc=0
    acquire_deploy_lock_ex 0 || lock_rc=$?
    if [ "$lock_rc" -eq "$DEPLOY_LOCK_HELD_RC" ]; then
        return 2
    fi
    [ "$lock_rc" -eq 0 ] || return "$lock_rc"
    # A .tmp orphaned by a kill between tail and mv would sit forever; clear it
    # before (re)writing.
    rm -f "$GENESIS_DEPLOY_RECEIPTS.tmp"
    local lines
    lines="$(wc -l < "$GENESIS_DEPLOY_RECEIPTS" 2>/dev/null || echo 0)"
    if [ "$lines" -gt "$_DEPLOY_RECEIPTS_KEEP" ]; then
        # A rewrite failure (orphaned .tmp dir, full fs, denied mv) must reach
        # the caller — returning 0 here made disk_hygiene report success on
        # every run while the ledger grew past its cap (Codex P2, #1804).
        if ! { tail -n "$_DEPLOY_RECEIPTS_KEEP" "$GENESIS_DEPLOY_RECEIPTS" > "$GENESIS_DEPLOY_RECEIPTS.tmp" \
            && mv "$GENESIS_DEPLOY_RECEIPTS.tmp" "$GENESIS_DEPLOY_RECEIPTS"; }; then
            rm -f "$GENESIS_DEPLOY_RECEIPTS.tmp"
            return 1
        fi
    fi
    return 0
}
