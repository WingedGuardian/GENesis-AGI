#!/usr/bin/env bash
# code_intel_index.sh — the ONE entrypoint for code-intelligence indexing.
#
#   Usage: code_intel_index.sh <repo_path> [cbm|gitnexus|both]
#
# Every code-intel index spawn (codebase-memory-mcp cli index_repository,
# gitnexus analyze) MUST go through this script. Raw spawns caused a
# container-wedging incident: three concurrent full-repo indexers on one
# worktree saturated the container's disk-write throttle, piling every
# writer into D-state (load ~58 at 79% memory) and hanging sshd/incus-exec.
# A guardrail test (test_code_intel_index.py) fails any new raw spawn site.
#
# What this enforces, in order:
#   1. WORKTREE SKIP — linked git worktrees (.git is a file) are never
#      indexed: their code is ~identical to the main repo, each index costs
#      a full separate graph (~GBs + heavy writes), and Serena (live LSP)
#      covers worktree sessions without any index.
#   2. SINGLE-FLIGHT — a non-blocking flock per repo path: if an index for
#      this repo is already running, exit 0 immediately (never queue).
#   3. RESOURCE CAPS — both tools run inside a systemd user scope with
#      MemoryMax / MemorySwapMax=0 / IOWeight / CPUQuota, so even a single
#      index cannot starve the container. Where no systemd user manager is
#      reachable (CI, containers without a user bus), falls back to
#      nice/ionice + a soft address-space rlimit.
#
# The script runs the requested tools SEQUENTIALLY in the foreground and
# exits when done — callers that want fire-and-forget background it
# themselves (`( code_intel_index.sh "$repo" both ) & disown`), which keeps
# the lock held by the backgrounded process for its full lifetime.
#
# Env overrides:
#   CODE_INTEL_INDEX_MEMORY_MAX   default 2G     (per systemd scope)
#   CODE_INTEL_INDEX_IO_WEIGHT    default 20     (1-10000; low = polite)
#   CODE_INTEL_INDEX_CPU_QUOTA    default 200%   (2 cores worth)
#   CODE_INTEL_INDEX_DISABLE=1    skip all indexing (escape hatch)

set -u

REPO_PATH="${1:-}"
TOOLS="${2:-both}"

MEM_MAX="${CODE_INTEL_INDEX_MEMORY_MAX:-2G}"
IO_WEIGHT="${CODE_INTEL_INDEX_IO_WEIGHT:-20}"
CPU_QUOTA="${CODE_INTEL_INDEX_CPU_QUOTA:-200%}"

_log() { printf '[code-intel-index] %s\n' "$*"; }

if [ "${CODE_INTEL_INDEX_DISABLE:-0}" = "1" ]; then
    _log "disabled via CODE_INTEL_INDEX_DISABLE — skipping"
    exit 0
fi

if [ -z "$REPO_PATH" ] || [ ! -d "$REPO_PATH" ]; then
    _log "ERROR: repo path missing or not a directory: '$REPO_PATH'"
    exit 1
fi
case "$TOOLS" in cbm|gitnexus|both) ;; *)
    _log "ERROR: tool must be cbm|gitnexus|both, got '$TOOLS'"; exit 1 ;;
esac

# Physical path (-P): the single-flight lock is keyed on this, and a symlinked
# spelling of the same repo must not get a second lock (= second concurrent index).
REPO_PATH="$(cd "$REPO_PATH" && pwd -P)"

# ── 1. Worktree skip ────────────────────────────────────────────────────
# In a linked worktree, <root>/.git is a FILE (gitdir pointer), not a dir.
if [ -f "$REPO_PATH/.git" ]; then
    _log "skip: $REPO_PATH is a linked git worktree (never indexed — use Serena there)"
    exit 0
fi

# ── 2. Single-flight lock (per repo path) ───────────────────────────────
LOCK_DIR="${GENESIS_HOME:-$HOME/.genesis}/locks"
mkdir -p "$LOCK_DIR" 2>/dev/null || LOCK_DIR="${TMPDIR:-/tmp}"
LOCK_FILE="$LOCK_DIR/code-intel-$(printf '%s' "$REPO_PATH" | sha1sum | cut -c1-16).lock"

# Take the lock only when flock AND a writable lock file are both available.
# If either is missing, proceed UNLOCKED with a warning — a missing lock tool
# must degrade to "no dedup", never to "silently skip indexing" (a bare
# `flock -n 9` failure is indistinguishable from "lock held" otherwise).
if command -v flock >/dev/null 2>&1 && { exec 9>"$LOCK_FILE"; } 2>/dev/null; then
    if ! flock -n 9; then
        _log "skip: an index for $REPO_PATH is already running (lock held)"
        exit 0
    fi
else
    _log "WARNING: flock or lock file unavailable ($LOCK_FILE) — proceeding UNLOCKED"
fi

# ── 3. Resource-capped runner ───────────────────────────────────────────
# Probe systemd-run exactly like .claude/mcp/run-codebase-memory does: the
# probe must create a real scope, because CC-spawned / hook-spawned contexts
# sometimes cannot reach the user manager even when systemd-run exists.
_SCOPE_OK=0
if command -v systemd-run >/dev/null 2>&1; then
    if systemd-run --user --scope --quiet \
        -p "MemoryMax=${MEM_MAX}" -p "MemorySwapMax=0" \
        -p "IOWeight=${IO_WEIGHT}" -p "CPUQuota=${CPU_QUOTA}" \
        -- /bin/true 2>/dev/null; then
        _SCOPE_OK=1
    fi
fi

_run_capped() {
    if [ "$_SCOPE_OK" = "1" ]; then
        systemd-run --user --scope --quiet \
            -p "MemoryMax=${MEM_MAX}" -p "MemorySwapMax=0" \
            -p "IOWeight=${IO_WEIGHT}" -p "CPUQuota=${CPU_QUOTA}" \
            --description "code-intel index: $REPO_PATH" \
            -- "$@"
    else
        # Fallback: polite scheduling + soft address-space cap. Mirrors the
        # run-codebase-memory launcher's degradation (never block on missing
        # systemd — CI and minimal containers must still work).
        local mem_kb=""
        if [[ "$MEM_MAX" =~ ^([0-9]+)(\.[0-9]+)?([Gg])$ ]]; then
            mem_kb=$(( ${BASH_REMATCH[1]} * 1024 * 1024 ))
        elif [[ "$MEM_MAX" =~ ^([0-9]+)(\.[0-9]+)?([Mm])$ ]]; then
            mem_kb=$(( ${BASH_REMATCH[1]} * 1024 ))
        else
            _log "WARNING: cannot parse '$MEM_MAX' for the rlimit fallback — running memory-uncapped (nice/ionice only)"
        fi
        (
            [ -n "$mem_kb" ] && ulimit -v "$mem_kb" 2>/dev/null
            if command -v ionice >/dev/null 2>&1; then
                exec nice -n 19 ionice -c 3 "$@"
            else
                exec nice -n 19 "$@"
            fi
        )
    fi
}

RC=0

if [ "$TOOLS" = "cbm" ] || [ "$TOOLS" = "both" ]; then
    if command -v codebase-memory-mcp >/dev/null 2>&1; then
        _log "indexing (codebase-memory-mcp): $REPO_PATH"
        _run_capped codebase-memory-mcp cli index_repository \
            "{\"repo_path\": \"$REPO_PATH\"}" || RC=$?
    else
        _log "codebase-memory-mcp not on PATH — skipped"
    fi
fi

if [ "$TOOLS" = "gitnexus" ] || [ "$TOOLS" = "both" ]; then
    _GN=""
    if command -v gitnexus >/dev/null 2>&1; then
        _GN="gitnexus"
    elif command -v npx >/dev/null 2>&1; then
        _GN="npx gitnexus"
    fi
    if [ -n "$_GN" ]; then
        _log "indexing (gitnexus analyze): $REPO_PATH"
        ( cd "$REPO_PATH" && _run_capped $_GN analyze --quiet ) || RC=$?
    else
        _log "gitnexus not available — skipped"
    fi
fi

_log "done (rc=$RC): $REPO_PATH"
exit "$RC"
