#!/usr/bin/env bash
# PreToolUse hook for Bash commands — blocks destructive operations.
# CC passes tool input as JSON on stdin with schema:
#   { "tool_input": { "command": "..." }, "tool_name": "Bash", ... }
#
# This is the GLOBAL chokepoint (user-level ~/.claude/settings.json): it fires
# for EVERY Bash call in EVERY directory, including non-genesis projects where
# the project-level Python guards are not loaded.
#
# 2026-08 rewrite (guard-correctness PR):
#   * rm checks DELEGATE to the token-parsing Python guards
#     (scripts/hooks/destructive_command_guard.py + protected_paths_guard.py)
#     instead of substring globs — the old *"rm -rf /"*|*"rm -rf ~"*|*"rm -rf ."*
#     cases blocked rm -rf on ANY absolute path, ANY ~/ path, and ANY
#     .-prefixed relative (.venv, .pytest_cache): a standing false-positive
#     cluster. USER-APPROVED POLICY (2026-08-01): deep non-protected paths
#     (depth >= 4) are now deletable everywhere; shallow/broad targets and the
#     protected data dirs (genesis data/DB, transcripts, backups, snapshots,
#     browser profiles) stay hard-blocked. If the guards are unavailable or
#     crash, the legacy globs run instead (degraded, never open).
#   * force-push detection is scoped to the SEGMENT containing `git push`
#     (split on ; && || | and newlines) — `rm -f x && git push` is not a
#     force push. Residual (documented): the split is quote-naive, so a
#     separator inside quotes can hide a same-segment -f from THIS hook;
#     inside genesis the argv-based git_push_guard still catches it.
#   * the soft push/PR reminders and the gh-pr-merge gate are SKIPPED inside
#     the genesis repo for interactive sessions — the project-level
#     git_push_guard runs the same (richer) gates there, and the duplicate
#     cost 2x live gh API calls per merge (audit D4). Dispatched sessions
#     (GENESIS_CC_SESSION=1) keep this belt until project-hook coverage in
#     autonomous sessions is separately verified.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Capture the payload ONCE — jq consumes stdin, and the rm delegation below
# needs the verbatim payload to re-feed the Python guards.
RAW=$(cat)
CMD=$(printf '%s' "$RAW" | jq -r '.tool_input.command // empty' 2>/dev/null)
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

# Is the CURRENT cwd inside a genesis checkout — i.e. one whose project-level
# git_push_guard.py is loaded for this session? Detected by resolving the
# repo's MAIN worktree root (git-common-dir's parent, so a linked worktree
# resolves to main) and checking it carries the project push guard. This is
# cwd-based, not tied to where THIS user-level script physically lives, so it
# works for the deployed main-tree copy and any worktree copy alike.
_in_genesis=0
_gc=$(git rev-parse --git-common-dir 2>/dev/null)
if [ -n "$_gc" ]; then
    _main_root=$(cd "$(dirname "$_gc")" 2>/dev/null && pwd)
    [ -n "$_main_root" ] && [ -f "$_main_root/scripts/hooks/git_push_guard.py" ] && _in_genesis=1
fi

# pip install -e from/to worktree — catches both explicit worktree paths AND
# "pip install -e ." run from inside a worktree directory.
if echo "$CMD" | grep -qE "pip install.*(-e|--editable)"; then
    _block=0
    # Check 1: explicit worktree path in command
    echo "$CMD" | grep -qiE "worktree" && _block=1
    # Check 2: CWD is a git worktree (git-common-dir != git-dir)
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

# rm safety — delegate to the token-parsing Python guards (one parser, zero
# divergence with the project-level hooks). Pre-filtered on *rm* so the
# python spawn cost (~50ms) is paid only when an rm might be present.
case "$CMD" in
    *rm*)
        _delegated=0
        _py=$(command -v python3 2>/dev/null || true)
        if [ -n "$_py" ] \
           && [ -f "$SCRIPT_DIR/hooks/destructive_command_guard.py" ] \
           && [ -f "$SCRIPT_DIR/hooks/protected_paths_guard.py" ]; then
            _delegated=1
            for _guard in destructive_command_guard.py protected_paths_guard.py; do
                _rc=0
                printf '%s' "$RAW" | "$_py" "$SCRIPT_DIR/hooks/$_guard" >&2 || _rc=$?
                if [ "$_rc" -eq 2 ]; then
                    exit 2
                elif [ "$_rc" -ne 0 ]; then
                    # Guard crashed/unusable — fall back to the legacy globs
                    # below (degraded, never open).
                    _delegated=0
                    break
                fi
            done
        fi
        if [ "$_delegated" -eq 0 ]; then
            case "$CMD" in
                *"rm -rf /"*|*"rm -rf ~"*|*"rm -rf ."*)  # "rm -rf ." also covers ".."
                    echo "BLOCKED: rm -rf on broad paths is not allowed. Be specific or ask the user." >&2
                    exit 2;;
            esac
        fi
        ;;
esac

# git-discard safety — DELEGATE to the Python guard (git_discard_guard.py: one
# parser, zero divergence with the project-level hook). Closed-set coarse blocks
# (reset --hard/--merge; clean unless the exact dry-run whitelist; checkout/
# switch with force) + snapshot-then-allow (other checkout/restore/switch get a
# `git stash create` recovery sha logged before running) — all with a
# `# discard-override` escape. Checked BEFORE the softer push/PR warnings
# below (a "git push" warning exits 0 and would short-circuit a hard-block).
# Pre-filtered so the python spawn cost is paid only for discard-shaped git ops.
# On guard-unavailable/crash, fall back to the legacy reset/clean globs (degraded,
# NEVER open — same policy as the rm delegation above). checkout/restore had no
# prior guard, so they get no degraded fallback (nothing to preserve).
# Pre-filter mirrors the guard's _TRIGGER_SUBSTRINGS: non-contiguous (`git -C
# dir checkout`, `git --git-dir /x reset`) and `switch` shapes must reach the
# python guard too (adversarial-review F5 — the old contiguous "git checkout"
# globs silently skipped them). The guard re-filters precisely; this only
# bounds the python spawn cost.
case "$CMD" in
    *git*checkout*|*git*restore*|*git*reset*|*git*clean*|*git*switch*)
        _gd_delegated=0
        _py=$(command -v python3 2>/dev/null || true)
        if [ -n "$_py" ] && [ -f "$SCRIPT_DIR/hooks/git_discard_guard.py" ]; then
            _gd_delegated=1
            _rc=0
            printf '%s' "$RAW" | "$_py" "$SCRIPT_DIR/hooks/git_discard_guard.py" >&2 || _rc=$?
            if [ "$_rc" -eq 2 ]; then
                exit 2
            elif [ "$_rc" -ne 0 ]; then
                _gd_delegated=0  # guard crashed/unusable — legacy fallback below
            fi
        fi
        if [ "$_gd_delegated" -eq 0 ]; then
            case "$CMD" in
                *"git reset --hard"*)
                    echo "BLOCKED: git reset --hard destroys uncommitted work. Use git stash or ask the user." >&2
                    exit 2;;
                *"git clean -f"*)  # substring also covers -fd
                    echo "BLOCKED: git clean removes untracked files permanently. Ask the user first." >&2
                    exit 2;;
            esac
        fi
        ;;
esac

# Force push — hard-block. MUST precede the soft "git push" warning below
# (which exits 0). Scoped to the SEGMENT containing `git push`: split the
# command on shell separators and require the force flag in the SAME segment,
# so `rm -f x && git push` is not a force push (the old whole-command grep
# false-matched exactly that). Match -f only as a FLAG token — a
# whitespace-delimited short-flag cluster containing 'f' ('-f', '-fv', '-uf';
# 'f' is force-only among push short flags); a branch name that merely
# CONTAINS "-f" (skill-funnel, bug-fix) never matches. Also covers --force*.
_force_push=0
while IFS= read -r _seg; do
    printf '%s' "$_seg" | grep -qE 'git push' || continue
    if printf '%s' "$_seg" | grep -qE -- '(^|[[:space:]])-[a-zA-Z]*f[a-zA-Z]*([[:space:]]|$)|--force'; then
        _force_push=1
    fi
done <<EOF
$(printf '%s' "$CMD" | sed -E 's/\|\||&&|;|\|/\n/g')
EOF
if [ "$_force_push" -eq 1 ]; then
    echo "BLOCKED: Force push not allowed. Use a PR." >&2
    exit 2
fi

# Inside the genesis repo, an INTERACTIVE session's push/PR/merge is gated by
# the richer project-level git_push_guard (native approval dialogs, CI/review
# gates) — running this hook's duplicates there only doubles the live gh API
# calls and stderr noise (audit D4). Dispatched sessions keep the belt.
if [ "$_in_genesis" -eq 1 ] && [ "${GENESIS_CC_SESSION:-}" != "1" ]; then
    exit 0
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
    # Quotes were dropped above, so any remaining separator is real. A
    # newline ends the command too, so cut there first.
    _after="${_after%%$'\n'*}"
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
        # No number in the command — resolve the open PR, honoring an explicit
        # --repo (the old bare `gh pr view` resolved the CWD branch's PR number
        # and then gated it against the OTHER repo — wrong-PR gate). With
        # --repo and no selector gh errors → _pr_num stays empty → fail CLOSED.
        _pr_num=$(gh pr view "${_repo_args[@]}" --json number --jq '.number' 2>/dev/null || true)
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
