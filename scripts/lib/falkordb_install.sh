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
# Route chosen 2026-09-06: the release module + a distro-managed Redis, NOT
# `falkordblite` pip — that package resolves deps through the Python package
# tree and ships vendored libssl/libcrypto that never gets security updates.
# The released module links the SYSTEM OpenSSL (ldd-verified), so patching
# redis/openssl is ordinary apt work.
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

# Pin the repository signing key: the full fingerprint of the ONE primary key
# the downloaded file must hold before it is installed as an apt keyring
# (rsa4096, "Redis (Package Signing)", created 2021-07-21). Deliberately NOT a
# test seam — an environment that could override the pin would make it advice.
# Provenance, stated honestly: Redis's own apt documentation publishes no
# fingerprint. This is the key packages.redis.io/gpg served on 2026-10-03,
# cross-checked against independent third-party pins of the same repository:
# Canonical's charmed-redis-rock and harbor-rocks (rockcraft.yaml `key-id`)
# and voxpupuli puppet-redis (manifests/init.pp `apt_key_id`). A rotated
# upstream key makes provisioning refuse until this value is updated.
FALKORDB_REDIS_KEY_FPR="54318FA4052D1E61A6B6F7BB5F4349D6BF53AA0C"

# CONSENT, as distinct from capability. The system half adds a THIRD-PARTY
# APT REPO and installs a daemon, and update.sh re-runs bootstrap.sh on every
# update — so it is OPT-IN: GENESIS_FALKORDB_PROVISION=1, or
# graph_engine.provision: true in the local config. The MODULE half stays
# automatic — one file under ~/.genesis/deps, nothing about the system.
# GENESIS_FALKORDB_PROVISION_DISABLED=1 turns the whole thing off, matching
# the kill-switch convention the other subsystems use.
FALKORDB_PROVISION_OPT_IN="${GENESIS_FALKORDB_PROVISION:-}"
FALKORDB_PROVISION_DISABLED="${GENESIS_FALKORDB_PROVISION_DISABLED:-}"
FALKORDB_LOCAL_CONFIG="${FALKORDB_LOCAL_CONFIG:-$HOME/.genesis/config/genesis.yaml}"

# The venv's interpreter, which bootstrap creates before this step runs. The
# lib's own path locates it, because install locations vary.
FALKORDB_PYTHON="${FALKORDB_PYTHON:-$(dirname "${BASH_SOURCE[0]}")/../../.venv/bin/python}"

# _falkordb_opted_in — env first, then graph_engine.provision in the local
# config, read the way Genesis reads that file (yaml.safe_load, as env.py
# does) so the gate can never disagree with the loader. Consent is a YAML
# boolean true and nothing else; under the YAML 1.1 rules safe_load applies
# that includes `yes` and `on` in any of their three cases. A missing key, a
# string, a non-mapping, a parse error, or no venv to parse with all read as
# "no" — and the last two say so.
_falkordb_opted_in() {
    local rc
    [ "$FALKORDB_PROVISION_OPT_IN" = "1" ] && return 0
    [ -r "$FALKORDB_LOCAL_CONFIG" ] || return 1
    if [ ! -x "$FALKORDB_PYTHON" ]; then
        echo "  NOTE: no venv Python at $FALKORDB_PYTHON to read $FALKORDB_LOCAL_CONFIG —"
        echo "        graph_engine.provision is treated as not set."
        return 1
    fi
    rc=0
    "$FALKORDB_PYTHON" - "$FALKORDB_LOCAL_CONFIG" <<'PY' 2>/dev/null || rc=$?
import sys
try:
    import yaml
    with open(sys.argv[1]) as fh:
        cfg = yaml.safe_load(fh)
except Exception:
    sys.exit(2)
section = cfg.get("graph_engine") if isinstance(cfg, dict) else None
value = section.get("provision") if isinstance(section, dict) else None
sys.exit(0 if isinstance(value, bool) and value else 1)
PY
    if [ "$rc" -eq 2 ]; then
        echo "  NOTE: $FALKORDB_LOCAL_CONFIG could not be parsed as YAML —"
        echo "        graph_engine.provision is treated as not set."
    fi
    [ "$rc" -eq 0 ]
}
# Provenance, in two pieces. The COMPLETION marker says the stand-down
# verified; the INSTALL claim is a stamp line appended to the apt list file
# we ourselves write — "<version>:<info-file mtime>", a fingerprint of that
# package instance (see _falkordb_redis_pkg_stamp). A run killed between
# install and stand-down retries exactly the unfinished step next time; an
# operator's purge or replacement changes the fingerprint, so the claim
# self-invalidates and the retry can never stand down a redis that is not
# the one we put there. The stamp lives in the apt list, NOT in a HOME
# marker: the claim must survive the directory that holds it being
# unwritable — losing it strands an installed daemon we can never retry.
FALKORDB_PROVISION_MARKER="${FALKORDB_PROVISION_MARKER:-$FALKORDB_DEPS_DIR/.redis-provisioned-by-genesis}"
FALKORDB_DPKG_INFO="${FALKORDB_DPKG_INFO:-/var/lib/dpkg/info}"

# _falkordb_redis_pkg_stamp — "<version>:<mtime of its dpkg info file>" for
# the INSTALLED redis-server. dpkg rewrites the info file on every install,
# so a purge+reinstall yields a different stamp. Empty when the identity
# cannot be pinned down; callers decline rather than trust a stale claim.
_falkordb_redis_pkg_stamp() {
    local ver mtime
    command -v dpkg-query >/dev/null 2>&1 || return 1
    ver="$(dpkg-query -W -f='${Version}' redis-server 2>/dev/null || true)"
    [ -n "$ver" ] || return 1
    mtime="$(stat -c %Y "$FALKORDB_DPKG_INFO/redis-server.list" 2>/dev/null || true)"
    [ -n "$mtime" ] || return 1
    printf '%s:%s' "$ver" "$mtime"
}
# Binaries that mean "someone else's redis is already here". A seam because
# `command -v` searches the real PATH, which a stubbed test environment cannot
# hide — without this the suite would pass or fail depending on whether the
# machine running it happens to have redis installed.
FALKORDB_REDIS_BINARIES="${FALKORDB_REDIS_BINARIES:-redis-server valkey-server}"

# The minimum the module itself enforces at load time (measured above). Stated
# as a constant so the remediation text and any future check cite one source.
FALKORDB_MIN_REDIS="8.0.0"

# _falkordb_redis_server_bin — the ABSOLUTE path the unit's ExecStart needs.
# systemd does no PATH lookup, so resolve at render time: /usr/bin/redis-server
# is right on Debian/Ubuntu and wrong under /usr/local (source builds,
# Homebrew, some RPM layouts), where the unit dies with a bare 203/EXEC.
# Falls back to the historical literal when nothing is on PATH — a wrong but
# absolute path is better than an empty ExecStart, which would not parse.
# (The unit is inert where redis is absent because nothing enables or pulls
# it in — genesis-server orders After= it and deliberately does not Wants=.)
_falkordb_redis_server_bin() {
    local binary path
    for binary in $FALKORDB_REDIS_BINARIES; do
        path="$(command -v "$binary" 2>/dev/null || true)"
        # Absolute AND executable, not merely non-empty. `command -v` echoes a
        # RELATIVE path from a relative PATH entry, and a bare name when a shell
        # function shadows the binary — either makes systemd refuse to PARSE the
        # unit, which is worse than the hardcoded literal this replaced: the
        # unit fails to load rather than merely failing to execute.
        case "$path" in /*) ;; *) path="" ;; esac
        if [ -n "$path" ] && [ -x "$path" ]; then
            printf '%s' "$path"
            return 0
        fi
    done
    printf '/usr/bin/redis-server'
}

# _falkordb_redis_present — is there a redis on this box we must not disturb?
# `dpkg -s` is the obvious check and WRONG: it exits 0 for a removed-but-not-
# purged package ("deinstall ok config-files"); ask for the status field.
# The binary check backs it up because a source-built redis or valkey is
# invisible to dpkg — still "someone else's database on this machine".
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
            echo "           — left in place but UNVERIFIED; redis will not be installed for it."
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

# _falkordb_meets_floor <upstream-version> — rc 0 only for a valid Debian
# version at or above the floor. dpkg does the ordering, so a prerelease such
# as `8.0~rc1` sorts below 8.0.0. Validation comes first because dpkg compares
# a malformed version (apt's `(none)`) as GREATER than any valid one.
_falkordb_meets_floor() {
    dpkg --validate-version "$1" >/dev/null 2>&1 \
        && dpkg --compare-versions "$1" ge "$FALKORDB_MIN_REDIS" 2>/dev/null
}

# _falkordb_repo_undo — remove what the repo-add block wrote. Called only from
# that block's clean-slate path (or right after it), so the list and keyring
# are this run's; the directory goes only if this run created it.
_falkordb_repo_undo() {
    sudo rm -f "$FALKORDB_APT_LIST" "$FALKORDB_APT_KEYRING" 2>/dev/null || true
    if [ "${_falkordb_made_keydir:-0}" = "1" ]; then
        sudo rmdir "$(dirname "$FALKORDB_APT_KEYRING")" 2>/dev/null || true
    fi
}

# _falkordb_signing_key_ok <keyfile> — rc 0 only when the file holds exactly
# one primary key and its fingerprint is FALKORDB_REDIS_KEY_FPR. On refusal it
# prints why. It reads the machine-readable colon listing, never gpg's
# human-formatted output, and takes the `fpr` record that immediately follows
# the `pub` record — a key's subkeys carry `fpr` records of their own. A
# throwaway GNUPGHOME keeps the listing from creating or touching anyone's
# keyring; nothing here changes the system.
_falkordb_signing_key_ok() {
    local keyfile="$1" gnupg_home listing rc parsed n fpr
    gnupg_home="$(mktemp -d 2>/dev/null || printf '')"
    if [ -z "$gnupg_home" ]; then
        echo "  WARNING: could not create a temp dir to check the signing key — repo NOT added."
        return 1
    fi
    rc=0
    listing="$(GNUPGHOME="$gnupg_home" gpg --batch --no-autostart --show-keys --with-colons "$keyfile" 2>/dev/null)" || rc=$?
    rm -rf "$gnupg_home" 2>/dev/null || true
    if [ "$rc" -ne 0 ]; then
        echo "  WARNING: gpg could not read the downloaded signing key (rc=$rc) — repo NOT added."
        echo "           Either the download is not a key, or gpg predates 2.2.8 (no --show-keys)."
        return 1
    fi
    rc=0
    parsed="$(printf '%s\n' "$listing" | awk -F: '
        $1 == "pub" || $1 == "sec" { n++; want = 1; next }
        want && $1 == "fpr" { if (n == 1) f = $10; want = 0; next }
        { want = 0 }
        END { printf "%d %s", n, f }')" || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "  WARNING: could not parse the signing key listing (rc=$rc) — repo NOT added."
        return 1
    fi
    n="${parsed%% *}"
    fpr="${parsed#* }"
    if [ "$n" != "1" ]; then
        echo "  WARNING: the downloaded signing key file holds $n keys, expected exactly 1 — repo NOT added."
        return 1
    fi
    if [ "$fpr" != "$FALKORDB_REDIS_KEY_FPR" ]; then
        echo "  WARNING: the downloaded signing key's fingerprint (${fpr:-none}) does not match"
        echo "           the pinned $FALKORDB_REDIS_KEY_FPR — repo NOT added."
        echo "           If Redis rotated its key, verify the new one and update the pin."
        return 1
    fi
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
    local codename rc keytmp stamp added_repo
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
        # Distinguish "we provisioned this on an earlier run", "we started but
        # never finished", and "the operator already had redis". Without this
        # split, every re-run on a box we provisioned would print an
        # operator-decision message about a decision that was already made —
        # misleading, and the kind of message that trains people to ignore
        # output.
        #
        # Ownership is decided by the stamp alone, BEFORE the completion
        # marker is consulted: the marker records that an install of ours once
        # finished, not which redis is on the box now. Read first, a purged or
        # replaced package would be reported as provisioned and the operator
        # would never see the >= $FALKORDB_MIN_REDIS warning below.
        stamp=""
        if [ -f "$FALKORDB_APT_LIST" ]; then
            stamp="$(_falkordb_redis_pkg_stamp || true)"
            # Ours only if the apt list we wrote carries a stamp line matching
            # THIS package's fingerprint (grep -xF: literal whole-line match).
            # An operator-authored list has no stamp; a stale stamp names a
            # package that is no longer installed. Both read as "not ours".
            if [ -z "$stamp" ] || \
               ! grep -qxF "# genesis-install-stamp: $stamp" "$FALKORDB_APT_LIST" 2>/dev/null; then
                stamp=""
            fi
        fi
        if [ -n "$stamp" ] && [ -f "$FALKORDB_PROVISION_MARKER" ]; then
            echo "  OK: redis-server already provisioned."
        elif [ -n "$stamp" ]; then
            # Ours, unfinished, and the fingerprint still matches THIS package:
            # retry JUST the stand-down. The operator-decision message below is
            # for a redis that is not ours, and printing it here would abandon
            # our own daemon on :6379.
            if _falkordb_stand_down_system_redis; then
                mkdir -p "$(dirname "$FALKORDB_PROVISION_MARKER")" 2>/dev/null || true
                date -u +%Y-%m-%dT%H:%M:%SZ > "$FALKORDB_PROVISION_MARKER" 2>/dev/null || true
                echo "  OK: redis-server provisioned (completed pending stand-down)."
            else
                echo "  WARNING: redis-server is ours, but its system unit is still"
                echo "           enabled on :6379. Stand it down by hand:"
                echo "           sudo systemctl disable --now redis-server"
            fi
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
        echo "           sudo bash -c 'GENESIS_FALKORDB_PROVISION=1; source scripts/lib/falkordb_install.sh && falkordb_redis_install'"
        return 0
    fi

    # Which suite does packages.redis.io serve for THIS box? Two cases:
    #   - UBUNTU_CODENAME (set by Ubuntu derivatives like Mint): the operator's
    #     own VERSION_CODENAME is their release name — Mint's 'wilma' has no
    #     redis suite — but the derivative declares the Ubuntu base it tracks,
    #     which does. Always prefer it.
    #   - otherwise require ID itself to be a distro the repo publishes suites
    #     for (ubuntu, debian). Any other codename would write an apt source
    #     that fails update forever — worse than declining to provision.
    codename=""
    os_id=""
    if [ -r "$FALKORDB_OS_RELEASE" ]; then
        # Guarded: a bare assignment carries the substitution's status, and an
        # os-release that fails to source would abort bootstrap under set -e —
        # against this lib's never-abort contract.
        rc=0
        # shellcheck disable=SC1090
        codename="$(. "$FALKORDB_OS_RELEASE" && printf '%s' "${UBUNTU_CODENAME:-}")" || rc=$?
        [ "$rc" -eq 0 ] || codename=""
        if [ -z "$codename" ]; then
            rc=0
            # shellcheck disable=SC1090
            os_id="$(. "$FALKORDB_OS_RELEASE" && printf '%s' "${ID:-}")" || rc=$?
            case "$os_id" in
                ubuntu|debian)
                    rc=0
                    # shellcheck disable=SC1090
                    codename="$(. "$FALKORDB_OS_RELEASE" && printf '%s' "${VERSION_CODENAME:-}")" || rc=$?
                    [ "$rc" -eq 0 ] || codename=""
                    ;;
            esac
        fi
    fi
    if [ -z "$codename" ]; then
        echo "  Skipped: no published redis apt suite for this system (${os_id:-unknown};"
        echo "           the repo serves ubuntu/debian and derivatives that declare"
        echo "           UBUNTU_CODENAME). Cannot pick an apt suite to provision."
        return 0
    fi

    # The repo is added only onto a clean slate — no list AND no keyring at our
    # paths — so everything this block writes is this run's, and every failure
    # before an install has been attempted removes it again (_falkordb_repo_undo):
    # the machine is left as it was found. A keyring already there belongs to
    # someone else and is neither overwritten nor removed.
    added_repo=0
    if [ ! -f "$FALKORDB_APT_LIST" ]; then
        if [ -e "$FALKORDB_APT_KEYRING" ]; then
            echo "  Skipped: $FALKORDB_APT_KEYRING already exists and is not ours —"
            echo "           leaving it and the apt sources alone. See SETUP.md to add the repo by hand."
            return 0
        fi
        if ! command -v curl >/dev/null 2>&1 || ! command -v gpg >/dev/null 2>&1; then
            echo "  Skipped: curl and gpg are both required to add the redis apt repo."
            return 0
        fi
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
        # Pin the repository signing key, BEFORE anything is written: a key
        # that is not the pinned one never becomes a keyring.
        if ! _falkordb_signing_key_ok "$keytmp"; then
            rm -f "$keytmp" 2>/dev/null || true
            return 0
        fi
        # The first system change. Recorded so the undo removes the directory
        # only when this run created it.
        _falkordb_made_keydir=0
        if [ ! -d "$(dirname "$FALKORDB_APT_KEYRING")" ]; then
            sudo mkdir -p "$(dirname "$FALKORDB_APT_KEYRING")" 2>/dev/null && _falkordb_made_keydir=1
        fi
        rc=0
        sudo gpg --yes --dearmor -o "$FALKORDB_APT_KEYRING" "$keytmp" 2>/dev/null || rc=$?
        rm -f "$keytmp" 2>/dev/null || true
        if [ "$rc" -ne 0 ]; then
            _falkordb_repo_undo
            echo "  WARNING: could not install the redis signing key (rc=$rc) — repo NOT added."
            return 0
        fi
        rc=0
        printf 'deb [signed-by=%s] %s %s main\n' "$FALKORDB_APT_KEYRING" "$FALKORDB_REPO_URL" "$codename" \
            | sudo tee "$FALKORDB_APT_LIST" >/dev/null 2>&1 || rc=$?
        if [ "$rc" -ne 0 ]; then
            _falkordb_repo_undo
            echo "  WARNING: could not write $FALKORDB_APT_LIST — repo NOT added."
            return 0
        fi
        echo "  Added: redis apt repo ($codename)"
        rc=0
        sudo apt-get update -qq >/dev/null 2>&1 || rc=$?
        if [ "$rc" -ne 0 ]; then
            # A source that cannot update is worse than no source: apt consults
            # every list on EVERY operation. And STOP here — a cached candidate
            # may still look installable, but installing would leave a package
            # whose source was just removed.
            _falkordb_repo_undo
            echo "  WARNING: apt-get update failed (rc=$rc) — removed the redis apt"
            echo "           source this run added; nothing installed."
            return 0
        fi
        added_repo=1
    fi

    # The candidate is verified immediately before EVERY install, not just when
    # this run added the repo: a re-run where the list file already exists runs
    # no update at all, and a stale index's only offer is the distro's 7.x,
    # which the module refuses to load on.
    local candidate
    rc=0
    # `|| rc=$?`: a bare assignment carries the pipeline's status, and this lib
    # is sourced under `set -euo pipefail` — an apt-cache failure would abort
    # the whole bootstrap instead of declining an optional provisioning step.
    candidate="$(apt-cache policy redis-server 2>/dev/null \
        | awk '/Candidate:/ {print $2}')" || rc=$?
    if [ "$rc" -ne 0 ]; then
        [ "$added_repo" -eq 0 ] || _falkordb_repo_undo
        echo "  Skipped: could not ask apt which redis-server it would install (rc=$rc)."
        echo "           The graph engine needs >= $FALKORDB_MIN_REDIS; unverifiable means not installed."
        return 0
    fi
    # The epoch is stripped: it is packaging, not the redis version, and would
    # lift a `5:7.0.15` over a floor that has none.
    if ! _falkordb_meets_floor "${candidate#*:}"; then
        echo "  Skipped: apt would install redis-server '${candidate:-unknown}', below the"
        echo "           $FALKORDB_MIN_REDIS floor the module enforces — nothing installed."
        if [ "$added_repo" -eq 1 ]; then
            _falkordb_repo_undo
            echo "           Removed the redis apt source this run added."
        else
            echo "           Re-run after 'sudo apt-get update' succeeds."
        fi
        return 0
    fi

    # From here on the source stays even if the install fails: a package from
    # it may now be on the box in some state, and removing the source would cut
    # that package off from its updates.
    rc=0
    sudo apt-get install -y -qq redis-server >/dev/null 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "  WARNING: redis-server install failed (rc=$rc) — the graph engine cannot start."
        return 0
    fi

    # Record WE installed THIS package BEFORE attempting the stand-down: a
    # stamp comment appended to the apt list, not a marker under HOME — the
    # list is system-side and written with the same sudo the install needed,
    # so provenance cannot silently die on an unwritable home directory. No
    # fingerprint, no claim: an unfingerprintable install must not leave
    # ownership state a later run could misread.
    stamp="$(_falkordb_redis_pkg_stamp || true)"
    if [ -n "$stamp" ]; then
        printf '# genesis-install-stamp: %s\n' "$stamp" \
            | sudo tee -a "$FALKORDB_APT_LIST" >/dev/null 2>&1 || {
            echo "  WARNING: could not record provisioning provenance — if the"
            echo "           stand-down below fails, no later run can retry it."
        }
    fi

    # The deb enables a SYSTEM redis on 6379. Genesis does not want it: our unit
    # is a per-user instance with --port 0 that speaks only over a unix socket.
    # Standing that unit down is part of provisioning, not cosmetic: if it
    # fails, a second redis keeps running on :6379 across reboots, so failure
    # must never read as the socket-only posture.
    if _falkordb_stand_down_system_redis; then
        # The completion marker records a FINISHED provisioning — including the
        # verified stand-down. Written only here so a failed disable is retried
        # on the next run rather than replayed as 'already provisioned'. The
        # parent is created first: on a fresh install nothing else has made
        # FALKORDB_DEPS_DIR yet, and a dropped write reads as 'incomplete'
        # forever — the NEXT bootstrap would stand down the unit again.
        mkdir -p "$(dirname "$FALKORDB_PROVISION_MARKER")" 2>/dev/null || true
        date -u +%Y-%m-%dT%H:%M:%SZ > "$FALKORDB_PROVISION_MARKER" 2>/dev/null || true
        echo "  Installed: redis-server (system unit disabled — Genesis uses a socket-only user unit)"
    else
        echo "  WARNING: redis-server installed, but its system unit could not be"
        echo "           stood down — the package's system redis is still enabled on"
        echo "           :6379. Stand it down by hand:"
        echo "           sudo systemctl disable --now redis-server"
    fi
    return 0
}

# _falkordb_stand_down_system_redis — disable AND verify the package's system
# unit, in one step both call sites share: the fresh-install path and the
# retry path for an incomplete earlier provisioning. rc 0 only when the unit
# is disabled for real; every failure is the caller's to report.
_falkordb_stand_down_system_redis() {
    local rc
    rc=0
    sudo systemctl disable --now redis-server >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 0 ] || return 1
    rc=0
    sudo systemctl is-enabled --quiet redis-server >/dev/null 2>&1 || rc=$?
    [ "$rc" -ne 0 ]
}

# falkordb_provision — the single entry point bootstrap calls.
falkordb_provision() {
    local arch
    if [ "$FALKORDB_PROVISION_DISABLED" = "1" ]; then
        echo "  Skipped: GENESIS_FALKORDB_PROVISION_DISABLED=1."
        return 0
    fi
    mkdir -p "$FALKORDB_DATA_DIR" 2>/dev/null || true
    # Preflight before touching anything: the module half refuses any
    # version/arch pair with no pinned digest, so a box that can never install
    # the module (an architecture we ship no asset for, or an unpinned pair
    # like 4.20.4/arm64v8 while only x64 is pinned) has nothing to provision.
    arch="$(_falkordb_arch)"
    if [ -z "$arch" ]; then
        echo "  Skipped: no FalkorDB module ships for $(uname -m 2>/dev/null || echo 'this architecture') —"
        echo "           nothing provisioned."
        return 0
    fi
    if [ -z "$(_falkordb_expected_sha "$FALKORDB_VERSION" "$arch")" ]; then
        echo "  Skipped: no pinned checksum for FalkorDB $FALKORDB_VERSION/$arch —"
        echo "           the module cannot be verified, so no system changes were made."
        echo "           To adopt this build, pin its digest in _falkordb_expected_sha."
        return 0
    fi
    # The MODULE first, and the system half only once it verifies: the module
    # is the reason redis is wanted, so a failed download, digest, or chmod
    # must not leave an apt repo and a daemon behind for an engine that cannot
    # start. A redis that is already on the box is the exception — that path
    # installs nothing, and it is where an unfinished stand-down of OUR
    # system unit gets retried, which a broken module must not postpone.
    falkordb_module_install
    if _falkordb_module_verified || _falkordb_redis_present; then
        falkordb_redis_install
    else
        echo "  Skipped: graph-engine server — the module is not installed and verified,"
        echo "           so no system changes were made. Re-run once it installs."
    fi
    return 0
}
