#!/usr/bin/env bash
# shellcheck shell=bash
#
# Running-binary sweep — which LIVE Claude Code processes are executing the
# binary that is on disk now, and which are still running one npm has replaced.
#
# WHY THIS EXISTS. On a live install a container was aligned to a candidate CC
# version and a multi-day local-first soak was declared started. Measured at the
# end of it, MOST interactive sessions were still executing the pre-align binary:
# npm had replaced the package underneath them, and a long-lived process keeps
# its original mapping until it restarts. `claude --version` did not reveal this
# — it spawns a FRESH child, which reads the new on-disk binary and truthfully
# reports the candidate while the session asking is not running it. The soak had
# accumulated days of "real use" on the old release.
#
# NOT the same as `cc_shadow_scan`. That scans on-disk COPIES and removes stale
# ones — "what is installed here?". This asks "what is actually RUNNING here?".
# A box passes one and fails the other: exactly one canonical binary at the
# intended version, while several live sessions execute a deleted predecessor.
#
# ── THE INVARIANT ────────────────────────────────────────────────────────────
#
#   Any process this sweep cannot POSITIVELY PROVE is not-Claude-Code must not
#   contribute to a clean verdict.
#
# This is written down because its ABSENCE is what made the previous design
# churn. Each round fixed the case a reviewer named and silently flipped the
# fail direction, because there was nothing to check a fix against. Positive
# proof of not-CC is cheap and available: the exe is readable and its basename
# is not a CC name, or the exe is unreadable but the cmdline is readable and
# argv[0] is not a CC name. Only when NEITHER can be read is a process
# genuinely unclassifiable — and that must block a clean verdict, never be
# skipped.
#
# SCOPE OF THAT COST, stated precisely because an earlier version of this
# comment was not. Measured INSIDE A PID-NAMESPACED CONTAINER, 0 of 109
# processes land in the unclassifiable bucket. That number does NOT generalise:
# a container's PID namespace contains no kernel threads, and on a host kernel
# they are a large fraction of all pids (measured ~170 of 320 on a comparable
# host) with exactly the unclassifiable shape — no exe, empty cmdline. Left
# unhandled they would make this script return 2 unconditionally off-container,
# including on any CI runner. That is why kernel threads are identified
# POSITIVELY as not-CC below rather than parked in the bucket.
#
# A rejected design, recorded so it is not re-proposed: classify by whether the
# executable lives under an enumerated Claude Code INSTALL ROOT. It fails on the
# only case that matters. npm's replace RENAMES the old package to
# `@anthropic-ai/.claude-code-<random>` and then deletes it, so a stale
# process's exe path points at a directory that no longer exists — measured live
# here, with two sessions on `.claude-code-1devilah/bin/claude.exe (deleted)`
# while `@anthropic-ai/` contained only `claude-code`. A root set enumerated
# from the install cannot contain a directory the installer deleted, so those
# processes classify as "not CC", are ignored, and the sweep exits 0. That
# swapped a bounded NAME test whose mistakes shout (a false alarm) for a path
# test whose mistakes whisper (ignored ⇒ all-clear).
#
# Method: `stat -L /proc/<pid>/exe` resolves the inode of the executable a
# process is running, and procfs keeps that reference valid even after the file
# is unlinked, so a replaced binary still resolves. NOTE the `-L` is
# load-bearing: without it `stat` returns the procfs magic-link's own inode, not
# the target's, and every process compares unequal.
#
# Identity is DEVICE + inode. An inode number is unique only within a
# filesystem, so a binary on another mount can collide numerically and be
# reported current while being an entirely different file.
#
# Exit codes:
#     0 — every live CC process runs the on-disk binary (or none are running)
#     1 — at least one runs a DIFFERENT (stale/replaced) binary
#     2 — cannot determine, so no clean verdict is claimed
#
# Usage:  scripts/check_cc_running_versions.sh [--quiet] [--proc-root DIR]

set -u
set -o pipefail

# Resolve HOME when unset: stripped-env/systemd/sandbox invocations can leave
# HOME unset, which under `set -u` aborts at the first ${HOME} use — and would
# abort with exit 1, the code that means "stale binaries found". Same
# passwd-fallback form the rest of the script suite uses.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)"
    if [ -z "${HOME:-}" ]; then
        echo "cc-running-versions: UNDETERMINED — HOME is unset and unresolvable" >&2
        exit 2
    fi
    export HOME
fi

QUIET=0
PROC_ROOT="/proc"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --quiet) QUIET=1 ;;
        --proc-root)
            shift
            [ "$#" -gt 0 ] || { echo "--proc-root requires a path" >&2; exit 2; }
            PROC_ROOT="$1"
            ;;
        -h|--help)
            sed -n '3,80p' "$0"
            echo
            echo "Options: --quiet   suppress per-process lines (never the verdict)"
            echo "         --proc-root DIR   scan an alternative procfs root"
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

# A typo'd --proc-root previously produced a confident "OK — all 0 live
# processes": the unmatched-glob guard cannot tell an empty procfs from a wrong
# path. Refuse instead.
if [ ! -d "$PROC_ROOT" ]; then
    echo "cc-running-versions: UNDETERMINED — proc root '$PROC_ROOT' is not a directory" >&2
    exit 2
fi
# Existing-but-wrong is the likelier typo, and checking only for existence left
# it open: MEASURED, `--proc-root /tmp` produced a confident "OK — all 0 live
# Claude Code process(es)", exit 0. Require it to actually look like a procfs.
if [ ! -d "$PROC_ROOT/1" ] && [ ! -e "$PROC_ROOT/self" ]; then
    echo "cc-running-versions: UNDETERMINED — '$PROC_ROOT' does not look like a procfs" >&2
    echo "  (no pid 1 and no self/ entry). Refusing rather than sweeping an empty directory." >&2
    exit 2
fi

# ── the denominator ─────────────────────────────────────────────────────────
# Under hidepid the sweep cannot verify its own DENOMINATOR. The two shapes
# differ and both matter: hidepid=1 leaves other users' pid directories VISIBLE
# but their contents unreadable, hidepid=2 and above hide them entirely. Either
# way a clean verdict would describe only this user's processes while claiming
# to describe every live one — the receipt's denominator would be unverified.
# Match the OPTION, never an enumerated value list. `hidepid=[12]` missed
# `hidepid=4` (HIDEPID_NOT_PTRACEABLE) and the symbolic spellings the 5.8+
# multi-instance procfs work introduced — `noaccess`, `invisible`,
# `ptraceable` — which is the same closed-set-of-values mistake this script
# rejects elsewhere. Anything that is not explicitly "off"/"0" refuses.
if [ "$PROC_ROOT" = "/proc" ] &&
    grep -qE '(^| )/proc proc [^ ]*(^|,)hidepid=' /proc/mounts 2>/dev/null &&
    ! grep -qE '(^| )/proc proc [^ ]*(^|,)hidepid=(0|off)(,| )' /proc/mounts 2>/dev/null; then
    echo "cc-running-versions: UNDETERMINED — /proc is mounted with hidepid, so other" >&2
    echo "  users' processes are hidden or unreadable and this run cannot claim to have" >&2
    echo "  swept them. (hidepid=1 leaves the directories visible but unreadable;" >&2
    echo "  hidepid=2 and above hide them entirely — under either, the DENOMINATOR is" >&2
    echo "  unverified, which is the same lie as a wrong verdict.)" >&2
    exit 2
fi

# ── the canonical binary, and every installed copy ──────────────────────────
CC_PROBE_DIRS="${CC_PROBE_DIRS:-/usr/local/bin:/usr/bin:$HOME/.npm-global/bin:$HOME/.local/bin}"

canonical_path=""
canonical_path="$(command -v claude 2>/dev/null)" || canonical_path=""
if [ -z "$canonical_path" ]; then
    echo "cc-running-versions: UNDETERMINED — no 'claude' on PATH; nothing to compare against" >&2
    exit 2
fi

canonical_real=""
canonical_real="$(readlink -f "$canonical_path" 2>/dev/null)" || canonical_real=""
[ -n "$canonical_real" ] || canonical_real="$canonical_path"

canonical_id=""
canonical_id="$(stat -c '%d:%i' "$canonical_real" 2>/dev/null)" || canonical_id=""
if [ -z "$canonical_id" ]; then
    echo "cc-running-versions: UNDETERMINED — cannot stat $canonical_real" >&2
    exit 2
fi

# Map every INSTALLED copy by dev:inode. A process running a copy that is
# installed-but-not-canonical is a different situation from one running a
# deleted predecessor, and saying which is more useful than refusing outright.
# The previous design refused the whole sweep whenever two copies disagreed on
# --version, which (a) no-opped entirely when either probe failed, and (b) threw
# away the per-process answer that resolves the question.
installed_ids="$canonical_id"
installed_desc="$canonical_id=$canonical_real"
_oldIFS="$IFS"
IFS=':'
for _d in $CC_PROBE_DIRS; do
    _cand="$_d/claude"
    [ -x "$_cand" ] || continue
    _cand_real="$(readlink -f "$_cand" 2>/dev/null)" || _cand_real="$_cand"
    _cand_id="$(stat -c '%d:%i' "$_cand_real" 2>/dev/null)" || continue
    case " $installed_ids " in *" $_cand_id "*) continue ;; esac
    installed_ids="$installed_ids $_cand_id"
    installed_desc="$installed_desc
$_cand_id=$_cand_real"
done
IFS="$_oldIFS"

say "on-disk canonical: $canonical_real"
say "                  dev:inode=$canonical_id"
say ""

# ── classification: a CLOSED name set, never a path guess ───────────────────
# `claude-code` is included defensively. The shipped package's bin map is
# {claude: bin/claude.exe}, so no such executable exists on this install — it
# costs one token and is not a demonstrated hole.
is_cc_name() {
    case "$1" in
        claude|claude.exe|claude-code) return 0 ;;
    esac
    return 1
}

# Interpreters that can run the CC entry script. `bun`/`deno` are defensive —
# not demonstrated on this install — but a reviewer measured the gap with a
# bun-wrapped CLI, and the whole point of a CLOSED set is that widening it is a
# token rather than an architecture. Missing one here is a false all-clear.
is_interpreter_name() {
    case "$1" in
        node|nodejs|bun|deno) return 0 ;;
    esac
    return 1
}

# Does this command line run the CC entry script under an interpreter?
#
# The FIRST NON-FLAG token, not argv[1]. Testing argv[1] treated
# `node --enable-source-maps /opt/cc/cli.js` as proof of NOT-CC, because the
# flag occupied the slot — and node CLIs routinely carry --enable-source-maps,
# --no-warnings or --max-old-space-size. Narrowing a detector that way trades a
# loud false positive for a silent false negative, which is the wrong direction
# for a check whose worst outcome is a false all-clear.
cmdline_runs_cc() {
    # shellcheck disable=SC2086 # deliberate word-split; globbing is off (set -f)
    set -- $1
    [ "$#" -ge 1 ] || return 1
    shift
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -*) shift ;;
            *)
                case "${1##*/}" in
                    cli.js) return 0 ;;
                esac
                return 1
                ;;
        esac
    done
    return 1
}

# A kernel thread is POSITIVELY not Claude Code, and saying so is what keeps the
# invariant affordable. Kernel threads have no mm, so no exe and an empty
# cmdline — the same shape as an uninspectable process — and parking them in the
# unclassifiable bucket makes the sweep return 2 on any host that has them.
#
# Two signals, either sufficient: this kernel exposes `Kthread:` in
# /proc/<pid>/status (verified present on 6.8), and the mm-less signature
# (no VmSize) is the portable fallback for kernels that do not.
is_kernel_thread() {
    _status="$1/status"
    [ -r "$_status" ] || return 1
    case "$(grep -m1 '^Kthread:' "$_status" 2>/dev/null)" in
        *1) return 0 ;;
        *0) return 1 ;;
    esac
    grep -q '^VmSize:' "$_status" 2>/dev/null && return 1
    return 0
}

current=0
stale=0
other_install=0
undetermined=0
unclassifiable=0

# Word-splitting a cmdline into positional parameters must NOT also glob against
# the sweep's own working directory. MEASURED: an unrelated `node * --flag`, swept
# from a directory containing cli.js, expanded into a CC-shaped argv and
# manufactured a refusal purely from the operator's `cd`. The inverse hides a real
# one. Nothing below needs pathname expansion; the pid glob already happened.
for procdir in "$PROC_ROOT"/[0-9]*; do
    # INSIDE the body, never before the `for`: the pid glob above must still
    # expand. Setting it earlier disabled that glob too, so `procdir` stayed the
    # literal pattern, the loop ran zero times, and the sweep reported
    # "OK — all 0 live Claude Code process(es)" on a box with two stale ones.
    # Total blindness presenting as a clean verdict — caught by running it live,
    # not by any test, which is why the live run is part of the check.
    set -f
    [ -d "$procdir" ] || continue   # no match → the glob stays literal
    pid="${procdir##*/}"

    exe_target=""
    exe_target="$(readlink "$procdir/exe" 2>/dev/null)" || exe_target=""

    # `2>` precedes `<` deliberately: bash applies redirections left to right, so
    # a stderr redirect written after the input redirect cannot suppress that
    # redirect's own "No such file" — and a process exiting mid-sweep is routine.
    cmdline=""
    cmdline="$(tr '\0' ' ' 2>/dev/null < "$procdir/cmdline")" || cmdline=""

    if [ -n "$exe_target" ]; then
        # Strip procfs' " (deleted)" suffix BEFORE the name test. This one line
        # carries the whole incident: npm renames-then-deletes, so a stale
        # process's link reads `.../claude.exe (deleted)`, and without the strip
        # `is_cc_name` sees "claude.exe (deleted)", fails, and the process is
        # silently dropped — turning the exact case this exists to catch into a
        # green verdict.
        exe_base="${exe_target% (deleted)}"
        exe_base="${exe_base##*/}"
        if is_cc_name "$exe_base"; then
            :   # CC, inode decides below
        elif is_interpreter_name "$exe_base"; then
            if cmdline_runs_cc "$cmdline"; then
                undetermined=$((undetermined + 1))
                say "  UNDETERMINED pid=$pid  interpreter-wrapped (exe=$exe_base) — the interpreter's inode says nothing about the CC revision"
                continue
            fi
            continue    # an interpreter running something else: positively not CC
        else
            continue    # readable exe, not a CC name: POSITIVELY not CC
        fi
    elif is_kernel_thread "$procdir"; then
        # POSITIVELY not CC, and this branch is why it must be checked: a kernel
        # thread has no exe and an empty cmdline, so without it every kthread
        # lands in the unclassifiable bucket and the sweep can never return 0 on
        # a host that has them. This container's PID namespace shows none, which
        # is exactly why the earlier "0 of 109" measurement did not reveal it.
        continue
    elif [ -n "${cmdline# }" ] && [ -n "$(printf '%s' "$cmdline" | tr -d '[:space:]')" ]; then
        # Exe unreadable (another user, or a procfs restriction) but the cmdline
        # is readable, so argv[0] still settles it. NO path/root-set clause here:
        # requiring one would mean another user's CC install — under THEIR home —
        # fails to match and is silently ignored, which is a false all-clear.
        # shellcheck disable=SC2086 # deliberate word-split; globbing is off (set -f)
        set -- $cmdline
        if [ "$#" -lt 1 ]; then
            # An all-NUL cmdline word-splits to nothing. Reading $1 here aborted
            # the script under `set -u` with exit 1 — the code meaning "stale
            # found". An environment shape must never read as a finding.
            unclassifiable=$((unclassifiable + 1))
            say "  UNDETERMINED pid=$pid  command line present but empty"
            continue
        fi
        arg0_base="${1##*/}"
        if is_cc_name "$arg0_base"; then
            unclassifiable=$((unclassifiable + 1))
            say "  UNDETERMINED pid=$pid  identified as CC by argv[0], but its executable could not be inspected"
            continue
        fi
        if is_interpreter_name "$arg0_base" && cmdline_runs_cc "$cmdline"; then
            unclassifiable=$((unclassifiable + 1))
            say "  UNDETERMINED pid=$pid  interpreter-wrapped CC by cmdline, executable not inspectable"
            continue
        fi
        continue        # readable cmdline, not a CC name: POSITIVELY not CC
    else
        # Nothing identifying could be read and it is not a kernel thread. It
        # cannot be PROVEN not-CC, so per the invariant it must not contribute to
        # a clean verdict. The count is unconditional — an earlier version put
        # the increment inside a `[ -r stat ]` guard with `continue` OUTSIDE it,
        # so a wholly unreadable pid dir (the hidepid=1 shape: visible, contents
        # EACCES) incremented nothing and voted CLEAN, violating the invariant in
        # the one branch written to enforce it.
        unclassifiable=$((unclassifiable + 1))
        if [ -r "$procdir/stat" ]; then
            say "  UNDETERMINED pid=$pid  neither executable nor command line could be read"
        else
            say "  UNDETERMINED pid=$pid  process directory is entirely unreadable"
        fi
        continue
    fi

    started=""
    started="$(stat -c %y "$procdir" 2>/dev/null | cut -c1-19)" || started=""

    run_id=""
    run_id="$(stat -L -c '%d:%i' "$procdir/exe" 2>/dev/null)" || run_id=""
    if [ -z "$run_id" ]; then
        # Realistically the process exited between the readlink and the stat.
        # NOT treated as stale: manufacturing a stale verdict for a process that
        # no longer exists is a false alarm on evidence quoted in a receipt.
        undetermined=$((undetermined + 1))
        say "  UNDETERMINED pid=$pid  running inode could not be resolved; started=$started"
        continue
    fi

    if [ "$run_id" = "$canonical_id" ]; then
        current=$((current + 1))
        say "  current      pid=$pid  id=$run_id  started=$started"
    elif case " $installed_ids " in *" $run_id "*) true ;; *) false ;; esac; then
        other_install=$((other_install + 1))
        say "  OTHER-COPY   pid=$pid  id=$run_id — an INSTALLED copy that is not the PATH-canonical one; started=$started"
        say "               running: $exe_target"
    else
        stale=$((stale + 1))
        say "  STALE        pid=$pid  id=$run_id (on-disk is $canonical_id)  started=$started"
        say "               running: $exe_target"
    fi
done

set +f

say ""
say "summary: current=$current stale=$stale other-copy=$other_install undetermined=$undetermined unclassifiable=$unclassifiable"

if [ "$stale" -gt 0 ] || [ "$other_install" -gt 0 ]; then
    if [ "$stale" -gt 0 ]; then
        echo "cc-running-versions: $stale live Claude Code process(es) are running a REPLACED binary." >&2
    fi
    if [ "$other_install" -gt 0 ]; then
        echo "cc-running-versions: $other_install live process(es) run an installed copy that is NOT the" >&2
        echo "  PATH-canonical one. Installed copies seen:" >&2
        printf '%s\n' "$installed_desc" | while IFS= read -r _line; do
            echo "    $_line" >&2
        done >&2
        echo "  Run cc_shadow_scan to collapse to one copy." >&2
    fi
    echo "  They keep running it until each restarts. Any soak those sessions performed is" >&2
    echo "  evidence about a different binary. Relaunch them, then re-run this." >&2
    exit 1
fi

if [ "$undetermined" -gt 0 ] || [ "$unclassifiable" -gt 0 ]; then
    echo "cc-running-versions: UNDETERMINED for $((undetermined + unclassifiable)) process(es) — refusing to" >&2
    echo "  report all-clear. A verdict that skipped processes it could not inspect would be a" >&2
    echo "  claim about sessions this run never saw." >&2
    exit 2
fi

say "cc-running-versions: OK — all $current live Claude Code process(es) run the on-disk binary."
exit 0
