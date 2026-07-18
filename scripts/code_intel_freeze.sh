#!/usr/bin/env bash
# code_intel_freeze.sh — the managed code-intelligence KILL-SWITCH.
#
#   Usage: code_intel_freeze.sh [repo_path]   (default: the repo this script lives in)
#
# Holds BOTH single-flight locks that gate all code-intel indexing, so while it
# runs NOTHING indexes:
#   1. the main repo's per-repo lock (same path code_intel_index.sh takes) — a
#      manual/hook entrypoint run for that repo hits the held lock and exits
#      CODE_INTEL_INDEX_LOCK_SKIP_RC (the idle runner sets 75 → "host-frozen,
#      keep the marker", never a false success);
#   2. the runner's self-lock — the idle runner (code_intel_runner.sh) exits its
#      tick immediately without processing ANY marker (any repo), so even a
#      second checkout can't sneak an index in.
#
# This is the repo-shipped replacement for the hand-armed `systemd-run` freeze
# transient that the 2026-07-13 load-spike postmortem flagged (rec #3): a
# kill-switch worth having is worth a unit file with arm/disarm + boot
# persistence. Arm/disarm as a systemd USER unit:
#
#   systemctl --user start   genesis-code-intel-freeze     # arm (this session)
#   systemctl --user enable  genesis-code-intel-freeze     # arm across reboots
#   systemctl --user stop    genesis-code-intel-freeze     # disarm
#   systemctl --user disable genesis-code-intel-freeze     # stop arming on boot
#
# The unit's ExecStartPre stops any in-flight index scope FIRST (kill-then-seal),
# so arming during a storm seals in ~a second instead of politely waiting out a
# 17-min full index. cbm 0.9 can't resume a killed full — acceptable for an
# emergency lever; the runner's full-backoff (index_marker.py) absorbs the
# aftermath and retries cheap fast passes until disarmed.
#
# The lock ACQUISITION recipe below is byte-for-byte the one in
# code_intel_index.sh / code_intel_runner.sh; the freeze E2E test
# (test_code_intel_freeze.py) drives the real entrypoint against a live armed
# freeze, so any drift in the recipe fails CI rather than silently no-op'ing.

set -u

REPO_PATH="${1:-}"
if [ -z "$REPO_PATH" ]; then
    # Default to the repo this script ships in (scripts/ -> repo root).
    REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fi
if [ ! -d "$REPO_PATH" ]; then
    printf '[code-intel-freeze] ERROR: repo path not a directory: %s\n' "$REPO_PATH" >&2
    exit 1
fi
# Physical path — the per-repo lock is keyed on it (must match the entrypoint's
# `cd && pwd -P`, so a symlinked spelling maps to the same lock).
REPO_PATH="$(cd "$REPO_PATH" && pwd -P)"

GENESIS_HOME="${GENESIS_HOME:-$HOME/.genesis}"
LOCK_DIR="$GENESIS_HOME/locks"
mkdir -p "$LOCK_DIR" 2>/dev/null || LOCK_DIR="${TMPDIR:-/tmp}"

# Same derivation as code_intel_index.sh (16-hex sha1 of the physical repo path)
# and code_intel_runner.sh (fixed runner self-lock name).
REPO_LOCK="$LOCK_DIR/code-intel-$(printf '%s' "$REPO_PATH" | sha1sum | cut -c1-16).lock"
RUNNER_LOCK="$LOCK_DIR/code-intel-runner.lock"

_log() { printf '%s [code-intel-freeze] %s\n' "$(date -Iseconds 2>/dev/null || date)" "$*"; }

if ! command -v flock >/dev/null 2>&1; then
    _log "ERROR: flock not available — cannot arm the freeze"
    exit 1
fi

# Acquire a lock on fd $1 for file $2, describing it as $3. Try non-blocking
# first (the common case: nothing indexing) so we can log a loud "waiting" line
# before we block — otherwise a mid-index arm looks like a silent hang.
_hold() {
    local fd="$1" file="$2" label="$3"
    eval "exec $fd>\"\$file\"" || { _log "ERROR: cannot open $label lock ($file)"; exit 1; }
    if ! flock -n "$fd"; then
        _log "waiting for in-flight indexing to release the $label lock…"
        flock "$fd"   # blocking — we WILL seize it (ExecStartPre already killed the scope)
    fi
    _log "held $label lock ($file)"
}

_hold 9 "$REPO_LOCK" "repo"
_hold 8 "$RUNNER_LOCK" "runner"

_log "ARMED — all code-intel indexing is frozen (repo=$REPO_PATH). Stop this unit to disarm."

# Hold the locks open until systemd stops us. The backgrounded `sleep` INHERITS
# lock fds 8/9, so it must be killed on the way out or an orphaned sleep keeps
# the locks held after we exit (systemd's cgroup SIGKILL would reap it, but the
# trap makes release deterministic for a plain SIGTERM too). Closing fds 8/9 in
# THIS shell releases the locks.
_sleep_pid=""
_disarm() { _log "disarming — releasing code-intel locks"; [ -n "$_sleep_pid" ] && kill "$_sleep_pid" 2>/dev/null; exit 0; }
trap _disarm TERM INT
sleep infinity &
_sleep_pid=$!
wait "$_sleep_pid"
