# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# genesis_etc_put — write a root-owned config file Genesis manages (journald and
# sshd drop-ins from bootstrap.sh, the memory and network resilience drop-ins and
# units), or KEEP the installed one when it was edited by hand.
#
# Every write is stamped by scripts/lib/managed_units.py (its docstring has the
# grammar), the same stamp the systemd user units carry, so the next run can tell
# its own output from a local edit:
#   missing                          -> written
#   equal to the stamped content     -> unchanged (no write)
#   unstamped, equal to the content  -> migrated: the same bytes plus the stamp,
#                                       written once (callers skip their reload or
#                                       restart for it: only a comment changed)
#   carrying a valid stamp           -> written (Genesis wrote it; its content moved)
#   a symlink or not a regular file  -> kept (a masked unit is a symlink)
#   anything else                    -> kept as edited, named, and listed in the
#                                       run summary; written only when taken
#
# Taken means GENESIS_TAKE_TEMPLATES names the file's full path, or holds
# `etc:*` (`update.sh --take-template <path>`). A bare `*` takes systemd user
# units only, never these. A taken file is copied first to
# ${GENESIS_MU_BACKUP_DIR:-~/.genesis/deploy-backups/<UTC time>}/<its path>.
# A supervised update session (GENESIS_UPDATE_TIER=1) never takes one.
#
# Not here, on purpose: refusing a whole update over an edited file, as update.sh
# does for an edited user unit. Each of these files has had one Genesis content
# version so far, so keeping an edit loses nothing yet. The refusal belongs with
# the first change to one of these contents; issue #3282 has what it must do.
#
# Callers run it after their own `sudo -n true` gate (or, in bootstrap.sh, with
# the bare sudo the step always used), so it uses plain `sudo` like they do.
# Always returns 0; sets GENESIS_ETC_RESULT to one of: written, migrated,
# unchanged, taken, kept-edited, kept-nonregular, failed. kept-nonregular also
# covers a path sudo could not inspect or read. Callers treat kept-nonregular as
# NOT applied (a masked unit means the protection is off) and kept-edited as
# applied by the operator's own hand. A path containing whitespace cannot be
# taken (the take list is space-separated), so its kept message omits the hint.

# Pure parameter expansion, not dirname: callers may run with a minimal PATH.
_GENESIS_ETC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_GENESIS_ETC_DIR" = "${BASH_SOURCE[0]}" ] && _GENESIS_ETC_DIR=.
_GENESIS_ETC_MU="${_GENESIS_ETC_MU:-$_GENESIS_ETC_DIR/managed_units.py}"

_genesis_etc_summary() {
    if [ -n "${GENESIS_DEPLOY_SUMMARY:-}" ] && [ -n "${GENESIS_ETC_SUMMARY_OK:-}" ]; then
        printf '%s\n' "$*" >> "$GENESIS_DEPLOY_SUMMARY" 2>/dev/null || true
    fi
}

_genesis_etc_taken() {
    local name names=()
    # As for units (managed_units.sh): a supervised update session never takes.
    [ "${GENESIS_UPDATE_TIER:-}" = "1" ] && return 1
    read -r -a names <<< "${GENESIS_TAKE_TEMPLATES:-}" || true
    for name in "${names[@]}"; do
        if [ "$name" = "etc:*" ] || [ "$name" = "$1" ]; then
            return 0
        fi
    done
    return 1
}

_genesis_etc_remedy() {
    local path="$1" class="$2"
    case "$class" in
        sshd)
            # OpenSSH keeps the FIRST value it reads, so an override must sort
            # before this file, not after it.
            echo "put your setting in a file that sorts before $(basename "$path") (e.g. $(dirname "$path")/00-aa-local.conf)" ;;
        unit)
            echo "put your change in $path.d/override.conf" ;;
        *)
            echo "put your change in a later-sorting .conf in $(dirname "$path")" ;;
    esac
}

# A path the run could not classify or read is never written. Reported as
# kept-nonregular: callers then neither reload nor (in bootstrap) abort, and the
# resilience libs report their policy as NOT applied.
_genesis_etc_uninspected() {
    echo "  WARNING: $2 $1 with sudo — left as it is, not written."
    GENESIS_ETC_RESULT="kept-nonregular"
    _genesis_etc_summary "kept-etc $1: could not be inspected"
}

# The take hint <text>, or nothing for a path with whitespace:
# GENESIS_TAKE_TEMPLATES is a space-separated list, so such a path cannot be taken.
_genesis_etc_take_hint() {
    case "$1" in
        *[[:space:]]*) ;;
        *) printf '%s' "$2" ;;
    esac
}

# genesis_etc_put <path> <content> <mode or ""> <class: dropin|unit|sshd>
genesis_etc_put() {
    local path="$1" content="$2" mode="$3" class="$4" stamped cur state
    GENESIS_ETC_RESULT="failed"
    GENESIS_ETC_BACKUP=""
    # Compare like with like: `$(sudo cat …)` below drops trailing newlines, so
    # drop them from the content too (the file is written with exactly one).
    content="$(printf '%s' "$content")"
    if ! stamped="$(printf '%s\n' "$content" | python3 -I -S "$_GENESIS_ETC_MU" stamp)"; then
        echo "  WARNING: could not stamp the content for $path — not written."
        return 0
    fi
    # ONE sudo call classifies the path. Separate `sudo test` probes would read a
    # failing sudo as "missing" and fall through to the write, overwriting an
    # edited file with no backup. Anything but a clean answer is kept, unwritten.
    local kind
    # shellcheck disable=SC2016  # $1 is expanded by the inner sh, not here
    kind="$(sudo /bin/sh -c 'if [ -L "$1" ]; then echo link; elif [ ! -e "$1" ]; then echo missing; elif [ -f "$1" ]; then echo file; else echo other; fi' _ "$path" 2>/dev/null)" || kind=""
    case "$kind" in
        missing|file) ;;
        link|other)
            echo "  WARNING: $path is a symlink (a masked unit?) or not a regular file — not writing through it."
            GENESIS_ETC_RESULT="kept-nonregular"
            _genesis_etc_summary "kept-etc $path: a symlink or not a regular file"
            return 0 ;;
        *)
            _genesis_etc_uninspected "$path" "could not inspect"
            return 0 ;;
    esac
    local result="written" verb="created-etc"
    if [ "$kind" = file ]; then
        verb="updated-etc"
        if ! cur="$(sudo cat "$path" 2>/dev/null)"; then
            _genesis_etc_uninspected "$path" "could not read"
            return 0
        fi
        if [[ "$cur" == "$stamped" ]]; then
            GENESIS_ETC_RESULT="unchanged"
            return 0
        elif [[ "$cur" == "$content" ]]; then
            result="migrated"
        else
            state="$(printf '%s\n' "$cur" | python3 -I -S "$_GENESIS_ETC_MU" verify 2>/dev/null)" || state=""
            if [ "$state" != "stamped" ]; then
                if ! _genesis_etc_taken "$path"; then
                    echo "  Kept: $path was edited by hand. Not overwritten."
                    echo "        To keep the edit and get updates: $(_genesis_etc_remedy "$path" "$class"),"
                    echo "        then delete this file (the next update writes the Genesis version)$(_genesis_etc_take_hint "$path" "; or take it with update.sh --take-template $path (the old file is backed up)")."
                    GENESIS_ETC_RESULT="kept-edited"
                    _genesis_etc_summary "kept-etc $path: edited by hand; $(_genesis_etc_remedy "$path" "$class")$(_genesis_etc_take_hint "$path" ", or update.sh --take-template $path")"
                    return 0
                fi
                : "${GENESIS_MU_BACKUP_DIR:=$HOME/.genesis/deploy-backups/$(date -u +%Y%m%dT%H%M%SZ)}"  # the path disk_hygiene.sh prunes; as managed_units.sh
                local backup="$GENESIS_MU_BACKUP_DIR$path"
                if ! (umask 077 && mkdir -p "$(dirname "$backup")" && sudo cat "$path" > "$backup"); then
                    echo "  WARNING: could not back up $path — kept, not overwritten."
                    GENESIS_ETC_RESULT="kept-edited"
                    _genesis_etc_summary "kept-etc $path: take requested but the backup failed; not overwritten"
                    return 0
                fi
                GENESIS_ETC_BACKUP="$backup"
                result="taken"
            fi
        fi
    fi
    if ! sudo mkdir -p "$(dirname "$path")" 2>/dev/null \
        || ! printf '%s\n' "$stamped" | sudo tee "$path" >/dev/null 2>&1; then
        echo "  WARNING: could not write $path (read-only /etc?) — not applied."
        return 0
    fi
    if [ -n "$mode" ]; then
        sudo chmod "$mode" "$path" 2>/dev/null \
            || echo "  WARNING: wrote $path but could not set its mode to $mode."
    fi
    GENESIS_ETC_RESULT="$result"
    case "$result" in
        taken) _genesis_etc_summary "taken-etc $path: Genesis version installed, old file saved in $GENESIS_ETC_BACKUP" ;;
        written) _genesis_etc_summary "$verb $path" ;;
    esac
    return 0
}
