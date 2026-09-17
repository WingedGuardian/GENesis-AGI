#!/usr/bin/env bash
# codebase-memory-mcp installer — ONE site, shared by bootstrap.sh and
# install.sh, and runnable on its own:
#
#   bash scripts/lib/cbm_installer.sh
#
# Runnable on its own on purpose. Repairing one optional code-intel binary
# should not require a full machine bootstrap, and bootstrap.sh deliberately
# refuses to run while genesis-server is live — which is exactly the state an
# operator is in when their MCP server fails to start and they go looking for
# an install command.
#
# WHY ONE SITE. The pin and its digest have to move together. When they lived
# inline in two scripts, bumping the commit in both while leaving the digest
# stale in both passed every test and turned the install into a permanent
# no-op behind a "non-critical" warning. Here there is one of each, so they
# cannot drift apart and no test has to police it.
#
# WHAT THE PIN ACTUALLY BUYS, stated narrowly because a comment that overclaims
# is worse than no comment. `main` is mutable third-party code, so the ~13KB
# installer WRAPPER is pinned to a reviewed commit and checked against the
# digest below before it reaches bash. The release archive that wrapper then
# downloads is verified against upstream's own checksums.txt, fetched from the
# same mutable location — trust-on-first-use, not a repository-owned pin. That
# gap is upstream's to close; this file does not close it.
#
# Bump both values together when re-reviewing the upstream installer.
GENESIS_CBM_INSTALLER_COMMIT="59a05eb1bf9e11deb060d782cd7d3a29f2ae2866"  # pragma: allowlist secret  (public commit SHA, not a secret)
GENESIS_CBM_INSTALLER_SHA256="13049c7cc51bc508d68b8ecb8a9fd9574ecb7c6f2c9dd5a19bf7d4c187321145"  # pragma: allowlist secret  (public file digest, not a secret)

# The kill-switch path resolves through the ONE shared site — a missing or
# unsourceable resolver leaves genesis_cbm_disable_file undefined, which the
# check below reads as UNRESOLVABLE and refuses (fail closed, never silent).
# shellcheck source=cbm_disable_file.sh
. "$(dirname "${BASH_SOURCE[0]}")/cbm_disable_file.sh" 2>/dev/null || true

# 0 installed/upgraded · 1 download failed · 2 the installer ran and failed
# 3 the pin and the digest in THIS file disagree — see below, it is not transient
# 4 the machine kill switch is active, or its path cannot be resolved — refused
genesis_cbm_install() {
    # The kill switch is enforced INSIDE the function, not left to each caller
    # to remember: a sourced caller that skips its own sentinel check inherits
    # the same refusal the MCP launcher enforces. FAIL CLOSED on an
    # unresolvable path — with HOME unset the default used to collapse to
    # /.genesis/… — absolute, but not this machine's switch — so `-e` came
    # back false and the install proceeded precisely when the machine said
    # not to.
    local _cbm_disable=""
    if ! declare -F genesis_cbm_disable_file >/dev/null \
        || ! _cbm_disable="$(genesis_cbm_disable_file)"; then
        printf 'codebase-memory-mcp: the kill-switch path cannot be resolved; refusing to install.\n' >&2
        printf '  Set HOME or an absolute CODEBASE_MEMORY_MCP_DISABLE_FILE.\n' >&2
        return 4
    fi
    if [ -e "$_cbm_disable" ]; then
        printf 'codebase-memory-mcp: machine kill switch is active (%s); refusing to install\n' \
            "$_cbm_disable" >&2
        # 4, not 3: 3 is the digest-mismatch code, and a script whose exit
        # status means two different things is not a status.
        return 4
    fi
    local installer="" rc=0
    installer="$(mktemp 2>/dev/null)" || installer=""
    if [ -z "$installer" ]; then
        printf 'codebase-memory-mcp: no temporary file available\n' >&2
        return 1
    fi
    # Cleanup is an explicit statement rather than a trap. MEASURED: a RETURN
    # trap set inside a function does fire on every return path, but it PERSISTS
    # in the caller's shell afterwards — and both callers run `set -u`, where a
    # leaked `rm -f "$installer"` on some later function return would reference
    # an unbound name. An interrupt mid-download therefore leaves ~13KB behind;
    # that is the accepted cost.
    # `-fsSL` already carries `-S`, so curl explains its own failure. The
    # `2>/dev/null` that used to sit here cancelled exactly that: a mistyped but
    # still 40-hex commit 404s, and the 404 was destroyed on its way out, so the
    # caller rendered a permanent machine-wide no-op as "download failed".
    if ! curl -fsSL \
            "https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/${GENESIS_CBM_INSTALLER_COMMIT}/install.sh" \
            -o "$installer"; then
        rm -f "$installer"
        return 1
    fi
    # A DIGEST MISMATCH IS NOT ORDINARILY TRANSIENT, and separating it from the
    # download is what lets us say so. A commit SHA is content-addressed, so the
    # bytes at a pinned path do not change under us; the overwhelmingly likely
    # reading is that the two constants at the top of this file disagree. It is
    # not the only one — anything rewriting the response in transit lands here
    # too — so the message points at both without asserting a cause it cannot
    # know. Reported loudly either way, because folding it into the same
    # "non-critical" warning as a network blip is how a wrong digest becomes a
    # permanent, silent no-op on every machine.
    if ! echo "${GENESIS_CBM_INSTALLER_SHA256}  $installer" | sha256sum -c - >/dev/null 2>&1; then
        printf 'codebase-memory-mcp: the pinned installer does not match the committed digest.\n' >&2
        printf '  A commit SHA is content-addressed, so retrying will not help. Either\n' >&2
        printf '  GENESIS_CBM_INSTALLER_COMMIT and GENESIS_CBM_INSTALLER_SHA256 in\n' >&2
        printf '  scripts/lib/cbm_installer.sh disagree, or the download was altered in\n' >&2
        printf '  transit. Re-review the upstream installer before changing either value.\n' >&2
        rm -f "$installer"
        return 3
    fi
    # --skip-config: upstream's config step registers the RAW binary as an MCP
    # command. Genesis owns registration, through its own memory-capped launcher.
    # ONLY flags the pinned installer's parser accepts may appear here — an
    # unrecognised one hits its `-*)` case and exits 2 before doing any work,
    # which is how `--ui` made this a silent no-op.
    #
    # STDERR IS NOT DISCARDED. Upstream reports a checksum mismatch, an
    # unexpected archive member and a non-running binary on stderr; muting it
    # renders a release-integrity failure and "not available today" identically.
    bash "$installer" --skip-config || rc=2
    rm -f "$installer"
    return "$rc"
}

# Direct invocation only — sourcing defines the function and nothing else.
# The kill-switch refusal lives INSIDE genesis_cbm_install (rc 4), so direct
# run, sourced callers and the install paths share one enforcement site.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    genesis_cbm_install
fi
