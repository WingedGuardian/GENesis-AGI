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
# unchanged, taken, kept-edited, kept-nonregular, uninspected, failed.
# uninspected is a path sudo could not classify or read: nothing was written
# (a path whose snapshot could not be taken locally is kept-nonregular).
# Callers treat kept-nonregular and uninspected as NOT applied (a masked unit
# means the protection is off) and kept-edited as applied by the operator's own
# hand; bootstrap.sh aborts on uninspected as it does on failed. A path containing whitespace cannot be
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
# uninspected, not kept-nonregular: a sudo that cannot authenticate lands here
# before any write, and bootstrap.sh must still abort on it as it did when that
# sudo failed at the write. The resilience libs report it as NOT applied.
# A local failure (no temp file for the snapshot) passes kept-nonregular as $3:
# sudo works there, so bootstrap keeps going, as it always has.
_genesis_etc_uninspected() {
    echo "  WARNING: $2 $1 with sudo — left as it is, not written."
    GENESIS_ETC_RESULT="${3:-uninspected}"
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

# Compare bytes, tolerating only trailing LF as the old shell comparison did.
# Installed bytes never pass through a Bash variable (which would discard NUL).
_genesis_etc_equal() {
    printf '%s\n' "$2" | python3 -I -S -c \
        'import pathlib,sys; sys.exit(pathlib.Path(sys.argv[1]).read_bytes().rstrip(b"\n") != sys.stdin.buffer.read().rstrip(b"\n"))' "$1" 2>/dev/null
}

_genesis_etc_original_intact() {
    local current status=1
    current="$(mktemp)" || return 1
    if sudo cat "$1" > "$current" 2>/dev/null && cmp -s -- "$2" "$current"; then
        status=0
    fi
    rm -f -- "$current"
    return "$status"
}

# Finalize exactly one put. Durable takeover backups are never removed here.
# retain is used after failed recovery, when the temporary file may be the only
# intact original. Its path is reported before the per-call state is cleared.
genesis_etc_finalize() {
    if [[ "$1" == commit && -n "${GENESIS_ETC_PENDING_SUMMARY:-}" ]]; then
        _genesis_etc_summary "$GENESIS_ETC_PENDING_SUMMARY"
    fi
    if [[ "$1" != retain && -n "${GENESIS_ETC_SNAPSHOT:-}" ]]; then
        rm -f -- "$GENESIS_ETC_SNAPSHOT" \
            || echo "  WARNING: could not remove private snapshot $GENESIS_ETC_SNAPSHOT"
    fi
    GENESIS_ETC_SNAPSHOT=""
    GENESIS_ETC_PENDING_SUMMARY=""
    GENESIS_ETC_TARGET=""
    GENESIS_ETC_DEFERRED=""
    GENESIS_ETC_EXISTED=0
    GENESIS_ETC_ATTEMPTED=0
    return 0
}

# Only the explicitly deferred bootstrap sshd caller uses rollback. Recover
# bytes before its fatal write error or rejected validation; never reload here.
genesis_etc_rollback() {
    local path="$1" reason="$2"
    if [[ "${GENESIS_ETC_DEFERRED:-}" != validate || "$path" != "${GENESIS_ETC_TARGET:-}" ]]; then
        echo "  WARNING: no current validation context for $path; not rolling back."
        return 1
    fi
    if [[ "$GENESIS_ETC_EXISTED" == 1 ]]; then
        if [[ -z "$GENESIS_ETC_SNAPSHOT" ]] \
            || ! sudo tee "$path" < "$GENESIS_ETC_SNAPSHOT" >/dev/null \
            || ! _genesis_etc_original_intact "$path" "$GENESIS_ETC_SNAPSHOT"; then
            echo "  WARNING: $reason; could not restore $path. Original bytes retained at $GENESIS_ETC_SNAPSHOT"
            genesis_etc_finalize retain
            return 1
        fi
        echo "  WARNING: $reason — your previous drop-in was put back, sshd untouched"
    else
        # A successful rm alone does not prove a failed sudo removed the target.
        # shellcheck disable=SC2016
        if ! sudo rm -f "$path" \
            || ! sudo /bin/sh -c '[ ! -e "$1" ] && [ ! -L "$1" ]' _ "$path"; then
            echo "  WARNING: $reason; could not remove newly written $path."
            genesis_etc_finalize retain
            return 1
        fi
        echo "  WARNING: $reason — drop-in removed, sshd untouched"
    fi
    genesis_etc_finalize discard
}

# genesis_etc_put <path> <content> <mode or ""> <class> [validate]
# validate retains original bytes and a pending success for the caller to
# finalize after sshd validation; existing four-argument callers stay immediate.
genesis_etc_put() {
    GENESIS_ETC_RESULT="failed"
    GENESIS_ETC_BACKUP=""
    GENESIS_ETC_SNAPSHOT=""
    GENESIS_ETC_PENDING_SUMMARY=""
    GENESIS_ETC_TARGET="$1"
    GENESIS_ETC_DEFERRED="${5:-}"
    GENESIS_ETC_EXISTED=0
    GENESIS_ETC_ATTEMPTED=0
    _genesis_etc_put_impl "$1" "$2" "$3" "$4"
    if [[ "$GENESIS_ETC_DEFERRED" != validate ]]; then
        genesis_etc_finalize commit
    fi
    return 0
}

_genesis_etc_put_impl() {
    local path="$1" content="$2" mode="$3" class="$4" stamped state legacy=0
    content="$(printf '%s' "$content")"
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
        GENESIS_ETC_EXISTED=1
        verb="updated-etc"
        if ! GENESIS_ETC_SNAPSHOT="$(mktemp)"; then
            _genesis_etc_uninspected "$path" "could not snapshot" kept-nonregular
            return 0
        fi
        if ! sudo cat "$path" > "$GENESIS_ETC_SNAPSHOT" 2>/dev/null; then
            _genesis_etc_uninspected "$path" "could not read"
            return 0
        fi
        if _genesis_etc_equal "$GENESIS_ETC_SNAPSHOT" "$content"; then
            legacy=1
        fi
    fi
    if ! stamped="$(printf '%s\n' "$content" | python3 -I -S "$_GENESIS_ETC_MU" stamp)"; then
        echo "  WARNING: could not stamp the content for $path — not written."
        if [[ "$legacy" == 1 ]] && _genesis_etc_original_intact "$path" "$GENESIS_ETC_SNAPSHOT"; then
            GENESIS_ETC_RESULT="unchanged"
        fi
        return 0
    fi
    if [ "$kind" = file ]; then
        if _genesis_etc_equal "$GENESIS_ETC_SNAPSHOT" "$stamped"; then
            GENESIS_ETC_RESULT="unchanged"
            return 0
        elif [[ "$legacy" == 1 ]]; then
            result="migrated"
        else
            state="$(python3 -I -S "$_GENESIS_ETC_MU" verify < "$GENESIS_ETC_SNAPSHOT" 2>/dev/null)" || state=""
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
                if ! (umask 077 && mkdir -p "$(dirname "$backup")" && cat "$GENESIS_ETC_SNAPSHOT" > "$backup"); then
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
    GENESIS_ETC_ATTEMPTED=1
    if ! sudo mkdir -p "$(dirname "$path")" 2>/dev/null \
        || ! printf '%s\n' "$stamped" | sudo tee "$path" >/dev/null 2>&1; then
        if [[ "$result" == migrated ]] && _genesis_etc_original_intact "$path" "$GENESIS_ETC_SNAPSHOT"; then
            echo "  WARNING: could not stamp $path; original effective configuration is unchanged."
            GENESIS_ETC_RESULT="unchanged"
        else
            echo "  WARNING: could not write $path (read-only /etc?) — not applied."
        fi
        return 0
    fi
    if [ -n "$mode" ]; then
        sudo chmod "$mode" "$path" 2>/dev/null \
            || echo "  WARNING: wrote $path but could not set its mode to $mode."
    fi
    GENESIS_ETC_RESULT="$result"
    case "$result" in
        taken) GENESIS_ETC_PENDING_SUMMARY="taken-etc $path: Genesis version installed, old file saved in $GENESIS_ETC_BACKUP" ;;
        written) GENESIS_ETC_PENDING_SUMMARY="$verb $path" ;;
    esac
    return 0
}
