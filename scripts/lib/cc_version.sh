# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# Single source of truth for the Claude Code version Genesis installs/pins,
# PLUS the shared `cc_ensure_local` aligner that keeps the LOCAL machine's CC at
# the pin.
#
# Sourced by scripts/install.sh (container), scripts/host-setup.sh (host VM),
# scripts/bootstrap.sh, and scripts/update.sh. update.sh ALSO dispatches this pin
# to the host VM via the guardian-gateway `update-cc` op. Bump CC_VERSION here in
# ONE place — the next install/bootstrap/update run aligns the local CC, so the
# container and host never drift.
#
# Governance model (see docs/reference/cc-compatibility.md): the npm pin below
# + the unified `update-cc` updater + DISABLE_UPDATES in ~/.claude/settings.json.
# Deliberately NO managed-settings `requiredMinimumVersion` floor — a hard floor
# removes the incident-recovery downgrade path and can brick CC.
#
# Honors an inherited CC_VERSION (e.g. `CC_VERSION=2.1.180 ./install.sh`).
CC_VERSION="${CC_VERSION:-2.1.173}"


# cc_ensure_local — install or align the LOCAL Claude Code CLI to $CC_VERSION.
#
# Idempotent, non-fatal, drift-healing. Container/local ONLY — the host VM's CC
# is synced separately by update.sh via the guardian `update-cc` op. Callers
# decide fatality: `cc_ensure_local || true` (update.sh/bootstrap.sh, where a
# failure must never abort) or `cc_ensure_local || SETUP_WARNINGS=1` (install.sh).
#
# Behavior:
#   - npm missing            -> warn + return 0 (nothing we can do; skip)
#   - claude already at pin   -> return 0 (no-op)
#   - claude present, drifted -> reinstall @pin to the SAME prefix the existing
#                                binary resolves from (NOT `npm config get prefix`
#                                — the two can differ, which would install beside
#                                the live binary and leave `which claude` stale)
#   - claude absent (fresh)   -> install @pin to `npm config get prefix`
#                                (/usr remapped to /usr/local to avoid /usr/lib
#                                misrouting — matches install.sh)
#   - post-install: re-check `claude --version`; still != pin -> warn + return 1
#
# Exact-match to the pin (the pin may legitimately go DOWN for incident rollback;
# `npm install @X.Y.Z` pins exactly X.Y.Z). NOTE `claude --version` prints
# "2.1.173 (Claude Code)", so the version is field 1 (awk '{print $1}').
cc_ensure_local() {
    local pin="${CC_VERSION:-}"
    if [ -z "$pin" ]; then
        echo "  cc_ensure_local: CC_VERSION unset — skipping" >&2
        return 0
    fi
    if ! command -v npm >/dev/null 2>&1; then
        echo "  cc_ensure_local: npm not found — cannot manage Claude Code (skipping)" >&2
        return 0
    fi

    local existing current prefix
    existing="$(command -v claude 2>/dev/null || true)"
    if [ -n "$existing" ]; then
        current="$(claude --version 2>/dev/null | awk '{print $1}')"
        if [ "$current" = "$pin" ]; then
            echo "  Claude Code already at pin ($pin)"
            return 0
        fi
        echo "--- Claude Code drift: ${current:-unknown} -> $pin (aligning) ---"
        # Reinstall to the existing binary's OWN prefix so `which claude` updates.
        prefix="$(dirname "$(dirname "$existing")")"   # /usr/local/bin/claude -> /usr/local
    else
        echo "  Claude Code not installed — installing pinned $pin"
        prefix="$(npm config get prefix 2>/dev/null || echo /usr/local)"
        [ "$prefix" = "/usr" ] && prefix="/usr/local"
    fi

    # Always pass --prefix explicitly (deterministic target). sudo only for
    # system prefixes; user prefixes (~/.npm-global, nvm) are user-writable.
    local -a npm_args
    npm_args=(npm install -g --prefix "$prefix" "@anthropic-ai/claude-code@${pin}")
    case "$prefix" in
        /usr|/usr/local|/opt/*)
            if [ "$(id -u)" != "0" ]; then
                if command -v sudo >/dev/null 2>&1; then
                    # PATH passthrough: npm is often nvm-managed + absent from
                    # sudo's secure_path (matches host-setup.sh).
                    npm_args=(sudo env "PATH=$PATH" "${npm_args[@]}")
                else
                    echo "  cc_ensure_local: sudo unavailable — cannot install to $prefix (skipping)" >&2
                    return 0
                fi
            fi
            ;;
    esac

    if ! timeout 300 "${npm_args[@]}"; then
        echo "  WARNING: cc_ensure_local: npm install failed (non-fatal)" >&2
        return 1
    fi
    hash -r 2>/dev/null || true   # drop bash's cached path to the old binary
    local installed
    installed="$(claude --version 2>/dev/null | awk '{print $1}')"
    if [ "$installed" = "$pin" ]; then
        echo "  + Claude Code now at pin ($pin)"
        return 0
    fi
    echo "  WARNING: cc_ensure_local: install ran but 'claude --version' is ${installed:-unknown} (expected $pin) — possible npm-prefix/PATH mismatch" >&2
    return 1
}
