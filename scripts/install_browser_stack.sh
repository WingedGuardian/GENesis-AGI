#!/usr/bin/env bash
# install_browser_stack.sh — provision what the browser tools launch.
#
# Runs genesis.browser.provision: the Camoufox engine the installed camoufox
# package pairs with, the Chromium the fallback launches, and the system libraries both need.
# Started by genesis-browser-stack.service (unattended: it never escalates, so a
# missing system library is reported with the command to run) and by an
# operator. Never from a Claude Code session: every session's MCP server shares
# the engine cache and the venv.
#
# Run by an operator, a missing system library is installed through `sudo -n`
# only where this user may already run commands as root without a password;
# otherwise the exact command is printed for the operator. Do not add a sudoers
# rule just for it: the command runs the venv's python, which this user can
# rewrite, so any such rule would hand that user root.
#
# Exit codes: the module's own (0 ready or deliberately skipped, 1 not ready),
# 0 when another run holds the lock (that run does the work), 75 when this run
# could not start at all.
set -uo pipefail
# Resolve HOME before the first dereference below. Under `set -u` a stripped
# environment aborts at the first `$HOME` expansion with "HOME: unbound
# variable", and a systemd unit is exactly the context where that can happen.
# Falls back to the passwd entry for the current uid — the same source
# Path.home() uses — and fails closed if even that is unresolvable.
# Enforced repo-wide by tests/test_scripts/test_home_guard_coverage.py.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || {
        printf 'browser-stack: HOME is unset and could not be resolved from passwd\n' >&2
        exit 75
    }
    export HOME
fi
GENESIS_HOME="${GENESIS_HOME:-$HOME/.genesis}"
REPO_DIR="${GENESIS_REPO_DIR:-$(unset CDPATH; cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_PY="$REPO_DIR/.venv/bin/python"
LOCK_DIR="$GENESIS_HOME/locks"

_log() { printf 'browser-stack: %s\n' "$1"; }

if [ ! -x "$VENV_PY" ]; then
    _log "no venv interpreter at $VENV_PY"
    exit 75
fi
if ! mkdir -p "$LOCK_DIR" 2>/dev/null; then
    _log "cannot create lock directory at $LOCK_DIR"
    exit 75
fi

# One run at a time: two fetches into one engine cache race on the same
# directory. A second run exits 0 because the first is doing the work.
RUNNER_LOCK="$LOCK_DIR/browser-stack.lock"
if ! command -v flock >/dev/null 2>&1 || ! { exec 8>"$RUNNER_LOCK"; } 2>/dev/null; then
    _log "runner lock unavailable (flock missing, or $RUNNER_LOCK unopenable)"
    exit 75
fi
lock_rc=0
flock -n -E 75 8 || lock_rc=$?
if [ "$lock_rc" -eq 75 ]; then
    _log "another run is in progress — exiting"
    exit 0
elif [ "$lock_rc" -ne 0 ]; then
    _log "lock acquisition failed (flock exit $lock_rc)"
    exit 75
fi

cd "$REPO_DIR" || { _log "repo directory $REPO_DIR is unreachable"; exit 75; }

# The runtime reads these from secrets.env (overriding its environment), so the
# oneshot must provision where it launches from. By name only: no other secret
# reaches the downloaders. Last assignment wins, as in systemd's own reader.
SECRETS_FILE="${SECRETS_PATH:-$REPO_DIR/secrets.env}"
_load_browser_setting() {
    local key="$1" line="" val
    [ -f "$SECRETS_FILE" ] || return 0
    line="$(grep -aE "^${key}=" "$SECRETS_FILE" | tail -1)" || line=""
    [ -n "$line" ] || return 0
    val="${line#*=}"
    case "$val" in
        \"*\") val="${val#\"}"; val="${val%\"}" ;;
        \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac
    [ -n "$val" ] && export "$key=$val"
    return 0
}
for _key in PLAYWRIGHT_BROWSERS_PATH XDG_CACHE_HOME CAMOUFOX_EXECUTABLE_PATH \
        GENESIS_BIG_TMP GENESIS_BROWSER_MIN_FREE_GB; do
    _load_browser_setting "$_key"
done
export GENESIS_BROWSER_STACK_LOCK_FD=8  # the module verifies this descriptor holds the lock
"$VENV_PY" -m genesis.browser.provision run
exit $?
