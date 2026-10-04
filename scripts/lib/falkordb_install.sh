# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# falkordb_provision — optional graph-engine provisioning for the memory graph.
#
# Genesis's memory graph is an in-process NetworkX projection of memory_links.
# The graph-DB adoption (issue #1641) replaces that projection with FalkorDB, a
# Redis module: SQLite stays the system of record and the engine is a derived,
# rebuildable index. This lib fetches and verifies the engine MODULE only — one
# file under ~/.genesis/deps. It installs nothing into the Python venv, changes
# nothing about the system, and arms nothing. The unit is rendered by the
# template loops in bootstrap.sh/install.sh and left DISABLED.
#
# The redis-server that loads the module is an operator step (SETUP.md, "Graph
# engine (FalkorDB)"): the module refuses anything below 8.0.0 and Ubuntu/Debian
# stable ship 7.x, so it comes from Redis's upstream apt repo. Automated
# provisioning of it is tracked separately (issue #2827).
#
# MEASURED on the reference install 2026-09-06, module v4.20.4:
#   - the module REFUSES to load on redis-server 7.0.15 ("FalkorDB requires
#     redis-server version 8.0.0 and up"), which is what Ubuntu 24.04 ships;
#   - redis-server 8.x from the official upstream apt repo loads it and
#     answers GRAPH.QUERY (verified at 8.8.2 and again at 8.10.1), including
#     the named-path `ALL(x IN relationships(p) ...)` form the adapter depends on;
#   - the released .so arrives mode 644 and redis refuses it outright with
#     "It does not have execute permissions" — hence the chmod below.
#
# Degrades gracefully everywhere: an unsupported architecture, an unpinned
# version, a missing download tool, or a failed download each produce a
# one-line note and rc=0. Like the other lib fragments, this must never abort
# install.sh/bootstrap.sh/update.sh under `set -e`.
#
# Test seams (defaults are the real paths/URLs; the test harness overrides
# these and stubs tools on PATH):
FALKORDB_VERSION="${FALKORDB_VERSION:-4.20.4}"
FALKORDB_DEPS_DIR="${FALKORDB_DEPS_DIR:-$HOME/.genesis/deps/falkordb}"
FALKORDB_DATA_DIR="${FALKORDB_DATA_DIR:-$HOME/.genesis/falkordb}"
FALKORDB_RELEASE_BASE="${FALKORDB_RELEASE_BASE:-https://github.com/FalkorDB/FalkorDB/releases/download}"

# GENESIS_FALKORDB_PROVISION_DISABLED=1 turns the whole thing off, matching
# the kill-switch convention the other subsystems use.
FALKORDB_PROVISION_DISABLED="${GENESIS_FALKORDB_PROVISION_DISABLED:-}"

# Binaries that can serve the unit's ExecStart. A seam because `command -v`
# searches the real PATH, which a stubbed test environment cannot hide.
FALKORDB_REDIS_BINARIES="${FALKORDB_REDIS_BINARIES:-redis-server valkey-server}"

# _falkordb_redis_server_bin — the unit's ExecStart executable, ready to sit
# inside the template's double quotes. systemd does no PATH lookup, so resolve
# at render time: /usr/bin/redis-server is right on Debian/Ubuntu and wrong
# under /usr/local (source builds, Homebrew, some RPM layouts), where the unit
# dies with a bare 203/EXEC. Falls back to that literal when nothing usable is
# on PATH — a wrong but absolute path is better than an ExecStart that does
# not parse. (The unit is inert where redis is absent because nothing enables
# or pulls it in — genesis-server orders After= it and does not Wants=.)
#
# systemd's rules for that word, per systemd.service(5) "Command Lines" and
# systemd.syntax(7) "Quoting", and MEASURED with `systemd-analyze --user
# verify` (systemd 255):
#   - whitespace splits an unquoted item, so the template quotes it;
#   - "%" starts a specifier (systemd.unit(5)) even inside quotes — a path
#     with `p%c` resolved to `p/<unit name>` — so it is doubled here;
#   - `"`, `\` and control characters make the unit FATALLY bad ("Executable
#     name contains special characters"), escaped or not, so such a path is
#     skipped exactly like a relative one;
#   - `$` is NOT substituted in the executable, so it is left alone.
_falkordb_redis_server_bin() {
    local binary path
    for binary in $FALKORDB_REDIS_BINARIES; do
        path="$(command -v "$binary" 2>/dev/null || true)"
        # Absolute AND executable, not merely non-empty. `command -v` echoes a
        # RELATIVE path from a relative PATH entry, and a bare name when a shell
        # function shadows the binary — either makes systemd refuse to PARSE the
        # unit, which is worse than the hardcoded literal fallback.
        case "$path" in /*) ;; *) path="" ;; esac
        case "$path" in *[[:cntrl:]\\\"]*) path="" ;; esac
        if [ -n "$path" ] && [ -x "$path" ]; then
            printf '%s' "${path//%/%%}"
            return 0
        fi
    done
    printf '/usr/bin/redis-server'
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
# reference install. An operator who overrides FALKORDB_VERSION to a pair with
# no pinned digest is REFUSED, not warned — the module is loaded as native
# code, so installing bytes we cannot authenticate is an integrity gap, not a
# supported escape hatch. Pinning a digest we have not actually run is the
# answer for a real upgrade, not an unverified install.
_falkordb_expected_sha() {
    case "$1/$2" in
        4.20.4/x64) printf '81ea6b989dc2fd4c9ad905e246018b220b02f0e40c406255f9da4768c1684555' ;;  # pragma: allowlist secret  (public release checksum, not a secret)
        *) printf '' ;;
    esac
}

# _falkordb_sha256 <file> — its digest, or "" when it cannot be computed.
# `|| true`: under pipefail the pipeline's status would reach the caller;
# 2>/dev/null hides the message, not the status.
_falkordb_sha256() {
    command -v sha256sum >/dev/null 2>&1 || return 0
    sha256sum "$1" 2>/dev/null | awk '{print $1}' || true
}

# _falkordb_module_verified — rc 0 only when the module the unit will load is
# on disk, executable, AND hashes to the pinned digest right now. Presence is
# not proof: the file is native code, so the digest binds every run rather
# than only the download that first placed it.
_falkordb_module_verified() {
    local arch expected target actual
    arch="$(_falkordb_arch)"
    [ -n "$arch" ] || return 1
    expected="$(_falkordb_expected_sha "$FALKORDB_VERSION" "$arch")"
    [ -n "$expected" ] || return 1
    target="$FALKORDB_DEPS_DIR/$FALKORDB_VERSION/falkordb.so"
    [ -f "$target" ] && [ -x "$target" ] || return 1
    actual="$(_falkordb_sha256 "$target")"
    [ -n "$actual" ] && [ "$actual" = "$expected" ]
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

    # Fail closed before any download: a version/arch pair we have not pinned
    # a digest for cannot be verified, and the module runs as native code.
    expected="$(_falkordb_expected_sha "$FALKORDB_VERSION" "$arch")"
    if [ -z "$expected" ]; then
        echo "  Skipped: no pinned checksum for FalkorDB $FALKORDB_VERSION/$arch —"
        echo "           refusing to install a module the repository has not verified."
        echo "           To adopt a newer build, pin its digest in _falkordb_expected_sha."
        return 0
    fi

    dest="$FALKORDB_DEPS_DIR/$FALKORDB_VERSION"
    target="$dest/falkordb.so"
    if [ -f "$target" ]; then
        # A file on disk is a CLAIM that it was verified once. Re-hash it:
        # corruption, a truncation that kept the mode, or hand-seeded bytes
        # would otherwise be loaded as native code indefinitely.
        actual="$(_falkordb_sha256 "$target")"
        if [ -z "$actual" ]; then
            echo "  WARNING: cannot verify the cached FalkorDB module (sha256sum unavailable)"
            echo "           — left in place but UNVERIFIED."
            return 0
        fi
        if [ "$actual" != "$expected" ]; then
            if ! rm -f "$target" 2>/dev/null || [ -e "$target" ]; then
                echo "  WARNING: cached $target fails the pinned digest and could not be"
                echo "           removed — FalkorDB module NOT verified."
                return 0
            fi
            echo "  WARNING: cached FalkorDB module failed the pinned digest — removed; re-downloading."
        else
            # Verified bytes, but redis refuses a module without the execute
            # bit — verify the bit rather than trusting it.
            if [ -x "$target" ]; then
                echo "  OK: FalkorDB module $FALKORDB_VERSION already present."
                return 0
            fi
            if chmod +x "$target" 2>/dev/null; then
                echo "  OK: FalkorDB module $FALKORDB_VERSION already present (restored +x)."
                return 0
            fi
            rm -f "$target" 2>/dev/null || true
            echo "  WARNING: $target exists without the execute bit and could not be"
            echo "           repaired — removed so the next run reinstalls cleanly."
            return 0
        fi
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

    actual="$(_falkordb_sha256 "$target.partial")"
    if [ -z "$actual" ]; then
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

    mv "$target.partial" "$target" 2>/dev/null || {
        rm -f "$target.partial" 2>/dev/null || true
        echo "  WARNING: could not place $target — FalkorDB module NOT installed."
        return 0
    }
    # Redis refuses a module without the execute bit; the release asset is 644.
    # A chmod failure is NOT survivable: reporting success would leave a file
    # the `-f "$target"` check above treats as installed, so every later run
    # would skip the repair and redis would refuse it at load time forever.
    # Remove it so the next run downloads and tries again.
    if ! chmod +x "$target" 2>/dev/null; then
        rm -f "$target" 2>/dev/null || true
        echo "  WARNING: could not set the execute bit on $target — FalkorDB module NOT installed."
        return 0
    fi
    echo "  Installed: FalkorDB module $FALKORDB_VERSION ($arch)"
    return 0
}

# falkordb_provision — the single entry point bootstrap calls. Fetches and
# verifies the module; the system side (redis-server >= 8.0.0) is the
# operator's, per SETUP.md.
falkordb_provision() {
    if [ "$FALKORDB_PROVISION_DISABLED" = "1" ]; then
        echo "  Skipped: GENESIS_FALKORDB_PROVISION_DISABLED=1."
        return 0
    fi
    mkdir -p "$FALKORDB_DATA_DIR" 2>/dev/null || true
    falkordb_module_install
    return 0
}
