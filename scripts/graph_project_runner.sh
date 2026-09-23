#!/usr/bin/env bash
# graph_project_runner.sh — scheduled rebuild of the FalkorDB memory-graph projection.
#
# Run hourly by genesis-graph-project.timer. This is F3's first slice: the bound
# on how stale the projection is allowed to get.
#
# WHY A SCHEDULE AND NOT AN INVALIDATION. `FalkorGraphStore.invalidate()` is a
# no-op by construction — the projection lives in the engine, not in the writer's
# process, so the 13 `memory_links` writers that call it have nothing to mark
# dirty. The projection is therefore stale from one build until the next, serving
# removed links and omitting new ones, and NOT raising, because a stale
# projection is indistinguishable from a current one from the engine's side. The
# store's own docstring states the consequence and names the cadence: "The window
# is what the cutover has to accept, or close first." This closes it to one hour.
# Write-level freshness needs a DB-side change signal (issue #1641) and is F3's
# second slice, not this one.
#
# EVERY INSTALL RUNS THIS TIMER. bootstrap.sh enables every rendered *.timer
# except genesis-backup.timer, so this fires on installs that have never
# provisioned a graph engine and never will. That is why the entrypoint is
# invoked with --if-armed: no engine is a clean exit 0 with one line, never an
# hourly failure. An install that HAS an engine gets real exit codes.
#
# NO ENGINE-SPECIFIC PLACEHOLDERS ANYWHERE IN THIS UNIT'S TEMPLATE. bootstrap.sh
# substitutes exactly four tokens — __HOME__, __VENV__, __REPO_DIR__ and
# __CC_BIN_DIR__ — and passes anything else through verbatim. A unit template
# that invented its own token would render with the token still in it on every
# install, which is a failure this repo has already had once.

set -uo pipefail

# Resolve HOME before the first dereference below. Under `set -u` a stripped
# environment aborts at the first `$HOME` expansion with "HOME: unbound
# variable", and a systemd unit is exactly the context where that can happen.
# Falls back to the passwd entry for the current uid — the same source
# Path.home() uses — and fails closed if even that is unresolvable.
# Enforced repo-wide by tests/test_scripts/test_home_guard_coverage.py; see CC
# memory sandbox_shell_no_home.
#
# 75, not the house snippet's 1, for consistency with this file's own exit
# contract: an unresolvable HOME is a tick that could not run, which is what 75
# means here.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || {
        printf 'graph-project: HOME is unset and could not be resolved from passwd\n' >&2
        exit 75
    }
    export HOME
fi

GENESIS_HOME="${GENESIS_HOME:-$HOME/.genesis}"
REPO_DIR="${GENESIS_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_PY="$REPO_DIR/.venv/bin/python"
LOCK_DIR="$GENESIS_HOME/locks"

_log() { printf 'graph-project: %s\n' "$1"; }

if [ ! -x "$VENV_PY" ]; then
    # An install whose venv is missing or half-built. Not this timer's problem
    # to report every hour — bootstrap is where that surfaces.
    _log "no venv interpreter at $VENV_PY — nothing to do"
    exit 0
fi

# One tick at a time. A projection is a full rebuild; two concurrent ones would
# duplicate the work and the engine's transient memory for no benefit. A tick
# that finds the lock held exits 0: the work is already being done, which is not
# a failure, and the next hour's tick will find a current projection.
if ! mkdir -p "$LOCK_DIR" 2>/dev/null; then
    # EX_TEMPFAIL, not 0. On an armed install this is usually PERMANENT (a
    # read-only or full $GENESIS_HOME), and exiting 0 would report hourly
    # SUCCESS forever while the projection silently stopped being rebuilt —
    # which is unobservable downstream, since a stale projection and a current
    # one are indistinguishable from the engine's side. Same code the sibling
    # runner uses for the same class.
    _log "cannot create lock directory at $LOCK_DIR"
    exit 75
fi
RUNNER_LOCK="$LOCK_DIR/graph-project-runner.lock"
if ! command -v flock >/dev/null 2>&1 || ! { exec 8>"$RUNNER_LOCK"; } 2>/dev/null; then
    # Also permanent-shaped: flock absent from the unit PATH, or the lock file
    # unopenable. Reported, not swallowed.
    _log "runner lock unavailable (flock missing, or $RUNNER_LOCK unopenable)"
    exit 75
fi
lock_rc=0
flock -n -E 75 8 || lock_rc=$?
if [ "$lock_rc" -eq 75 ]; then
    _log "another projection is already in progress — exiting"
    exit 0
elif [ "$lock_rc" -ne 0 ]; then
    _log "lock acquisition failed (flock exit $lock_rc)"
    exit 75
fi

# --if-armed decides "is there an engine here at all"; everything past that point
# is a real result. Exit code is the entrypoint's, unaltered: an armed install
# whose projection fails must fail the unit, or the staleness bound this whole
# file exists to enforce would go quiet exactly when it stopped holding.
cd "$REPO_DIR" || { _log "repo directory $REPO_DIR is unreachable"; exit 75; }
"$VENV_PY" -m genesis.memory.graphstore_project --if-armed
exit $?
