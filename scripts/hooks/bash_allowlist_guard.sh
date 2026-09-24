#!/usr/bin/env bash
# PreToolUse(Bash) hook that enforces GENESIS_BASH_ALLOWLIST in DISPATCHED
# sessions.
#
# WHY THIS FILE EXISTS. Scoped background profiles restrict Bash to a named set
# of binaries by exporting GENESIS_BASH_ALLOWLIST. That export is only a
# declaration; something has to READ it. The only reader in this repository was
# scripts/bash_safety_hook.sh, the global user-level chokepoint — and this
# repository has never wired that hook, in any ref, so on a fresh clone the
# restriction was declared and never enforced.
#
# Wiring it in the repo's .claude/settings.json would not have helped: a
# dispatched session runs with a working directory outside any git repository,
# and Claude Code discovers project settings by git-root detection, so repo
# settings never load there. The channel that DOES reach a dispatch is the
# --settings file the invoker already injects (see cc_span_settings_path in
# src/genesis/cc/invoker.py), which is what registers this hook.
#
# MEASURED 2026-09-23, CC 2.1.246, from a dispatch-shaped invocation (cwd
# outside any repo, --dangerously-skip-permissions): a PreToolUse Bash hook
# supplied via --settings fires, and its exit 2 refuses the call — the command
# demonstrably does not run. With the allowlist exported and no such hook
# registered, the same non-allowlisted command runs.
#
# REGISTERED UNCONDITIONALLY, on every dispatch. That is safe because the
# predicate is a documented no-op when GENESIS_BASH_ALLOWLIST is unset, so the
# conditionality lives in the environment rather than in the settings file —
# which in turn lets that file stay a single fixed path written idempotently,
# with no per-invocation content for two concurrent dispatches to race over.
#
# WHAT THE NO-OP COSTS, measured rather than waved at: the env test below runs
# before stdin is touched, so the guard itself does no payload read, no jq and
# no awk — about 5ms. But reaching it still spawns the launcher (bash, plus a
# `git rev-parse`) and then this script, so the registration costs ~30ms per
# Bash call in EVERY dispatched session, not only scoped ones (MEASURED over
# 20 invocations on a live install; expect the figure to move with load). That
# is affordable against a tool call, and it is not nothing — do not repeat the
# earlier claim in this comment that an unscoped session "pays nothing".
#
# FAIL DIRECTION: CLOSED, and only here. When an allowlist IS in force and the
# command cannot be recovered from the payload, this refuses. A containment
# hook that cannot see the command has not established anything, and exiting 0
# there would reproduce the exact silence this guard exists to remove. That
# refusal cannot brick an install: it is scoped to sessions that declared an
# allowlist, i.e. dispatched ones, which are never the repair path — an
# interactive session is, and it declares no allowlist, so it never reaches
# this branch at all.

set -u

# No allowlist → nothing to enforce. Return before reading stdin so this costs
# nothing in the sessions that are not scoped.
[ -n "${GENESIS_BASH_ALLOWLIST:-}" ] || exit 0

_ALLOW=$GENESIS_BASH_ALLOWLIST

# Derive the directory with parameter expansion rather than `dirname`. With
# `dirname` off PATH the old form became `cd ""`, which SUCCEEDS and silently
# resolves the predicate against the process CWD — a dispatched session's
# working directory, i.e. somewhere an allowlisted command can write. It is
# fail-closed today only because nothing is planted there, which is not a
# property to rely on. No external tool can now fail this open.
_DIR=${BASH_SOURCE[0]%/*}
if [ "$_DIR" = "${BASH_SOURCE[0]}" ]; then
    # No slash to strip, so the directory cannot be derived from the
    # invocation path and $PWD would be a guess. The invoker always registers
    # an absolute path, so this shape is unexpected — refuse rather than guess.
    echo "BLOCKED: this session restricts Bash to [$_ALLOW], but the guard's own" >&2
    echo "directory could not be derived from its invocation path, so the" >&2
    echo "allowlist predicate cannot be located." >&2
    exit 2
fi
_LIB="$_DIR/bash_allowlist_lib.sh"

if [ ! -r "$_LIB" ]; then
    echo "BLOCKED: this session restricts Bash to [$_ALLOW], but the allowlist" >&2
    echo "predicate ($_LIB) is unreadable, so the restriction cannot be applied." >&2
    exit 2
fi
# shellcheck source=scripts/hooks/bash_allowlist_lib.sh
. "$_LIB"

if ! command -v jq >/dev/null 2>&1; then
    echo "BLOCKED: this session restricts Bash to [$_ALLOW], but jq is not on" >&2
    echo "PATH, so the command cannot be read out of the hook payload. Install" >&2
    echo "jq on this host; until then no command in this session can be cleared." >&2
    exit 2
fi

_RAW=$(cat)

# GATE ON tool_name, exactly as the chokepoint does. A matcher is a REGEX, and
# an install may wire this unanchored — bare "Bash" also matches "BashOutput".
# A sibling tool's payload carries no `.tool_input.command`, so without this
# the fail-closed leg below would hard-block it with a message about a command
# it never had. The chokepoint grew this gate after the same defect was found
# there in round 1; this is its sibling and was left behind.
#
# An absent or unreadable tool_name deliberately FALLS THROUGH to the command
# checks, which fail closed on their own — only a positively-identified other
# tool exits early.
_TOOL=$(printf '%s' "$_RAW" | jq -r '.tool_name // empty' 2>/dev/null)
if [ -n "$_TOOL" ] && [ "$_TOOL" != "Bash" ]; then
    exit 0
fi

_rc=0
_CMD=$(printf '%s' "$_RAW" | jq -r '.tool_input.command // empty' 2>/dev/null) || _rc=$?
if [ "$_rc" -ne 0 ] || [ -z "$_CMD" ]; then
    echo "BLOCKED: this session restricts Bash to [$_ALLOW], and no command" >&2
    echo "could be read from the hook payload, so it cannot be cleared." >&2
    exit 2
fi

genesis_bash_allowlist_verdict "$_CMD"
exit $?
