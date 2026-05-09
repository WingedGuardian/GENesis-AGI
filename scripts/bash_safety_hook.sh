#!/usr/bin/env bash
# PreToolUse hook for Bash commands — blocks destructive operations.
# CC passes tool input as JSON on stdin with schema:
#   { "tool_input": { "command": "..." }, "tool_name": "Bash", ... }

CMD=$(jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$CMD" ] && exit 0

# pip install -e from/to worktree — catches both explicit worktree paths AND
# "pip install -e ." run from inside a worktree directory.
if echo "$CMD" | grep -qE "pip install.*(-e|--editable)"; then
    _block=0
    # Check 1: explicit worktree path in command
    echo "$CMD" | grep -qiE "worktree" && _block=1
    # Check 2: CWD is a git worktree (git-common-dir != git-dir)
    _gc=$(git rev-parse --git-common-dir 2>/dev/null)
    _gd=$(git rev-parse --git-dir 2>/dev/null)
    [ -n "$_gc" ] && [ -n "$_gd" ] && [ "$_gc" != "$_gd" ] && _block=1
    if [ "$_block" = 1 ]; then
        echo "BLOCKED: pip install -e from/to a worktree redirects ALL system genesis imports." >&2
        echo "This crashes the live bridge. Use PYTHONPATH instead:" >&2
        echo "  PYTHONPATH=/path/to/worktree/src pytest tests/" >&2
        exit 2
    fi
fi

# git worktree remove --force / -f
if echo "$CMD" | grep -qE "worktree remove.*(--force|-f )"; then
    echo "BLOCKED: git worktree remove --force destroys uncommitted work in the worktree." >&2
    echo "Use git worktree remove without --force, or ask the user first." >&2
    exit 2
fi

# Push / PR protection — remind to get explicit user approval
if echo "$CMD" | grep -qE "^git push|[;&|] *git push"; then
    echo "⚠️  STOP: git push detected. Have you received explicit user approval for this push? Do NOT take prior authorization as blanket approval. If you haven't asked in the last few messages, STOP and ask now." >&2
    exit 0
fi
if echo "$CMD" | grep -qE "^gh pr create|[;&|] *gh pr create"; then
    echo "⚠️  STOP: gh pr create detected. Have you received explicit user approval for this PR? Did you run a code review first? If not, STOP and ask now." >&2
    exit 0
fi

# gh pr merge — hard-block if GitHub hasn't confirmed the PR is conflict-free
if echo "$CMD" | grep -qE "^gh pr merge|[;&|] *gh pr merge"; then
    _pr_num=$(echo "$CMD" | grep -oE '[0-9]+' | head -1)
    _repo_flag=$(echo "$CMD" | grep -oP -- '--repo \S+' || true)
    _mergeable=""
    if [ -n "$_pr_num" ]; then
        _mergeable=$(gh pr view "$_pr_num" $_repo_flag --json mergeable --jq '.mergeable' 2>/dev/null)
        if [ "$_mergeable" = "UNKNOWN" ]; then
            echo "BLOCKED: PR #$_pr_num mergeable status is UNKNOWN." >&2
            echo "GitHub hasn't finished conflict analysis. Wait until mergeable status is known before retrying." >&2
            exit 2
        fi
        if [ "$_mergeable" = "CONFLICTING" ]; then
            echo "BLOCKED: PR #$_pr_num has merge conflicts. Resolve before merging." >&2
            exit 2
        fi
    fi
    echo "⚠️  STOP: gh pr merge detected (mergeable=$_mergeable). Have you received explicit user approval for this merge?" >&2
    exit 0
fi

# Other destructive commands
case "$CMD" in
    *"rm -rf /"*|*"rm -rf ~"*|*"rm -rf ."*|*"rm -rf .."*)
        echo "BLOCKED: rm -rf on broad paths is not allowed. Be specific or ask the user." >&2
        exit 2;;
    *"git push"*"--force"*|*"git push"*"-f"*)
        echo "BLOCKED: Force push not allowed. Use a PR." >&2
        exit 2;;
    *"git reset --hard"*)
        echo "BLOCKED: git reset --hard destroys uncommitted work. Use git stash or ask the user." >&2
        exit 2;;
    *"git clean -f"*|*"git clean -fd"*)
        echo "BLOCKED: git clean removes untracked files permanently. Ask the user first." >&2
        exit 2;;
esac
