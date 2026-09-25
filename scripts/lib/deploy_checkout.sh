#!/bin/bash

# update.sh has exactly one deploy object: the local checkout it is about to
# mutate. Resolve the branch from the remote that will actually be fetched, then
# prove that checkout can receive it before any deploy state is touched.
genesis_resolve_deploy_branch() {
    local repo="$1"
    local remote="$2"
    local branch="${GENESIS_DEPLOY_BRANCH:-}"
    local remote_head

    if [ -z "$branch" ]; then
        remote_head="$(
            git -C "$repo" symbolic-ref --quiet --short "refs/remotes/$remote/HEAD" \
                2>/dev/null || true
        )"
        branch="${remote_head#"$remote"/}"
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
        echo "ERROR: update.sh must run from a Git checkout." >&2
        echo "       GENESIS_ROOT=$repo" >&2
        return 1
    fi

    git_dir="$(git -C "$repo" rev-parse --path-format=absolute --git-dir 2>/dev/null)" || return 1
    common_dir="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 1
    if [ "$git_dir" != "$common_dir" ]; then
        echo "ERROR: update.sh must not run from a linked worktree." >&2
        echo "       GENESIS_ROOT=$repo" >&2
        echo "       Run from the primary checkout instead." >&2
        return 1
    fi

    if ! current_branch="$(git -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null)"; then
        echo "ERROR: update.sh must not run from detached HEAD." >&2
        echo "       Expected branch: $deploy_branch" >&2
        return 1
    fi

    if [ "$current_branch" != "$deploy_branch" ] \
        && [ "${GENESIS_ALLOW_NON_DEPLOY_BRANCH:-0}" != "1" ]; then
        echo "ERROR: refusing to deploy from branch '$current_branch'." >&2
        echo "       Deploy branch: $deploy_branch" >&2
        echo "       Switch the primary checkout back before retrying." >&2
        return 1
    fi
}
