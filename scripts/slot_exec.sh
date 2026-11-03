#!/usr/bin/env bash
# slot_exec.sh — run an interactive command in its own systemd user scope.
#
# Usage: slot_exec.sh [--unit-prefix P] [--slice S] [--omit] <label> -- <cmd> [args...]
#
#   Defaults: --unit-prefix genesis-slot, --slice genesis-slots.slice.
#   The scope is named <P>-<label>-<8 hex>.scope. --omit marks it
#   ManagedOOMPreference=omit: systemd-oomd never chooses it, but only while
#   oomd's monitored ancestor has the scope's owner (the user manager). Under a
#   root-owned monitor such as user.slice, oomd ignores the mark
#   (systemd.resource-control(5), ManagedOOMPreference=).
#   The scope sets TasksMax=infinity, so it adds no task cap below the per-user
#   slice budget the session had before (a new user-manager scope would
#   otherwise get DefaultTasksMax). Its description is the label, never the
#   command line: an argv can carry a prompt, and systemd logs the description.
#
# Why: every interactive Claude Code process used to inherit the cgroup of the
# tmux SERVER that started its pane, and the tmux server lives wherever its
# first client ran (a login session scope, or genesis-server.service when the
# dashboard terminal started it). tmux 3.4 does move each pane into its own
# tmux-spawn-*.scope, but it asks systemd and does not wait for the move, so the
# shell has usually forked claude already and claude stays behind. Measured:
# every foreground slot sat in ONE login scope, so systemd-oomd's smallest
# action against foreground work was "all of it". This wrapper gives each
# command a scope of its own BEFORE it starts.
#
# How: probe that systemd will create a scope here, then exec systemd-run
# --scope, which registers the scope with its own pid and execs the command in
# place (no fork, no new session): the pid, the controlling terminal, the
# process group and the exit status are the command's own. If the probe fails
# (no user manager, no systemd-run, a refused property), say so once and exec
# the command unscoped, which is exactly the behaviour before this wrapper.
#
# The environment the command sees is unchanged. systemd-run adds INVOCATION_ID,
# which this wrapper removes (restoring the caller's own, if it had one); XDG_RUNTIME_DIR is supplied to systemd-run alone
# when the caller lacks it (slot panes do), so systemd-run uses the manager's
# private socket instead of depending on the session bus.

set -u

prefix="genesis-slot"
slice="genesis-slots.slice"
omit=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --unit-prefix|--slice)
            if [ "$#" -lt 2 ]; then echo "slot_exec: $1 needs a value" >&2; exit 64; fi
            if [ "$1" = "--slice" ]; then slice="$2"; else prefix="$2"; fi
            shift 2 ;;
        --omit) omit=1; shift ;;
        --) break ;;
        -*) echo "slot_exec: unknown option $1" >&2; exit 64 ;;
        *) break ;;
    esac
done
label="${1:-}"
if [ "$#" -lt 3 ] || [ "${2:-}" != "--" ]; then
    echo "usage: slot_exec.sh [--unit-prefix P] [--slice S] [--omit] <label> -- <cmd> [args...]" >&2
    exit 64
fi
shift 2

# A unit name allows [A-Za-z0-9:_.\-]; the label is ours (a slot number, "gm",
# "bare", "tmux"), so anything else is a caller bug, not input to escape.
case "$label" in
    ''|*[!A-Za-z0-9_-]*) label="x" ;;
esac
case "$prefix" in
    ''|*[!A-Za-z0-9_-]*) prefix="genesis-slot" ;;
esac
case "$slice" in
    *[!A-Za-z0-9_.-]*|-.slice) slice="genesis-slots.slice" ;;
    *.slice) ;;
    *) slice="genesis-slots.slice" ;;
esac

# Already inside one of our slot scopes (a claude started from a shell that is
# itself a slot's child): never nest. Only slot scopes count. The tmux server's
# scope does not: a claude left in it by tmux's late pane move must still get a
# scope of its own, or it would inherit the server's omit.
# (SLOT_EXEC_CGROUP_FILE is a test seam; it defaults to the real file.)
_cg=""
read -r _cg < "${SLOT_EXEC_CGROUP_FILE:-/proc/self/cgroup}" 2>/dev/null || _cg=""
case "$_cg" in
    */genesis-slot-*.scope) exec "$@" ;;
esac

if ! command -v systemd-run >/dev/null 2>&1; then
    echo "slot_exec: systemd-run not found; starting without a scope of its own." >&2
    exec "$@"
fi

# systemd-run --user reaches the user manager over $XDG_RUNTIME_DIR/systemd/private,
# or falls back to the session bus when the variable is unset. Give it the
# private socket when the caller lacks the variable, and keep it away from the
# command so its environment stays exactly as before.
# (SLOT_EXEC_RUNTIME_DIR is a test seam; it defaults to the real per-user dir.)
_unset_xdg=()
_run_env=()
_rt="${SLOT_EXEC_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "$_rt" ]; then
    _run_env=("XDG_RUNTIME_DIR=$_rt")
    _unset_xdg=(-u XDG_RUNTIME_DIR)
fi

_props=(-p TasksMax=infinity)
[ "$omit" = "1" ] && _props+=(-p ManagedOOMPreference=omit)

# systemd-run adds INVOCATION_ID for the new scope. Remove it unless the caller
# already had one, which is restored as it was. (`env` reads its -u options
# before any NAME=value, so the assignment must come last.)
_inv=(-u INVOCATION_ID)
if [ -n "${INVOCATION_ID+x}" ]; then _inv=("INVOCATION_ID=$INVOCATION_ID"); fi

# The probe creates and immediately ends a throwaway scope with the same slice
# and properties, so a refusal is seen here, before the command could be lost
# inside systemd-run. 15 s matches the invoker's probe of the same call
# (src/genesis/cc/invoker.py): a user manager that cannot answer that fast would
# also hang the launch, and an interactive launch has a person waiting on it.
_err=""
if ! _err=$(env "${_run_env[@]}" timeout -k 2 15 systemd-run --user --scope --quiet --collect \
        --slice="$slice" "${_props[@]}" true 2>&1 >/dev/null); then
    _err="${_err//$'\n'/ }"
    echo "slot_exec: could not create a systemd scope (${_err:-no detail}); starting without one." >&2
    exec "$@"
fi

# The command goes through `env`, which would read a first word containing `=`
# as an assignment: callers pass a program name or an absolute path.
_hex=$(od -An -N4 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')
[ -n "$_hex" ] || _hex="$$"
exec env "${_run_env[@]}" systemd-run --user --scope --quiet --collect \
    --slice="$slice" --unit="${prefix}-${label}-${_hex}" \
    --description="Genesis ${prefix} ${label}" "${_props[@]}" \
    -- env "${_unset_xdg[@]}" "${_inv[@]}" "$@"
