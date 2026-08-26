#!/usr/bin/env bash
# cc-slot.sh — Persistent tmux slot for Claude Code sessions.
#
# THE one interactive launcher: every door (SSH slot hostnames, manual SSH,
# the dashboard web terminal via the bashrc claude() wrapper) converges here,
# on the same attach-or-create tmux sessions. Idempotent by construction — a
# door walked twice attaches the SAME claude instead of spawning a second one.
#
# Usage: cc-slot.sh <hostname>               SSH RemoteCommand; parses a
#                                            hostname like "genesis-3-4" to
#                                            slot 4 -> session "cc-4"
#        cc-slot.sh manual [claude-args...]  manual/dashboard door: prints the
#                                            slot map, takes the LOWEST free
#                                            slot, forwards extra args to
#                                            claude inside the session

set -euo pipefail

# Resolve HOME when unset: stripped-env/systemd/sandbox invocations can leave
# HOME unset, which under `set -u` aborts at the first ${HOME} use. Fall back
# to the passwd entry for the current uid (same source Path.home() uses); fail
# closed if unresolvable. See CC memory sandbox_shell_no_home.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

# SSH RemoteCommand doesn't source .bashrc (interactive guard) — set PATH explicitly
export PATH="$HOME/.n/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"

# SSH from Windows sends no locale, so tmux marks the client non-UTF-8 and
# renders every non-ASCII glyph as "_". Force a UTF-8 locale for the client.
export LANG="${LANG:-C.UTF-8}"

GENESIS_ROOT="${HOME}/genesis"
SESSION_PREFIX="cc"

# --- Parse slot number from hostname (or allocate one in manual mode) ---
if [[ $# -lt 1 ]]; then
    echo "Usage: cc-slot.sh <hostname>            (e.g., genesis-3-4)" >&2
    echo "       cc-slot.sh manual [claude-args]  (lowest free slot)" >&2
    exit 1
fi

MODE_ARG="$1"
shift
# Extra claude args exist only in manual mode (SSH RemoteCommand passes %n only).
CLAUDE_EXTRA_ARGS=("$@")

if [[ "$MODE_ARG" == "manual" ]]; then
    # Manual/dashboard door. Show what already exists so reattach is the
    # visible easy path, then take the lowest slot with no live session.
    # New-by-default is deliberate: auto-reattach would trap "I want a fresh
    # session" in a loop; reattach stays one printed command away.
    slot_map=$(tmux list-sessions \
        -F '#{session_name}|#{session_attached}|#{t:session_activity}' \
        2>/dev/null | grep "^${SESSION_PREFIX}-" || true)
    if [[ -n "$slot_map" ]]; then
        echo "Existing slots (reattach: tmux attach -t <name>):" >&2
        while IFS='|' read -r name attached activity; do
            state="detached"
            [[ "$attached" -ge 1 ]] && state="attached"
            echo "  ${name}  ${state}  (last activity: ${activity})" >&2
        done <<<"$slot_map"
    fi
    SLOT=1
    # '=' forces exact-name match: a bare -t is prefix-matched by tmux, so
    # cc-1 would falsely read as existing whenever only cc-10 does.
    while tmux has-session -t "=${SESSION_PREFIX}-${SLOT}" 2>/dev/null; do
        SLOT=$((SLOT + 1))
    done
else
    SLOT="${MODE_ARG##*-}"

    if ! [[ "$SLOT" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: Invalid slot '$SLOT' (parsed from '$MODE_ARG')." >&2
        echo "Slot must be a positive integer (1, 2, 3, ...)." >&2
        exit 1
    fi
fi

SESSION_NAME="${SESSION_PREFIX}-${SLOT}"

# Handle nested tmux
unset TMUX

# --- Dynamic session cap (RAM + CPU aware) ---
RESERVED_MB=4096        # OS + Genesis runtime + background work headroom
PER_SESSION_MB=900      # Measured: ~800MB (claude + 4 MCP servers) + buffer
CPU_CAP=$(nproc)        # Never exceed core count (thrashing)

avail_kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
avail_mb=$((avail_kb / 1024))
ram_cap=$(( (avail_mb - RESERVED_MB) / PER_SESSION_MB ))

# Floor: 1 (never lock out); Ceiling: nproc
max_sessions=$ram_cap
[[ $max_sessions -lt 1 ]] && max_sessions=1
[[ $max_sessions -gt $CPU_CAP ]] && max_sessions=$CPU_CAP

# Numeric slots only: retired cc-manual-<ts>-<pid> sessions from the old
# wrapper (and any other cc-* stray) must not consume cap headroom — manual
# allocation can only ever probe/create cc-<N>.
existing=$(tmux list-sessions -F '#{session_name}' 2>/dev/null \
           | grep -cE "^${SESSION_PREFIX}-[0-9]+$" || true)

# Reattaching to existing session — always allow ('=' = exact-name match)
_SESSION_EXISTS=0
if tmux has-session -t "=$SESSION_NAME" 2>/dev/null; then
    _SESSION_EXISTS=1  # bypass cap check; also skips the OAuth gate below —
                       # attach does NOT re-run the pane command, so any token
                       # injection would be moot (and would waste a probe).
elif [[ $existing -ge $max_sessions ]]; then
    echo "ERROR: Session cap reached (${existing}/${max_sessions} active)." >&2
    echo "RAM: ${avail_mb}MB available, ${RESERVED_MB}MB reserved, ${PER_SESSION_MB}MB/session" >&2
    echo "" >&2
    echo "Active sessions:" >&2
    tmux list-sessions -F '  #{session_name}  (last activity: #{t:session_activity})' \
        2>/dev/null | grep "^  ${SESSION_PREFIX}-" >&2
    echo "" >&2
    echo "Kill an idle session: tmux kill-session -t ${SESSION_PREFIX}-<N>" >&2
    exit 1
fi

echo "→ Slot ${SLOT} (session: ${SESSION_NAME}, cap: ${existing}/${max_sessions})" >&2

# Redirect CC temp to dedicated directory (keeps /tmp clean)
export TMPDIR="$HOME/.genesis/cc-tmp"
mkdir -p "$TMPDIR"
chmod 700 "$TMPDIR"

# Move CC's Bash sandbox off volatile /tmp onto persistent disk.
# CC uses CLAUDE_CODE_TMPDIR for its sandbox root (/claude-<uid>/<cwd>/).
# Without this, intermittent ENOENT failures on /tmp break the Bash tool.
export CLAUDE_CODE_TMPDIR="$HOME/.genesis/cc-tmp"

# Permission mode for this interactive dev console. Default: auto — auto-approves
# common ops but still prompts on deny/ask rules, which the operator answers in
# the tmux session (keeps deny-rule safety). To launch friction-free with
# --dangerously-skip-permissions, set GENESIS_CC_PERMISSION_MODE=bypass. SSH
# RemoteCommand does not source .bashrc, so this script also reads an optional
# ~/.genesis/cc-slot.env (e.g. a single line: GENESIS_CC_PERMISSION_MODE=bypass).
# That file is also where the OAuth-durability lever lives:
# GENESIS_CC_SLOT_OAUTH=conditional (default) | always | off — set `off` for a
# slot that must keep Remote Control / claude.ai connectors even after the login
# dies (see the OAuth block below).
# Headless/autonomous CC sessions (CCInvoker -p) keep bypass separately — no
# human is present to answer a prompt.
if [ -f "${HOME}/.genesis/cc-slot.env" ]; then
    # Don't let a malformed override file kill the session under `set -e` (the
    # sourced file is the final command of an && chain, so a non-zero exit would
    # abort the script and drop the SSH session with no diagnostic).
    . "${HOME}/.genesis/cc-slot.env" \
        || echo "cc-slot: warning: ~/.genesis/cc-slot.env sourced with errors (continuing)" >&2
fi
case "${GENESIS_CC_PERMISSION_MODE:-auto}" in
    bypass|dangerous|skip) CC_PERM_FLAG="--dangerously-skip-permissions" ;;
    *)                     CC_PERM_FLAG="--permission-mode auto" ;;
esac

# Forwarded manual-mode args: shell-quote each one (%q) — the command below is
# a single string tmux hands to the default shell (bash on Genesis installs).
# A caller-supplied permission flag suppresses CC_PERM_FLAG so claude never
# receives two conflicting permission arguments.
CLAUDE_ARGS_Q=""
_HAS_BARE=0
if [[ ${#CLAUDE_EXTRA_ARGS[@]} -gt 0 ]]; then
    for arg in "${CLAUDE_EXTRA_ARGS[@]}"; do
        case "$arg" in
            --dangerously-skip-permissions|--permission-mode|--permission-mode=*)
                CC_PERM_FLAG="" ;;
        esac
        # --bare ignores CLAUDE_CODE_OAUTH_TOKEN (CC auth precedence), so
        # injecting the setup-token would be inert — skip the OAuth gate below.
        [ "$arg" = "--bare" ] && _HAS_BARE=1
    done
    CLAUDE_ARGS_Q=$(printf ' %q' "${CLAUDE_EXTRA_ARGS[@]}")
fi

# --- OAuth login durability (login-dead-conditional; WS-1) -------------------
# On slot CREATE only: if the interactive /login is dead, continue this pane on
# the stored 1-year setup-token so the session survives without a re-login
# prompt. genesis.cc.login_gate makes the decision (reusing login_health's
# shared login-dead gate — one authority, same as CCInvoker); the token itself
# is read INSIDE the pane shell (never in any argv/ps) via a conditional prefix
# on the command string. Lever GENESIS_CC_SLOT_OAUTH=conditional(default)|always|off.
# cc-slot.sh is a registered reader of cc_oauth_token.env — see
# docs/architecture/shared-artifacts.md.
_OAUTH_SRC=""
_slot_oauth_mode="${GENESIS_CC_SLOT_OAUTH:-conditional}"
_slot_oauth_mode="${_slot_oauth_mode,,}"   # normalize; the gate is the value authority
if [ "$_SESSION_EXISTS" = "0" ] && [ "$_slot_oauth_mode" != "off" ] && [ "$_HAS_BARE" = "0" ]; then
    # The gate both DECIDES and AUTHORS the notice: on inject it exits 0 and
    # prints the mode-appropriate human notice to stdout — captured here so the
    # notice text lives in ONE place (no bash/gate divergence), and so lever
    # semantics (unknown value → fail-closed, peer-route/override exclusion,
    # stale-token exclusion) are enforced solely in genesis.cc.login_gate, a
    # faithful mirror of CCInvoker's fallback contract. `if …; then` keeps a
    # non-zero exit from aborting the launch under `set -e`; `timeout 30` bounds
    # a hung probe. `env` hands the resolved mode across (a plain
    # `GENESIS_CC_SLOT_OAUTH=always` in ~/.genesis/cc-slot.env is a non-exported
    # shell var the subprocess would otherwise never see).
    if _oauth_notice=$(timeout 30 env GENESIS_CC_SLOT_OAUTH="$_slot_oauth_mode" \
            "${GENESIS_ROOT}/.venv/bin/python" -m genesis.cc.login_gate); then
        # %q so the notice cannot break out of the pane command string (any
        # future notice edit is injection-proof by construction, not by luck).
        _notice_q=$(printf '%q' "$_oauth_notice")
        # Runs in the PANE shell: read the token with the SAME parser the gate
        # used (login_health.read_fallback_token — no sed/parser divergence),
        # export it ONLY when non-empty (a failed/empty read never exports a
        # blank credential), then echo the notice to stderr. The token flows
        # python-stdout → $(...) → a shell var → the process ENV, never any argv
        # (no ps/scrollback leak). `$(...)`, `\$`, and the literal single-quoted
        # python defer to the pane shell; ${GENESIS_ROOT}/${_notice_q} expand here.
        _OAUTH_SRC="_gt=\"\$(\"${GENESIS_ROOT}/.venv/bin/python\" -c 'import sys; from genesis.cc.login_health import read_fallback_token as r; sys.stdout.write(r() or str())' 2>/dev/null)\"; if [ -n \"\$_gt\" ]; then export CLAUDE_CODE_OAUTH_TOKEN=\"\$_gt\"; printf '%s\\n' ${_notice_q} >&2; fi; unset _gt; "
    fi
fi

# -u: force UTF-8 output even if a future client's locale detection fails.
#
# The inner command drops the old `exec claude` so that when claude EXITS we can
# record why before the pane vanishes: cc_exit_capture.sh logs the exit status
# (signal-decoded) + a pane-scrollback tail to ~/.genesis/logs/cc_exit_<slot>.log.
# Without this a dying session (V8 abort, OOM kill, clean exit) leaves no trace —
# the 2026-08-19 death was undiagnosable for exactly that reason. `exit $__ec`
# reproduces claude's exit code as the pane's, so tmux sees the same dead-status
# and the `-A` attach-or-create behaviour (this runs only on CREATE) is unchanged.
# Captures when claude EXITS on its own (crash/OOM-of-claude/clean quit — the
# cases we care about); a SIGHUP to the pane itself (tmux kill-session / unit
# stop) may reap the wrapper before the trailer runs — the watchgod OOM sampler
# covers that subset. Capture is best-effort and never alters the exit code.
# `\$` defers expansion to the pane's shell; the `${...}` expand here in cc-slot.sh.
# The token-prep prefix `${_OAUTH_SRC}` goes BEFORE `cd` so the original
# `cd $ROOT && claude` guard stays intact: `_OAUTH_SRC` ends in `;`, so placing it
# after the `&&` (`cd $ROOT && <prefix>; claude`) would bind the `&&` to the prefix
# only and launch claude even when cd fails. The prefix is cwd-independent (absolute
# python path, home-anchored token file), and if the repo is gone that python is too
# → the read silently no-ops before cd fails. Do NOT move it after the `&&`
# (test_cd_guard_skips_claude_on_bad_cd).
exec tmux -u new-session -A -s "$SESSION_NAME" \
    -e "GENESIS_SLOT=${SLOT}" \
    -e "GENESIS_CC_PERMISSION_MODE=${GENESIS_CC_PERMISSION_MODE:-auto}" \
    -e "CLAUDE_CODE_TMPDIR=$CLAUDE_CODE_TMPDIR" \
    -e "LANG=$LANG" \
    "${_OAUTH_SRC}cd ${GENESIS_ROOT} && claude ${CC_PERM_FLAG}${CLAUDE_ARGS_Q}; __ec=\$?; ${GENESIS_ROOT}/scripts/cc_exit_capture.sh ${SLOT} \$__ec >/dev/null 2>&1; exit \$__ec"
