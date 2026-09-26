#!/bin/bash

# update.sh has exactly one deploy object: the local checkout it is about to
# mutate. Resolve the branch from the remote that will actually be fetched, then
# prove that checkout can receive it before any deploy state is touched.
genesis_local_github_value() {
    local key="$1"
    local py
    # Venv FIRST, bare python3 only as a fallback. PyYAML is a hard dependency of
    # the venv (pyproject.toml), not of the system interpreter — and before this
    # helper existed, update.sh read `public_repo` through "$VENV_DIR/bin/python"
    # for exactly that reason. Reading it with bare python3 would silently lose
    # the key on any host whose system Python has no PyYAML.
    if [ -n "${VENV_DIR:-}" ] && [ -x "$VENV_DIR/bin/python" ]; then
        py="$VENV_DIR/bin/python"
    else
        py="$(command -v python3 2>/dev/null)" || return 1
    fi
    GH_KEY="$key" "$py" - <<'PY' 2>/dev/null
import os
from pathlib import Path

key = os.environ["GH_KEY"]
config = Path.home() / ".genesis" / "config" / "genesis.yaml"

try:
    import yaml

    github = (yaml.safe_load(config.read_text(encoding="utf-8")) or {}).get("github") or {}
    value = github.get(key) if isinstance(github, dict) else None
    print(str(value).strip() if value is not None else "")
except Exception:
    # DECLINE. There used to be a 55-line hand parser here for a python3 without
    # PyYAML.
    #
    # Returning nothing is correct on its own terms: the caller falls through to
    # the remote's advertised HEAD, and `genesis_assert_deploy_checkout` then
    # refuses the deploy outright if that disagrees with the branch actually
    # checked out. The hand parser instead RETURNED A BRANCH -- a guess, produced
    # by a path whose entire premise is that the config could not be read, and
    # handed to the one caller that acts on it by mutating a checkout.
    #
    # It was also unnecessary on the path that matters. `pyyaml` is a hard
    # dependency of the venv (pyproject.toml), this helper prefers the venv
    # interpreter, and every other YAML read in update.sh already runs on
    # "$VENV_DIR/bin/python". The bare-python3 fallback exists only for a
    # checkout with no venv yet — and there, declining is the safe answer.
    #
    # And the parser's own docstring had the right answer all along: returning
    # nothing lets the caller fall through to the remote HEAD and be refused by
    # the checkout validation if that disagrees. Three review rounds fixed three
    # defects in it -- a lossy quoted scalar, ignored YAML hierarchy, and being
    # reached on ANY safe_load failure rather than just a missing module -- each
    # fix creating the surface for the next. Deleting it closes all three.
    print("")
PY
}

# NOTE: update.sh is the only consumer of `github.deploy_branch` today. The
# dashboard update check (`dashboard/routes/updates.py`, hard-coded
# `origin/main` — remote AND branch) and the version collector
# (`learning/signals/genesis_version.py`, `<remote>/main` — it resolves the
# remote but not the branch) both ignore it, so setting this key to anything but the remote's
# default branch makes those two describe a branch this script does not deploy.
# (`observability/snapshots/deploy_health.py` counts `HEAD..@{upstream}`, so it
# follows whatever the checkout tracks and does not diverge on the branch name.)
# Giving all of them one resolver is issue #2419; no Python reader reads this key
# in the meantime.
genesis_resolve_deploy_branch() {
    local repo="$1"
    local remote="$2"
    local branch
    local remote_head

    branch="$(genesis_local_github_value deploy_branch || true)"

    if [ -z "$branch" ]; then
        # Ask the remote for its advertised HEAD without depending on a local
        # tracking ref for that branch; refs/remotes/<remote>/HEAD is only the
        # fallback when the live query cannot answer.
        # `|| true` is load-bearing. update.sh runs under `set -Eeuo pipefail`,
        # so a timed-out or refused `ls-remote` makes this substitution non-zero
        # even though awk completed, and the ASSIGNMENT then exits the shell —
        # before the cached-HEAD fallback below can run. An unreachable remote
        # would refuse a deployment that the local symbolic ref could have
        # resolved. An unsuccessful probe must read as an EMPTY answer, not as a
        # fatal one; the `check-ref-format` validation below still gates whatever
        # either path produces.
        remote_head="$(
            timeout 15 git -C "$repo" ls-remote --symref "$remote" HEAD 2>/dev/null |
                awk '$1 == "ref:" && $2 ~ /^refs\/heads\// && $3 == "HEAD" {
                    sub("^refs/heads/", "", $2); print $2; exit
                }' || true
        )"
        if [ -n "$remote_head" ] \
            && git -C "$repo" check-ref-format --branch "$remote_head" >/dev/null 2>&1; then
            # Cache the live default so local-only consumers see the same target.
            git -C "$repo" symbolic-ref "refs/remotes/$remote/HEAD" \
                "refs/remotes/$remote/$remote_head" >/dev/null 2>&1 || true
        else
            remote_head="$(
                git -C "$repo" symbolic-ref --quiet --short "refs/remotes/$remote/HEAD" \
                    2>/dev/null || true
            )"
            case "$remote_head" in
                "$remote/"*) remote_head="${remote_head#"$remote"/}" ;;
                *) remote_head="" ;;
            esac
        fi
        branch="$remote_head"
    fi
    branch="${branch:-main}"

    if ! git -C "$repo" check-ref-format --branch "$branch" >/dev/null 2>&1; then
        echo "ERROR: invalid Genesis deploy branch: $branch" >&2
        return 1
    fi
    printf '%s\n' "$branch"
}

# The branch-INDEPENDENT half of the checkout validation: is this a git checkout,
# and is it the primary one rather than a linked worktree? Split out so update.sh
# can run it BEFORE `genesis_resolve_deploy_branch`, whose live-probe path
# refreshes `refs/remotes/<remote>/HEAD` — a ref stored in the COMMON git dir,
# shared with every worktree. Run from a linked worktree, the old order wrote
# that shared ref and only then refused.
genesis_assert_primary_checkout() {
    local repo="$1"
    local git_dir common_dir

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
}

genesis_assert_deploy_checkout() {
    local repo="$1"
    local deploy_branch="$2"
    local current_branch

    genesis_assert_primary_checkout "$repo" || return 1

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
