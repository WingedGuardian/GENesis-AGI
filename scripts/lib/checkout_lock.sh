genesis_checkout_lock() {
    local root="${1:?checkout root required}" common_dir lock_path wait_s lock_fd
    if [ -n "${GENESIS_CHECKOUT_LOCK_FD:-}" ]; then
        return 0
    fi
    if ! common_dir="$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
        || [ -z "$common_dir" ]; then
        echo "WARNING: cannot resolve checkout lock path for $root; continuing without the lock" >&2
        return 0
    fi
    lock_path="$common_dir/genesis-checkout.lock"
    # Opened under a local name: GENESIS_CHECKOUT_LOCK_FD means "held", and the
    # re-entry check above trusts it, so it is set only once flock succeeds. Set
    # at open, a signal trap running during the wait below would take the lock
    # as held and change the checkout unfenced.
    if ! exec {lock_fd}>>"$lock_path"; then
        echo "WARNING: cannot open checkout lock $lock_path; continuing without the lock" >&2
        return 0
    fi
    wait_s="${GENESIS_CHECKOUT_LOCK_WAIT_S:-300}"
    case "$wait_s" in
        ''|*[!0-9]*)
            # A typo here must not read as "checkout busy" further down.
            echo "WARNING: GENESIS_CHECKOUT_LOCK_WAIT_S='$wait_s' is not a whole number of seconds; using 300" >&2
            wait_s=300
            ;;
    esac
    # ASSUMED, unmeasured bound
    # Shared holders can starve a waiting exclusive lock, as in deploy_code_only.sh.
    if flock -x -w "$wait_s" "$lock_fd"; then
        GENESIS_CHECKOUT_LOCK_FD=$lock_fd
        return 0
    fi
    exec {lock_fd}>&-
    echo "checkout busy (a Claude launch holds genesis-checkout.lock); nothing changed" >&2
    return 1
}

# Run a command with the checkout-lock descriptor closed in it. git runs hooks
# (post-checkout, post-merge) as children of the command, and a hook that leaves
# work running in the background would otherwise inherit the descriptor and hold
# the lock after the parent releases it, so every Claude launch waits on it.
# bash cannot mark a descriptor close-on-exec, so each git call made while the
# lock is held goes through here. A no-op wrapper when the lock is not held.
genesis_without_checkout_lock() {
    if [ -n "${GENESIS_CHECKOUT_LOCK_FD:-}" ]; then
        "$@" {GENESIS_CHECKOUT_LOCK_FD}>&-
    else
        "$@"
    fi
}

genesis_checkout_unlock() {
    if [ -n "${GENESIS_CHECKOUT_LOCK_FD:-}" ]; then
        exec {GENESIS_CHECKOUT_LOCK_FD}>&-
        unset GENESIS_CHECKOUT_LOCK_FD
    fi
}
