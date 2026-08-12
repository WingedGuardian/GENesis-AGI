#!/usr/bin/env bash
# opencode-wrapper.sh — single-instance opencode with automatic cleanup
#
# Ensures only one opencode session runs at a time.
# Kills any existing opencode processes before starting a new one.
# Traps EXIT so child processes die when the terminal closes.

set -euo pipefail

# Resolve HOME when unset: stripped-env/systemd/sandbox invocations can leave
# HOME unset, which under `set -u` aborts at the first ${HOME} use. Fall back
# to the passwd entry for the current uid (same source Path.home() uses); fail
# closed if unresolvable. See CC memory sandbox_shell_no_home.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

OPENCODE_BIN="${HOME}/.npm-global/bin/opencode"

kill_opencode() {
    local count
    count=$(pgrep -fc opencode-ai 2>/dev/null || true)
    if [ "$count" -gt 0 ]; then
        echo "[opencode-wrapper] Killing $count existing opencode process(es)..."
        pkill -f opencode-ai 2>/dev/null || true
        sleep 1
        # Force-kill any survivors (langservers can be stubborn)
        pkill -9 -f opencode-ai 2>/dev/null || true
    fi
}

cleanup() {
    echo ""
    echo "[opencode-wrapper] Cleaning up opencode processes..."
    kill_opencode
}

# Kill any existing instances before starting
kill_opencode

# Ensure cleanup on exit (terminal close, Ctrl+C, etc.)
trap cleanup EXIT INT TERM HUP

# Launch opencode with all arguments passed through
exec "$OPENCODE_BIN" "$@"
