# shellcheck shell=bash
# deploy_recovery.sh — mutating and printing deploy-recovery helpers.
#
# Unlike deploy_checkout.sh's silent predicates, these helpers may echo; they
# leave terminating the caller to the script that sourced them.

# Git calls that run hooks go through genesis_without_checkout_lock
# (lib/checkout_lock.sh), so a hook's background child cannot hold the checkout
# lock. bootstrap.sh sources this lib without that one and never holds the lock,
# so a plain pass-through stands in; checkout_lock.sh, sourced before or after,
# provides the real one.
declare -F genesis_without_checkout_lock >/dev/null \
    || genesis_without_checkout_lock() { "$@"; }

EPHEMERAL_CLEAR_PATHS=(AGENTS.md config/procedure_triggers.yaml)
EPHEMERAL_BACKUP_ROOT="$HOME/.genesis/premerge-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"

# Tracked AND different from HEAD in the index or the worktree.
_ephemeral_is_dirty() {
    git -C "$GENESIS_ROOT" ls-files --error-unmatch "$1" &>/dev/null \
        && ! git -C "$GENESIS_ROOT" diff --quiet HEAD -- "$1" 2>/dev/null
}

# Save <path>'s local edits under <dest-root>/<path>/: the worktree and index
# patches against HEAD, plus a copy of the file. Returns non-zero on ANY failure.
_ephemeral_backup() {
    local p="$1" dest="$2/$1"
    mkdir -p "$dest" && chmod 700 "$2" "$dest" \
        && git -C "$GENESIS_ROOT" diff --binary HEAD -- "$p" > "$dest/worktree.patch" \
        && git -C "$GENESIS_ROOT" diff --binary --cached HEAD -- "$p" > "$dest/index.patch" \
        && { [ ! -e "$GENESIS_ROOT/$p" ] || cp -p "$GENESIS_ROOT/$p" "$dest/current"; }
}

# Does the backup under <dest-root> still describe <path>'s edits exactly?
_ephemeral_backup_is_current() {
    local p="$1" dest="$2/$1"
    [ -f "$dest/worktree.patch" ] && [ -f "$dest/index.patch" ] \
        && cmp -s "$dest/worktree.patch" <(git -C "$GENESIS_ROOT" diff --binary HEAD -- "$p") \
        && cmp -s "$dest/index.patch" <(git -C "$GENESIS_ROOT" diff --binary --cached HEAD -- "$p")
}

# Called by _do_rollback before its checkout, which may clear an ephemeral file's
# edit first (_ephemeral_clear_before_reset). The pre-stop backup can be missing (a
# --post-merge run takes none) or stale (an indexer rewrote AGENTS.md after it), so
# each dirty ephemeral file whose CURRENT edits are not already saved is backed up
# under <root>/rollback. Never fails: a failed backup is named, not fatal, and the
# edit is then not cleared (the clear needs a current backup).
_ephemeral_backup_before_reset() {
    local root="$1" p
    for p in "${EPHEMERAL_CLEAR_PATHS[@]}"; do
        _ephemeral_is_dirty "$p" || continue
        if _ephemeral_backup_is_current "$p" "$root" \
            || _ephemeral_backup_is_current "$p" "$root/late"; then
            continue
        fi
        if _ephemeral_backup "$p" "$root/rollback"; then
            echo "  Backed up local edits to $p before the rollback: $root/rollback/$p"
        else
            echo "  WARNING: could not back up local edits to $p before the rollback; they are left in place, and the rollback refuses if this update changed $p."
        fi
    done
    return 0
}

# Clear an ephemeral file's local edit before the rollback's checkout, but only when
# the checkout would otherwise refuse over it (the range HEAD..tag touches it) AND a
# current backup of exactly that edit exists. Anything else is left for the checkout
# to judge.
_ephemeral_clear_before_reset() {
    local root="$1" p
    for p in "${EPHEMERAL_CLEAR_PATHS[@]}"; do
        _ephemeral_is_dirty "$p" || continue
        git -C "$GENESIS_ROOT" diff --quiet HEAD "$ROLLBACK_TAG" -- "$p" 2>/dev/null && continue
        if _ephemeral_backup_is_current "$p" "$root" \
            || _ephemeral_backup_is_current "$p" "$root/late" \
            || _ephemeral_backup_is_current "$p" "$root/rollback"; then
            genesis_without_checkout_lock git -C "$GENESIS_ROOT" checkout -q HEAD -- "$p" 2>&1 \
                || echo "  WARNING: could not clear the backed-up edit to $p; the rollback will refuse over it."
        fi
    done
    return 0
}

# The rollback undoes this run's merge by switching the branch the run started on
# back to the rollback tag with a NON-forced checkout:
#     git checkout -q --no-overwrite-ignore -B "$ORIGINAL_BRANCH" "$ROLLBACK_TAG"
# Not `reset --hard`, which discards every edit, and not `reset --keep`, which
# overwrites ignored files and rewrites the index (both measured): another session
# can edit the checkout after the merge, and those edits are nobody's to throw
# away. The
# checkout is a two-way switch from HEAD to the tag, measured on git 2.43:
#   - it rewrites only the paths that differ between HEAD and the tag; every other
#     path keeps its edits in place, and the index keeps its staged state (a staged
#     change, a staged new file, content that exists in the index alone);
#   - it refuses, rc 1, moving nothing (HEAD, the branch, the index and every file
#     as they were), when a path it writes carries a local change: staged or
#     unstaged, behind assume-unchanged or skip-worktree, a mode or type change;
#   - with --no-overwrite-ignore it refuses the same way over an untracked or
#     IGNORED file where it writes, an ignored file inside a directory it would
#     replace with a file, an ignored symlink where it needs a directory, and an
#     untracked nested repository where it writes a file. `reset --keep`
#     overwrites or deletes the ignored ones without asking.
# git checks each path and writes it inside that one command, so an edit or an
# ignored file that appears at any moment before it is kept or makes it refuse;
# no separate scan runs earlier that could go stale. What remains is the window
# inside the checkout itself, which checks every path it will write and then
# writes them all: a change landing during that write phase is not seen. A
# refusal leaves the merged code,
# the edit and the migrated database in place for a person to sort out: a
# rollback left undone is recoverable, a lost edit is not.
#
# Handled before the checkout:
#   - a SUBMODULE (gitlink) changed by the range refuses the rollback: files inside
#     a submodule are outside every check git makes here, and a gitlink-to-file
#     switch replaced a submodule's modified and untracked files (measured, for
#     `reset --keep` and for this checkout alike);
#   - the ephemeral files an indexer rewrites would make every rollback over them
#     refuse, so an edit the range touches is cleared once a current backup of it
#     exists (_ephemeral_clear_before_reset), exactly as before the merge.
#
# Undo this deploy's merge with a non-forced checkout. Return 0 only when the
# original branch is at the rollback commit; otherwise leave the code in place.
genesis_rollback_checkout() {
    local GENESIS_ROOT="$1" ROLLBACK_TAG="$2" ORIGINAL_BRANCH="$3"
    local EPHEMERAL_BACKUP_ROOT="$4" rb_commit="$5"

    # Save current edits not yet covered by the pre-stop backup; the clear below
    # discards one only when a current backup of those edits exists.
    _ephemeral_backup_before_reset "$EPHEMERAL_BACKUP_ROOT"

    # A submodule (gitlink, mode 160000) on either side of the range refuses
    # the rollback: the files inside it are outside every check git makes
    # here. The listing is captured before it is read, so a git failure
    # refuses too rather than reading as "no submodule". It is plumbing
    # (diff-tree), not `git diff`, because porcelain honours
    # diff.ignoreSubmodules and a .gitmodules `ignore = all`, which drop
    # gitlink lines from the listing (measured, git 2.43).
    local rb_raw="" rb_line="" rb_gitlinks="" rb_mode_re='^:([0-7]+) ([0-7]+) '
    if ! rb_raw="$(git -C "$GENESIS_ROOT" diff-tree -r --raw --no-renames --no-abbrev HEAD "$ROLLBACK_TAG" 2>/dev/null)"; then
        echo "  CRITICAL: cannot list what the rollback to $ROLLBACK_TAG changes, so the merge was NOT rolled back."
        return 1
    fi
    while IFS= read -r rb_line; do
        if [[ "$rb_line" =~ $rb_mode_re ]] \
            && { [ "${BASH_REMATCH[1]}" = 160000 ] || [ "${BASH_REMATCH[2]}" = 160000 ]; }; then
            rb_gitlinks+="${rb_line#*$'\t'}"$'\n'
        fi
    done <<< "$rb_raw"
    if [ -n "$rb_gitlinks" ]; then
        echo "  CRITICAL: the rollback to $ROLLBACK_TAG would change a submodule, whose files git does not check before replacing them, so the merge was NOT rolled back:"
        printf '%s' "$rb_gitlinks" | sed 's/^/    submodule: /'
        return 1
    fi

    _ephemeral_clear_before_reset "$EPHEMERAL_BACKUP_ROOT" || true
    # The checkout refreshes the index itself (measured, git 2.43); this
    # refresh is kept so a file rewritten with identical bytes (an indexer,
    # a touch) can never read as a local change. It updates only cached stat
    # data: a real edit, assume-unchanged included, still refuses.
    git -C "$GENESIS_ROOT" update-index -q --refresh >/dev/null 2>&1 || true
    # -B names the branch: it is $ORIGINAL_BRANCH that is moved back, never
    # whatever branch HEAD may have been switched to since the check above
    # (a reset moves HEAD's branch). A refusal (rc 1) moves nothing: HEAD,
    # the branch, the index and every file stay as they were.
    # Hooks are switched off: checkout runs post-checkout AFTER it has moved
    # HEAD and the files, and returns the hook's exit status, so a failing hook
    # would report a completed rollback as refused and keep the migrated
    # database under the old code. Whatever the exit status, the rollback
    # counts as done when HEAD is the tag on $ORIGINAL_BRANCH, the same test
    # the restart below uses.
    git -C "$GENESIS_ROOT" -c core.hooksPath=/dev/null checkout -q --no-overwrite-ignore -B "$ORIGINAL_BRANCH" "$ROLLBACK_TAG" 2>&1 || true
    if genesis_checkout_unmoved "$GENESIS_ROOT" "$rb_commit" "$ORIGINAL_BRANCH"; then
        return 0
    fi
    echo "  CRITICAL: git refused to switch $ORIGINAL_BRANCH back to $ROLLBACK_TAG (its reason is above: usually a local change to a file this update changed, or an untracked or ignored file where the rollback writes, each kept as it is; or another git operation in progress), so the merge was NOT rolled back."
    return 1
}
