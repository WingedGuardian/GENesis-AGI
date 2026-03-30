#!/usr/bin/env bash
# Test claude -p CLI output format for Genesis CC integration.
# Run OUTSIDE a Claude Code session (or it will fail with nesting error).
#
# Usage: bash scripts/test_cc_cli.sh
# Output: scripts/cc_cli_output/ directory with captured responses

set -euo pipefail

OUTDIR="$(dirname "$0")/cc_cli_output"
mkdir -p "$OUTDIR"

# Strip env vars that block nesting
export CLAUDECODE=
export CLAUDE_CODE_ENTRYPOINT=

echo "=== Test 1: --output-format json ==="
claude -p "Respond with exactly: hello world" --output-format json \
    > "$OUTDIR/test_json.txt" 2>&1 || true
echo "Saved to $OUTDIR/test_json.txt"
head -20 "$OUTDIR/test_json.txt"

echo ""
echo "=== Test 2: --output-format stream-json (requires --verbose) ==="
claude -p "Respond with exactly: hello world" --output-format stream-json --verbose \
    > "$OUTDIR/test_stream_json.txt" 2>&1 || true
echo "Saved to $OUTDIR/test_stream_json.txt"
echo "(stream-json requires --verbose with -p; Genesis uses json format instead)"
head -30 "$OUTDIR/test_stream_json.txt"

echo ""
echo "=== Test 3: --effort high ==="
claude -p "Respond with exactly: hello world" --output-format json --effort high \
    > "$OUTDIR/test_effort.txt" 2>&1 || true
echo "Saved to $OUTDIR/test_effort.txt"
head -20 "$OUTDIR/test_effort.txt"

echo ""
echo "=== Test 4: --system-prompt ==="
claude -p "What are you?" --output-format json \
    --system-prompt "You are Genesis. Respond in one sentence." \
    > "$OUTDIR/test_system_prompt.txt" 2>&1 || true
echo "Saved to $OUTDIR/test_system_prompt.txt"
head -20 "$OUTDIR/test_system_prompt.txt"

echo ""
echo "=== Done. Inspect $OUTDIR/ for raw output. ==="
echo "Check if 'type: result' JSON shape matches CCInvoker._parse_output()"
