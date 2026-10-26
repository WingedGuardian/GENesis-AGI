# shellcheck shell=bash
# Live-system guard for bootstrap.sh — sourced, not executed.
#
# bootstrap_refuse_if_server_live [args...]
#
# bootstrap.sh is a setup/repair tool, NOT a deploy path. Running it on a
# machine with a live genesis-server re-runs installers (curl|bash, npm -g,
# pip), re-registers MCP servers + systemd units, and edits dotfiles on top
# of a running system — the direct trigger of the 2026-07-13 eth0/load
# incident. Code changes deploy via scripts/update.sh, or scripts/deploy_code_only.sh
# for a code-only change (a locked pull + restart, serialized with update.sh).
#
# This function makes that rule mechanical: refuse to proceed while
# genesis-server is live, unless one of two overrides applies:
#
#   GENESIS_BOOTSTRAP_ALLOW_LIVE=1  — sanctioned-caller opt-out. update.sh
#       sets it on its bootstrap invocation: its ERR trap is armed there, so
#       a guard refusal inside update.sh would trigger a FULL update rollback
#       (git reset to the rollback tag) — the guard must never gate the
#       sanctioned deploy path, which stops the server itself beforehand.
#   --force  — human override for deliberate live repair. Proceeds with a
#       loud warning.
#
# Server-live detection mirrors update.sh's _ensure_server_down check so the
# two can't drift: live iff `systemctl --user is-active` reports active OR a
# `python -m genesis serve` process exists. Detection is FAIL-OPEN: any other
# outcome (unknown unit rc=4, no session D-Bus, pgrep miss/absent) reads as
# not-running and bootstrap proceeds — a broken detector must never block a
# fresh-install bootstrap (where no server exists yet).
#
# Return codes (caller decides severity, cf. venv_setup.sh):
#   0 — proceed (not live, opt-out set, or --force given; warning printed
#       when forcing past a live server)
#   1 — refused: server is live, no override (message already printed)

_genesis_server_is_live() {
    if systemctl --user is-active --quiet genesis-server.service 2>/dev/null; then
        return 0
    fi
    if pgrep -f "python -m genesis serve" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

bootstrap_refuse_if_server_live() {
    # Sanctioned-caller opt-out (update.sh deploy path) — silent, unconditional.
    if [ -n "${GENESIS_BOOTSTRAP_ALLOW_LIVE:-}" ]; then
        return 0
    fi

    local force=false arg
    for arg in "$@"; do
        [ "$arg" = "--force" ] && force=true
    done

    if ! _genesis_server_is_live; then
        return 0
    fi

    if [ "$force" = "true" ]; then
        echo "WARNING: proceeding on a LIVE system by --force (genesis-server is running)."
        return 0
    fi

    cat >&2 <<'EOF'
REFUSED: genesis-server is RUNNING — bootstrap.sh is a setup/repair tool,
not a deploy path. Running it on a live system re-runs installers and
re-registers services on top of a running server (this triggered the
2026-07-13 eth0/load incident).

To deploy code changes, use the sanctioned path instead:
    scripts/update.sh
  or, for a code-only change (locked pull + restart, serialized with update.sh):
    scripts/deploy_code_only.sh

To run bootstrap for deliberate live repair anyway:
    ./scripts/bootstrap.sh --force
EOF
    return 1
}

# refuse_if_test_fenced_live <acting-root>
#
# The suite-wide test fence (tests/_live_fence.py) exports
# GENESIS_TEST_FENCE_LIVE_ROOTS: a colon-separated list of real paths no test
# may let a deploy script act on (this checkout, the real ~/.genesis and
# ~/genesis). The decision lives HERE, inside the script, because only the
# script knows the root it will act on after bash's own expansion — no amount
# of argv or shell-string prediction from outside converges (BASH_ENV,
# --rcfile, nested -c, a path built in a variable all defeat it).
#
#   unset/empty  → return 0 immediately: production behaviour never changes.
#   set          → resolve the acting root, HOME, explicit GENESIS_HOME (a
#                  "~" or "~/x" one both literally and against HOME) and
#                  default HOME/.genesis, preserving pathname bytes;
#                  unprovable resolution, colon paths and "~user" explicit
#                  homes refuse while armed (the list cannot encode colons);
#                  any of them equal to or under a listed root prints
#                  "REFUSED (test fence): <script> would act on <path>" to
#                  stderr, appends the same line to $GENESIS_TEST_FENCE_LOG
#                  when set (so the offending test fails at teardown), and
#                  returns 1.
#
# Callers run `refuse_if_test_fenced_live "$ROOT" || exit 97` as the FIRST
# statement after resolving the root they act on, before any side effect —
# 97 so a fenced refusal can't be mistaken for an ordinary script failure.

refuse_if_test_fenced_live() {
    [ -n "${GENESIS_TEST_FENCE_LIVE_ROOTS:-}" ] || return 0
    local _candidate _listed _hit="" _resolved _status _reason=""
    # Split on colons by parameter expansion, not `read` or unquoted expansion:
    # read is line-oriented (a listed root may contain a literal newline) and
    # expansion would glob (/tmp/live[1] must compare as itself, not matches).
    local -a _roots=()
    local _rest=$GENESIS_TEST_FENCE_LIVE_ROOTS _part
    while [[ $_rest == *:* ]]; do
        _part=${_rest%%:*}
        _roots+=("$_part")
        _rest=${_rest#*:}
    done
    _roots+=("$_rest")
    local -a _candidates=("$1" "${HOME:-}" "${GENESIS_HOME:-}")
    [ -z "${HOME:-}" ] || _candidates+=("$HOME/.genesis")
    # Backup expands a tilde GENESIS_HOME against HOME, while the lock path
    # reads it literally (#3438), relative to the cwd. Check both: the raw
    # value above is the literal form, and "~" or "~/x" adds its HOME form.
    # A "~user" form names a home this guard cannot prove, so it refuses.
    if [[ "${GENESIS_HOME:-}" == '~'* ]]; then
        if [[ "$GENESIS_HOME" != '~' && "$GENESIS_HOME" != '~/'* ]]; then
            _hit=$GENESIS_HOME
            _reason="unsupported tilde-form GENESIS_HOME"
        elif [ -z "${HOME:-}" ]; then
            _hit=$GENESIS_HOME
            _reason="tilde GENESIS_HOME with no HOME"
        elif [[ "$HOME" == *$'\n' ]]; then
            # Backup's $(...) expansion strips a trailing newline, so the home
            # it acts on is not the one this guard would resolve.
            _hit=$GENESIS_HOME
            _reason="tilde GENESIS_HOME with a newline-terminated HOME"
        else
            _candidates+=("$HOME${GENESIS_HOME#\~}")
        fi
    fi
    for _candidate in "${_candidates[@]}"; do
        [ -z "$_hit" ] || break
        [ -n "$_candidate" ] || continue
        # Adapt backup_capture_path's sentinel/status capture locally: remove
        # exactly the formatter LF, never trailing LF bytes in the pathname.
        if _resolved=$(realpath -m -- "$_candidate" 2>/dev/null; _status=$?; printf '.'; exit "$_status"); then
            _resolved=${_resolved%.}
            if [[ "$_resolved" != *$'\n' ]]; then
                _hit=$_candidate
                _reason="unprovable path resolution"
                break
            fi
            _resolved=${_resolved%$'\n'}
        else
            _resolved=""
        fi
        if [[ "$_resolved" != /* ]]; then
            _hit=$_candidate
            _reason="unprovable path resolution"
            break
        fi
        if [[ "$_resolved" == *:* ]]; then
            _hit=$_resolved
            _reason="colon path is not representable"
            break
        fi
        for _listed in "${_roots[@]}"; do
            [ -n "$_listed" ] || continue
            # "${_listed%/}/" * covers the listed-root-is-/ case: "/" trims to
            # "" so the pattern is /* — every absolute descendant.
            if [ "$_resolved" = "$_listed" ] || [[ "$_resolved" == "${_listed%/}/"* ]]; then
                _hit="$_resolved"
                break 2
            fi
        done
    done
    [ -n "$_hit" ] || return 0
    # Name the script that sourced this lib (BASH_SOURCE[1]), not $0: under a
    # BASH_ENV/--rcfile launch $0 is "bash" while the script still names itself.
    local _script="${BASH_SOURCE[1]:-$0}"
    local _msg="REFUSED (test fence): ${_script##*/} would act on $_hit${_reason:+ ($_reason)}"
    printf '%s\n' "$_msg" >&2
    if [ -n "${GENESIS_TEST_FENCE_LOG:-}" ]; then
        printf '%s\n' "$_msg" >> "$GENESIS_TEST_FENCE_LOG" 2>/dev/null || true
    fi
    return 1
}
