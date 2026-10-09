# shellcheck shell=bash
# Install a rendered systemd user unit, or KEEP the installed one when it was
# edited by hand. Sourced by bootstrap.sh and setup-vnc.sh; needs GENESIS_ROOT.
#
# Every render is stamped by scripts/lib/managed_units.py (its docstring has the
# grammar), so the next run can tell its own output from a local edit. An edited
# unit is never overwritten: it is kept, named, and listed in the run summary.
# Local changes belong in a drop-in (<unit>.d/*.conf), which nothing here touches.
#
# Inputs from the environment, all optional:
#   GENESIS_TAKE_TEMPLATES  space-separated unit file names (or *) whose hand
#                           edits may be replaced; the old file is backed up first
#   GENESIS_DEPLOY_SUMMARY  file the run summary lines are appended to
#   GENESIS_MU_PREV         a revision whose templates also count as Genesis's own
#                           (an update's rollback tag: what was installed before)
# Outputs: GENESIS_MU_CHANGED=1 when a unit file was written; GENESIS_MU_KEPT counts
# the units kept because they were edited.

GENESIS_MU_PY="$GENESIS_ROOT/scripts/lib/managed_units.py"
GENESIS_MU_CHANGED=0
GENESIS_MU_KEPT=0

genesis_mu_summary() {
    if [ -n "${GENESIS_DEPLOY_SUMMARY:-}" ]; then
        printf '%s\n' "$*" >> "$GENESIS_DEPLOY_SUMMARY" 2>/dev/null || true
    fi
}

genesis_mu_taken() {
    # `read -a`, not an unquoted `for name in $VAR`: that would glob-expand `*`
    # (--take-templates) into the current directory's file names.
    local name names=()
    read -r -a names <<< "${GENESIS_TAKE_TEMPLATES:-}" || true
    for name in "${names[@]}"; do
        if [ "$name" = "*" ] || [ "$name" = "$1" ]; then
            return 0
        fi
    done
    return 1
}

# Copy <target> (a symlink is copied as a symlink) under one backup dir per run.
genesis_mu_backup() {
    : "${GENESIS_MU_BACKUP_DIR:=$HOME/.genesis/deploy-backups/$(date -u +%Y%m%dT%H%M%SZ)}"
    mkdir -p "$GENESIS_MU_BACKUP_DIR" && cp -P -- "$1" "$GENESIS_MU_BACKUP_DIR/" || return 1
    genesis_mu_summary "taken $(basename "$1"): template installed, old file saved in $GENESIS_MU_BACKUP_DIR"
}

genesis_mu_keep() {
    local name="$1" why="$2"
    GENESIS_MU_KEPT=$((GENESIS_MU_KEPT + 1))
    echo "  Kept: $name ($why). Not overwritten."
    genesis_mu_summary "kept $name: $why; put local changes in ~/.config/systemd/user/$name.d/*.conf, or take the template with --take-template $name"
}

# genesis_install_managed_unit <template path relative to the repo> <unstamped render file> <target>
genesis_install_managed_unit() {
    local rel="$1" render="$2" target="$3" name stamped state
    name="$(basename "$target")"
    stamped="$(mktemp -p "$(dirname "$render")" mu.XXXXXX)" || return 1
    local rc=0
    python3 -I -S "$GENESIS_MU_PY" stamp < "$render" > "$stamped" || rc=$?
    if [ "$rc" -ne 0 ]; then
        rm -f "$stamped"
        if [ "$rc" -eq 3 ]; then
            echo "  WARNING: $rel carries the genesis-managed marker itself; $name not installed"
        else
            echo "  WARNING: could not stamp the render of $rel (exit $rc); $name left as it is"
        fi
        return 0
    fi
    if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
        # A link (a masked unit is one, to /dev/null) or a directory is never
        # written through. A taken LINK is saved and replaced; a directory is kept.
        if [ -L "$target" ] && genesis_mu_taken "$name" && genesis_mu_backup "$target"; then
            rm -f -- "$target"
        else
            genesis_mu_keep "$name" "a symlink or not a regular file"
            rm -f "$stamped"
            return 0
        fi
    fi
    if [ -f "$target" ]; then
        if cmp -s "$stamped" "$target"; then
            echo "  OK: $name (unchanged)"
            rm -f "$stamped"
            return 0
        fi
        state="$(genesis_mu_verdict "$name")"
        if [ -z "$state" ]; then
            state="$(python3 -I -S "$GENESIS_MU_PY" classify --repo "$GENESIS_ROOT" --template "$rel" \
                --target "$target" --upto HEAD ${GENESIS_MU_PREV:+--also "$GENESIS_MU_PREV"} \
                --accept-legacy 2>/dev/null)" || state="error"
        fi
        case "$state" in
            stamped|legacy) ;;
            edited)
                if ! genesis_mu_taken "$name" || ! genesis_mu_backup "$target"; then
                    genesis_mu_keep "$name" "edited by hand"
                    rm -f "$stamped"
                    return 0
                fi
                ;;
            *)
                genesis_mu_keep "$name" "could not be checked"
                rm -f "$stamped"
                return 0
                ;;
        esac
        cat "$stamped" > "$target"
        echo "  Updated: $name"
        genesis_mu_summary "updated $name"
    else
        cat "$stamped" > "$target"
        echo "  Created: $name"
        genesis_mu_summary "created $name"
    fi
    rm -f "$stamped"
    # shellcheck disable=SC2034  # read by the sourcing script (bootstrap.sh)
    GENESIS_MU_CHANGED=1
}

# Judge every installed unit ONCE, before a render loop writes any of them: the
# legacy check scans template history, which is slow, so one scan per run rather
# than one per unit. Sets GENESIS_MU_VERDICTS to a "<unit><TAB><state>" file in
# <dir>; a failed check leaves it empty and each unit is then classified alone.
# `--take '*'` only keeps the exit at 0 so an edited unit still yields a verdict;
# nothing is taken here, the install step decides that per unit.
genesis_mu_precheck() {
    local unit_dir="$1" out="$2/verdicts"
    GENESIS_MU_VERDICTS=""
    if python3 -I -S "$GENESIS_MU_PY" check --repo "$GENESIS_ROOT" --unit-dir "$unit_dir" \
        --upto HEAD ${GENESIS_MU_PREV:+--also "$GENESIS_MU_PREV"} --accept-legacy --tsv \
        --take '*' > "$out" 2>/dev/null; then
        GENESIS_MU_VERDICTS="$out"
    fi
}

genesis_mu_verdict() {
    local unit state
    if [ -z "${GENESIS_MU_VERDICTS:-}" ] || [ ! -f "$GENESIS_MU_VERDICTS" ]; then
        return 0
    fi
    while IFS=$'\t' read -r unit state; do
        if [ "$unit" = "$1" ]; then
            printf '%s\n' "$state"
            return 0
        fi
    done < "$GENESIS_MU_VERDICTS"
}

# Timers the operator turned off: one unit name per line, `#` starts a comment.
GENESIS_DISABLED_TIMERS="${GENESIS_HOME:-$HOME/.genesis}/config/disabled_timers"

genesis_timer_opted_out() {
    # A loop, not `sed | grep -q`: under pipefail grep's early exit can SIGPIPE
    # the writer and turn a match into a failure.
    local line
    [ -f "$GENESIS_DISABLED_TIMERS" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"
        line="${line//[[:space:]]/}"
        [ "$line" = "$1" ] && return 0
    done < "$GENESIS_DISABLED_TIMERS"
    return 1
}

# The rollback tag of the update.sh run that launched us, if any: what was
# installed before the merge was rendered from it.
genesis_mu_prev_from_state() {
    local state="$HOME/.genesis/update_state.json"
    [ -f "$state" ] || return 0
    python3 -I -S -c 'import json, sys
d = json.load(open(sys.argv[1]))
t = d.get("rollback_tag") if isinstance(d, dict) else None
print(t if isinstance(t, str) else "")' "$state" 2>/dev/null || true
}
