genesis_checkout_lock() {
    local root="${1:?checkout root required}" common_dir lock_path wait_s
    if [ -n "${GENESIS_CHECKOUT_LOCK_FD:-}" ]; then
        return 0
    fi
    if ! common_dir="$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
        || [ -z "$common_dir" ]; then
        echo "WARNING: cannot resolve checkout lock path for $root; continuing without the lock" >&2
        return 0
    fi
    lock_path="$common_dir/genesis-checkout.lock"
    if ! exec {GENESIS_CHECKOUT_LOCK_FD}>>"$lock_path"; then
        echo "WARNING: cannot open checkout lock $lock_path; continuing without the lock" >&2
        return 0
    fi
    wait_s="${GENESIS_CHECKOUT_LOCK_WAIT_S:-300}"
    # ASSUMED, unmeasured bound
    # Shared holders can starve a waiting exclusive lock, as in deploy_code_only.sh.
    if flock -x -w "$wait_s" "$GENESIS_CHECKOUT_LOCK_FD"; then
        return 0
    fi
    genesis_checkout_unlock
    echo "checkout busy (a Claude launch holds genesis-checkout.lock); nothing changed" >&2
    return 1
}

genesis_checkout_unlock() {
    if [ -n "${GENESIS_CHECKOUT_LOCK_FD:-}" ]; then
        exec {GENESIS_CHECKOUT_LOCK_FD}>&-
        unset GENESIS_CHECKOUT_LOCK_FD
    fi
}
