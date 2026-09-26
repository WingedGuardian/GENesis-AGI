#!/bin/bash

# update.sh has exactly one deploy object: the local checkout it is about to
# mutate. Resolve the branch from the remote that will actually be fetched, then
# prove that checkout can receive it before any deploy state is touched.
genesis_local_github_value() {
    local key="$1"
    GH_KEY="$key" python3 - <<'PY' 2>/dev/null
import os
from pathlib import Path

key = os.environ["GH_KEY"]
config = Path.home() / ".genesis" / "config" / "genesis.yaml"


def _scalar(value: str) -> str:
    """Read a SIMPLE YAML scalar, or nothing at all.

    This fallback exists only for a python3 without PyYAML, and the one thing it
    must never do is return a LOSSY reading. `'release/it''s'` is a valid
    single-quoted scalar meaning `release/it's`; stopping at the doubled quote
    yields `release/it`, which is ALSO a valid branch name, so no later
    validation catches the difference and update.sh would fetch and activate a
    different branch from the one every Python consumer resolves with
    yaml.safe_load.

    Returning nothing lets the caller fall through to the remote HEAD and be
    refused by the checkout validation if that disagrees. Returning half a branch
    name is the failure this whole script exists to prevent.
    """
    value = value.strip()
    if value and value[0] in "'\"":
        quote = value[0]
        end = value.find(quote, 1)
        if end < 1:
            return ""
        rest = value[end + 1 :].strip()
        # Anything but a comment after the closing quote means the scalar uses
        # syntax this parser does not implement -- a doubled quote, an escape,
        # a continuation -- so decline instead of guessing.
        if rest and not rest.startswith("#"):
            return ""
        return value[1:end].strip()
    return value.split(" #", 1)[0].strip()


try:
    import yaml

    github = (yaml.safe_load(config.read_text(encoding="utf-8")) or {}).get("github") or {}
    value = github.get(key) if isinstance(github, dict) else None
    print(str(value).strip() if value is not None else "")
except Exception:
    value = ""
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    in_github = False
    # The DIRECT-child indentation of the github block, learned from its first
    # child. Without it this loop ignores YAML hierarchy entirely and a nested
    # mapping wins: `github:` with `deploy_branch: right` and then
    # `credentials:` containing `deploy_branch: wrong` resolved to `wrong` here
    # while yaml.safe_load and every Python consumer resolved `right`. Both are
    # valid branch names, so nothing downstream catches it and update.sh could
    # fetch and activate a branch no other reader agrees on.
    child_indent = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            in_github = stripped.startswith("github:")
            child_indent = None
            if in_github and stripped != "github:":
                flow = stripped.split(":", 1)[1].strip()
                if flow.startswith("{") and flow.endswith("}"):
                    for item in flow[1:-1].split(","):
                        item_key, separator, item_value = item.partition(":")
                        if separator and item_key.strip() == key:
                            value = _scalar(item_value)
            continue
        if not in_github:
            continue
        if child_indent is None:
            child_indent = indent
        if indent > child_indent:
            # Deeper than a direct child, so it belongs to some nested mapping.
            continue
        if indent < child_indent:
            # Dedented out of the github block without returning to column 0,
            # which this parser cannot model. Stop rather than guess.
            in_github = False
            continue
        if stripped.startswith(f"{key}:"):
            value = _scalar(stripped.split(":", 1)[1])
    print(value)
PY
}

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
