#!/usr/bin/env bash
# Phase 6 — CI completeness backstop for .genesis-hook-versions.
#
# For every tracked hook, walk its FULL git history and assert that every
# version ever shipped on this branch has its sha256 recorded in
# .genesis-hook-versions. A version that shipped but is NOT recorded is a
# latent wedge: on a community install still carrying that version,
# scripts/hooks/sync-hooks.sh sees dst != src AND dst-hash not-in-ledger,
# mis-classifies it as "user-modified", and SKIPS it forever — the install
# never picks up the fixed hook.
#
# The per-commit pre-commit gate (scripts/check_hook_versions.sh) only records
# the CURRENT tree, so an intermediate version committed without running
# scripts/update_hook_versions.sh (or under `git commit --no-verify`) slips
# through. This script is the backstop that catches such a gap in CI.
#
# REQUIRES FULL HISTORY: a shallow clone (GitHub Actions default) sees only the
# tip commit, so the history walk would be empty and this check would falsely
# pass. It therefore SKIPS (exit 0, with a clear notice) on a shallow clone —
# the CI job that runs it MUST check out with fetch-depth: 0. This mirrors the
# graceful-degradation pattern in scripts/check_subsystem_map.py.
#
# Exit codes:
#     0 — every shipped hook version is recorded (or shallow clone → skipped)
#     1 — one or more shipped versions are missing from .genesis-hook-versions

set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSIONS_FILE="$REPO_ROOT/.genesis-hook-versions"

# Keep in lockstep with sync-hooks.sh HOOKS_TO_SYNC, check_hook_versions.sh,
# and update_hook_versions.sh.
TRACKED_HOOKS=(commit-msg post-commit pre-commit prepare-commit-msg pre-push)

cd "$REPO_ROOT" || { echo "ERROR: cannot cd to repo root $REPO_ROOT" >&2; exit 1; }

if [[ ! -f "$VERSIONS_FILE" ]]; then
    echo "ERROR: $VERSIONS_FILE not found — the hook version ledger is missing." >&2
    exit 1
fi

# Shallow clone → the history walk is meaningless; skip rather than false-pass.
if [[ "$(git rev-parse --is-shallow-repository 2>/dev/null)" == "true" ]]; then
    echo "NOTICE: shallow clone detected — skipping hook-version completeness check."
    echo "        (Run in a job that checks out with fetch-depth: 0 to enforce it.)"
    exit 0
fi

# Map "<hook>:<sha256>" → first-seen short commit, so a version that recurs
# across commits is reported ONCE (keyed on the hash, not per-commit).
declare -A MISSING
_EMPTY_SHA=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
for h in "${TRACKED_HOOKS[@]}"; do
    path="scripts/hooks/$h"
    # Every commit on this branch that touched the hook = every version shipped.
    # git log is newest-first; the -z guard below keeps the newest occurrence.
    for c in $(git log --format='%H' -- "$path" 2>/dev/null); do
        hash=$(git show "$c:$path" 2>/dev/null | sha256sum | awk '{print $1}')
        # git show prints nothing for a commit where the path was absent (e.g.
        # the delete side of a rename); sha256sum of empty input is the fixed
        # _EMPTY_SHA — skip it so it can't masquerade as a real missing version.
        [[ -z "$hash" || "$hash" == "$_EMPTY_SHA" ]] && continue
        key="$h:$hash"
        if ! grep -qxF "$key" "$VERSIONS_FILE"; then
            [[ -z "${MISSING[$key]:-}" ]] && MISSING[$key]="${c:0:12}"
        fi
    done
done

# NOTE: under `set -u`, an associative array with NO elements is treated as
# unbound, so ${#MISSING[@]} would error rather than yield 0. The ${MISSING[*]+x}
# guard expands to empty (→ -z true) only when the array has no elements.
if [[ -z "${MISSING[*]+x}" ]]; then
    echo "OK: all shipped versions of ${TRACKED_HOOKS[*]} are recorded in .genesis-hook-versions"
    exit 0
fi

echo "" >&2
echo "BLOCKED: shipped hook version(s) missing from .genesis-hook-versions:" >&2
# Emit on stdout so `sort` orders them, then redirect the sorted result to stderr
# (piping a loop whose body writes to >&2 would leave sort with empty input).
for key in "${!MISSING[@]}"; do
    echo "  - $key (first seen in ${MISSING[$key]})"
done | sort >&2
echo "" >&2
echo "A community install still carrying one of these versions would be wedged:" >&2
echo "sync-hooks.sh would treat the hook as user-modified and never update it." >&2
echo "" >&2
echo "Fix: append each missing '<hook>:<sha256>' line to .genesis-hook-versions" >&2
echo "(scripts/update_hook_versions.sh records the CURRENT tree; historical" >&2
echo "versions must be added by hand from the digests listed above)." >&2
echo "" >&2

exit 1
