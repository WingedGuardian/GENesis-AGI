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
CC_VERSION="${CC_VERSION:-2.1.218}"

# Node.js major that the pinned Claude Code requires — derived from the CC pin's
# engines.node (e.g. `@anthropic-ai/claude-code@2.1.201` declares node >=22).
# BUMP THIS IN LOCKSTEP whenever a CC pin raises the Node floor: a stale Node
# major is what left a host on Node 18, unable to run the pinned CC, with
# Guardian's `claude -p` recovery brain silently offline. Consumed by
# host-setup.sh (host Node install) and update.sh, which dispatches it to the
# host VM via the guardian-gateway `update-node` op — mirroring `update-cc`.
NODE_MAJOR="${NODE_MAJOR:-22}"

# Known CC install prefixes (bin dirs), probed when `command -v claude` fails —
# a PATH-blind install (user npm prefix whose PATH export only fires in
# interactive shells) must be treated as installed, not reinstalled forever.
# Colon-separated; overridable for tests and exotic layouts.
CC_PROBE_DIRS="${CC_PROBE_DIRS:-/usr/local/bin:/usr/bin:$HOME/.npm-global/bin}"


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

    local existing current prefix p
    existing="$(command -v claude 2>/dev/null || true)"
    if [ -z "$existing" ]; then
        # PATH-blind check before declaring absence: a user-prefix install
        # (~/.npm-global with its PATH export in .bashrc AFTER the interactive
        # early-exit) is invisible to every non-interactive shell — which made
        # this function reinstall CC on EVERY update run on one machine while
        # a perfectly good copy sat one directory away.
        local _oldIFS="$IFS"
        IFS=':'
        for p in $CC_PROBE_DIRS; do
            if [ -x "$p/claude" ]; then
                existing="$p/claude"
                echo "  cc_ensure_local: claude found at $existing but NOT on this shell's PATH — treating as installed (fix the PATH wiring, or move the install to a system prefix)" >&2
                break
            fi
        done
        IFS="$_oldIFS"
    fi
    if [ -n "$existing" ]; then
        # Version via the resolved binary, not a PATH lookup — $existing may
        # have come from the PATH-blind prefix probe above.
        current="$("$existing" --version 2>/dev/null | awk '{print $1}')"
        if [ "$current" = "$pin" ]; then
            echo "  Claude Code already at pin ($pin)"
            return 0
        fi
        echo "--- Claude Code drift: ${current:-unknown} -> $pin (aligning) ---"
        # Reinstall to the existing binary's OWN prefix so `which claude` updates
        # (npm config prefix can differ, which would install a 2nd copy and leave
        # `which claude` stale). Assumes an npm-global install (Genesis has no
        # native-installer path — see docs/reference/cc-compatibility.md); a
        # non-npm launcher would be reinstalled to the wrong place, but the
        # post-install verify below downgrades that to a non-fatal warning.
        prefix="$(dirname "$(dirname "$existing")")"   # /usr/local/bin/claude -> /usr/local
    else
        echo "  Claude Code not installed — installing pinned $pin"
        prefix="$(npm config get prefix 2>/dev/null)"
        [ -n "$prefix" ] || prefix="/usr/local"        # guard empty-but-rc-0 output
        [ "$prefix" = "/usr" ] && prefix="/usr/local"  # avoid /usr/lib misrouting
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
    # Verify against the binary we just installed, NOT a PATH lookup: in
    # non-interactive shells a user npm prefix (~/.npm-global, nvm) is often
    # absent from PATH, which made a SUCCESSFUL install report a false
    # "PATH mismatch" warning (seen live on a parity run 2026-07-04).
    installed="$("$prefix/bin/claude" --version 2>/dev/null | awk '{print $1}')"
    [ -n "$installed" ] || installed="$(claude --version 2>/dev/null | awk '{print $1}')"
    if [ "$installed" = "$pin" ]; then
        echo "  + Claude Code now at pin ($pin)"
        return 0
    fi
    echo "  WARNING: cc_ensure_local: install ran but 'claude --version' is ${installed:-unknown} (expected $pin) — possible npm-prefix/PATH mismatch" >&2
    return 1
}


# cc_shadow_scan — enforce the ONE-canonical-copy policy for Claude Code.
#
# Four real incidents motivated this (2026-07): an nvm-tree copy that shadowed
# the pinned CC in interactive shells only (user saw a months-old version); a
# native-installer symlink in ~/.local/bin doing the same; leftover native
# version blobs (~490MB dead weight); and a user-prefix copy invisible to
# non-interactive shells. Shadow copies drift silently because update-cc /
# cc_ensure_local only manage the canonical copy.
#
# Canonical = a copy that REPORTS THE PIN ($CC_VERSION): first the PATH
# resolution if it's at the pin, else the first at-pin copy in CC_PROBE_DIRS.
# Version-verified selection is the core safety property — `command -v` alone
# follows the INVOKING shell's PATH, and an interactive PATH can put a stale
# copy first (the exact incident class this scan exists to fix), which would
# otherwise crown the stale copy canonical and sudo-remove the good one.
# FAIL-SAFE: if NO copy at the pin exists anywhere, nothing is removed —
# a scan that cannot prove a good copy exists has no business deleting.
#
# Every OTHER copy on a known surface is removed, with loud logging. Removal
# is gated on the artifact being PROVABLY a claude-code install (npm package
# dir or a symlink into one, or the native-installer layout); anything
# unprovable is warned about and left alone. The canonical's own package dir
# and (for a native canonical) the native versions dir are never touched.
#
# Opt-out for deliberate multi-copy setups: CC_SHADOW_SCAN=0.
# Non-fatal by design; call as `cc_shadow_scan || true`.
cc_shadow_scan() {
    if [ "${CC_SHADOW_SCAN:-1}" = "0" ]; then
        echo "  cc_shadow_scan: disabled (CC_SHADOW_SCAN=0)"
        return 0
    fi
    local pin="${CC_VERSION:-}"
    if [ -z "$pin" ]; then
        echo "  cc_shadow_scan: CC_VERSION unset — skipping (cannot verify a canonical)" >&2
        return 0
    fi

    local canonical="" canon_real="" canon_pkg="" p v
    local -a _candidates=()
    p="$(command -v claude 2>/dev/null || true)"
    [ -n "$p" ] && _candidates+=("$p")
    local _oldIFS="$IFS"
    IFS=':'
    for p in $CC_PROBE_DIRS; do
        _candidates+=("$p/claude")
    done
    IFS="$_oldIFS"
    for p in "${_candidates[@]}"; do
        [ -x "$p" ] || continue
        v="$("$p" --version 2>/dev/null | awk '{print $1}')"
        if [ "$v" = "$pin" ]; then
            canonical="$p"
            break
        fi
    done
    if [ -z "$canonical" ]; then
        echo "  cc_shadow_scan: no claude at the pin ($pin) found — REFUSING to remove anything (align with cc_ensure_local / update-cc first)" >&2
        return 0
    fi
    canon_real="$(readlink -f "$canonical" 2>/dev/null || echo "$canonical")"
    # The canonical's own npm package dir — never removed, even when a STALE
    # extra symlink points into it (that link alone goes; nuking the package
    # would destroy the canonical).
    case "$canon_real" in
        */@anthropic-ai/claude-code/*)
            canon_pkg="$(readlink -f "${canon_real%%/@anthropic-ai/claude-code/*}/@anthropic-ai/claude-code" 2>/dev/null || true)"
            ;;
    esac

    # Native-installer version blobs: shadows by definition under the npm-only
    # canon (docs/reference/cc-compatibility.md), and BIG (~250MB each) —
    # UNLESS the canonical itself is a native install (then they ARE the
    # canonical's payload; leave them and let the operator migrate to npm).
    if [ -d "$HOME/.local/share/claude/versions" ]; then
        case "$canon_real" in
            "$HOME/.local/share/claude/"*)
                echo "  cc_shadow_scan: canonical is a native install — keeping $HOME/.local/share/claude/versions (consider migrating to the npm install path)" >&2
                ;;
            *)
                echo "  cc_shadow_scan: removing native-installer version blobs ($HOME/.local/share/claude/versions)"
                rm -rf "$HOME/.local/share/claude/versions"
                ;;
        esac
    fi

    local candidate real
    for candidate in \
        "$HOME"/.nvm/versions/node/*/bin/claude \
        "$HOME/.claude/local/claude" \
        "$HOME/.local/bin/claude" \
        "$HOME/.npm-global/bin/claude" \
        /usr/local/bin/claude \
        /usr/bin/claude; do
        [ -e "$candidate" ] || [ -L "$candidate" ] || continue
        real="$(readlink -f "$candidate" 2>/dev/null || echo "$candidate")"
        # The canonical copy itself (or a same-file alias like /bin vs /usr/bin
        # under usrmerge) is never touched.
        [ "$real" = "$canon_real" ] && continue
        _cc_remove_shadow "$candidate" "$canon_pkg"
    done

    # Aliases/functions can shadow every file-level fix — detect, never edit
    # a user's rc files.
    local rc hits
    for rc in "$HOME/.bashrc" "$HOME/.bash_aliases" "$HOME/.zshrc" "$HOME/.profile"; do
        [ -f "$rc" ] || continue
        hits="$(grep -nE '^[[:space:]]*alias claude=' "$rc" 2>/dev/null || true)"
        [ -n "$hits" ] && echo "  WARNING: cc_shadow_scan: 'claude' alias in $rc shadows the canonical copy — remove it manually: $hits" >&2
    done
    return 0
}

# _cc_remove_shadow <path> <canon_pkg> — remove one shadow copy, ONLY if
# provably a claude-code install. System-prefix removals need passwordless
# sudo; user paths are removed directly. Unprovable artifacts are warned and
# kept. A package dir equal to <canon_pkg> (the canonical's own package) is
# never removed — only the stale link into it.
_cc_remove_shadow() {
    local candidate="$1" canon_pkg="${2:-}" target pkg_dir=""
    target="$(readlink "$candidate" 2>/dev/null || true)"

    if [[ "$target" == *"@anthropic-ai/claude-code"* ]]; then
        # npm-style symlink → the package dir it points into (resolve relative
        # to the symlink's own directory).
        pkg_dir="$(cd "$(dirname "$candidate")" 2>/dev/null && cd "$(dirname "$target")" 2>/dev/null && pwd)"
        pkg_dir="${pkg_dir%%/@anthropic-ai/claude-code*}/@anthropic-ai/claude-code"
        [[ "$pkg_dir" == *"@anthropic-ai/claude-code" && -d "$pkg_dir" ]] || pkg_dir=""
    elif [[ "$candidate" == "$HOME/.claude/local/claude" ]]; then
        # migrate-installer layout: the launcher plus its own npm subtree
        # (~/.claude/local/node_modules/...) — remove both, not just the
        # launcher (the package tree is hundreds of MB of dead weight).
        pkg_dir="$HOME/.claude/local/node_modules/@anthropic-ai/claude-code"
        [ -d "$pkg_dir" ] || pkg_dir=""
    elif [[ "$target" == *"/.local/share/claude/"* ]]; then
        # Native-installer symlink — the blob dir is handled (and guarded)
        # by the sweep in cc_shadow_scan; only the link goes here.
        pkg_dir=""
    else
        echo "  WARNING: cc_shadow_scan: $candidate is not provably a claude-code install — left in place (remove manually if it is one)" >&2
        return 1
    fi

    # Never rm -rf the canonical's own package — a stale SECOND link into it
    # (e.g. an old entry-file path) loses only the link.
    if [ -n "$pkg_dir" ] && [ -n "$canon_pkg" ] \
        && [ "$(readlink -f "$pkg_dir" 2>/dev/null)" = "$canon_pkg" ]; then
        echo "  cc_shadow_scan: $candidate is a stale link into the CANONICAL package — removing the link only"
        pkg_dir=""
    fi

    local -a rm_link=(rm -f "$candidate")
    local -a rm_pkg=()
    [ -n "$pkg_dir" ] && rm_pkg=(rm -rf "$pkg_dir")
    case "$candidate" in
        /usr/*|/opt/*)
            if ! sudo -n true 2>/dev/null; then
                echo "  WARNING: cc_shadow_scan: shadow at $candidate needs sudo to remove — skipped" >&2
                return 1
            fi
            rm_link=(sudo -n "${rm_link[@]}")
            [ -n "$pkg_dir" ] && rm_pkg=(sudo -n "${rm_pkg[@]}")
            ;;
    esac
    echo "  cc_shadow_scan: removing shadow copy $candidate${pkg_dir:+ (+ $pkg_dir)}"
    "${rm_link[@]}"
    [ -n "$pkg_dir" ] && "${rm_pkg[@]}"
    return 0
}


# cc_align_host_sync — align the HOST VM's Node.js major + Claude Code to the
# repo pins via the guardian gateway, healing drift. Extracted from update.sh's
# _sync_deploy_targets so BOTH update.sh and the nightly genesis-cc-align timer
# (scripts/cc_align_host.sh) share ONE implementation — a pin bump reaches the
# host's `claude -p` recovery brain without waiting for the next manual update.
#
# Args: <host_user> <host_ip> <ssh_key> <host_ver_raw>
#   host_ver_raw = raw JSON from a prior `ssh … version` call. The CALLER fetches
#   it once (update.sh reuses the same response for its redeploy decision), so
#   this function never issues the version probe itself.
# Reads globals: NODE_MAJOR, CC_VERSION (set at the top of this file when sourced).
# APPENDS any alignment failure to the global HOST_CC_DEGRADED (comma-joined; the
#   `:+` form is set -u-safe). It NEVER re-inits that global — the caller owns the
#   init and may set sibling sentinels (guardian_config_unreadable) in branches
#   this function doesn't cover. Progress → stdout.
# Non-fatal by contract: ALWAYS returns 0 so a host hiccup can't abort an
#   update run under set -e (the ERR trap is already disarmed by then). Call as
#   `cc_align_host_sync … || true` to match the cc_ensure_local convention.
cc_align_host_sync() {
    local host_user="$1" host_ip="$2" ssh_key="$3" host_ver_raw="$4"
    local host_node_major host_cc

    # HOST_VER_RAW empty = genuinely could not reach/parse the gateway — DISTINCT
    # from "CC absent" (the conflation the old inline message got wrong).
    if [ -z "$host_ver_raw" ]; then
        echo "  Host gateway unreachable (no version response) — skipping Node/CC sync (non-fatal)"
        HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_host_unreachable"
        return 0
    fi

    host_node_major="$(printf '%s' "$host_ver_raw" \
        | grep -oE '"node_version": "v[0-9]+' | grep -oE '[0-9]+' || true)"
    host_cc="$(printf '%s' "$host_ver_raw" \
        | grep -oE '"cc_version": "[0-9]+\.[0-9]+\.[0-9]+' \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)"

    # ── Persist the probe for the deploy-staleness check ──
    # observability/snapshots/deploy_health.py reads deployed_commit from this
    # state file so the health path never SSHes. Both writers (update.sh and
    # the nightly cc-align timer) funnel through here. Atomic tmp+mv; a failed
    # parse (gateway emitted non-JSON) or unreachable probe (early return
    # above) never clobbers the last-known-good state.
    local _state_file="$HOME/.genesis/host_gateway_state.json"
    local _state_tmp="${_state_file}.tmp.$$"
    if GENESIS_HOST_VER_RAW="$host_ver_raw" python3 - "$_state_tmp" 2>/dev/null <<'PYEOF'
import json
import os
import sys
from datetime import UTC, datetime

payload = {
    "checked_at": datetime.now(UTC).isoformat(),
    "version": json.loads(os.environ["GENESIS_HOST_VER_RAW"]),
}
with open(sys.argv[1], "w") as f:
    json.dump(payload, f, indent=2)
PYEOF
    then
        mv -f "$_state_tmp" "$_state_file" 2>/dev/null || rm -f "$_state_tmp"
    else
        rm -f "$_state_tmp"
    fi

    # ── Node.js major sync (prerequisite for CC) ──
    if printf '%s' "${NODE_MAJOR:-}" | grep -qE '^[0-9]{1,2}$'; then
        if [ "$host_node_major" = "$NODE_MAJOR" ]; then
            echo "  Host Node.js already at major $NODE_MAJOR — no Node sync needed"
        else
            echo "--- Host Node.js: ${host_node_major:-unknown} → syncing to major $NODE_MAJOR ---"
            # 600s: NodeSource repo-add + apt install is heavier than an npm
            # install (update-cc uses 300s); bounds a hung dpkg lock.
            if timeout 600 ssh -i "$ssh_key" -o BatchMode=yes -o ConnectTimeout=30 \
                "${host_user}@${host_ip}" "update-node $NODE_MAJOR" 2>&1; then
                echo "  Host Node.js updated to major $NODE_MAJOR"
                host_node_major="$NODE_MAJOR"
            else
                echo "  WARNING: Host Node.js sync failed — CC install will likely fail (host stays on ${host_node_major:-unknown})"
                HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_host_node"
            fi
        fi
    fi

    # ── Claude Code sync: absence => INSTALL, drift => update ──
    if printf '%s' "${CC_VERSION:-}" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
        if [ -z "$host_cc" ]; then
            # cc_version was "unavailable"/unparseable → CC is NOT installed on the
            # host. INSTALL it (do not skip) — the exact case the old code silently
            # ignored, leaving Guardian's recovery brain offline.
            echo "--- Host Claude Code not installed — installing $CC_VERSION ---"
            if timeout 300 ssh -i "$ssh_key" -o BatchMode=yes -o ConnectTimeout=30 \
                "${host_user}@${host_ip}" "update-cc $CC_VERSION" 2>&1; then
                echo "  Host Claude Code installed ($CC_VERSION)"
            else
                echo "  WARNING: Host Claude Code install FAILED — Guardian intelligent recovery is OFFLINE"
                HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_host_cc"
            fi
        elif [ "$host_cc" = "$CC_VERSION" ]; then
            echo "  Host Claude Code already at pin ($CC_VERSION) — no CC sync needed"
        else
            echo "--- Host Claude Code drift: $host_cc → syncing to $CC_VERSION ---"
            if timeout 300 ssh -i "$ssh_key" -o BatchMode=yes -o ConnectTimeout=30 \
                "${host_user}@${host_ip}" "update-cc $CC_VERSION" 2>&1; then
                echo "  Host Claude Code updated to $CC_VERSION"
            else
                echo "  WARNING: Host Claude Code sync failed — host remains on $host_cc"
                HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_host_cc"
            fi
        fi
    fi
    return 0
}
