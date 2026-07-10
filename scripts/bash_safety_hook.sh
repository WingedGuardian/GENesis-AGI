#!/usr/bin/env bash
# PreToolUse hook for Bash commands — blocks destructive operations.
# CC passes tool input as JSON on stdin with schema:
#   { "tool_input": { "command": "..." }, "tool_name": "Bash", ... }

CMD=$(jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$CMD" ] && exit 0

# Bash allowlist gate — scoped background profiles (e.g. "steward") export
# GENESIS_BASH_ALLOWLIST (comma-separated command binaries, e.g. "gh"). When set,
# the command's first token must be one of them, and no chaining/piping/
# substitution/redirection is permitted (those could escape the allowlist).
# Unset → no effect (every other session behaves exactly as before).
if [ -n "$GENESIS_BASH_ALLOWLIST" ]; then
    # Reject embedded newlines first — a `case` glob does not reliably match
    # $'\n', so use a line count (printf adds no trailing newline, so any count
    # > 0 means an embedded newline → a second command on its own line).
    if [ "$(printf '%s' "$CMD" | wc -l)" -gt 0 ]; then
        echo "BLOCKED: multi-line commands are not permitted in an allowlisted session ($GENESIS_BASH_ALLOWLIST)." >&2
        exit 2
    fi
    case "$CMD" in
        *';'*|*'&&'*|*'||'*|*'|'*|*'`'*|*'$('*|*'>'*|*'<'*)
            echo "BLOCKED: this session's Bash may not chain, pipe, substitute, or redirect (allowlist: $GENESIS_BASH_ALLOWLIST)." >&2
            exit 2;;
    esac
    _first=$(printf '%s' "$CMD" | awk '{print $1}')
    case ",$GENESIS_BASH_ALLOWLIST," in
        *",$_first,"*) : ;;  # first token is allowlisted — fall through to the standard checks
        *)
            echo "BLOCKED: this session may only run [$GENESIS_BASH_ALLOWLIST] commands; got '$_first'." >&2
            exit 2;;
    esac
fi

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

# genesis serve from/against a worktree — booting the FULL runtime from a
# worktree spawns children that inherit its PYTHONPATH and cold-starts every
# path-keyed subsystem (Serena LSP, code indexers, GitNexus) against the
# worktree as a "new" ~190K-LOC project. This OOM-crashed the container on
# 2026-07-03 (same failure family as the 2026-03-16 editable-install spiral).
# PYTHONPATH-to-worktree is for pytest ONLY; runtime verification of worktree
# code goes through merge-then-verify or a minimal blueprint-only harness.
if echo "$CMD" | grep -qE "genesis[[:space:]]+serve"; then
    _block=0
    # Check 1: explicit worktree path anywhere in the command (incl. PYTHONPATH=)
    echo "$CMD" | grep -qiE "worktree" && _block=1
    # Check 2: CWD is a git worktree (git-common-dir != git-dir)
    _gc=$(git rev-parse --git-common-dir 2>/dev/null)
    _gd=$(git rev-parse --git-dir 2>/dev/null)
    [ -n "$_gc" ] && [ -n "$_gd" ] && [ "$_gc" != "$_gd" ] && _block=1
    if [ "$_block" = 1 ]; then
        echo "BLOCKED: never boot the full Genesis runtime from/against a worktree." >&2
        echo "Children inherit PYTHONPATH and path-keyed subsystems reindex the worktree" >&2
        echo "as a new project — this OOM-crashed the container on 2026-07-03." >&2
        echo "PYTHONPATH to a worktree is for pytest only. For runtime verification:" >&2
        echo "merge-then-verify with rollback, or a blueprint-only Flask harness." >&2
        exit 2
    fi
fi

# git worktree remove --force / -f
if echo "$CMD" | grep -qE "worktree remove.*(--force|-f )"; then
    echo "BLOCKED: git worktree remove --force destroys uncommitted work in the worktree." >&2
    echo "Use git worktree remove without --force, or ask the user first." >&2
    exit 2
fi

# Hard-blocked destructive commands — checked BEFORE the softer push/PR warnings
# below, because the generic "git push" warning (exit 0) would otherwise
# short-circuit a force-push before this block could hard-block it.
case "$CMD" in
    *"rm -rf /"*|*"rm -rf ~"*|*"rm -rf ."*)  # "rm -rf ." also covers ".."
        echo "BLOCKED: rm -rf on broad paths is not allowed. Be specific or ask the user." >&2
        exit 2;;
    *"git reset --hard"*)
        echo "BLOCKED: git reset --hard destroys uncommitted work. Use git stash or ask the user." >&2
        exit 2;;
    *"git clean -f"*)  # substring also covers -fd
        echo "BLOCKED: git clean removes untracked files permanently. Ask the user first." >&2
        exit 2;;
esac

# Force push — hard-block. MUST precede the soft "git push" warning below (which
# exits 0). Match -f only as a FLAG token — a whitespace-delimited short-flag
# cluster containing 'f' (so '-f', '-fv', '-uf' all count; 'f' is force-only
# among push short flags). The leading start/space anchor is what keeps a branch
# name that merely CONTAINS "-f" — e.g. "skill-funnel", "bug-fix" — from looking
# like a force push. Also covers any --force* variant.
if echo "$CMD" | grep -qE 'git push' \
   && echo "$CMD" | grep -qE -- '(^|[[:space:]])-[a-zA-Z]*f[a-zA-Z]*([[:space:]]|$)|--force'; then
    echo "BLOCKED: Force push not allowed. Use a PR." >&2
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

# gh pr merge — hard-block if GitHub hasn't confirmed the PR is conflict-free.
# Fail CLOSED on an unresolvable PR: a no-arg `gh pr merge` from the PR branch
# used to skip this check entirely (2026-07-10 P1 triage).
if echo "$CMD" | grep -qE "^gh pr merge|[;&|] *gh pr merge"; then
    # PR number can appear AFTER a flag (`gh pr merge --admin 123` is valid
    # gh syntax), so scan all args after 'merge', skipping flags — an
    # anchored "merge <digits>" match would miss it and silently fall back
    # to the current branch's PR, checking/merging the WRONG one
    # (2026-07-10 review). Drop quoted substrings first so digits inside
    # e.g. --subject "fix 123" can't false-match (mirrors the Python hook's
    # shlex tokenizer).
    _after="${CMD#*gh pr merge}"
    _after=$(printf '%s' "$_after" | sed -E "s/'[^']*'//g; s/\"[^\"]*\"//g")
    # Stop at the first shell separator — a chained `; echo 456` must not
    # let its digits stand in for this merge's PR (2026-07-10 review).
    # Quotes were dropped above, so any remaining separator is real.
    _after="${_after%%[;&|]*}"
    _pr_num=""
    for _tok in $_after; do
        case "$_tok" in
            -*) continue ;;                       # flag — never the PR number
            \#[0-9]*) _tok="${_tok#\#}" ;;        # #123 → 123
            *pull/[0-9]*)
                _tok=$(printf '%s' "$_tok" | grep -oE 'pull/[0-9]+' \
                    | grep -oE '[0-9]+' | head -1) ;;
        esac
        case "$_tok" in
            *[!0-9]*|'') continue ;;              # not a pure integer
            *) _pr_num="$_tok"; break ;;
        esac
    done
    _repo_args=()
    _repo=$(echo "$CMD" | grep -oP -- '--repo \K\S+' || true)
    [ -n "$_repo" ] && _repo_args=(--repo "$_repo")
    if [ -z "$_pr_num" ]; then
        # No number in the command — resolve the current branch's open PR
        _pr_num=$(gh pr view --json number --jq '.number' 2>/dev/null || true)
    fi
    if [ -z "$_pr_num" ]; then
        echo "BLOCKED: cannot resolve which PR this merges (no number in the command, no open PR for the current branch)." >&2
        echo "Specify the PR number: gh pr merge <N> --squash --admin" >&2
        exit 2
    fi
    _mergeable=$(gh pr view "$_pr_num" "${_repo_args[@]}" --json mergeable --jq '.mergeable' 2>/dev/null)
    if [ "$_mergeable" = "UNKNOWN" ]; then
        echo "BLOCKED: PR #$_pr_num mergeable status is UNKNOWN." >&2
        echo "GitHub hasn't finished conflict analysis. Wait until mergeable status is known before retrying." >&2
        exit 2
    fi
    if [ "$_mergeable" = "CONFLICTING" ]; then
        echo "BLOCKED: PR #$_pr_num has merge conflicts. Resolve before merging." >&2
        exit 2
    fi
    echo "⚠️  STOP: gh pr merge detected (PR #$_pr_num, mergeable=$_mergeable). Have you received explicit user approval for this merge?" >&2
    exit 0
fi
