#!/usr/bin/env bash
# store_cc_token.sh — intake a `claude setup-token` OAuth token for the host
# Guardian recovery brain's FALLBACK authentication.
#
# The host Guardian's `claude -p` recovery brain authenticates via a one-time
# manual `claude login` (no refresh). If that login dies, the brain goes dark.
# A `claude setup-token` 1-year OAuth token (used via CLAUDE_CODE_OAUTH_TOKEN —
# NOT an ANTHROPIC_API_KEY) stored here is synced to the host by the
# credential-bridge awareness tick and injected by diagnosis.py ONLY as a
# fallback when the host's own login is dead (a working login is never touched).
#
# The token is read from STDIN ONLY (never argv — no shell-history / ps leak),
# written 0600 to a DEDICATED file (NOT secrets.env, which is load_dotenv'd with
# override=True and would hijack the container's own CC auth — see
# credential_bridge.py), and stamped with its creation epoch. This script prints
# ZERO token material.
#
# Usage:
#   claude setup-token | scripts/store_cc_token.sh     # store (stdin only)
#   scripts/store_cc_token.sh --remove                 # remove everywhere
#   scripts/store_cc_token.sh --help

set -euo pipefail

TOKEN_FILE="${HOME}/.genesis/cc_oauth_token.env"
SHARED_FILE="${HOME}/.genesis/shared/guardian/cc_oauth_token.env"

usage() {
    sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
    --remove)
        removed=0
        for f in "$TOKEN_FILE" "$SHARED_FILE"; do
            if [ -f "$f" ]; then
                rm -f "$f"
                removed=1
            fi
        done
        if [ "$removed" -eq 1 ]; then
            echo "CC OAuth token removed (container source + shared mount)."
            echo "The host will fall back to its own \`claude login\`."
        else
            echo "No CC OAuth token file found — nothing to remove."
        fi
        exit 0
        ;;
    "")
        : # fall through to stdin intake
        ;;
    *)
        echo "ERROR: unknown argument '$1'. Token must be piped via stdin — never passed as an argument." >&2
        echo "  claude setup-token | $0" >&2
        exit 1
        ;;
esac

if [ -t 0 ]; then
    echo "ERROR: token must be piped via stdin (never an argument), e.g.:" >&2
    echo "  claude setup-token | $0" >&2
    exit 1
fi

# Read the first non-empty line (setup-token prints one token line). Trailing
# no-newline is handled by the `|| [ -n "$line" ]` guard.
TOKEN=""
while IFS= read -r line || [ -n "$line" ]; do
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    if [ -n "$line" ]; then
        TOKEN="$line"
        break
    fi
done

if [ -z "$TOKEN" ]; then
    echo "ERROR: no token received on stdin." >&2
    exit 1
fi

# Sanity-check the shape but do not hard-fail (the CLI format could evolve).
case "$TOKEN" in
    sk-ant-oat*) : ;;
    *) echo "WARNING: token does not start with 'sk-ant-oat' — storing anyway." >&2 ;;
esac

mkdir -p "$(dirname "$TOKEN_FILE")"
CREATED=$(date +%s)
OLD_UMASK=$(umask)
umask 077
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\nGENESIS_CC_TOKEN_CREATED_AT=%s\n' "$TOKEN" "$CREATED" > "$TOKEN_FILE.tmp"
chmod 600 "$TOKEN_FILE.tmp"
mv "$TOKEN_FILE.tmp" "$TOKEN_FILE"
umask "$OLD_UMASK"

WHEN=$(date -d "@$CREATED" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "$CREATED")
echo "CC OAuth token stored: ${#TOKEN} chars, created_at=$CREATED ($WHEN)."
echo "File: $TOKEN_FILE (0600). It syncs to the host on the next awareness tick (~5 min)."
echo "The host uses it ONLY as a fallback when its own \`claude login\` is dead."
