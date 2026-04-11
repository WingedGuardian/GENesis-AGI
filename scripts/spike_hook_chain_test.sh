#!/usr/bin/env bash
# Phase 6 spike — end-to-end hook chain smoke test.
#
# Validates that:
#   1. post-commit hook fires on `fix:` commits, writes a marker, writes an audit obs row
#   2. post-commit hook SKIPS `fix(local):`, `feat:`, `chore:`
#   3. contribution_offer_hook.py reads the marker, injects [Contribution] on stdout, unlinks it
#   4. After injection, the marker dir is empty and the offer hook is silent
#   5. Dedup: same commit hash doesn't double-insert the observation
#
# All state is isolated to a throwaway dir under ~/tmp (NOT /tmp — tmpfs is tiny).

set -u
set -o pipefail

PASS=0
FAIL=0
FAIL_DETAILS=()

# --- Throwaway paths ---
TEST_ROOT=$(mktemp -d ~/tmp/phase6-spike-XXXXXX)
export GENESIS_HOME="$TEST_ROOT/genesis-home"
export GENESIS_DB_PATH="$TEST_ROOT/test.db"
TEST_REPO="$TEST_ROOT/fake-repo"

# --- Locate the Phase 6 scripts (from the worktree this test lives in) ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKTREE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
POST_COMMIT="$WORKTREE_ROOT/scripts/hooks/post-commit"
OFFER_HOOK="$WORKTREE_ROOT/scripts/contribution_offer_hook.py"
AUDIT_HELPER="$WORKTREE_ROOT/scripts/hooks/emit_bugfix_audit.py"

# --- Setup ---
mkdir -p "$GENESIS_HOME" "$TEST_REPO"
cd "$TEST_REPO"
git init -q -b main
git config user.email "spike@test"
git config user.name "Spike Tester"

# Copy hooks into .git/hooks (bootstrap.sh pattern)
mkdir -p .git/hooks
cp "$POST_COMMIT" .git/hooks/post-commit
cp "$AUDIT_HELPER" .git/hooks/emit_bugfix_audit.py  # audit helper must be colocated
chmod +x .git/hooks/post-commit .git/hooks/emit_bugfix_audit.py

# Create a minimal observations table in the test DB
sqlite3 "$GENESIS_DB_PATH" "CREATE TABLE observations (
    id TEXT PRIMARY KEY,
    person_id TEXT,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    category TEXT,
    content TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    speculative INTEGER NOT NULL DEFAULT 0,
    retrieved_count INTEGER NOT NULL DEFAULT 0,
    influenced_action INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,
    resolution_notes TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    content_hash TEXT
);
CREATE INDEX idx_obs_source_hash ON observations(source, content_hash);"

# Seed file so we have something to commit
echo "initial" > README
git add README
git commit -q -m "chore: initial commit"

# Helper: assert
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

# Helper: count markers in pending dir
_marker_count() {
    local d="$GENESIS_HOME/pending-offers"
    [[ -d "$d" ]] || { echo 0; return; }
    find "$d" -maxdepth 1 -name '*.json' ! -name '.*' 2>/dev/null | wc -l
}

# Helper: count obs rows of given type
_obs_count() {
    local t="$1"
    sqlite3 "$GENESIS_DB_PATH" "SELECT COUNT(*) FROM observations WHERE type='$t';"
}

# Helper: wait briefly for async audit helper (runs in background)
_wait_async() {
    sleep 0.3
}

# =========================================================================
echo "=== Test 1: fix: commit writes marker + observation ==="
echo "change1" >> README
git add README
git commit -q -m "fix: correct the README grammar"
_wait_async
_assert "marker count == 1 after fix: commit" '[[ "$(_marker_count)" == "1" ]]'
_assert "observation count == 1 after fix: commit" '[[ "$(_obs_count bugfix_committed)" == "1" ]]'

echo
echo "=== Test 2: contribution_offer_hook injects [Contribution] and unlinks ==="
HOOK_OUTPUT=$(python3 "$OFFER_HOOK" 2>/dev/null)
_assert "offer hook stdout contains '[Contribution]'" '[[ "$HOOK_OUTPUT" == *"[Contribution]"* ]]'
_assert "offer hook stdout mentions commit subject" '[[ "$HOOK_OUTPUT" == *"README grammar"* ]]'
_assert "marker count == 0 after offer hook" '[[ "$(_marker_count)" == "0" ]]'

echo
echo "=== Test 3: offer hook is silent when no pending offers ==="
HOOK_OUTPUT2=$(python3 "$OFFER_HOOK" 2>/dev/null)
_assert "offer hook silent when pending-offers empty" '[[ -z "$HOOK_OUTPUT2" ]]'

echo
echo "=== Test 4: fix(scope): also triggers ==="
echo "change2" >> README
git add README
git commit -q -m "fix(parser): handle unicode in input"
_wait_async
_assert "marker present after fix(parser): commit" '[[ "$(_marker_count)" == "1" ]]'
_assert "observation count == 2 after second fix commit" '[[ "$(_obs_count bugfix_committed)" == "2" ]]'
# drain for next test
python3 "$OFFER_HOOK" > /dev/null 2>&1 || true

echo
echo "=== Test 5: fix(local): is OPT-OUT — no marker, no observation ==="
echo "change3" >> README
git add README
git commit -q -m "fix(local): personal config tweak"
_wait_async
_assert "marker count == 0 after fix(local): commit" '[[ "$(_marker_count)" == "0" ]]'
_assert "observation count still == 2 after fix(local):" '[[ "$(_obs_count bugfix_committed)" == "2" ]]'

echo
echo "=== Test 6: feat: does NOT trigger ==="
echo "change4" >> README
git add README
git commit -q -m "feat: add a new section"
_wait_async
_assert "marker count == 0 after feat: commit" '[[ "$(_marker_count)" == "0" ]]'
_assert "observation count still == 2 after feat:" '[[ "$(_obs_count bugfix_committed)" == "2" ]]'

echo
echo "=== Test 7: chore: does NOT trigger ==="
echo "change5" >> README
git add README
git commit -q -m "chore: bump dependency"
_wait_async
_assert "marker count == 0 after chore: commit" '[[ "$(_marker_count)" == "0" ]]'
_assert "observation count still == 2 after chore:" '[[ "$(_obs_count bugfix_committed)" == "2" ]]'

echo
echo "=== Test 8: dedup — same content_hash doesn't double-insert ==="
# Re-running the audit helper with the same inputs should be a no-op.
python3 "$AUDIT_HELPER" "deadbeefcafe" "fix: duplicate test" 2>/dev/null
BEFORE=$(_obs_count bugfix_committed)
python3 "$AUDIT_HELPER" "deadbeefcafe" "fix: duplicate test" 2>/dev/null
AFTER=$(_obs_count bugfix_committed)
_assert "dedup: second audit call did not add a row" '[[ "$BEFORE" == "$AFTER" ]]'

echo
echo "=== Test 9: latency — offer hook returns <100ms with empty pending dir ==="
# Measure 10 invocations
START=$(date +%s%N)
for i in 1 2 3 4 5 6 7 8 9 10; do
    python3 "$OFFER_HOOK" > /dev/null 2>&1
done
END=$(date +%s%N)
ELAPSED_NS=$(( END - START ))
ELAPSED_MS=$(( ELAPSED_NS / 1000000 ))
PER_INVOCATION_MS=$(( ELAPSED_MS / 10 ))
echo "  10 invocations took ${ELAPSED_MS}ms (${PER_INVOCATION_MS}ms per call)"
_assert "per-invocation latency <100ms (empty path)" '[[ "$PER_INVOCATION_MS" -lt 100 ]]'

echo
echo "=== Test 10: malformed marker is cleaned up gracefully ==="
mkdir -p "$GENESIS_HOME/pending-offers"
echo "not-json-garbage" > "$GENESIS_HOME/pending-offers/bad.json"
HOOK_OUTPUT3=$(python3 "$OFFER_HOOK" 2>/dev/null)
_assert "offer hook silent on malformed marker" '[[ -z "$HOOK_OUTPUT3" ]]'
_assert "malformed marker was unlinked" '[[ "$(_marker_count)" == "0" ]]'

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

# Cleanup on success
rm -rf "$TEST_ROOT"
echo "Cleanup OK. Spike step 2 PASSED."
exit 0
