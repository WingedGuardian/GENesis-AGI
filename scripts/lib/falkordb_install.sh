# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# falkordb_provision — optional graph-engine provisioning for the memory graph.
#
# Genesis's memory graph is an in-process NetworkX projection of memory_links.
# The graph-DB adoption (issue #1641) replaces that projection with FalkorDB, a
# Redis module: SQLite stays the system of record and the engine is a derived,
# rebuildable index. This lib provisions the SERVER side only — it installs
# nothing into the Python venv, wires no consumer, and arms nothing. The unit it
# ships is rendered by bootstrap's template loop and left DISABLED; a later
# slice adds the client and the config that can select it.
#
# Route chosen 2026-09-06 after measuring the alternatives: the standalone
# release module + a distro-managed Redis, NOT the `falkordblite` pip package.
# The pip route resolves its dependencies through a relative path into the
# Python package tree and ships VENDORED libssl/libcrypto that would never
# receive system security updates. The released module links the SYSTEM
# OpenSSL (verified by ldd on the shipped .so), so patching redis/openssl is
# ordinary apt work.
#
# MEASURED on the reference install 2026-09-06, module v4.20.4:
#   - the module REFUSES to load on redis-server 7.0.15 ("FalkorDB requires
#     redis-server version 8.0.0 and up"), which is what Ubuntu 24.04 ships;
#   - redis-server 8.x from the official upstream apt repo loads it and
#     answers GRAPH.QUERY (verified at 8.8.2 and again at 8.10.1, which is
#     what the repo serves today), including the named-path
#     `ALL(x IN relationships(p) ...)` form the adapter depends on;
#   - the released .so arrives mode 644 and redis refuses it outright with
#     "It does not have execute permissions" — hence the chmod below.
#
# Degrades gracefully everywhere: an unsupported architecture, a missing
# download tool, no sudo, an unknown distro codename, or a pre-existing
# redis-server each produce a one-line note and rc=0. Like the other lib
# fragments, this must never abort install.sh/bootstrap.sh/update.sh under
# `set -e`.
#
# Test seams (defaults are the real paths/URLs; the test harness overrides
# these and stubs sudo/apt-get/dpkg/curl/systemctl on PATH):
FALKORDB_VERSION="${FALKORDB_VERSION:-4.20.4}"
FALKORDB_DEPS_DIR="${FALKORDB_DEPS_DIR:-$HOME/.genesis/deps/falkordb}"
FALKORDB_DATA_DIR="${FALKORDB_DATA_DIR:-$HOME/.genesis/falkordb}"
FALKORDB_APT_KEYRING="${FALKORDB_APT_KEYRING:-/etc/apt/keyrings/redis-archive-keyring.gpg}"
FALKORDB_APT_LIST="${FALKORDB_APT_LIST:-/etc/apt/sources.list.d/redis.list}"
FALKORDB_KEY_URL="${FALKORDB_KEY_URL:-https://packages.redis.io/gpg}"
FALKORDB_REPO_URL="${FALKORDB_REPO_URL:-https://packages.redis.io/deb}"
FALKORDB_RELEASE_BASE="${FALKORDB_RELEASE_BASE:-https://github.com/FalkorDB/FalkorDB/releases/download}"
FALKORDB_OS_RELEASE="${FALKORDB_OS_RELEASE:-/etc/os-release}"

# CONSENT, as distinct from capability.
#
# Every other gate in this file asks "can we?" — sudo, apt, arch, codename.
# This one asks "may we?", and it is a different question. The package half
# adds a THIRD-PARTY APT REPO and installs a database daemon on someone's
# machine. update.sh re-runs bootstrap.sh on every update, so without this an
# operator who merely pulled Genesis would silently acquire a new apt trust
# anchor for a feature that is inert until a later release wires a consumer.
#
# So the system half is OPT-IN: set GENESIS_FALKORDB_PROVISION=1 (or
# graph_engine.provision: true in the local config) to allow it. The MODULE
# half stays automatic — it writes one file under ~/.genesis/deps and changes
# nothing about the system, so it costs an unwilling operator disk space and
# nothing else, and it means arming the engine later is a one-command step.
#
# GENESIS_FALKORDB_PROVISION_DISABLED=1 turns the whole thing off, module
# included, matching the kill-switch convention the other subsystems use.
FALKORDB_PROVISION_OPT_IN="${GENESIS_FALKORDB_PROVISION:-}"
FALKORDB_PROVISION_DISABLED="${GENESIS_FALKORDB_PROVISION_DISABLED:-}"
FALKORDB_LOCAL_CONFIG="${FALKORDB_LOCAL_CONFIG:-$HOME/.genesis/config/genesis.yaml}"

# _falkordb_opted_in — env first, then `provision: true` as a DIRECT child of a
# TOP-LEVEL `graph_engine:` in the local config. Deliberately a narrow grep
# rather than a YAML parse: this is a shell fragment with no parser available,
# and the fail direction is correct — anything it cannot read plainly reads as
# "no" (flow style, `graph_engine: {provision: true}`, is one such case).
#
# The KEY PATH is exact on purpose. This gate authorises adding a third-party
# apt repo and installing a system package, so it must read consent only where
# consent was written: `graph_engine:` must start at column 0, and `provision:`
# must sit at the indentation of the block's first child. Accepting any nested
# `provision: true` would let an unrelated sub-block — say a per-backend
# setting — stand in for a system change the operator never agreed to.
_falkordb_opted_in() {
    [ "$FALKORDB_PROVISION_OPT_IN" = "1" ] && return 0
    [ -r "$FALKORDB_LOCAL_CONFIG" ] || return 1
    awk '
        function indent_of(line) { match(line, /^[ \t]*/); return RLENGTH }
        /^graph_engine[[:space:]]*:/ { in_block = 1; child_indent = -1; next }
        # Any other column-0 key closes the block; graph_engine is top-level,
        # so the parent indent is always 0 and needs no tracking.
        /^[^[:space:]#]/ { in_block = 0 }
        # Skipped BEFORE the indent is captured: a comment must never define
        # what "direct child" means, or `# provision: true` would set the depth
        # that a deeper real key then matches.
        in_block && /^[[:space:]]*($|#)/ { next }
        in_block {
            ci = indent_of($0)
            if (child_indent < 0) child_indent = ci
            if (ci == child_indent &&
                # A trailing comment is still consent -- an operator who
                # annotates their own config has not withdrawn it. The
                # SPACE before `#` is required, not decoration. YAML only
                # starts a comment after a space, so `true#x` is the STRING
                # "true#x" and must not read as consent; and a TAB there makes
                # the whole file unparseable to PyYAML (measured), so matching
                # space-only leaves that case an under-read rather than
                # granting consent off a config Genesis itself cannot load.
                $0 ~ /^[[:space:]]*provision[[:space:]]*:[[:space:]]*(true|yes|on)( +#.*)?[[:space:]]*$/) found = 1
        }
        END { exit(found ? 0 : 1) }
    ' "$FALKORDB_LOCAL_CONFIG" 2>/dev/null
}
# Provenance marker. NOT the apt list file: SETUP.md tells operators with a
# pre-existing redis to create exactly that path by hand, and upstream's own
# install docs produce it too — so branching on it would have Genesis claim
# credit for a system change it explicitly declined to make. This file is
# written only by us, only after our own successful install.
FALKORDB_PROVISION_MARKER="${FALKORDB_PROVISION_MARKER:-$FALKORDB_DEPS_DIR/.redis-provisioned-by-genesis}"
# Binaries that mean "someone else's redis is already here". A seam because
# `command -v` searches the real PATH, which a stubbed test environment cannot
# hide — without this the suite would pass or fail depending on whether the
# machine running it happens to have redis installed.
FALKORDB_REDIS_BINARIES="${FALKORDB_REDIS_BINARIES:-redis-server valkey-server}"

# The minimum the module itself enforces at load time (measured above). Stated
# as a constant so the remediation text and any future check cite one source.
FALKORDB_MIN_REDIS="8.0.0"

# _falkordb_redis_present — is there a redis on this box we must not disturb?
#
# `dpkg -s` is the obvious check and it is WRONG: it exits 0 for a
# removed-but-not-purged package (status "deinstall ok config-files"), so a box
# where someone ran `apt remove redis-server` would be treated as having redis
# and never provisioned. Ask for the status field instead.
#
# The dpkg answer is also not the whole answer: a source-built redis under
# /usr/local, or valkey, is invisible to dpkg. Those still mean "someone else's
# database is on this machine", so the binary check backs it up.
_falkordb_redis_present() {
    local status
    if command -v dpkg-query >/dev/null 2>&1; then
        status="$(dpkg-query -W -f='${db:Status-Status}' redis-server 2>/dev/null || true)"
        [ "$status" = "installed" ] && return 0
    fi
    local binary
    for binary in $FALKORDB_REDIS_BINARIES; do
        command -v "$binary" >/dev/null 2>&1 && return 0
    done
    return 1
}

# _falkordb_arch — map uname to the release's asset suffix, or "" if we ship no
# asset for this machine. The release carries x64/arm64v8 (plus alpine/rhel and
# macos variants we do not use: this is a glibc Linux install path).
_falkordb_arch() {
    case "$(uname -m 2>/dev/null || echo unknown)" in
        x86_64|amd64) printf 'x64' ;;
        aarch64|arm64) printf 'arm64v8' ;;
        *) printf '' ;;
    esac
}

# _falkordb_expected_sha <version> <arch> — the pinned digest, or "" when we
# have not pinned that pair.
#
# The GitHub release ships NO checksum or signature assets (0 of 15, verified
# 2026-09-06), so without this the download is trusted on TLS alone. The x64
# digest below was computed from the exact artifact that was load-tested on the
# reference install. An operator who overrides FALKORDB_VERSION gets a loud
# warning instead of a silent unverified install: pinning a digest we have not
# actually run would be worse than admitting we have none.
_falkordb_expected_sha() {
    case "$1/$2" in
        4.20.4/x64) printf '81ea6b989dc2fd4c9ad905e246018b220b02f0e40c406255f9da4768c1684555' ;;  # pragma: allowlist secret  (public release checksum, not a secret)
        *) printf '' ;;
    esac
}

# falkordb_module_install — fetch + verify the engine module. No sudo: it lands
# under ~/.genesis/deps, the established home for downloaded dependencies.
falkordb_module_install() {
    local arch dest target url expected actual rc
    arch="$(_falkordb_arch)"
    if [ -z "$arch" ]; then
        echo "  Skipped: no FalkorDB module ships for $(uname -m 2>/dev/null || echo 'this architecture')."
        return 0
    fi

    dest="$FALKORDB_DEPS_DIR/$FALKORDB_VERSION"
    target="$dest/falkordb.so"
    if [ -f "$target" ]; then
        echo "  OK: FalkorDB module $FALKORDB_VERSION already present."
        return 0
    fi

    if ! command -v curl >/dev/null 2>&1; then
        echo "  Skipped: curl not available — cannot fetch the FalkorDB module."
        return 0
    fi

    mkdir -p "$dest" 2>/dev/null || {
        echo "  WARNING: could not create $dest — FalkorDB module NOT installed."
        return 0
    }

    url="$FALKORDB_RELEASE_BASE/v$FALKORDB_VERSION/falkordb-$arch.so"
    # Download to a sibling temp then move, so an interrupted fetch can never
    # leave a half-file that the `-f "$target"` check above would later treat
    # as installed.
    rc=0
    curl -fsSL --max-time 300 -o "$target.partial" "$url" || rc=$?
    if [ "$rc" -ne 0 ]; then
        rm -f "$target.partial" 2>/dev/null || true
        echo "  WARNING: download failed (curl rc=$rc) — FalkorDB module NOT installed."
        echo "           $url"
        return 0
    fi

    expected="$(_falkordb_expected_sha "$FALKORDB_VERSION" "$arch")"
    if [ -z "$expected" ]; then
        echo "  WARNING: no pinned checksum for FalkorDB $FALKORDB_VERSION/$arch —"
        echo "           installing UNVERIFIED (the upstream release ships no checksums)."
    else
        actual=""
        if command -v sha256sum >/dev/null 2>&1; then
            # `|| true`: under pipefail the pipeline's status would reach the
            # caller; 2>/dev/null hides the message, not the status.
            actual="$(sha256sum "$target.partial" 2>/dev/null | awk '{print $1}' || true)"
        fi
        if [ -z "$actual" ]; then
            # We HAVE a pin and cannot check it — refuse. That is different from
            # the unpinned case above, which is a deliberate operator override.
            rm -f "$target.partial" 2>/dev/null || true
            echo "  WARNING: cannot verify the FalkorDB module (sha256sum unavailable)"
            echo "           and a digest is pinned for this version — refusing to install."
            return 0
        elif [ "$actual" != "$expected" ]; then
            rm -f "$target.partial" 2>/dev/null || true
            echo "  WARNING: FalkorDB module checksum MISMATCH — refusing to install."
            echo "           expected $expected"
            echo "           actual   $actual"
            return 0
        fi
    fi

    mv "$target.partial" "$target" 2>/dev/null || {
        rm -f "$target.partial" 2>/dev/null || true
        echo "  WARNING: could not place $target — FalkorDB module NOT installed."
        return 0
    }
    # Redis refuses a module without the execute bit; the release asset is 644.
    chmod +x "$target" 2>/dev/null || true
    echo "  Installed: FalkorDB module $FALKORDB_VERSION ($arch)"
    return 0
}

# falkordb_redis_install — provide a redis-server new enough to load the module.
#
# Ubuntu/Debian stable ship 7.x, below the module's hard 8.0.0 floor, so this
# adds the official upstream apt repo. That is a real system change and it is
# gated twice: on passwordless sudo, and on there being no redis-server already.
#
# The pre-existing check is the important one and it is deliberately total —
# it skips the REPO ADD as well as the install. Adding the repo to a box that
# already runs redis would silently promote the operator's 7.x to 8.x on their
# next unrelated `apt upgrade`, which is a worse thing to do to someone's
# machine than declining to provision.
falkordb_redis_install() {
    local codename rc keytmp
    if ! _falkordb_opted_in; then
        echo "  Skipped: graph-engine server not provisioned (opt-in)."
        echo "           It adds the upstream redis apt repo and installs redis-server >= $FALKORDB_MIN_REDIS."
        echo "           To allow it: GENESIS_FALKORDB_PROVISION=1 ./scripts/bootstrap.sh"
        echo "           or set graph_engine.provision: true in ~/.genesis/config/genesis.yaml"
        return 0
    fi
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "  Skipped: no apt-get — install redis-server >= $FALKORDB_MIN_REDIS by hand to use the graph engine."
        return 0
    fi

    if _falkordb_redis_present; then
        # Distinguish "we provisioned this on an earlier run" from "the operator
        # already had redis". Our apt list file is the marker: we write it, and
        # only on a box that had no redis. Without this split, every re-run on a
        # box we provisioned would print an operator-decision message about a
        # decision that was already made — misleading, and the kind of message
        # that trains people to ignore output.
        if [ -f "$FALKORDB_PROVISION_MARKER" ]; then
            echo "  OK: redis-server already provisioned."
        else
            echo "  Skipped: redis-server is already installed — leaving it, and the apt repo, alone."
            echo "           FalkorDB needs >= $FALKORDB_MIN_REDIS. Adding the upstream repo would"
            echo "           upgrade your redis on the next apt upgrade, so that is your call:"
            echo "           see SETUP.md, 'Graph engine (FalkorDB)', for the commands."
        fi
        return 0
    fi

    if ! sudo -n true 2>/dev/null; then
        echo "  Skipped: sudo unavailable non-interactively. To provision the graph engine's server:"
        echo "           sudo bash -c 'source scripts/lib/falkordb_install.sh && falkordb_redis_install'"
        return 0
    fi

    codename=""
    if [ -r "$FALKORDB_OS_RELEASE" ]; then
        # Guarded: a bare assignment carries the substitution's status, and an
        # os-release that fails to source would abort bootstrap under set -e —
        # against this lib's never-abort contract.
        rc=0
        # shellcheck disable=SC1090
        codename="$(. "$FALKORDB_OS_RELEASE" && printf '%s' "${VERSION_CODENAME:-}")" || rc=$?
        [ "$rc" -eq 0 ] || codename=""
    fi
    if [ -z "$codename" ]; then
        echo "  Skipped: could not read VERSION_CODENAME from $FALKORDB_OS_RELEASE — cannot pick an apt suite."
        return 0
    fi

    if [ ! -f "$FALKORDB_APT_LIST" ]; then
        if ! command -v curl >/dev/null 2>&1 || ! command -v gpg >/dev/null 2>&1; then
            echo "  Skipped: curl and gpg are both required to add the redis apt repo."
            return 0
        fi
        sudo mkdir -p "$(dirname "$FALKORDB_APT_KEYRING")" 2>/dev/null || true
        # Deliberately NOT `curl | gpg`: after a pipeline `$?` is the LAST
        # component's status, so a failed download followed by a "successful"
        # dearmor of nothing would install an empty keyring and report success.
        # Fetch to a temp file, check curl on its own, then convert.
        keytmp="$(mktemp 2>/dev/null || printf '')"
        if [ -z "$keytmp" ]; then
            echo "  WARNING: could not create a temp file for the signing key — repo NOT added."
            return 0
        fi
        rc=0
        curl -fsSL --max-time 60 -o "$keytmp" "$FALKORDB_KEY_URL" || rc=$?
        if [ "$rc" -ne 0 ] || [ ! -s "$keytmp" ]; then
            rm -f "$keytmp" 2>/dev/null || true
            echo "  WARNING: could not download the redis signing key (rc=$rc) — repo NOT added."
            return 0
        fi
        rc=0
        sudo gpg --yes --dearmor -o "$FALKORDB_APT_KEYRING" "$keytmp" 2>/dev/null || rc=$?
        rm -f "$keytmp" 2>/dev/null || true
        if [ "$rc" -ne 0 ]; then
            echo "  WARNING: could not install the redis signing key (rc=$rc) — repo NOT added."
            return 0
        fi
        printf 'deb [signed-by=%s] %s %s main\n' \
            "$FALKORDB_APT_KEYRING" "$FALKORDB_REPO_URL" "$codename" \
            | sudo tee "$FALKORDB_APT_LIST" >/dev/null 2>&1 || {
                echo "  WARNING: could not write $FALKORDB_APT_LIST — repo NOT added."
                return 0
            }
        echo "  Added: redis apt repo ($codename)"
        rc=0
        sudo apt-get update -qq >/dev/null 2>&1 || rc=$?
        [ "$rc" -eq 0 ] || echo "  WARNING: apt-get update failed (rc=$rc) — the install below may not find redis 8.x."
    fi

    rc=0
    sudo apt-get install -y -qq redis-server >/dev/null 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "  WARNING: redis-server install failed (rc=$rc) — the graph engine cannot start."
        return 0
    fi

    # The deb enables a SYSTEM redis on 6379. Genesis does not want it: our unit
    # is a per-user instance with --port 0 that speaks only over a unix socket.
    #
    # Reached only when _falkordb_redis_present said no, so this stands down a
    # unit THIS run created. Record that, both so a re-run can tell our work
    # from an operator's and so the disable is never replayed against a redis
    # someone installed afterwards.
    mkdir -p "$(dirname "$FALKORDB_PROVISION_MARKER")" 2>/dev/null || true
    date -u +%Y-%m-%dT%H:%M:%SZ > "$FALKORDB_PROVISION_MARKER" 2>/dev/null || true
    sudo systemctl disable --now redis-server >/dev/null 2>&1 || true
    echo "  Installed: redis-server (system unit disabled — Genesis uses a socket-only user unit)"
    return 0
}

# falkordb_provision — the single entry point bootstrap calls.
falkordb_provision() {
    if [ "$FALKORDB_PROVISION_DISABLED" = "1" ]; then
        echo "  Skipped: GENESIS_FALKORDB_PROVISION_DISABLED=1."
        return 0
    fi
    mkdir -p "$FALKORDB_DATA_DIR" 2>/dev/null || true
    falkordb_redis_install
    falkordb_module_install
    return 0
}
