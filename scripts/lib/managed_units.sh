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
    # A supervised update session never takes a template, through any door
    # (update.sh refuses its flags; this covers bootstrap and setup-vnc).
    [ "${GENESIS_UPDATE_TIER:-}" = "1" ] && return 1
    read -r -a names <<< "${GENESIS_TAKE_TEMPLATES:-}" || true
    for name in "${names[@]}"; do
        if [ "$name" = "*" ] || [ "$name" = "$1" ]; then
            return 0
        fi
    done
    return 1
}

# Whether <name> itself is in GENESIS_TAKE_TEMPLATES. `*` does not count: a
# symlinked unit is usually a mask (-> /dev/null), and a blanket
# --take-templates must never unmask one. Only --take-template <name> replaces it.
genesis_mu_taken_explicitly() {
    local name names=()
    [ "${GENESIS_UPDATE_TIER:-}" = "1" ] && return 1
    read -r -a names <<< "${GENESIS_TAKE_TEMPLATES:-}" || true
    for name in "${names[@]}"; do
        if [ "$name" = "$1" ]; then
            return 0
        fi
    done
    return 1
}

# Copy <target> (a symlink is copied as a symlink) under one backup dir per run.
genesis_mu_backup() {
    : "${GENESIS_MU_BACKUP_DIR:=$HOME/.genesis/deploy-backups/$(date -u +%Y%m%dT%H%M%SZ)}"
    mkdir -p "$GENESIS_MU_BACKUP_DIR" && cp -P -- "$1" "$GENESIS_MU_BACKUP_DIR/" || return 1
    GENESIS_MU_TAKING=1
}

genesis_mu_keep() {
    local name="$1" why="$2"
    GENESIS_MU_KEPT=$((GENESIS_MU_KEPT + 1))
    echo "  Kept: $name ($why). Not overwritten."
    genesis_mu_summary "kept $name: $why; put local changes in ~/.config/systemd/user/$name.d/*.conf, or take the template with --take-template $name"
}

# Replace <target> with the bytes of <new> in one rename, but only while <target>
# still holds the bytes of <expected> (a regular file, not a link), or, with an
# empty <expected>, while nothing is there. The verdict that allowed the write was
# reached on <expected>; a file edited or swapped for a link since then is not
# replaced. Returns 2 then, 1 when the write fails. The re-check and the rename
# are not one atomic step, so an edit landing between them still loses; the
# window is the rename, not the whole render loop.
genesis_mu_replace() {
    local new="$1" target="$2" expected="$3" tmp
    tmp="$(mktemp -p "$(dirname "$target")" ".$(basename "$target").XXXXXX")" || return 1
    if ! cat -- "$new" > "$tmp"; then
        rm -f -- "$tmp"
        return 1
    fi
    if [ -n "$expected" ]; then
        chmod --reference="$target" "$tmp" 2>/dev/null || chmod "$(printf '%o' $((0666 & ~$(umask))))" "$tmp"
        if [ -L "$target" ] || [ ! -f "$target" ] || ! cmp -s -- "$expected" "$target"; then
            rm -f -- "$tmp"
            return 2
        fi
    else
        chmod "$(printf '%o' $((0666 & ~$(umask))))" "$tmp"
        if [ -e "$target" ] || [ -L "$target" ]; then
            rm -f -- "$tmp"
            return 2
        fi
    fi
    if ! mv -fT -- "$tmp" "$target"; then
        rm -f -- "$tmp"
        return 1
    fi
}

# genesis_install_managed_unit <template path relative to the repo> <unstamped render file> <target>
genesis_install_managed_unit() {
    local rel="$1" render="$2" target="$3" name stamped state snap="" wrc=0
    GENESIS_MU_TAKING=0
    name="$(basename "$target")"
    stamped="$(mktemp -p "$(dirname "$render")" mu.XXXXXX)" || return 1
    local rc=0
    python3 -I -S "$GENESIS_MU_PY" stamp < "$render" > "$stamped" || rc=$?
    if [ "$rc" -ne 0 ]; then
        rm -f "$stamped"
        # A unit that cannot be rendered is a failed deploy, not a warning:
        # the code would run against the old unit. Named in the summary too,
        # since bootstrap's own output is cut to `tail -10` under update.sh.
        local why="could not stamp the render of $rel (exit $rc)"
        [ "$rc" -eq 3 ] && why="$rel carries the genesis-managed marker itself"
        echo "  ERROR: $why; $name not installed"
        genesis_mu_summary "not installed $name: $why"
        return 1
    fi
    if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
        # A link (a masked unit is one, to /dev/null) or a directory is never
        # written through. A link taken BY NAME is saved and replaced (never by
        # `*`, which would unmask); a directory is kept.
        if [ -L "$target" ] && genesis_mu_taken_explicitly "$name" && genesis_mu_backup "$target"; then
            rm -f -- "$target"
        elif [ -L "$target" ]; then
            genesis_mu_keep "$name" "a symlink (masked?); only --take-template $name replaces it"
            rm -f "$stamped"
            return 0
        else
            genesis_mu_keep "$name" "not a regular file; remove it by hand to install the template"
            rm -f "$stamped"
            return 0
        fi
    fi
    if [ -f "$target" ]; then
        # Judge a snapshot, and replace only while the unit still holds it: an
        # edit made after the precheck (or after this read) is never overwritten.
        snap="$(mktemp -p "$(dirname "$stamped")" mu-snap.XXXXXX)" || { rm -f "$stamped"; return 1; }
        if ! cat -- "$target" > "$snap"; then
            genesis_mu_keep "$name" "could not be read"
            rm -f "$stamped" "$snap"
            return 0
        fi
        if cmp -s "$stamped" "$snap"; then
            echo "  OK: $name (unchanged)"
            rm -f "$stamped" "$snap"
            return 0
        fi
        state="$(genesis_mu_verdict "$name" "$snap")"
        if [ -z "$state" ]; then
            state="$(python3 -I -S "$GENESIS_MU_PY" classify --repo "$GENESIS_ROOT" --template "$rel" \
                --target "$snap" --upto HEAD ${GENESIS_MU_PREV:+--also "$GENESIS_MU_PREV"} \
                --accept-legacy 2>/dev/null)" || state="error"
        fi
        case "$state" in
            stamped|legacy) ;;
            edited)
                if ! genesis_mu_taken "$name" || ! genesis_mu_backup "$target"; then
                    genesis_mu_keep "$name" "edited by hand"
                    rm -f "$stamped" "$snap"
                    return 0
                fi
                ;;
            *)
                genesis_mu_keep "$name" "could not be checked"
                rm -f "$stamped" "$snap"
                return 0
                ;;
        esac
    fi
    genesis_mu_replace "$stamped" "$target" "$snap" || wrc=$?
    rm -f "$stamped" ${snap:+"$snap"}
    if [ "$wrc" -eq 2 ]; then
        genesis_mu_keep "$name" "it changed while this run was installing it; run the update again"
        return 0
    elif [ "$wrc" -ne 0 ]; then
        echo "  ERROR: could not write $target"
        return 1
    fi
    if [ "$GENESIS_MU_TAKING" = "1" ]; then
        # Named only once the template is in: a take that lost the race above
        # was reported as kept instead.
        echo "  Taken: $name (old file saved in $GENESIS_MU_BACKUP_DIR)"
        genesis_mu_summary "taken $name: template installed, old file saved in $GENESIS_MU_BACKUP_DIR"
    elif [ -n "$snap" ]; then
        echo "  Updated: $name"
        genesis_mu_summary "updated $name"
    else
        echo "  Created: $name"
        genesis_mu_summary "created $name"
    fi
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

# genesis_mu_verdict <unit> <snapshot>: the precheck's verdict, printed only when
# it was reached on the same bytes as <snapshot>; nothing otherwise, and the
# caller classifies the snapshot itself.
genesis_mu_verdict() {
    local unit state digest now
    if [ -z "${GENESIS_MU_VERDICTS:-}" ] || [ ! -f "$GENESIS_MU_VERDICTS" ]; then
        return 0
    fi
    now="$(sha256sum -- "$2" 2>/dev/null)" || return 0
    now="${now%% *}"
    while IFS=$'\t' read -r unit state digest; do
        if [ "$unit" = "$1" ]; then
            [ -n "$digest" ] && [ "$digest" = "$now" ] && printf '%s\n' "$state"
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
