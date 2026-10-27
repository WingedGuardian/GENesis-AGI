# shellcheck shell=bash
# Signal a process only while it is still the SAME process (review finding:
# a numeric pid checked, then signalled, can be recycled in between).
#
# A process's generation is its pid AND its start time (/proc/<pid>/stat
# field 22, clock ticks since boot). The disk guardian records both when it
# pauses a command, and every signal it or `scripts/watchgod thaw` sends goes
# through wg_signal_gen, which signals only that generation.
#
# How the binding is made, best first:
#   * pidfd (Linux 5.3+, Python 3.9+): open a pidfd for the pid, THEN check the
#     start time, then signal through the pidfd. A pidfd names one process for
#     its whole life, never a later holder of the same number. If the start
#     time matches after the pidfd is open, the pid held that generation when
#     the pidfd was opened (it existed before and still exists, so the number
#     was never free in between), and the signal can only reach it. No window.
#     bash cannot open a pidfd itself, so a short python3 does.
#   * Otherwise (no python3, no pidfd here, or a fake process table in tests):
#     re-read the start time immediately before `kill`. RESIDUAL WINDOW: the
#     few microseconds between that read and the kill(2). A pid would have to
#     exit, be reaped, and be handed to a new process in that gap; Linux
#     allocates pids cyclically up to pid_max, so this needs a full wrap of the
#     pid space inside the gap. Narrow, not closed: there is no atomic
#     check-and-signal on a numeric pid in bash.
#
# Pure functions; nothing runs at source time. DG_PROC (default /proc) points
# the start-time reads at a fake process table in tests; with a fake table the
# pidfd path is skipped, since a pidfd always names a real process.

wg_starttime() {
    # Print the start time of pid $1 (stat field 22). 0 printed; 1 gone (no
    # /proc entry); 2 cannot tell (the entry exists but its stat could not be
    # read or parsed). A read error is never "gone" (review finding).
    # The WHOLE record: comm (field 2) may hold a newline, so the first line
    # can end before its closing ")" (review finding).
    local proc="${DG_PROC:-/proc}" rest=""
    IFS= read -r -d '' rest 2>/dev/null < "$proc/$1/stat" || true  # no NUL: always 1 at EOF
    if [[ -z "$rest" ]]; then
        [[ -e "$proc/$1" ]] && return 2
        return 1
    fi
    [[ "$rest" == *") "* ]] || return 2
    rest="${rest##*) }"
    local -a f
    read -r -a f <<< "$rest"
    [[ "${f[19]:-}" =~ ^[0-9]+$ ]] || return 2
    printf '%s' "${f[19]}"
}

wg_same_gen() {
    # Is pid $1 still the process that started at $2? 0 yes; 1 no, and that is
    # established (no /proc entry, or a start time read and different); 2
    # cannot tell (the entry exists but its stat could not be read or parsed).
    # A caller never drops a record or sends a signal on 2: a read error is
    # not proof that the process is gone (review finding).
    local s rc=0
    s="$(wg_starttime "$1")" || rc=$?
    (( rc )) && return "$rc"
    [[ "$s" == "$2" ]] && return 0
    return 1
}

# Exit codes: 0 signalled; 10 not that process (gone, or the pid now names
# another one); 13 cannot tell (its stat could not be read); 11 that process,
# but the signal failed; 12 no pidfd here
# (including ENOSYS or a seccomp EPERM from either pidfd call), so the caller
# falls back to the start-time-checked kill, which then reports a genuine
# EPERM itself. Anything before the send that goes
# wrong exits 12, never 0: 0 is printed only after the signal went.
_WG_PIDFD_PY='
import errno, os, signal, sys
# ENOSYS (no such syscall) and EPERM (a seccomp filter that denies it) mean
# pidfds are unavailable here, not that the process refused: 12, fall back.
NOPIDFD = (errno.ENOSYS, errno.EPERM)
try:
    pid = int(sys.argv[1]); want = sys.argv[2]; sig = getattr(signal, "SIG" + sys.argv[3])
    try:
        pfd = os.pidfd_open(pid)
    except ProcessLookupError:
        sys.exit(10)
    except OSError:
        sys.exit(12)
    try:
        # Bytes: comm need not be valid UTF-8, and a decode error would
        # silently drop to the fallback path (review finding).
        with open("/proc/%d/stat" % pid, "rb") as fh:
            raw = fh.read()
    except (FileNotFoundError, ProcessLookupError):
        sys.exit(10)
    except OSError:
        sys.exit(13)
    if b") " not in raw:
        sys.exit(13)
    f = raw.rsplit(b") ", 1)[1].split()
    if len(f) < 20 or not f[19].isdigit():
        sys.exit(13)
    if f[19] != want.encode():
        sys.exit(10)
except SystemExit:
    raise
except BaseException:
    sys.exit(12)
try:
    signal.pidfd_send_signal(pfd, sig)
except ProcessLookupError:
    sys.exit(10)
except OSError as e:
    sys.exit(12 if e.errno in NOPIDFD else 11)
except BaseException:
    sys.exit(11)
sys.exit(0)
'

wg_signal_gen() {
    # Send SIG$1 (STOP, CONT, …) to pid $2 only if it is still the process
    # that started at $3. Returns 0 sent; 1 not that process (gone, or the pid
    # was recycled); 2 that process could not be signalled; 3 cannot tell
    # whether it is that process (nothing sent).
    local sig="$1" pid="$2" st="$3" rc
    [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 1 )) && [[ "$st" =~ ^[0-9]+$ ]] || return 2
    if [[ "${DG_PROC:-/proc}" == /proc ]] && command -v python3 >/dev/null 2>&1; then
        rc=0
        python3 -I -S -c "$_WG_PIDFD_PY" "$pid" "$st" "$sig" 2>/dev/null || rc=$?
        case "$rc" in
            0) return 0 ;;
            10) return 1 ;;
            11) return 2 ;;
            13) return 3 ;;
        esac
        # 12, or python itself failed before sending: fall back below.
    fi
    # Fallback: re-validate right before the kill (the residual window above).
    rc=0; wg_same_gen "$pid" "$st" || rc=$?
    (( rc == 1 )) && return 1
    (( rc == 2 )) && return 3
    kill -"$sig" "$pid" 2>/dev/null || return 2
    return 0
}
