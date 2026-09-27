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
    # for exactly that reason. On a host whose system Python has no PyYAML, bare
    # python3 cannot read the file at all — which now REFUSES (see below) rather
    # than reading as "no override", so the venv-first order is what keeps an
    # ordinary install working, not merely what keeps it correct.
    if [ -n "${VENV_DIR:-}" ] && [ -x "$VENV_DIR/bin/python" ]; then
        py="$VENV_DIR/bin/python"
    else
        py="$(command -v python3 2>/dev/null)" || return 1
    fi
    # THREE outcomes, and the distinction between the last two is the point:
    #   exit 0, a value   the key is set
    #   exit 0, empty     no config file, or the key is not set — ordinary
    #   exit 3, empty     the config EXISTS but cannot be read or parsed
    #
    # The third used to read as the second. That "declined" rather than guessed,
    # which was better than the hand-written parser it replaced (that one
    # RETURNED a branch from a file it could not parse). But declining is still a
    # permissive default: with a persisted override the reader could not see,
    # the resolver fell through to the remote's advertised HEAD, and when the
    # checkout happened to be on that branch too, validation passed and the
    # update deployed a target the operator had configured away from. A config
    # that is present but unreadable now REFUSES; callers must not swallow it.
    GH_KEY="$key" "$py" - <<'PY'
import os
import sys
from pathlib import Path

key = os.environ["GH_KEY"]
config = Path.home() / ".genesis" / "config" / "genesis.yaml"

if not config.exists():
    print("")
    sys.exit(0)

try:
    import yaml

    doc = yaml.safe_load(config.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"cannot read {config}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
    sys.exit(3)

if doc is None:
    doc = {}
github = doc.get("github") if isinstance(doc, dict) else None
if not isinstance(doc, dict) or not isinstance(github, (dict, type(None))):
    print(f"cannot read {config}: the `github` section is not a mapping", file=sys.stderr)
    sys.exit(3)

value = (github or {}).get(key)
print(str(value).strip() if value is not None else "")
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

    if ! branch="$(genesis_local_github_value deploy_branch)"; then
        echo "ERROR: cannot read github.deploy_branch from ~/.genesis/config/genesis.yaml;" >&2
        echo "       refusing rather than guessing a deploy target. Fix or remove the file." >&2
        return 1
    fi

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
