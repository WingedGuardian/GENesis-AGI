#!/usr/bin/env bash
#
# cc_settings_align.sh — recurring CONTAINER-side reconcile of Claude Code's
# auto-updater suppression.
#
# WHY: CC's auto-updater is held off by two keys in the USER-level
# ~/.claude/settings.json — DISABLE_AUTOUPDATER=1 and DISABLE_UPDATES=1 (the
# second is the stricter one; it also blocks a manual `claude update`). Repo and
# project settings do not cover it: they apply only when CC launches from that
# directory, and the updater runs where they do not. The npm pin only governs
# what a DELIBERATE install writes; these two keys are what stop CC moving on
# its own, and without them the pin is advisory.
#
# WHY A TIMER AND NOT JUST THE ALIGN PATH: cc_ensure_local re-asserts the keys on
# every install/bootstrap/update, but that only helps a box that RUNS one. A box
# can sit far longer than expected between deploys (measured on a live install:
# 14 days), and that is exactly the window in which a settings file that drifted
# after setup stays silently unprotected. This timer closes it.
#
# CONTAINER-ONLY by design, and deliberately NOT folded into cc_align_host.sh:
# that script is host-only by contract and its unit is hardened on that basis, so
# a container-side filesystem write does not belong there.
#
# Exit codes: 0 ONLY when suppression was positively verified — the keys were
# already correct, or were repaired (a repair is logged loudly; a REPEAT repair
# across runs is the real "something on this machine keeps rewriting
# settings.json" signal), or another run of THIS script already holds the lock
# and is doing the work. Every other path exits non-zero, including the
# structural ones (no lock, no library, function renamed away): a path that
# verified nothing must never report success, or the unit becomes a green light
# for an unguarded auto-updater. The unit then enters `failed` and the miss is
# visible in
# `systemctl --user status genesis-cc-settings-align.service` instead of dying in
# a journal line. That distinction is the whole point of a dedicated unit: its
# status means exactly one thing, with no other work to conflate it with.
#
# Invoked by scripts/systemd/genesis-cc-settings-align.{service,timer}.template.

set -u

# Resolve HOME when unset: stripped-env/systemd/sandbox invocations can leave
# HOME unset, which under `set -u` aborts at the first ${HOME} use. Fall back to
# the passwd entry for the current uid (same source Path.home() uses); fail
# closed if unresolvable.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

GENESIS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_ENV="$GENESIS_ROOT/scripts/lib/cc_version.sh"

# ── Single-flight guard: two concurrent reconciles would race each other on the
# same read-modify-write. Non-blocking — a run already in flight makes this one
# redundant. (The function itself also compare-and-swaps, which covers writers
# that do NOT take this lock, e.g. CC itself.)
LOCKFILE="$HOME/.genesis/locks/cc_settings_align.lock"
if ! mkdir -p "$(dirname "$LOCKFILE")" 2>/dev/null && [ ! -d "$(dirname "$LOCKFILE")" ]; then
    echo "cc_settings_align: cannot create $(dirname "$LOCKFILE") — suppression NOT verified"
    exit 3
fi
if ! exec {LOCK_FD}>"$LOCKFILE"; then
    # NOT a green no-op: we verified nothing. Exiting 0 here would leave the unit
    # reporting success while the auto-updater ran unguarded — the exact
    # silently-unprotected state this timer exists to prevent, one layer up.
    echo "cc_settings_align: cannot open lockfile $LOCKFILE — suppression NOT verified"
    exit 3
fi
# `if ! flock` cannot distinguish "lock held" (rc 1) from "flock is not on PATH"
# (rc 127) — and this script's contract is that a run which verified NOTHING
# never exits 0. Without this probe a box missing util-linux reported "another
# run is in progress", exited 0, and left the file unrepaired: a green unit over
# an unguarded auto-updater, which is the precise failure this design claims to
# have closed.
if ! command -v flock >/dev/null 2>&1; then
    echo "cc_settings_align: flock not found — cannot take the single-flight lock;" \
         "suppression NOT verified"
    exit 3
fi
if ! flock -n "$LOCK_FD"; then
    echo "cc_settings_align: another run is in progress — skipping"
    exit 0
fi

if [ ! -f "$CC_ENV" ]; then
    echo "cc_settings_align: $CC_ENV missing — cannot load the reconciler; suppression NOT verified"
    exit 3
fi

# shellcheck source=/dev/null
source "$CC_ENV"

if ! declare -F cc_ensure_updater_suppressed >/dev/null 2>&1; then
    # A rename or move upstream would otherwise leave this timer firing daily,
    # green, and completely inert — for months, with unit state saying healthy.
    echo "cc_settings_align: cc_ensure_updater_suppressed not defined in $CC_ENV — suppression NOT verified"
    exit 3
fi

# Clear the state before the call so an OLDER cc_version.sh — one that defines
# the function but does not set this variable — cannot be read as `ok` by the
# `:-` default below. Version skew across a partial deploy would otherwise be a
# green unit that verified nothing, the same false-green class as a missing lock.
unset CC_SUPPRESSION_STATE

# Deliberately quiet on the common path: this runs on a timer, and a line per run
# would train the operator to ignore the journal for this unit.
cc_ensure_updater_suppressed || true

if [ -z "${CC_SUPPRESSION_STATE+set}" ]; then
    echo "cc_settings_align: cc_ensure_updater_suppressed did not set" \
         "CC_SUPPRESSION_STATE (version skew?) — suppression NOT verified"
    exit 3
fi

# A REPEAT repair is the signal that matters — one repair is drift being healed,
# two in a row means something on this machine keeps rewriting settings.json and
# the heal is not holding. That signal needs a RECEIVER: during the long gaps
# between deploys (the window this timer exists for) update.sh is not running, so
# CC_SUPPRESSION_STATE never reaches update_history, and unit state is the only
# channel left. Remember the last outcome so the second consecutive repair can
# escalate to a failed unit instead of accumulating identical journal lines in a
# unit that stays green.
_STATE_FILE="$HOME/.genesis/cc_settings_align.last"
_prev_raw="$(cat "$_STATE_FILE" 2>/dev/null || true)"
_prev="${_prev_raw%% *}"                    # first field = the state
_prev_since="${_prev_raw#* }"               # remainder = when it first appeared
[ "$_prev_since" = "$_prev_raw" ] && _prev_since=""   # old single-field format

_now="$(date -u +%Y-%m-%d 2>/dev/null || echo unknown)"
# Carry the date the CURRENT state first appeared, so a repeat can say how long
# it has been failing for the same reason. A unit that reports an identical
# fresh failure every day is one an operator learns to scroll past; one that
# says "unchanged since <date>" is a different message even at the same severity.
if [ "$_prev" = "${CC_SUPPRESSION_STATE}" ] && [ -n "$_prev_since" ]; then
    _since="$_prev_since"
else
    _since="$_now"
fi
printf '%s %s\n' "${CC_SUPPRESSION_STATE}" "$_since" > "$_STATE_FILE" 2>/dev/null || true

case "${CC_SUPPRESSION_STATE:-ok}" in
    ok)
        exit 0
        ;;
    repaired)
        echo "cc_settings_align: auto-updater suppression was MISSING and has been restored"
        if [ "$_prev" = "repaired" ]; then
            echo "cc_settings_align: WARNING — this is the SECOND consecutive repair, so the fix is not holding:" \
                 "something on this machine keeps rewriting ~/.claude/settings.json. Find that writer."
            exit 3
        fi
        echo "cc_settings_align: if this repeats on the next run the unit will fail, which is the signal to investigate"
        exit 0
        ;;
    contended)
        # A competing writer won the race (or kept winning until the retries ran
        # out). Distinct from `failed`: nothing is wrong with the file or the
        # environment, and the next run will very likely succeed. Still non-zero,
        # because this run verified nothing.
        echo "cc_settings_align: another writer is changing ~/.claude/settings.json;" \
             "suppression NOT verified this run — retrying on the next timer"
        exit 3
        ;;
    *)
        # failed — suppression is NOT in effect. Fail the unit so this is visible
        # as unit state, not just a journal line nobody reads.
        echo "cc_settings_align: WARNING — could not establish auto-updater suppression (${CC_SUPPRESSION_STATE}); CC may self-update past the pin"
        if [ "$_prev" = "${CC_SUPPRESSION_STATE}" ]; then
            # Same cause as last run. Say so rather than emitting an identical
            # fresh failure daily — a repeat that reads as new is a repeat that
            # gets ignored. The remediation is a one-time operator action
            # (unparseable JSON, unwritable directory, a settings.json that is
            # not a regular file); it will not self-heal, so the message should
            # make its age obvious.
            echo "cc_settings_align: unchanged since ${_since} — this will not self-heal;" \
                 "the cause above needs a one-time fix"
        fi
        exit 3
        ;;
esac
