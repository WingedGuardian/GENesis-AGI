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
#                                return 0; return 1 when there is none. <abs> must
#                                already be resolved (realpath).
#
# The caller sets HOME, GENESIS_DIR, TRANSCRIPT_DIR and _SCRIPT_DIR before calling either.

backup_core_paths() {
    # The merge-gate override store can be relocated (GENESIS_MERGE_OVERRIDE_DIR);
    # resolve it the way backup.sh §6d and restore.sh's audit section do. On a fresh
    # DR box that setting arrives with secrets.env, after §4c, so restore can only
    # check the default there; backup.sh, which has the setting loaded, refuses an
    # entry overlapping the configured store, so such an archive is never written.
    local override_store
    override_store="$(python3 "$_SCRIPT_DIR/hooks/audit_jsonl.py" --store-dir GENESIS_MERGE_OVERRIDE_DIR 2>/dev/null \
        || printf '%s' "$HOME/.genesis/merge_overrides")"
    printf '%s\n' \
        "$GENESIS_DIR" \
        "$TRANSCRIPT_DIR" \
        "$HOME/.genesis/eval" \
        "$HOME/.genesis/shared" \
        "$HOME/.genesis/merge_overrides" \
        "$override_store" \
        "$HOME/.genesis/restore-creds" \
        "$HOME/.genesis/restore_status.json" \
        "$HOME/.ssh" \
        "$HOME/.config/gh" \
        "$HOME/.local/state/genesis-guardian"
}

backup_core_overlap() {
    local abs="$1" core core_real
    while IFS= read -r core; do
        [ -n "$core" ] || continue
        core_real="$(realpath -m -- "$core")"
        case "$abs/" in
            "$core_real"/*) printf '%s\n' "$core"; return 0 ;;
        esac
        case "$core_real/" in
            "$abs"/*) printf '%s\n' "$core"; return 0 ;;
        esac
    done < <(backup_core_paths)
    return 1
}
