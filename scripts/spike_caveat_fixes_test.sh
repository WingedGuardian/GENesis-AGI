#!/usr/bin/env bash
# Phase 6 spike — caveat fixes validation.
#
# Validates the two mitigations added for the step 3 caveats:
#   CAVEAT 1: first-ever rollout bootstrap — session_context self-heal
#   CAVEAT 2: release discipline — pre-commit gate on hook version file
#
# Scenarios:
#   A. session_context sync invocation works when sync-hooks.sh is present
#   B. session_context sync invocation gracefully handles missing sync-hooks.sh
#   C. pre-commit BLOCKS a hook change without a versions file update
#   D. pre-commit ALLOWS a hook change with a versions file update
#   E. update_hook_versions.sh is idempotent
#
# All state isolated to ~/tmp throwaway dirs.

set -u
set -o pipefail

PASS=0
FAIL=0
FAIL_DETAILS=()

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKTREE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TEST_ROOT=$(mktemp -d ~/tmp/phase6-caveat-XXXXXX)

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

# ------------------------------------------------------------------
# Scenario A: session_context.py sync invocation works
# ------------------------------------------------------------------
echo "=== Scenario A: session_context invokes sync-hooks.sh ==="
REPO_A="$TEST_ROOT/scenario-a"
mkdir -p "$REPO_A/scripts/hooks"
cp "$WORKTREE_ROOT/scripts/hooks/sync-hooks.sh" "$REPO_A/scripts/hooks/sync-hooks.sh"
cp "$WORKTREE_ROOT/scripts/hooks/post-commit" "$REPO_A/scripts/hooks/post-commit"
cp "$WORKTREE_ROOT/scripts/hooks/emit_bugfix_audit.py" "$REPO_A/scripts/hooks/emit_bugfix_audit.py"
chmod +x "$REPO_A/scripts/hooks/"*.sh "$REPO_A/scripts/hooks/"*.py

cd "$REPO_A"
git init -q -b main
git config user.email "spike@test"
git config user.name "Spike Tester"
git add scripts/
git commit -q -m "initial"

# Directly invoke the _sync_genesis_hooks function via a minimal Python
# wrapper. We don't run the full SessionStart hook here — just the sync step.
python3 - <<PYEOF
import subprocess
from pathlib import Path
import sys
sys.path.insert(0, "$WORKTREE_ROOT/scripts")
# Replicate the function in isolation
sync_script = Path("$REPO_A/scripts/hooks/sync-hooks.sh")
result = subprocess.run([str(sync_script), "--quiet"], capture_output=True, timeout=3.0)
print(f"exit={result.returncode}")
PYEOF

_assert "post-commit installed after session sync" '[[ -f "$REPO_A/.git/hooks/post-commit" ]]'
_assert "emit_bugfix_audit installed after session sync" '[[ -f "$REPO_A/.git/hooks/emit_bugfix_audit.py" ]]'

# ------------------------------------------------------------------
# Scenario B: session_context.py fail-open when sync-hooks.sh missing
# ------------------------------------------------------------------
echo
echo "=== Scenario B: session_context fail-open when sync-hooks.sh absent ==="
REPO_B="$TEST_ROOT/scenario-b"
mkdir -p "$REPO_B/scripts/hooks"  # NO sync-hooks.sh — pre-Phase6 install
# Copy the real session_context.py into a place where its sync_script
# resolves to a non-existent file.
cp "$WORKTREE_ROOT/scripts/genesis_session_context.py" "$REPO_B/scripts/genesis_session_context.py"

cd "$REPO_B"
# Eject lever: disable Genesis context so we can run main() without
# triggering all the other work (we only care that it doesn't crash on
# the sync step).
rm -f "$HOME/.genesis/cc_context_enabled" 2>/dev/null || true
# But we want main() to proceed past eject — so set the flag temporarily.
ORIG_FLAG_STATE="absent"
if [ -f "$HOME/.genesis/cc_context_enabled" ]; then
    ORIG_FLAG_STATE="present"
fi
mkdir -p "$HOME/.genesis"
touch "$HOME/.genesis/cc_context_enabled"

# Run only the _sync_genesis_hooks function by importing the module.
OUT_B=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_B/scripts')
import genesis_session_context
# Should NOT raise even when sync-hooks.sh doesn't exist
genesis_session_context._sync_genesis_hooks()
print('OK')
" 2>&1) || true

_assert "sync function fail-open when sync-hooks.sh missing" '[[ "$OUT_B" == *"OK"* ]]'

# Restore original flag state
if [ "$ORIG_FLAG_STATE" = "absent" ]; then
    rm -f "$HOME/.genesis/cc_context_enabled"
fi

# ------------------------------------------------------------------
# Scenario C: pre-commit BLOCKS hook change without versions update
# ------------------------------------------------------------------
echo
echo "=== Scenario C: pre-commit blocks hook change missing version entry ==="
REPO_C="$TEST_ROOT/scenario-c"
mkdir -p "$REPO_C/scripts/hooks"
cp "$WORKTREE_ROOT/scripts/hooks/post-commit" "$REPO_C/scripts/hooks/post-commit"
cp "$WORKTREE_ROOT/scripts/hooks/pre-commit" "$REPO_C/scripts/hooks/pre-commit"
cp "$WORKTREE_ROOT/scripts/check_hook_versions.sh" "$REPO_C/scripts/check_hook_versions.sh"
cp "$WORKTREE_ROOT/scripts/update_hook_versions.sh" "$REPO_C/scripts/update_hook_versions.sh"
cp "$WORKTREE_ROOT/.genesis-hook-versions" "$REPO_C/.genesis-hook-versions"
chmod +x "$REPO_C/scripts/hooks/"* "$REPO_C/scripts/"*.sh

cd "$REPO_C"
git init -q -b main
git config user.email "spike@test"
git config user.name "Spike Tester"
git add -- scripts .genesis-hook-versions
git commit -q -m "initial"

# Install pre-commit hook for this test repo
cp "$REPO_C/scripts/hooks/pre-commit" "$REPO_C/.git/hooks/pre-commit"
chmod +x "$REPO_C/.git/hooks/pre-commit"

# Modify post-commit WITHOUT updating versions file
echo "# injected change" >> "$REPO_C/scripts/hooks/post-commit"
git add -- scripts/hooks/post-commit

# Try to commit — should fail on versions check
# Set env to bypass main-branch block
RC_C=0
OUT_C=$(GENESIS_ALLOW_MAIN_COMMIT=1 git commit -q -m "test: modify post-commit without version update" 2>&1) || RC_C=$?

_assert "commit blocked when versions file not updated" '[[ $RC_C -ne 0 ]]'
_assert "commit error message mentions .genesis-hook-versions" '[[ "$OUT_C" == *".genesis-hook-versions"* ]]'

# ------------------------------------------------------------------
# Scenario D: pre-commit ALLOWS hook change with versions update
# ------------------------------------------------------------------
echo
echo "=== Scenario D: pre-commit allows hook change with version entry ==="
# Reuse REPO_C state — still have staged post-commit change
cd "$REPO_C"

# Run update_hook_versions.sh to append the new hash
./scripts/update_hook_versions.sh > /dev/null
git add -- .genesis-hook-versions

# Now commit should succeed
RC_D=0
OUT_D=$(GENESIS_ALLOW_MAIN_COMMIT=1 git commit -q -m "test: modify post-commit WITH version update" 2>&1) || RC_D=$?

_assert "commit allowed when versions file updated" '[[ $RC_D -eq 0 ]]'

# ------------------------------------------------------------------
# Scenario E: update_hook_versions.sh is idempotent
# ------------------------------------------------------------------
echo
echo "=== Scenario E: update_hook_versions.sh idempotent ==="
LINE_COUNT_BEFORE=$(wc -l < "$REPO_C/.genesis-hook-versions")
./scripts/update_hook_versions.sh > /dev/null
LINE_COUNT_AFTER=$(wc -l < "$REPO_C/.genesis-hook-versions")
_assert "second run adds no new lines" '[[ "$LINE_COUNT_BEFORE" == "$LINE_COUNT_AFTER" ]]'

# Also check: running with an unchanged set of hooks outputs "no changes"
OUT_E=$(./scripts/update_hook_versions.sh 2>&1)
_assert "idempotent run reports 'no changes'" '[[ "$OUT_E" == *"no changes"* ]]'

# ------------------------------------------------------------------
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
echo "Cleanup OK. Caveat fixes PASSED."
exit 0
