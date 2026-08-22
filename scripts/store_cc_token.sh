#!/usr/bin/env bash
# store_cc_token.sh — intake a `claude setup-token` OAuth token used as a
# FALLBACK CC credential, by two consumers, when a primary `claude login` dies.
#
# The token (a 1-year `claude setup-token`, used via CLAUDE_CODE_OAUTH_TOKEN —
# NOT an ANTHROPIC_API_KEY) is stored 0600 to a DEDICATED container file and
# synced to the host shared mount by the credential-bridge awareness tick. It is
# a SHARED fallback, read by consumers on BOTH the host (the Guardian recovery
# brain, via guardian/diagnosis.py) AND the container itself (its own CC sessions,
# foreground + background, via cc/login_health.py) — each injecting it only as a
# fallback when its own login is down (see those modules for the exact activation
# gate); a HEALTHY login is never overridden. It is NOT host-Guardian-only. The
# authoritative, CI-enforced consumer list lives in
# docs/architecture/shared-artifacts.md (checked by
# scripts/check_shared_artifact_consumers.py) — update THAT when a consumer changes.
#
# The token is read from STDIN or a --file PATH (never a bare argv token — no
# shell-history / ps leak); with --file, create the source 0600 (`umask 077`) and
# `rm` it after, since it holds the raw token in plaintext. It is written 0600 to
# a DEDICATED file — NOT secrets.env,
# which is load_dotenv'd with override=True and would UNCONDITIONALLY set
# CLAUDE_CODE_OAUTH_TOKEN for every process that loads it, hijacking a HEALTHY
# container login (the dedicated file is instead injected conditionally by
# cc/login_health.py). This script prints ZERO token material.
#
# Usage:
#   claude setup-token | scripts/store_cc_token.sh      # store (stdin)
#   scripts/store_cc_token.sh --file /path/to/token      # store from a file
#   scripts/store_cc_token.sh --remove                  # remove everywhere
#   scripts/store_cc_token.sh --help

set -euo pipefail

# Resolve HOME robustly. This install's stripped-env interactive shells can
# leave HOME unset (a recurring pattern here — same class as the gh/update.sh
# "HOME: unbound variable" failures), which under `set -u` would abort at the
# first ${HOME} use below before any token is read. Fall back to the passwd
# entry for the current uid, then a conventional path, and export so any child
# inherits it.
if [ -z "${HOME:-}" ]; then
    # Resolve from passwd (field 6) — the SAME source Python's expanduser/
    # Path.home() uses when HOME is unset, so this script and credential_bridge.py
    # agree on the token path. Fail CLOSED if it can't be resolved: guessing
    # /home/<user> could write the credential where the bridge never reads it
    # (root=/root, custom homes), silently reporting success while Guardian
    # gets nothing.
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    if [ -z "$HOME" ]; then
        echo "ERROR: HOME is unset and could not be resolved from passwd." >&2
        echo "Re-run with HOME exported, e.g.:  HOME=\"\$(getent passwd \"\$(id -u)\" | cut -d: -f6)\" $0" >&2
        exit 1
    fi
    export HOME
fi

TOKEN_FILE="${HOME}/.genesis/cc_oauth_token.env"
SHARED_FILE="${HOME}/.genesis/shared/guardian/cc_oauth_token.env"

usage() {
    # Print the leading comment block (the header, after the shebang) up to the
    # first non-comment line — robust to the header's length (do NOT hardcode a
    # line range; that silently over-reads into code when the header grows).
    awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
}

read_token() {
    # Concatenate ALL non-whitespace from stdin into one token. A real setup-token
    # is a single line, but a copy-paste from a wrapped terminal can inject hard
    # newlines mid-token — stripping all whitespace rejoins it, instead of silently
    # storing only the first fragment (which would arm a BROKEN fallback credential
    # and only fail later, during an outage). Warn (stderr, never stdout, so the
    # captured token stays clean) if the input spanned >1 non-empty line, so a
    # genuinely multi-value paste is visible rather than silently merged.
    local line token="" nonempty=0
    while IFS= read -r line || [ -n "$line" ]; do
        line="$(printf '%s' "$line" | tr -d '[:space:]')"
        if [ -n "$line" ]; then
            nonempty=$((nonempty + 1))
            token="$token$line"
        fi
    done
    if [ "$nonempty" -gt 1 ]; then
        echo "WARNING: input spanned $nonempty non-empty lines — concatenated into one token." >&2
        echo "         A setup-token is a single line; verify the stored value is correct." >&2
    fi
    printf '%s' "$token"
}

TOKEN=""
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
    --file)
        # Read from a file instead of stdin. The token never touches argv (only
        # the PATH does), so there is no shell-history / ps leak.
        if [ -z "${2:-}" ]; then
            echo "ERROR: --file requires a PATH argument." >&2
            echo "  $0 --file /path/to/token-file" >&2
            exit 1
        fi
        if [ ! -f "$2" ] || [ ! -r "$2" ]; then
            echo "ERROR: --file: '$2' is not a readable regular file." >&2
            exit 1
        fi
        # Non-fatal nudge: the source file holds the raw token in plaintext. If it
        # is readable by group/other (e.g. created under the default umask 022) it
        # was an exposure — advise creating it 0600 and removing it after.
        src_mode="$(stat -c '%a' -- "$2" 2>/dev/null || true)"
        if [ -n "$src_mode" ] && [ "$(( 0${src_mode} & 077 ))" -ne 0 ]; then
            echo "WARNING: '$2' is readable by group/other — the token sat there in plaintext." >&2
            echo "         Create it with 'umask 077' (or 'install -m 600') and 'rm' it after storing." >&2
        fi
        TOKEN="$(read_token < "$2")"
        ;;
    "")
        if [ -t 0 ]; then
            echo "ERROR: token must be piped via stdin or supplied with --file, e.g.:" >&2
            echo "  claude setup-token | $0" >&2
            echo "  $0 --file /path/to/token-file" >&2
            exit 1
        fi
        TOKEN="$(read_token)"
        ;;
    *)
        echo "ERROR: unknown argument '$1'. The token comes from stdin or --file PATH — never a bare argument." >&2
        echo "  claude setup-token | $0" >&2
        echo "  $0 --file /path/to/token-file" >&2
        exit 1
        ;;
esac

if [ -z "$TOKEN" ]; then
    echo "ERROR: no token received (stdin/--file was empty)." >&2
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
