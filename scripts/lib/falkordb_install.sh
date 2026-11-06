# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# falkordb_provision — graph-engine provisioning for the memory graph.
#
# Genesis's memory graph is an in-process NetworkX projection of memory_links.
# The graph-DB adoption (issue #1641) replaces that projection with FalkorDB, a
# Redis module: SQLite stays the system of record and the engine is a derived,
# rebuildable index. The WHOLE engine (redis-server, redis-cli, the module and
# the shared libraries the module links against) ships as one pip wheel,
# `falkordblite`, pinned by version and sha256 in falkordb-requirements.txt.
# This lib installs that wheel into the venv BEFORE the editable install (so the
# editable install finds it satisfied rather than fetching it unverified),
# verifies the installed files against digests COMMITTED in this repo, and
# copies them to a version-scoped directory the unit runs from:
# ~/.genesis/deps/falkordb/<version>-<wheel tag>/. It changes nothing about the
# system and arms nothing: the unit is rendered by the template loops in
# bootstrap.sh/install.sh and left DISABLED.
#
# The wheels are manylinux_2_38 for x86_64 and aarch64: glibc 2.38 or newer on
# one of those two machines is the supported OS floor (Ubuntu 24.04). Below it
# the installers refuse, before installing anything.
#
# Integrity, end to end:
#   - pip --require-hashes refuses a wheel file that is not one of the six
#     pinned digests, and installs nothing. It does NOT re-verify a wheel that
#     is already installed ("Requirement already satisfied"), and the venv can
#     also be written by unhashed installs, so the installed files are not
#     trusted on pip's word;
#   - falkordb_engine_check.py compares every engine file in the venv with
#     falkordb-engine-digests.txt (generated from the pinned wheel files, never
#     the installed wheel's RECORD), copies them to the version-tag directory,
#     re-verifies the copy and writes its engine.sha256 there;
#   - the unit's ExecStartPre runs `sha256sum -c` on that file at EVERY start,
#     so bytes changed after provisioning fail the start before redis runs.
#     That catches DRIFT; it is not a barrier against the engine itself: the
#     unit's sandbox is best-effort under a user manager (#2824, and MEASURED on
#     an install where it is skipped), so the integrity check that holds is the
#     re-verify against the committed digests at every provision.
#   - The directory name carries the version and wheel tag, so a pin bump lands
#     beside the running engine's files instead of on top of them.
#
# Degrades gracefully everywhere: an OS below the floor, a missing venv, a
# failed pip run or a failed check each produce a short note and rc=0. Like the
# other lib fragments, nothing here aborts install.sh/bootstrap.sh/update.sh
# under `set -e`; the callers decide, and they stop on two results only: the OS
# floor, and FALKORDB_PIP_REFUSED (the hashed wheel did not install).
#
# Test seams (defaults are the real paths; the test harness overrides these and
# stubs tools on PATH):
_FALKORDB_LIB_DIR="$(unset CDPATH; cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Where the engine copies live: one directory per <version>-<wheel tag>.
FALKORDB_DEPS_DIR="${FALKORDB_DEPS_DIR:-$HOME/.genesis/deps/falkordb}"
FALKORDB_REQUIREMENTS="${FALKORDB_REQUIREMENTS:-$_FALKORDB_LIB_DIR/falkordb-requirements.txt}"
FALKORDB_DIGESTS_FILE="${FALKORDB_DIGESTS_FILE:-$_FALKORDB_LIB_DIR/falkordb-engine-digests.txt}"
FALKORDB_ENGINE_CHECK="${FALKORDB_ENGINE_CHECK:-$_FALKORDB_LIB_DIR/falkordb_engine_check.py}"
# The glibc floor the wheels are built for (manylinux_2_38).
FALKORDB_GLIBC_FLOOR="2.38"
# The machines the wheels are built for. A seam so tests can play another arch.
FALKORDB_UNAME="${FALKORDB_UNAME:-uname}"
# The systemctl the active-engine rule asks. A seam so tests can stub the
# engine's state without a user manager.
FALKORDB_SYSTEMCTL="${FALKORDB_SYSTEMCTL:-systemctl}"

# GENESIS_FALKORDB_PROVISION_DISABLED=1 turns the whole thing off, matching
# the kill-switch convention the other subsystems use.
FALKORDB_PROVISION_DISABLED="${GENESIS_FALKORDB_PROVISION_DISABLED:-}"
# GENESIS_FALKORDB_SWITCH=1 is the explicit step that moves an ACTIVE engine's
# unit onto the wheel's binaries. Without it a deploy never switches a running
# engine (see falkordb_unit_keep_existing).
FALKORDB_SWITCH="${GENESIS_FALKORDB_SWITCH:-}"

# Results of the last falkordb_provision in this shell. Reset on every source:
# a value inherited from the environment is not a verification this run made.
FALKORDB_ENGINE_OK=""
FALKORDB_ENGINE_DIR=""
FALKORDB_REDIS_SERVER=""
FALKORDB_REDIS_CLI=""
FALKORDB_MODULE=""
FALKORDB_REPLACED=""
FALKORDB_DIGESTS=""
FALKORDB_PIP_REFUSED=""

# _falkordb_glibc_ok — rc 0 when this machine's glibc meets the wheels' floor.
# `getconf GNU_LIBC_VERSION` prints "glibc 2.39"; a non-glibc libc prints
# nothing usable, and that is below the floor too (the wheels are glibc-only).
_falkordb_glibc_ok() {
    local out ver major minor floor_major floor_minor
    out="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    ver="${out##* }"
    case "$ver" in
        [0-9]*.[0-9]*) ;;
        *) return 1 ;;
    esac
    major="${ver%%.*}"
    minor="${ver#*.}"
    minor="${minor%%[!0-9]*}"
    floor_major="${FALKORDB_GLIBC_FLOOR%%.*}"
    floor_minor="${FALKORDB_GLIBC_FLOOR#*.}"
    case "$major$minor" in *[!0-9]*|"") return 1 ;; esac
    [ "$major" -gt "$floor_major" ] && return 0
    [ "$major" -eq "$floor_major" ] && [ "$minor" -ge "$floor_minor" ]
}

# _falkordb_arch_ok — rc 0 on a machine the wheels are built for.
_falkordb_arch_ok() {
    case "$("$FALKORDB_UNAME" -m 2>/dev/null || true)" in
        x86_64|aarch64) return 0 ;;
    esac
    return 1
}

# falkordb_os_floor_ok — rc 0 when this machine meets Genesis's supported OS
# floor: glibc 2.38 or newer AND an x86_64 or aarch64 machine. The installers
# call it BEFORE installing anything, and refuse otherwise (owner ruling):
# pyproject.toml declares the same wheel, which is built for nothing else, so
# off the floor the editable install itself fails, after pip falls back to
# compiling the engine from its source distribution.
falkordb_os_floor_ok() { _falkordb_glibc_ok && _falkordb_arch_ok; }

# falkordb_os_floor_message — the one line an installer prints when it
# refuses, naming what failed (the glibc, the machine, or both).
falkordb_os_floor_message() {
    local have arch reasons=""
    if ! _falkordb_glibc_ok; then
        have="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
        reasons="this OS has ${have:-no glibc}; Genesis needs glibc $FALKORDB_GLIBC_FLOOR or newer (Ubuntu 24.04+)"
    fi
    if ! _falkordb_arch_ok; then
        arch="$("$FALKORDB_UNAME" -m 2>/dev/null || true)"
        [ -z "$reasons" ] || reasons="$reasons, and "
        reasons="${reasons}this machine is ${arch:-of an unknown architecture}; Genesis needs x86_64 or aarch64"
    fi
    printf '%s, because its graph engine ships as wheels built only for those' "$reasons"
}

# _falkordb_unit_word <path> — a resolved path ready to sit inside the
# template's double quotes, or "" when the unit cannot carry it.
#
# systemd's rules for that word, per systemd.service(5) "Command Lines" and
# systemd.syntax(7) "Quoting", and MEASURED with `systemd-analyze --user
# verify` (systemd 255):
#   - whitespace splits an unquoted item, so the template quotes it;
#   - "%" starts a specifier (systemd.unit(5)) even inside quotes — a path
#     with `p%c` resolved to `p/<unit name>` — so it is doubled here;
#   - `"`, `\` and control characters make the unit FATALLY bad ("Executable
#     name contains special characters"), escaped or not, so such a path is
#     refused, as is a relative one (systemd does no PATH lookup);
#   - `$` is refused too: systemd substitutes `${VAR}` and `$VAR` from the
#     unit's environment inside arguments (MEASURED), so a path holding one
#     would reach redis as some other path. The engine check refuses the same
#     set, so a copy is never placed where the unit could not name it. In the
#     readiness ExecStartPost the redis-cli path is a separate argument handed
#     to the shell as "$1", never spliced into the script.
_falkordb_unit_word() {
    local path="$1"
    case "$path" in /*) ;; *) return 0 ;; esac
    case "$path" in *[[:cntrl:]\\\"\$]*) return 0 ;; esac
    printf '%s' "${path//%/%%}"
}

# The four placeholders' values, from the last verified provision. Empty when
# the engine is not verified, which the render never reaches: the loops consult
# falkordb_unit_keep_existing first and skip the unit then.
_falkordb_redis_server_bin() { _falkordb_unit_word "$FALKORDB_REDIS_SERVER"; }
_falkordb_redis_cli_bin() { _falkordb_unit_word "$FALKORDB_REDIS_CLI"; }
_falkordb_module_path() { _falkordb_unit_word "$FALKORDB_MODULE"; }
_falkordb_digests_path() { _falkordb_unit_word "$FALKORDB_DIGESTS"; }

# _falkordb_engine_verified — rc 0 when this shell's last provision verified
# the engine and every path it resolved is one the unit can carry.
_falkordb_engine_verified() {
    [ "$FALKORDB_ENGINE_OK" = "1" ] || return 1
    [ -n "$(_falkordb_redis_server_bin)" ] \
        && [ -n "$(_falkordb_redis_cli_bin)" ] \
        && [ -n "$(_falkordb_module_path)" ] \
        && [ -n "$(_falkordb_digests_path)" ]
}

# falkordb_unit_loaded_module <rendered unit path> — the module path the unit's
# --loadmodule names, with %h expanded to $HOME and %% read back as %, or ""
# when it names none. The unit is rendered from our own template, which quotes
# that one argument.
falkordb_unit_loaded_module() {
    local line path
    line="$(grep -o -- '--loadmodule "[^"]*"' "$1" 2>/dev/null | head -n 1 || true)"
    path="${line#--loadmodule \"}"
    path="${path%\"}"
    [ -n "$line" ] || return 0
    case "$path" in "%h"/*) path="$HOME${path#%h}" ;; esac
    printf '%s' "${path//%%/%}"
}

# falkordb_unit_keep_existing <rendered unit path> — rc 0 when the renderer must
# NOT render the unit; FALKORDB_KEEP_REASON names why. rc 1 means render.
#
# Keep when:
#   - the engine is not verified this run (OS below the floor, pip or the check
#     failed, or provisioning was disabled). There are no verified paths to
#     render with, and an existing unit is better left on what it runs today
#     than pointed at binaries nothing vouched for. With no unit yet, nothing
#     is written: an unrendered unit and an inert one serve the same box.
#   - the unit exists, the engine is ACTIVE, the unit loads a different module
#     than the wheel's, and GENESIS_FALKORDB_SWITCH is not 1. A deploy must not
#     move a running engine onto new binaries (owner ruling): the rewrite would
#     land at its next restart or reboot, unannounced. Switching is the
#     explicit step `GENESIS_FALKORDB_SWITCH=1 scripts/bootstrap.sh`, followed
#     by a restart of the engine.
# Otherwise render: there is no unit yet, the engine is not running, it already
# loads the wheel's module, or the switch was asked for.
falkordb_unit_keep_existing() {
    local current wheel state
    FALKORDB_KEEP_REASON=""
    if ! _falkordb_engine_verified; then
        FALKORDB_KEEP_REASON="unverified"
        return 0
    fi
    [ -f "$1" ] || return 1
    [ "$FALKORDB_SWITCH" = "1" ] && return 1
    current="$(falkordb_unit_loaded_module "$1")"
    wheel="$FALKORDB_MODULE"
    [ "$current" != "$wheel" ] || return 1
    # Render only on a state that PROVES the engine is not running. `is-active`
    # prints nothing (rc 1) when it cannot reach the user manager, and
    # `activating` during the RestartSec window after a crash (MEASURED,
    # systemd 255), and either one, read as "not running", would rewrite the
    # unit under a live engine, so the binaries would switch at its next restart.
    state="$($FALKORDB_SYSTEMCTL --user is-active "${1##*/}" 2>/dev/null || true)"
    case "$state" in
        inactive|failed) return 1 ;;
    esac
    # shellcheck disable=SC2034  # read by the renderers' "Kept:" message
    FALKORDB_KEEP_REASON="active"
    # shellcheck disable=SC2034  # read by the renderers' "Kept:" message
    [ -n "$state" ] || FALKORDB_KEEP_REASON="state_unknown"
    return 0
}

# _falkordb_read_check_output <text> — take the KEY=value lines the engine
# check printed. Read line by line, never eval'd: the values are paths.
_falkordb_read_check_output() {
    local line
    while IFS= read -r line; do
        case "$line" in
            FALKORDB_ENGINE_DIR=*) FALKORDB_ENGINE_DIR="${line#*=}" ;;
            FALKORDB_REDIS_SERVER=*) FALKORDB_REDIS_SERVER="${line#*=}" ;;
            FALKORDB_REDIS_CLI=*) FALKORDB_REDIS_CLI="${line#*=}" ;;
            FALKORDB_MODULE=*) FALKORDB_MODULE="${line#*=}" ;;
            FALKORDB_REPLACED=*) FALKORDB_REPLACED="${line#*=}" ;;
            FALKORDB_DIGESTS=*) FALKORDB_DIGESTS="${line#*=}" ;;
        esac
    done <<< "$1"
}

# _falkordb_clear_results — forget the last provision's verdict and paths.
_falkordb_clear_results() {
    FALKORDB_ENGINE_OK=""
    FALKORDB_ENGINE_DIR=""
    FALKORDB_REDIS_SERVER=""
    FALKORDB_REDIS_CLI=""
    FALKORDB_MODULE=""
    FALKORDB_REPLACED=""
    FALKORDB_DIGESTS=""
    FALKORDB_PIP_REFUSED=""
    export FALKORDB_ENGINE_OK FALKORDB_ENGINE_DIR FALKORDB_REDIS_SERVER \
        FALKORDB_REDIS_CLI FALKORDB_MODULE FALKORDB_DIGESTS
}

# falkordb_provision <venv dir> — the single entry point the installers call,
# before the editable install. Installs the hash-pinned wheel, verifies it
# against the committed digests, places the version-tag copy the unit runs, and
# exports its paths for the render step. Always rc 0. When the hash-pinned pip
# run itself fails it sets FALKORDB_PIP_REFUSED=1: the editable install that
# follows would fetch the same mandatory wheel with no hash check, so the
# installers must not run it (bootstrap stops; install.sh skips it and warns).
falkordb_provision() {
    local venv="${1:-}" py out rc
    _falkordb_clear_results

    if [ "$FALKORDB_PROVISION_DISABLED" = "1" ]; then
        echo "  Skipped: GENESIS_FALKORDB_PROVISION_DISABLED=1."
        return 0
    fi
    if ! falkordb_os_floor_ok; then
        echo "  Skipped: this install's OS is below the supported floor (Ubuntu 24.04 / glibc $FALKORDB_GLIBC_FLOOR on x86_64 or aarch64); the graph engine cannot be installed."
        return 0
    fi
    py="$venv/bin/python"
    if [ -z "$venv" ] || [ ! -x "$py" ]; then
        echo "  WARNING: no venv python at ${venv:-<unset>}/bin/python — graph engine NOT installed."
        return 0
    fi
    if [ ! -f "$FALKORDB_REQUIREMENTS" ] || [ ! -f "$FALKORDB_DIGESTS_FILE" ] \
        || [ ! -f "$FALKORDB_ENGINE_CHECK" ]; then
        echo "  WARNING: $FALKORDB_REQUIREMENTS, $FALKORDB_DIGESTS_FILE or $FALKORDB_ENGINE_CHECK missing — graph engine NOT installed."
        return 0
    fi

    rc=0
    out="$("$py" -m pip install --quiet --disable-pip-version-check \
        --only-binary=:all: --no-deps --require-hashes \
        -r "$FALKORDB_REQUIREMENTS" 2>&1)" || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "  WARNING: graph engine wheel did not install (pip rc=$rc) — graph engine NOT installed."
        [ -z "$out" ] || printf '%s\n' "$out" | tail -n 3 | sed 's/^/           /' || true
        # shellcheck disable=SC2034  # read by bootstrap.sh and install.sh
        FALKORDB_PIP_REFUSED=1
        return 0
    fi

    rc=0
    out="$("$py" -I "$FALKORDB_ENGINE_CHECK" \
        --requirements "$FALKORDB_REQUIREMENTS" \
        --digests "$FALKORDB_DIGESTS_FILE" \
        --deps-root "$FALKORDB_DEPS_DIR" 2>&1)" || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "  WARNING: graph engine NOT verified — ${out:-the engine check failed (rc=$rc)}"
        return 0
    fi
    _falkordb_read_check_output "$out"
    if [ -z "$FALKORDB_ENGINE_DIR" ] || [ -z "$FALKORDB_REDIS_SERVER" ] \
        || [ -z "$FALKORDB_REDIS_CLI" ] || [ -z "$FALKORDB_MODULE" ] \
        || [ -z "$FALKORDB_DIGESTS" ]; then
        _falkordb_clear_results
        echo "  WARNING: graph engine check printed no engine paths — graph engine NOT verified."
        return 0
    fi
    FALKORDB_ENGINE_OK=1
    echo "  OK: graph engine verified ($FALKORDB_ENGINE_DIR)"
    if [ "$FALKORDB_REPLACED" = "1" ]; then
        echo "  WARNING: the graph engine copy at $FALKORDB_ENGINE_DIR did not match the committed digests, or another account could write it, and was rebuilt."
        echo "           A running genesis-falkordb still runs the old bytes: restart it (systemctl --user restart genesis-falkordb)."
    fi
    return 0
}

# falkordb_prune_engines <rendered unit path> — remove engine copies no unit
# needs. Under FALKORDB_DEPS_DIR every direct child DIRECTORY is removed except
#   - the copy this run verified (FALKORDB_ENGINE_DIR), when it verified one;
#   - the top-level directory the unit's --loadmodule resolves into, which a
#     kept unit (an active engine, a failed provision) still runs. This is
#     also what retires the legacy <deps>/4.20.4/ once no unit loads it.
# Never prunes when a drop-in (<unit>.d/*.conf) exists, which can replace
# ExecStart, or when the unit file exists but its module cannot be read: what
# it runs is unknown, so everything stays. Skips files, symlinks, dotted
# names (an in-flight copy belongs to the engine check), and any name holding
# `/` or `..`; never touches anything outside FALKORDB_DEPS_DIR. Always rc 0.
falkordb_prune_engines() {
    local unit="${1:-}" root loaded="" keep_loaded="" keep_current="" entry name
    root="${FALKORDB_DEPS_DIR%/}"
    [ -n "$root" ] && [ -d "$root" ] && [ ! -L "$root" ] || return 0
    # The run that SWITCHES a unit has just pointed it at the new copy, and the
    # engine is still running the old one until its restart. Keep the old copy
    # for this run, so if the new engine fails on that restart the old copy is
    # still on disk to point the unit back at (by hand: nothing re-renders it
    # automatically); the next ordinary run prunes it.
    [ "$FALKORDB_SWITCH" = "1" ] && return 0
    # Compare RESOLVED paths: the unit was rendered from the check's normalized
    # paths, and this run's FALKORDB_DEPS_DIR may be spelled differently ($HOME
    # with a trailing slash, //, a symlinked home), and a raw string compare
    # would then miss the copy the unit runs and delete it.
    root="$(realpath -m -- "$root" 2>/dev/null)" || return 0
    # A drop-in (<unit>.d/*.conf, the supported place for local changes; see
    # managed_units.sh) can replace ExecStart, and then the base unit no longer
    # says what runs: prune nothing.
    if [ -n "$unit" ] && compgen -G "${unit}.d/*.conf" >/dev/null 2>&1; then
        return 0
    fi
    if [ -n "$unit" ] && [ -e "$unit" ]; then
        loaded="$(falkordb_unit_loaded_module "$unit" 2>/dev/null || true)"
        [ -n "$loaded" ] || return 0
        loaded="$(realpath -m -- "$loaded" 2>/dev/null)" || return 0
        case "$loaded" in
            "$root"/*)
                keep_loaded="${loaded#"$root"/}"
                keep_loaded="${keep_loaded%%/*}"
                ;;
            # A unit loading something outside the deps dir is one this rule
            # cannot reason about: prune nothing.
            *) return 0 ;;
        esac
    fi
    if _falkordb_engine_verified; then
        local current
        current="$(realpath -m -- "$FALKORDB_ENGINE_DIR" 2>/dev/null)" || return 0
        case "$current" in
            "$root"/*) keep_current="${current#"$root"/}" ;;
        esac
        # A verified copy outside the deps dir, or nested, is not a name this
        # loop could match: prune nothing rather than guess what it is.
        case "$keep_current" in ""|*/*) return 0 ;; esac
    fi
    for entry in "$root"/*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        name="${entry##*/}"
        case "$name" in ""|.*|*/*|*..*) continue ;; esac
        [ -L "$entry" ] && continue
        [ -d "$entry" ] || continue
        [ "$name" = "$keep_current" ] && continue
        [ "$name" = "$keep_loaded" ] && continue
        if rm -rf -- "$entry" 2>/dev/null; then
            echo "  Removed graph engine copy no unit runs: $name"
        else
            echo "  WARNING: could not remove $entry"
        fi
    done
    return 0
}
