#!/bin/bash

genesis_deploy_config_file() {
    printf '%s\n' "${GENESIS_DEPLOY_CONFIG:-${GENESIS_HOME:-$HOME/.genesis}/config/deploy.conf}"
}

genesis_deploy_pending_file() {
    printf '%s\n' "$(dirname "$(genesis_deploy_config_file)")/deploy.pending"
}

genesis_read_deploy_branch_file() {
    local file="$1"
    [ -r "$file" ] || return 1
    local branch
    branch="$(
        sed -n 's/^[[:space:]]*DEPLOY_BRANCH[[:space:]]*=[[:space:]]*//p' "$file" \
            | tail -n 1
    )"
    branch="${branch%$'\r'}"
    branch="${branch#"${branch%%[![:space:]]*}"}"
    branch="${branch%"${branch##*[![:space:]]}"}"
    if [[ "$branch" == \"*\" ]] || [[ "$branch" == \'*\' ]]; then
        branch="${branch:1:${#branch}-2}"
    fi
    [ -n "$branch" ] || return 1
    printf '%s\n' "$branch"
}

genesis_resolve_deploy_branch() {
    local repo="$1"
    local branch="${GENESIS_DEPLOY_BRANCH:-}"
    local config remote_head

    if [ -z "$branch" ]; then
        config="$(genesis_deploy_config_file)"
        if [ -r "$config" ]; then
            branch="$(genesis_read_deploy_branch_file "$config" || true)"
        fi
    fi

    if [ -z "$branch" ]; then
        remote_head="$(git -C "$repo" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
        branch="${remote_head#origin/}"
    fi
    branch="${branch:-main}"

    if ! git -C "$repo" check-ref-format --branch "$branch" >/dev/null 2>&1; then
        echo "ERROR: invalid Genesis deploy branch: $branch" >&2
        return 1
    fi
    printf '%s\n' "$branch"
}

genesis_assert_deploy_checkout() {
    local repo="$1"
    local deploy_branch="$2"
    local git_dir common_dir current_branch

    if ! git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "ERROR: Genesis deploy entry point must run from a Git checkout." >&2
        echo "       GENESIS_ROOT=$repo" >&2
        return 1
    fi

    git_dir="$(git -C "$repo" rev-parse --path-format=absolute --git-dir 2>/dev/null)" || return 1
    common_dir="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 1
    if [ "$git_dir" != "$common_dir" ]; then
        echo "ERROR: Genesis deploy entry points must not run from a linked worktree." >&2
        echo "       GENESIS_ROOT=$repo" >&2
        echo "       Run from the primary checkout instead." >&2
        return 1
    fi

    if ! current_branch="$(git -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null)"; then
        echo "ERROR: Genesis deploy entry points must not run from detached HEAD." >&2
        echo "       Expected branch: $deploy_branch" >&2
        return 1
    fi

    if [ "$current_branch" != "$deploy_branch" ] \
        && [ "${GENESIS_ALLOW_NON_DEPLOY_BRANCH:-0}" != "1" ]; then
        echo "ERROR: refusing to deploy from branch '$current_branch'." >&2
        echo "       Configured deploy branch: $deploy_branch" >&2
        echo "       Switch the primary checkout back before retrying." >&2
        return 1
    fi
}

genesis_ensure_deploy_config() {
    local deploy_branch="$1"
    local force="${2:-0}"
    local config config_dir tmp

    config="$(genesis_deploy_config_file)"
    if [ -e "$config" ] && [ "$force" != "1" ] \
        && { [ -z "${GENESIS_DEPLOY_BRANCH:-}" ] \
            || [ "${GENESIS_PERSIST_DEPLOY_BRANCH:-0}" != "1" ]; }; then
        return 0
    fi
    if [ -n "${GENESIS_DEPLOY_BRANCH:-}" ] \
        && [ "${GENESIS_PERSIST_DEPLOY_BRANCH:-0}" != "1" ] \
        && [ "$force" != "1" ]; then
        return 0
    fi

    config_dir="$(dirname "$config")"
    if ! mkdir -p "$config_dir"; then
        echo "ERROR: could not create Genesis deploy config directory: $config_dir" >&2
        return 1
    fi
    if ! tmp="$(mktemp "$config_dir/deploy.conf.XXXXXX")"; then
        echo "ERROR: could not create Genesis deploy config: $config" >&2
        return 1
    fi
    if ! printf 'DEPLOY_BRANCH=%s\n' "$deploy_branch" > "$tmp" \
        || ! chmod 600 "$tmp" \
        || ! mv "$tmp" "$config"; then
        rm -f "$tmp"
        echo "ERROR: could not persist Genesis deploy config: $config" >&2
        return 1
    fi
}

genesis_write_deploy_pending() {
    local deploy_branch="$1"
    local pending pending_dir tmp

    pending="$(genesis_deploy_pending_file)"
    pending_dir="$(dirname "$pending")"
    if ! mkdir -p "$pending_dir"; then
        echo "ERROR: could not create Genesis deploy pending directory: $pending_dir" >&2
        return 1
    fi
    if ! tmp="$(mktemp "$pending_dir/deploy.pending.XXXXXX")"; then
        echo "ERROR: could not create Genesis deploy pending branch file: $pending" >&2
        return 1
    fi
    if ! printf 'DEPLOY_BRANCH=%s\n' "$deploy_branch" > "$tmp" \
        || ! chmod 600 "$tmp" \
        || ! mv "$tmp" "$pending"; then
        rm -f "$tmp"
        echo "ERROR: could not persist Genesis deploy pending branch: $pending" >&2
        return 1
    fi
}
