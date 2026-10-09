# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# The paths the core backup already restores, single-sourced by scripts/backup.sh
# and scripts/restore.sh for the opt-in extra directories (backup §6f, restore §4c).
# restore.sh replaces an extra directory as a unit, so an entry that overlaps one
# of these (equal to it, inside it, or containing it) would swap a freshly
# restored core path out of the way. backup.sh refuses such an entry and
# restore.sh refuses such an archive, from this one list. (Only this list is
# shared: backup.sh's other refusals, such as the backups repo, are its own.)
#
#   backup_core_paths          — print each core path, one per line (unresolved).
#   backup_core_overlap <abs>  — print the first core path that <abs> overlaps and
#                                return 0; return 1 when there is none, or 2 on
#                                inspection failure. <abs> must
#                                already be resolved (realpath).
#
# The caller sets HOME, GENESIS_DIR, TRANSCRIPT_DIR and _SCRIPT_DIR before calling either.

# Commands return exactly one path plus a formatter LF. Preserve path bytes and
# the original exit status; clear output on failure, refuse malformed success.
backup_capture_path() {
    local _bk_name="$1" _bk_value _bk_status
    shift
    case "$_bk_name" in ''|[0-9]*|*[!A-Za-z0-9_]*) return 2 ;; esac
    if _bk_value=$("$@"; _bk_status=$?; printf '.'; exit "$_bk_status"); then
        _bk_value=${_bk_value%.}
        if [[ "$_bk_value" != *$'\n' ]]; then
            printf -v "$_bk_name" '%s' ''
            return 2
        fi
        _bk_value=${_bk_value%$'\n'}
        if [ -z "$_bk_value" ]; then
            printf -v "$_bk_name" '%s' ''
            return 2
        fi
        printf -v "$_bk_name" '%s' "$_bk_value"
    else
        _bk_status=$?
        printf -v "$_bk_name" '%s' ''
        return "$_bk_status"
    fi
}

_backup_core_path_array() {
    # The merge-gate override store can be relocated (GENESIS_MERGE_OVERRIDE_DIR);
    # resolve it the way backup.sh §6d and restore.sh's audit section do. On a fresh
    # DR box that setting arrives with secrets.env, after §4c, so restore can only
    # check the default there; backup.sh, which has the setting loaded, refuses an
    # entry overlapping the configured store, so such an archive is never written.
    local override_store
    if ! backup_capture_path override_store python3 "$_SCRIPT_DIR/hooks/audit_jsonl.py" --store-dir GENESIS_MERGE_OVERRIDE_DIR 2>/dev/null; then
        override_store="$HOME/.genesis/merge_overrides"
    fi
    # Callers own this dynamically scoped array; safety uses no line transport.
    _backup_core_paths=(
        "$GENESIS_DIR" "$TRANSCRIPT_DIR" "$HOME/.genesis/eval"
        "$HOME/.genesis/shared" "$HOME/.genesis/merge_overrides"
        "$override_store" "$HOME/.genesis/restore-creds"
        "$HOME/.genesis/restore_status.json" "$HOME/.ssh"
        "$HOME/.config/gh" "$HOME/.local/state/genesis-guardian"
    )
}

backup_core_paths() {
    local -a _backup_core_paths=()
    _backup_core_path_array
    printf '%s\n' "${_backup_core_paths[@]}"
}

# Normalized absolute paths: 0 overlap, 1 disjoint, 2 invalid input. Root is an
# ancestor of every absolute path; appending '/' must not turn it into '//'.
backup_paths_overlap() {
    local _bk_left="$1" _bk_right="$2"
    case "$_bk_left" in /*) ;; *) return 2 ;; esac
    case "$_bk_right" in /*) ;; *) return 2 ;; esac
    if [ "$_bk_left" = / ] || [ "$_bk_right" = / ]; then
        return 0
    fi
    case "$_bk_left/" in "$_bk_right"/*) return 0 ;; esac
    case "$_bk_right/" in "$_bk_left"/*) return 0 ;; esac
    return 1
}

backup_core_overlap() {
    local abs="$1" core core_real
    local -a _backup_core_paths=()
    [ -n "$abs" ] || return 2
    _backup_core_path_array || return 2
    for core in "${_backup_core_paths[@]}"; do
        [ -n "$core" ] || continue
        backup_capture_path core_real realpath -m -- "$core" || return 2
        if backup_paths_overlap "$abs" "$core_real"; then
            printf '%s\n' "$core"
            return 0
        elif [ "$?" -ne 1 ]; then
            return 2
        fi
    done
    return 1
}
