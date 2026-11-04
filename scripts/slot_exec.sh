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
#   A slot scope (the default prefix, no --omit) also gets a memory cap,
#   MemoryMax=55% of what systemd sees as the machine (SLOT_EXEC_MEMORY_MAX
#   overrides) and MemorySwapMax=5% (SLOT_EXEC_SWAP_MAX), with
#   OOMPolicy=continue. A session whose tree outgrows both loses its largest
#   process to the kernel's OOM killer (and the next largest, if it is still
#   over), only inside this scope, and the scope keeps running. The swap cap is what makes the memory cap bite:
#   measured, with swap uncapped a scope over MemoryMax just swaps and nothing
#   is killed, so a runaway would thrash the shared swap instead. oomd is not that containment: on
#   systemd 255 one oomd action can kill several cgroups (a cgroup.threads pass
#   fails with EINVAL and oomd moves on to the next candidate), so a slot that
#   reaches the box-wide pressure line takes others with it.
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

# A slot request from inside one of our slot scopes (a claude started from a
# shell that is itself a slot's child) never nests. Only slot scopes count. The
# tmux server's scope does not: a claude left in it by tmux's late pane move
# must still get a scope of its own, or it would inherit the server's omit. A
# request for a DIFFERENT scope (a tmux server, --omit) always gets it: a tmux
# server started from a slot shell must not live, and die, with that slot.
# (SLOT_EXEC_CGROUP_FILE is a test seam; it defaults to the real file.)
_slot_request=0
[ "$prefix" = "genesis-slot" ] && [ "$omit" = "0" ] && _slot_request=1
_cg=""
read -r _cg < "${SLOT_EXEC_CGROUP_FILE:-/proc/self/cgroup}" 2>/dev/null || _cg=""
case "$_cg" in
    */genesis-slot-*.scope) [ "$_slot_request" = "1" ] && exec "$@" ;;
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

_base=(-p TasksMax=infinity)
[ "$omit" = "1" ] && _base+=(-p ManagedOOMPreference=omit)
_caps=()
if [ "$_slot_request" = "1" ]; then
    _mem="${SLOT_EXEC_MEMORY_MAX:-55%}"
    _swap="${SLOT_EXEC_SWAP_MAX:-5%}"
    # A malformed override falls back to the default, never to no cap.
    [[ "$_mem" =~ ^(infinity|[1-9][0-9]?%|[1-9][0-9]*[KMGT])$ ]] || _mem="55%"
    [[ "$_swap" =~ ^(infinity|0|[1-9][0-9]?%|[1-9][0-9]*[KMGT])$ ]] || _swap="5%"
    _caps=(-p "MemoryMax=$_mem" -p "MemorySwapMax=$_swap")
fi

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
#
# A slot's properties degrade one step at a time, never straight to no scope:
#   1. caps + OOMPolicy=continue;
#   2. caps alone, only when systemd refused OOMPolicy itself (a systemd that
#      does not know the property on a scope; there a scope does not stop on
#      an OOM kill);
#   3. no caps, only when systemd refused a Memory property — said aloud;
#   4. no scope at all — said aloud, today's behaviour before this wrapper.
# A step down happens only when systemd-run's refusal NAMES the property
# ("Unknown assignment: OOMPolicy=…", "Invalid OOMPolicy setting: …"). Any
# other failure (a bus hiccup, a timeout) ends the ladder: dropping
# OOMPolicy=continue where systemd does support it would let the first OOM
# kill stop the whole scope, claude included.
_err=""
_probe() {
    _err=$(env "${_run_env[@]}" timeout -k 2 15 systemd-run --user --scope --quiet --collect \
        --slice="$slice" "$@" true 2>&1 >/dev/null)
}
_tiers=()
if [ "${#_caps[@]}" -gt 0 ]; then
    _tiers=("caps-oom" "caps" "base")
else
    _tiers=("base")
fi
_props=()
_cap_err=""
for _tier in "${_tiers[@]}"; do
    case "$_tier" in
        caps-oom) _try=("${_base[@]}" "${_caps[@]}" -p OOMPolicy=continue) ;;
        caps) _try=("${_base[@]}" "${_caps[@]}") ;;
        base) _try=("${_base[@]}") ;;
    esac
    _probe "${_try[@]}"
    _rc=$?
    if [ "$_rc" = 0 ]; then
        _props=("${_try[@]}")
        if [ "$_tier" = "base" ] && [ "${#_caps[@]}" -gt 0 ]; then
            echo "slot_exec: systemd refused the session memory cap (${_cap_err:-no detail}); starting in a scope without it." >&2
        fi
        break
    fi
    case "$_rc" in 124|137) _err="timed out after 15 s"; break ;; esac
    case "$_tier:$_err" in
        caps-oom:*OOMPolicy*) continue ;;
        caps-oom:*Memory*|caps:*Memory*) _cap_err="${_err//$'\n'/ }"; continue ;;
    esac
    break
done
if [ "${#_props[@]}" = 0 ]; then
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
