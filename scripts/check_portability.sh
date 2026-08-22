#!/bin/bash
# Fail fast on machine-specific runtime assumptions that should not survive public migration work.
#
# Patterns are ADDRESS CLASSES, not an enumeration of past leaks: any
# RFC1918 IPv4 literal, any Tailscale CGNAT IPv4 literal, any IPv6 ULA
# literal. An enumerated list only catches the leaks somebody already
# shipped; a class catches the next one. Documentation examples in code
# comments should use the RFC 5737 ranges (192.0.2.0/24, 198.51.100.0/24,
# 203.0.113.0/24) — those are deliberately NOT flagged.
#
# Scope split with CI: this script owns NETWORK-ADDRESS portability;
# the ci.yml "Portability scan" step owns paths (/home/<user>/genesis)
# and timezone literals with its own curated excludes. Don't re-add a
# path pattern here — provisioning scripts (host-setup.sh, install.sh)
# legitimately construct the standard /home/ubuntu container layout and
# would drown the signal.

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

# --warn: NON-gating advisory sweep for the contributor-facing CI. It also
# covers tests/ and ALWAYS exits 0 (fixtures may carry in-class placeholder
# addresses — surfaced as warnings, never a hard block). Default (no flag)
# keeps the fail-fast gate for local/maintainer use. This is the single source
# of truth for the network-address class vocabulary shared with sanitize.py's
# _PORTABILITY_PATTERNS and the commit-msg hook (a drift test asserts parity).
WARN=0
if [ "${1:-}" = "--warn" ]; then
    WARN=1
    shift
fi

REPO_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"

patterns=(
  # RFC1918 IPv4 literals (any private address, not just known subnets)
  '\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b'
  '\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b'
  '\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b'
  # Tailscale CGNAT range (100.64.0.0/10)
  '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b'
  # IPv6 ULA literals (fc00::/7 — covers fdXX: container/host/Tailscale prefixes)
  '\b[fF][cdCD][0-9a-fA-F]{2}:'
)

targets=(
  "$REPO_DIR/src"
  "$REPO_DIR/config"
  "$REPO_DIR/scripts"
  "$REPO_DIR/env.example"
)
# Advisory mode also sweeps tests/ — the gitleaks/portability gates exclude it,
# so an in-class fixture value ships unseen; surfacing it as a warning closes
# that blind spot without gating (exact install values are hard-gated by the
# CI secret scan instead).
if [ "$WARN" -eq 1 ]; then
    targets+=("$REPO_DIR/tests")
fi

# scripts/hooks/commit-msg is a scanner DEFINITION (its pattern string
# contains address fragments as data) — same rationale as CI excluding
# sanitize.py from its own scan.
excludes=(
  --glob '!**/.git/**'
  --glob '!**/.claude/**'
  --glob '!**/logs/**'
  --glob '!**/docs/**'
  --glob '!**/CLAUDE.md'
  --glob '!**/genesis-container-setup.md'
  --glob '!**/scripts/check_portability.sh'
  --glob '!**/scripts/hooks/commit-msg'
  --glob '!**/scripts/cc_cli_output/**'
  --glob '!**/scripts/spike_*'
  # netclass.py is a LAN/WAN classifier — it defines the RFC-grounded private
  # address ranges (10/8, 172.16/12, 192.168/16, 100.64/10, fc00::/7) as data,
  # not install-specific addresses. Same rationale as excluding sanitize.py.
  --glob '!**/src/genesis/util/netclass.py'
)

# Scan only targets that exist: a missing path makes rg exit 2 even when
# it found matches elsewhere, and `if rg` would then read that as "clean"
# — real hits silently dropped (partial checkouts, test fixtures).
existing=()
for t in "${targets[@]}"; do
    [ -e "$t" ] && existing+=("$t")
done
if [ ${#existing[@]} -eq 0 ]; then
    echo "check_portability: no scan targets exist under $REPO_DIR" >&2
    exit 2
fi

# ~/tmp per project convention — bare mktemp lands in $TMPDIR, which a CC
# session points at the watchdog-policed cc-tmp.
mkdir -p "$HOME/tmp"
SCAN_OUT="$(mktemp -p "$HOME/tmp" genesis-portability-scan.XXXXXX)"
trap 'rm -f "$SCAN_OUT"' EXIT

status=0
for pattern in "${patterns[@]}"; do
    rc=0
    rg -n --hidden "${excludes[@]}" "$pattern" "${existing[@]}" >"$SCAN_OUT" || rc=$?
    if [ "$rc" -eq 0 ]; then
        if [ "$WARN" -eq 1 ]; then
            # Advisory: emit the hits for the caller to annotate; never gate.
            cat "$SCAN_OUT"
        else
            echo "Portability check failed for pattern: $pattern"
            cat "$SCAN_OUT"
            status=1
        fi
    elif [ "$rc" -ge 2 ]; then
        # rg itself failed (bad glob, I/O error) — never report clean.
        echo "check_portability: rg error (exit $rc) for pattern: $pattern" >&2
        status="$rc"
    fi
done

# --warn is purely advisory — CI must never fail on it (grep/rg no-match and
# fixture hits alike exit 0). The exact-value hard gate lives in the CI secret scan.
if [ "$WARN" -eq 1 ]; then
    exit 0
fi
exit "$status"
