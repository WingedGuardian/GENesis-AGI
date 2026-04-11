#!/usr/bin/env bash
# Phase 6 spike — step 3: idempotent upgrade path validation.
#
# Validates sync-hooks.sh across 5 scenarios:
#   1. Fresh install — no hook present, sync installs it
#   2. Stale install — old version present (known prior), sync updates it
#   3. User-modified install — unknown hash, sync skips with warning, exit 2
#   4. Idempotency — running sync twice is a no-op the second time
#   5. Worktree — sync correctly resolves git-common-dir
#
# All state isolated to ~/tmp throwaway repos.

set -u
set -o pipefail

PASS=0
FAIL=0
FAIL_DETAILS=()

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKTREE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYNC_TOOL="$WORKTREE_ROOT/scripts/hooks/sync-hooks.sh"
SRC_POST_COMMIT="$WORKTREE_ROOT/scripts/hooks/post-commit"
SRC_AUDIT_HELPER="$WORKTREE_ROOT/scripts/hooks/emit_bugfix_audit.py"

TEST_ROOT=$(mktemp -d ~/tmp/phase6-upgrade-XXXXXX)

_assert() {
    local label="$1"
    local cond="$2"
    if eval "$cond"; then
        PASS=$((PASS + 1))
        echo "  PASS: $label"
    else
        FAIL=$((FAIL + 1))
        FAIL_DETAILS+=("$label :: $cond")
        echo "  FAIL: $label"
    fi
}

# Build a fake repo that looks like Genesis — has scripts/hooks/ tracked,
# has a .genesis-hook-versions file, and is a git repo.
_make_fake_genesis_repo() {
    local dir="$1"
    mkdir -p "$dir/scripts/hooks"
    cp "$SRC_POST_COMMIT" "$dir/scripts/hooks/post-commit"
    cp "$SRC_AUDIT_HELPER" "$dir/scripts/hooks/emit_bugfix_audit.py"
    cp "$WORKTREE_ROOT/scripts/hooks/sync-hooks.sh" "$dir/scripts/hooks/sync-hooks.sh"
    chmod +x "$dir/scripts/hooks/"*.sh "$dir/scripts/hooks/"*.py 2>/dev/null
    cd "$dir"
    git init -q -b main
    git config user.email "spike@test"
    git config user.name "Spike Tester"
    git add scripts/
    git commit -q -m "initial"
}

# =========================================================================
echo "=== Test 1: Fresh install (no hook present) ==="
REPO1="$TEST_ROOT/fresh"
_make_fake_genesis_repo "$REPO1"

_assert "post-commit NOT present before sync" '[[ ! -f "$REPO1/.git/hooks/post-commit" ]]'

OUT=$("$REPO1/scripts/hooks/sync-hooks.sh" 2>&1)
RC=$?
_assert "sync exit code 0 on fresh install" '[[ $RC -eq 0 ]]'
_assert "sync reports installed: post-commit" '[[ "$OUT" == *"installed: post-commit"* ]]'
_assert "post-commit IS present after sync" '[[ -f "$REPO1/.git/hooks/post-commit" ]]'
_assert "post-commit is executable after sync" '[[ -x "$REPO1/.git/hooks/post-commit" ]]'
_assert "emit_bugfix_audit.py IS present after sync" '[[ -f "$REPO1/.git/hooks/emit_bugfix_audit.py" ]]'
_assert "content hashes match" '[[ "$(sha256sum "$REPO1/.git/hooks/post-commit" | awk "{print \$1}")" == "$(sha256sum "$REPO1/scripts/hooks/post-commit" | awk "{print \$1}")" ]]'

echo
echo "=== Test 2: Idempotency (run sync twice) ==="
OUT2=$("$REPO1/scripts/hooks/sync-hooks.sh" 2>&1)
RC2=$?
_assert "second sync exit code 0" '[[ $RC2 -eq 0 ]]'
_assert "second sync reports no 'installed:' (no-op)" '[[ "$OUT2" != *"installed:"* ]]'
_assert "second sync reports no 'updated:' (no-op)" '[[ "$OUT2" != *"updated:"* ]]'

echo
echo "=== Test 3: Stale install (known prior version) ==="
REPO3="$TEST_ROOT/stale"
_make_fake_genesis_repo "$REPO3"
# Install an OLD version of post-commit (simulate: user had old Phase 6 code)
cat > "$REPO3/.git/hooks/post-commit" <<'OLDHOOK'
#!/bin/bash
# old version — does nothing
exit 0
OLDHOOK
chmod +x "$REPO3/.git/hooks/post-commit"
OLD_HASH=$(sha256sum "$REPO3/.git/hooks/post-commit" | awk '{print $1}')

# Register this hash as a known prior version
echo "post-commit:$OLD_HASH" > "$REPO3/.genesis-hook-versions"

OUT3=$("$REPO3/scripts/hooks/sync-hooks.sh" 2>&1)
RC3=$?
_assert "sync exit code 0 on stale install" '[[ $RC3 -eq 0 ]]'
_assert "sync reports 'updated:' message" '[[ "$OUT3" == *"updated: post-commit"* ]]'
NEW_HASH=$(sha256sum "$REPO3/.git/hooks/post-commit" | awk '{print $1}')
SRC_HASH=$(sha256sum "$REPO3/scripts/hooks/post-commit" | awk '{print $1}')
_assert "post-commit now matches source" '[[ "$NEW_HASH" == "$SRC_HASH" ]]'
_assert "post-commit no longer matches old version" '[[ "$NEW_HASH" != "$OLD_HASH" ]]'

echo
echo "=== Test 4: User-modified install (unknown hash, SKIP with warning) ==="
REPO4="$TEST_ROOT/user-modified"
_make_fake_genesis_repo "$REPO4"
# Install a user-modified version — NOT in .genesis-hook-versions
cat > "$REPO4/.git/hooks/post-commit" <<'USERHOOK'
#!/bin/bash
# user's custom hook — should NOT be clobbered by sync
echo "my custom post-commit" >&2
exit 0
USERHOOK
chmod +x "$REPO4/.git/hooks/post-commit"
USER_HASH=$(sha256sum "$REPO4/.git/hooks/post-commit" | awk '{print $1}')

# Empty versions file (no known priors)
: > "$REPO4/.genesis-hook-versions"

OUT4=$("$REPO4/scripts/hooks/sync-hooks.sh" 2>&1)
RC4=$?
_assert "sync exit code 2 on user-modified hook" '[[ $RC4 -eq 2 ]]'
_assert "sync warns 'skipping post-commit'" '[[ "$OUT4" == *"skipping post-commit"* ]]'
PRESERVED_HASH=$(sha256sum "$REPO4/.git/hooks/post-commit" | awk '{print $1}')
_assert "user-modified hook was LEFT ALONE" '[[ "$PRESERVED_HASH" == "$USER_HASH" ]]'

echo
echo "=== Test 5: Worktree (git-common-dir resolution) ==="
REPO5="$TEST_ROOT/with-worktree"
_make_fake_genesis_repo "$REPO5"
# Create a worktree off main
cd "$REPO5"
git worktree add "$TEST_ROOT/wt1" -b feat/test-branch main 2>&1 >/dev/null
# Run sync from INSIDE the worktree, not the main repo
cd "$TEST_ROOT/wt1"
OUT5=$("$REPO5/scripts/hooks/sync-hooks.sh" 2>&1)
RC5=$?
# The hook should land in REPO5/.git/hooks (shared via git-common-dir),
# not in the worktree's fake .git/hooks
_assert "worktree sync exit code 0" '[[ $RC5 -eq 0 ]]'
_assert "post-commit installed in SHARED hooks dir" '[[ -f "$REPO5/.git/hooks/post-commit" ]]'
# Worktree's .git is a file, not a dir — there's no .git/hooks/ in the worktree itself
_assert "worktree .git is a file (not a dir)" '[[ -f "$TEST_ROOT/wt1/.git" ]]'

echo
echo "=== Test 6: Helper files (emit_bugfix_audit.py) always overwrite ==="
REPO6="$TEST_ROOT/helper-drift"
_make_fake_genesis_repo "$REPO6"
# Install a DIFFERENT version of the helper — no version tracking for helpers
echo "# stale helper" > "$REPO6/.git/hooks/emit_bugfix_audit.py"
chmod +x "$REPO6/.git/hooks/emit_bugfix_audit.py"
OUT6=$("$REPO6/scripts/hooks/sync-hooks.sh" 2>&1)
RC6=$?
_assert "sync exit code 0 on helper drift" '[[ $RC6 -eq 0 ]]'
_assert "helper gets overwritten" '[[ "$(sha256sum "$REPO6/.git/hooks/emit_bugfix_audit.py" | awk "{print \$1}")" == "$(sha256sum "$REPO6/scripts/hooks/emit_bugfix_audit.py" | awk "{print \$1}")" ]]'

# =========================================================================
echo
echo "================================="
echo "RESULT: $PASS passed, $FAIL failed"
echo "================================="
if [[ $FAIL -gt 0 ]]; then
    echo "Failures:"
    for d in "${FAIL_DETAILS[@]}"; do
        echo "  - $d"
    done
    echo
    echo "Test root kept for inspection: $TEST_ROOT"
    exit 1
fi

rm -rf "$TEST_ROOT"
echo "Cleanup OK. Spike step 3 PASSED."
exit 0
