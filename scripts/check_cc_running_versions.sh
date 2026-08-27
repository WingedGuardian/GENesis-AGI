#!/usr/bin/env bash
# shellcheck shell=bash
#
# Running-binary sweep — report which LIVE Claude Code processes are actually
# executing the binary currently on disk, and which are still running a copy
# that has since been replaced.
#
# Why this exists (a real incident, 2026-08-27): the container was aligned to a
# candidate CC version and a 2-3 day local-first soak was declared started.
# Measured at the end of it, THREE of four interactive sessions were still
# executing the pre-align binary — npm had replaced the package underneath
# them, and a long-lived process keeps its original mapping until it restarts.
# `claude --version` did not reveal this: it spawns a FRESH child, which reads
# the new on-disk binary and truthfully reports the candidate while the session
# asking the question is not running it. So the soak had accumulated days of
# "real use" on the OLD release for most of the box.
#
# This is NOT what `cc_shadow_scan` covers. That function scans on-disk COPIES
# (`command -v claude` plus CC_PROBE_DIRS) and removes stale ones. A box can
# pass it cleanly — exactly one canonical binary, at the intended version —
# while several live sessions still execute a deleted predecessor. The two
# checks are complementary: shadow_scan asks "what is installed here?", this
# asks "what is actually RUNNING here?". Neither implies the other.
#
# Method: for each process, `stat -L /proc/<pid>/exe` resolves the inode of the
# executable that process is running — and procfs keeps that reference alive
# even after the file is unlinked, so a replaced binary still resolves. That
# inode is compared against the inode of the binary `command -v claude`
# resolves to today. Equal = running current; different = stale.
#
#   NOTE: `stat` WITHOUT `-L` returns the procfs inode of the magic symlink
#   itself (a number in the hundreds-of-millions here), not the target's. Using
#   it would make every process compare unequal and report a false box-wide
#   staleness. The `-L` is load-bearing.
#
# Node-wrapped installs: where CC runs as `node .../cli.js` rather than a native
# `claude.exe`, /proc/<pid>/exe resolves to the Node interpreter, whose inode
# says nothing about which CC revision is loaded. Those are reported UNDETERMINED
# and the script exits non-zero. It refuses to answer rather than answering
# wrongly — a false "all current" is precisely the failure this exists to catch.
#
# Exit codes:
#     0 — every live CC process is running the on-disk binary (or none are running)
#     1 — at least one process is running a DIFFERENT (stale/replaced) binary
#     2 — cannot determine: no `claude` on PATH, or an undetermined process
#         (node-wrapped) with no outright stale one
#
# Usage:  scripts/check_cc_running_versions.sh [--quiet]

set -u
set -o pipefail

QUIET=0
PROC_ROOT="/proc"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --quiet) QUIET=1 ;;
        --proc-root)
            # Scan an alternative procfs root. Mirrors --pin-file in
            # check_cc_node_lockstep.py: a normal CLI affordance that also lets
            # the tests exercise the OK / STALE / UNDETERMINED branches against
            # a fixture tree, instead of only ever seeing whichever branch this
            # host happens to be in.
            shift
            [ "$#" -gt 0 ] || { echo "--proc-root requires a path" >&2; exit 2; }
            PROC_ROOT="$1"
            ;;
        -h|--help)
            sed -n '3,45p' "$0"
            exit 0
            ;;
        *)
            echo "unknown argument: $1 (try --help)" >&2
            exit 2
            ;;
    esac
    shift
done

say() { [ "$QUIET" = "1" ] || echo "$@"; }

# --- resolve the canonical on-disk binary -----------------------------------
# Every capture below is guarded. A bare `x=$(cmd)` under `set -e` inherits the
# substitution's status and aborts before any `rc=$?` can run; this script does
# not enable errexit, but the guards keep it correct if that ever changes.
canonical_path=""
canonical_path="$(command -v claude 2>/dev/null)" || canonical_path=""
if [ -z "$canonical_path" ]; then
    echo "cc-running-versions: UNDETERMINED — no 'claude' on PATH; cannot establish a canonical binary" >&2
    exit 2
fi

canonical_real=""
canonical_real="$(readlink -f "$canonical_path" 2>/dev/null)" || canonical_real=""
[ -n "$canonical_real" ] || canonical_real="$canonical_path"

canonical_inode=""
canonical_inode="$(stat -c %i "$canonical_real" 2>/dev/null)" || canonical_inode=""
if [ -z "$canonical_inode" ]; then
    echo "cc-running-versions: UNDETERMINED — cannot stat $canonical_real" >&2
    exit 2
fi

canonical_version=""
canonical_version="$("$canonical_path" --version 2>/dev/null | awk '{print $1}')" || canonical_version=""

say "on-disk canonical: $canonical_real"
say "                  inode=$canonical_inode version=${canonical_version:-unknown}"
say ""

# --- sweep live processes ---------------------------------------------------
current=0
stale=0
undetermined=0
unreadable=0

for procdir in "$PROC_ROOT"/[0-9]*; do
    [ -d "$procdir" ] || continue   # no match → the glob stays literal
    pid="${procdir##*/}"

    exe_target=""
    exe_target="$(readlink "$procdir/exe" 2>/dev/null)" || exe_target=""
    if [ -z "$exe_target" ]; then
        # Not ours to inspect (another user), or the process exited mid-sweep.
        # Only count it if it still looks like a CC process from its cmdline.
        # `2>` comes BEFORE `<` deliberately: bash applies redirections left to
        # right, so a stderr redirect written after the input redirect cannot
        # suppress that redirect's own "No such file" — and a process exiting
        # between the glob and this read is routine, not an error worth printing.
        cmdline=""
        cmdline="$(tr '\0' ' ' 2>/dev/null < "$procdir/cmdline")" || cmdline=""
        case "$cmdline" in
            *claude*) unreadable=$((unreadable + 1)) ;;
        esac
        continue
    fi

    # Strip a trailing " (deleted)" so the path test works on replaced binaries.
    exe_clean="${exe_target% (deleted)}"
    exe_base="${exe_clean##*/}"

    is_cc=0
    node_wrapped=0
    case "$exe_clean" in
        *claude*) is_cc=1 ;;
    esac
    if [ "$is_cc" = "0" ]; then
        case "$exe_base" in
            node|nodejs)
                cmdline=""
                cmdline="$(tr '\0' ' ' 2>/dev/null < "$procdir/cmdline")" || cmdline=""
                case "$cmdline" in
                    *claude-code*|*/claude\ *|*/claude) is_cc=1; node_wrapped=1 ;;
                esac
                ;;
        esac
    fi
    [ "$is_cc" = "1" ] || continue

    started=""
    started="$(stat -c %y "$procdir" 2>/dev/null | cut -c1-19)" || started=""

    if [ "$node_wrapped" = "1" ]; then
        undetermined=$((undetermined + 1))
        say "  UNDETERMINED pid=$pid  node-wrapped install (exe=$exe_base) — cannot map inode to a CC revision; started=$started"
        continue
    fi

    run_inode=""
    run_inode="$(stat -L -c %i "$procdir/exe" 2>/dev/null)" || run_inode=""
    if [ -z "$run_inode" ]; then
        undetermined=$((undetermined + 1))
        say "  UNDETERMINED pid=$pid  cannot resolve running inode; started=$started"
        continue
    fi

    if [ "$run_inode" = "$canonical_inode" ]; then
        current=$((current + 1))
        say "  current      pid=$pid  inode=$run_inode  started=$started"
    else
        stale=$((stale + 1))
        say "  STALE        pid=$pid  inode=$run_inode (on-disk is $canonical_inode)  started=$started"
        say "               running: $exe_target"
    fi
done

say ""
say "summary: current=$current stale=$stale undetermined=$undetermined unreadable=$unreadable"

if [ "$stale" -gt 0 ]; then
    echo "cc-running-versions: $stale live Claude Code process(es) are running a REPLACED binary, not ${canonical_version:-the on-disk version}." >&2
    echo "  They will keep doing so until each one restarts. Any soak or validation those sessions" >&2
    echo "  performed is evidence about the OLD release, not the installed one. Relaunch them, then re-run this." >&2
    exit 1
fi

if [ "$undetermined" -gt 0 ]; then
    echo "cc-running-versions: UNDETERMINED for $undetermined process(es) — refusing to report all-clear." >&2
    exit 2
fi

if [ "$unreadable" -gt 0 ]; then
    say "note: $unreadable CC-looking process(es) belong to another user and were not inspected."
fi

say "cc-running-versions: OK — all $current live Claude Code process(es) run the on-disk binary."
exit 0
