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

# Push / PR protection — requires explicit user approval every time
if echo "$CMD" | grep -qE "^git push|[;&|] *git push"; then
    echo "BLOCKED: git push requires explicit user approval. Ask the user before pushing." >&2
    exit 2
fi
if echo "$CMD" | grep -qE "^gh pr create|[;&|] *gh pr create"; then
    echo "BLOCKED: PR creation requires explicit user approval. Ask the user before creating a PR." >&2
    exit 2
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
