#!/usr/bin/env bash
# Shared Bash-allowlist predicate for scoped background profiles.
#
# ONE implementation, TWO entry points, because there are two places a
# dispatched session's Bash can be intercepted and they must not drift:
#
#   * scripts/bash_safety_hook.sh — the GLOBAL user-level chokepoint, wired by
#     hand in ~/.claude/settings.json on installs that choose to. This repo
#     wires it nowhere, in any ref.
#   * scripts/hooks/bash_allowlist_guard.sh — the thin hook the invoker injects
#     into every dispatched session via --settings, which is the only channel
#     that reaches one (a dispatch's cwd is outside any git repo, so Claude
#     Code's git-root discovery never loads the repo's .claude/settings.json).
#
# An install may have both wired. That is fine and deliberate: the predicate is
# pure, so both reach the same verdict on the same command and the second
# refusal is idempotent rather than a conflict.
#
# THE CONTRACT. GENESIS_BASH_ALLOWLIST is a comma-separated list of command
# BINARIES (e.g. "gh"). When it is set, the command's first token must be one
# of them, and no chaining / piping / substitution / redirection is permitted —
# each of those is a way to reach a second, unlisted binary from an allowlisted
# first token. When it is UNSET the predicate returns 0 unconditionally, which
# is what makes registering the guard on every dispatch safe: an invocation
# that declares no allowlist behaves exactly as it did before.
#
# The chaining test is a deliberately CRUDE substring case rather than a parse.
# It runs shell-side with no access to the canonical tokenizer, so anchoring it
# would mean modelling shell grammar with globs — an open set that a review loop
# finds one member of per round without converging. Over-blocking here costs an
# allowlisted session a rephrasing; under-blocking costs the containment.

# Verdict for one command under the current GENESIS_BASH_ALLOWLIST.
#
#   $1 — the command string, already extracted from the hook payload.
#
# Returns 0 when the command is permitted OR no allowlist is in force, and 2
# with a reason on stderr when it is refused. The caller propagates the code;
# exit 2 is what Claude Code reads as a PreToolUse refusal.
genesis_bash_allowlist_verdict() {
    local cmd=$1
    local allow=${GENESIS_BASH_ALLOWLIST:-}

    # Unset → no effect. Every session without an allowlist behaves exactly as
    # it did before this guard existed.
    [ -n "$allow" ] || return 0

    # Reject embedded newlines FIRST — a `case` glob does not reliably match
    # $'\n', so use a line count. printf adds no trailing newline, so any count
    # > 0 means an embedded newline, i.e. a second command on its own line.
    local lines
    lines=$(printf '%s' "$cmd" | wc -l)
    if [ "$lines" -gt 0 ]; then
        echo "BLOCKED: multi-line commands are not permitted in an allowlisted session ($allow)." >&2
        return 2
    fi

    # `&` is listed SEPARATELY from `&&` and is not redundant with it: a bare
    # `&` backgrounds the first command and runs the next one, so
    # `gh pr list & curl …` reaches a second binary with an allowlisted first
    # token. MEASURED: before this entry that command exited 0 through the
    # shipped predicate while bash ran the second half.
    #
    # The operator set was ENUMERATED rather than extended by one member, so
    # the next round does not find the next one. Against real bash, with `gh`
    # as the first token and each operator in turn:
    #
    #   &   &(no space)   reach a second binary  -> ADDED here
    #   ;   &&   ||   |   `…`   $(…)   >   <     reach one, already listed
    #   ;;   ;&   ;;&                            parse only inside `case`, and
    #                                            every spelling contains `;`,
    #                                            so they are already covered
    #   (   )                                    reach nothing in any position
    #                                            the entries above leave open —
    #                                            `(cmd)` after an allowlisted
    #                                            token is a syntax error, and
    #                                            reaching a subshell needs a
    #                                            separator already blocked
    #
    # `(` and `)` are listed ANYWAY, by owner decision, as defence in depth
    # against a construction the enumeration above did not model. Recording the
    # trade rather than the conclusion: the measurement says they close nothing
    # reachable today, and they DO cost — a parenthesis in an ordinary argument
    # (a `--jq` filter, a search query, a PR title) is now refused in an
    # allowlisted session. That cost is real and is pinned by tests, so the next
    # reader can price it rather than rediscover it. The operator can still pass
    # such an argument; it has to come from a tool that is not Bash.
    case "$cmd" in
        *';'*|*'&'*|*'|'*|*'`'*|*'$('*|*'>'*|*'<'*|*'('*|*')'*)
            echo "BLOCKED: this session's Bash may not chain, pipe, substitute, or redirect (allowlist: $allow)." >&2
            return 2;;
    esac

    local first
    first=$(printf '%s' "$cmd" | awk '{print $1}')
    case ",$allow," in
        *",$first,"*) return 0 ;;
        *)
            echo "BLOCKED: this session may only run [$allow] commands; got '$first'." >&2
            return 2;;
    esac
}

# Is an allowlist in force for this session? Entry points call this BEFORE
# reading stdin, so an unscoped session skips the payload read, jq and awk —
# about 5ms for the guard itself. It is NOT free end to end: reaching the guard
# still costs the launcher hop, MEASURED at ~30ms per Bash call in every
# dispatched session. Cheap against a tool call; not nothing.
genesis_bash_allowlist_active() {
    [ -n "${GENESIS_BASH_ALLOWLIST:-}" ]
}
