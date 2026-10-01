#!/usr/bin/env bash
# code_intel_index.sh — the ONE entrypoint for code-intelligence indexing.
#
#   Usage: code_intel_index.sh <repo_path> [cbm|gitnexus|both] [fast|moderate|full]
#
# Every code-intel index spawn (codebase-memory-mcp cli index_repository,
# gitnexus analyze) MUST go through this script. Raw spawns caused a
# container-wedging incident: three concurrent full-repo indexers on one
# worktree saturated the container's disk-write throttle, piling every
# writer into D-state (load ~58 at 79% memory) and hanging sshd/incus-exec.
# A guardrail test (test_code_intel_index.py) fails any new raw spawn site.
#
# What this enforces, in order:
#   1. WORKTREE SKIP — linked git worktrees (.git is a file) are never
#      indexed: their code is ~identical to the main repo, each index costs
#      a full separate graph (~GBs + heavy writes), and Serena (live LSP)
#      covers worktree sessions without any index.
#   2. SINGLE-FLIGHT — a non-blocking flock per repo path: if an index for
#      this repo is already running, exit 0 immediately (never queue).
#   3. RESOURCE CAPS — both tools use a systemd user scope with MemoryMax /
#      MemorySwapMax=0 / IOWeight / CPUQuota. Codebase refuses when that scope
#      is unavailable; GitNexus retains its legacy nice/ionice + address-space
#      rlimit fallback.
#
# The script runs the requested tools SEQUENTIALLY in the foreground and
# exits when done — but it is NOT meant to be called on every commit anymore.
# Triggers write an index-request marker (scripts/lib/index_marker.py) and the
# idle-gated runner (scripts/code_intel_runner.sh) is the only caller that
# consumes a marker and invokes this entrypoint, when the box is idle. A
# fire-and-forget spawn here was the second index-storm's trigger.
#
# MODE (3rd arg, default fast): fast = filtered files, no similarity/semantic
# edges — cheap, the routine default. full = all files + similarity/semantic,
# the expensive pipeline that saturated the container's read throttle; reserve
# it for the runner's weekly-full window. GitNexus is incremental regardless.
#
# Exit codes: 0 success · 1 hard error · 3 a REQUESTED tool was missing from
# PATH (nothing indexed — the runner must NOT treat this as success, or a
# minimal-PATH service unit silently disables indexing forever) · the lock-skip
# path returns CODE_INTEL_INDEX_LOCK_SKIP_RC (default 0; the runner sets 75 to
# tell "lock held / host-frozen — keep the marker" apart from a real success).
#
# Env overrides:
#   CODE_INTEL_INDEX_MEMORY_MAX   legacy override for both tools
#   CODE_INTEL_CBM_MEMORY_MAX     default 4G     (requested CBM batch ceiling)
#   CODE_INTEL_GITNEXUS_MEMORY_MAX default 8G    (measured GitNexus rebuild)
#   CODE_INTEL_FILE_CACHE_RESERVE_BYTES default 2G (clean cache kept outside job)
#   CODE_INTEL_INDEX_IO_WEIGHT    default 20     (1-10000; low = polite)
#   CODE_INTEL_INDEX_CPU_QUOTA    default 200%   (2 cores worth)
#   CODE_INTEL_INDEX_MODE         default fast   (fast|moderate|full; 3rd arg wins)
#   CODE_INTEL_INDEX_PERSISTENCE  default true   (cbm .codebase-memory artifact)
#   CODE_INTEL_INDEX_LOCK_SKIP_RC default 0      (runner sets 75)
#   CODE_INTEL_INDEX_DISABLE=1    skip all indexing (escape hatch)

set -u

# Private child launcher. The supervising script owns admission, scope control,
# pausing and cleanup; making that supervisor an OOM target can remove the only
# process able to stop or thaw the heavy job. Apply the kernel's maximum
# preference only in the disposable child, immediately before exec, and verify
# the effective value. +1000 is maximally preferred among tasks eligible in the
# applicable OOM domain; cgroup OOM-domain boundaries still govern eligibility.
if [ "${1:-}" = "--exec-indexer-with-oom-adj" ]; then
    shift
    if [ "$#" -eq 0 ]; then
        printf '%s\n' "code-intel: missing indexer command" >&2
        exit 125
    fi
    if [ -n "${CODE_INTEL_INDEX_OOM_SCORE_ADJ:-}" ] \
        && [ "$CODE_INTEL_INDEX_OOM_SCORE_ADJ" != "1000" ]; then
        printf '%s\n' \
            "code-intel: CODE_INTEL_INDEX_OOM_SCORE_ADJ requires 1000; refusing unsafe batch workload" >&2
        exit 125
    fi
    if [ -n "${CODE_INTEL_CHILD_CAP_BYTES:-}" ]; then
        if [ ! -x /usr/bin/python3 ] \
            || ! /usr/bin/python3 -I "${BASH_SOURCE[0]%/*}/code_intel_cbm_admission.py" \
                "$CODE_INTEL_CHILD_CAP_BYTES" \
                "${CODE_INTEL_CHILD_RESERVE_BYTES:-}" \
                "${CODE_INTEL_CHILD_SCOPE_UNIT:-}" \
                "${CODE_INTEL_FILE_CACHE_RESERVE_BYTES:-2147483648}"; then
            # The parent owns this unique marker. It distinguishes an
            # admission refusal from a raw indexer exit status, so the queue
            # keeps the request without charging a failed indexing attempt.
            if [ -n "${CODE_INTEL_CHILD_REFUSAL_MARKER:-}" ]; then
                printf '%s\n' refused > "$CODE_INTEL_CHILD_REFUSAL_MARKER" 2>/dev/null || true
            fi
            printf '%s\n' "code-intel: refusing Codebase batch without proven scope admission" >&2
            exit 125
        fi
    fi

    # One-way failure injection: this can only make a test/refusal stricter. It
    # cannot redirect the proof or let an unprotected workload execute.
    if [ "${CODE_INTEL_TEST_FORCE_OOM_ADJ_FAILURE:-0}" = "1" ]; then
        printf '%s\n' "code-intel: cannot establish oom_score_adj=1000; refusing batch workload" >&2
        exit 125
    fi
    _oom_adj_file=/proc/self/oom_score_adj
    if ! { printf '%s\n' 1000 > "$_oom_adj_file"; } 2>/dev/null; then
        printf '%s\n' "code-intel: cannot establish oom_score_adj=1000; refusing batch workload" >&2
        exit 125
    fi
    _oom_adj_actual=""
    read -r _oom_adj_actual < "$_oom_adj_file" 2>/dev/null || _oom_adj_actual=""
    if [ "$_oom_adj_actual" != "1000" ]; then
        printf '%s\n' \
            "code-intel: cannot establish oom_score_adj=1000 (read back '${_oom_adj_actual:-unavailable}'); refusing batch workload" >&2
        exit 125
    fi
    exec "$@"
fi

_CODE_INTEL_ENTRYPOINT="${BASH_SOURCE[0]}"
case "$_CODE_INTEL_ENTRYPOINT" in
    /*) : ;;
    */*)
        _code_intel_entry_dir="${_CODE_INTEL_ENTRYPOINT%/*}"
        _code_intel_entry_base="${_CODE_INTEL_ENTRYPOINT##*/}"
        _code_intel_entry_dir="$(unset CDPATH; cd -P -- "$_code_intel_entry_dir" 2>/dev/null && pwd)" \
            || { printf '%s\n' "code-intel: cannot resolve entrypoint path" >&2; exit 1; }
        _CODE_INTEL_ENTRYPOINT="$_code_intel_entry_dir/$_code_intel_entry_base"
        ;;
    *)
        _CODE_INTEL_ENTRYPOINT="$(pwd -P)/$_CODE_INTEL_ENTRYPOINT"
        ;;
esac

# Resolve HOME when unset: stripped-env/systemd/sandbox invocations can leave
# HOME unset, which under `set -u` aborts at the first ${HOME} use. Fall back
# to the passwd entry for the current uid (same source Path.home() uses); fail
# closed if unresolvable. See CC memory sandbox_shell_no_home.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

REPO_PATH="${1:-}"
TOOLS="${2:-both}"
MODE="${3:-${CODE_INTEL_INDEX_MODE:-fast}}"

_LEGACY_MEM_MAX="${CODE_INTEL_INDEX_MEMORY_MAX:-}"
# Four GiB is the provisional batch ceiling. The bounded child checks actual
# destination-scope headroom for the entire requested cap before indexing.
CBM_MEM_MAX="${CODE_INTEL_CBM_MEMORY_MAX:-${_LEGACY_MEM_MAX:-4G}}"
# Measured 2026-09-16: a forced full rebuild peaked at 4,874,166,272 bytes
# (4.54 GiB) and completed under an 8 GiB, swapless scope. The old shared 2G
# cap killed it on the way up. Keep headroom for repository growth; admission
# control and the pressure watchdog still decide when the job may run.
GITNEXUS_MEM_MAX="${CODE_INTEL_GITNEXUS_MEMORY_MAX:-${_LEGACY_MEM_MAX:-8G}}"

# ── Bound the cap by what this INSTALL actually has ──────────────────────────
# A fixed 8G is a cap, not a reservation, and on a large host that is fine. On a
# 4-5 GiB container it is worse than no cap at all: the child scope never
# reaches its own MemoryMax, so the PARENT cgroup hits its limit first and the
# kernel picks a victim from every process in it -- Genesis, Qdrant, the running
# session. The cap is supposed to make this job safe to run unattended, and at
# that size it removes the only thing standing between a rebuild and the
# services around it. The pressure watchdog does not cover this either: it
# samples load and I/O wait, neither of which moves early enough on an OOM path.
#
# So the effective cap is min(configured, what this box can spare), and when
# what it can spare is below the measured working set the job is REFUSED rather
# than run with a cap that cannot bite.
_genesis_mem_bytes() {  # "8G"/"512M"/"1024K"/"5.5G"/bytes -> bytes on stdout, or nothing
    local v="${1:-}"
    # Fractional values are legal systemd (MemoryMax=5.5G); Bash arithmetic is
    # integer-only, so the multiply goes through awk. A bare number must be an
    # integer byte count — a unitless "5.5" is malformed, not 5.5 bytes.
    [[ "$v" =~ ^([0-9]+(\.[0-9]+)?)([GgMmKk])$ || "$v" =~ ^[0-9]+$ ]] || return 1
    # Bound the MANTISSA before the multiply, per unit. Bounding the RESULT is
    # not equivalent: the integer branches below multiply in Bash, so by the
    # time a result bound runs, the product has already wrapped and the bound
    # is inspecting a plausible-looking small number. MEASURED on the first
    # version of this fix: "18014398514243865K" wrapped to 4876166144 and was
    # accepted as a legitimate 4.87 GB cap, above the gitnexus minimum.
    # Per-unit limits keep mantissa x scale under the byte bound:
    #   G: 10^18 / 2^30 = 9.3e8  -> 9 digits
    #   M: 10^18 / 2^20 = 9.5e11 -> 11 digits
    #   K: 10^18 / 2^10 = 9.8e14 -> 14 digits
    local _mb_mant _mb_lim
    case "$v" in
        *[Gg]) _mb_mant="${v%[Gg]}"; _mb_lim=9 ;;
        *[Mm]) _mb_mant="${v%[Mm]}"; _mb_lim=11 ;;
        *[Kk]) _mb_mant="${v%[Kk]}"; _mb_lim=14 ;;
        *)     _mb_mant="$v";        _mb_lim="$_GENESIS_MEM_MAX_DIGITS" ;;
    esac
    # Integer part only: a fractional mantissa goes through awk (double), and
    # the result bound below catches anything awk produces that is too large.
    _genesis_uint_bounded "${_mb_mant%%.*}" "$_mb_lim" || return 1
    local _mb_out
    _mb_out="$(
    case "$v" in
        # `%.0f`, not `%d`: mawk implements %d through a signed 32-bit int and
        # clamps 8 GiB to 2147483647, which reads as "below the working set"
        # and refuses every run. %f goes through double, exact past 2^32.
        # awk is only reached for FRACTIONAL values — integer mantissas use
        # Bash's 64-bit arithmetic so minimal environments without awk
        # (the rlimit fallback's whole reason to exist) still get a cap.
        *[Gg]) [[ "${v%[Gg]}" == *.* ]] && awk -v n="${v%[Gg]}" 'BEGIN{printf "%.0f", n * 1073741824}' \
                || printf '%s' "$(( ${v%[Gg]} * 1073741824 ))" ;;
        *[Mm]) [[ "${v%[Mm]}" == *.* ]] && awk -v n="${v%[Mm]}" 'BEGIN{printf "%.0f", n * 1048576}' \
                || printf '%s' "$(( ${v%[Mm]} * 1048576 ))" ;;
        *[Kk]) [[ "${v%[Kk]}" == *.* ]] && awk -v n="${v%[Kk]}" 'BEGIN{printf "%.0f", n * 1024}' \
                || printf '%s' "$(( ${v%[Kk]} * 1024 ))" ;;
        *) printf '%s' "$v" ;;
    esac
    )"
    # A parseable value can still be too large to compute with ("99999999999G").
    # Emitting nothing routes it to the caller's existing "not a parseable
    # memory value" refusal rather than into a wrapped cap.
    _genesis_uint_bounded "$_mb_out" || return 1
    printf '%s' "$_mb_out"
}

# Bash arithmetic is signed 64-bit and wraps SILENTLY. A wrapped product can
# re-cross zero on the very next subtraction and present as enormous headroom:
# MEASURED, MemAvailable=9007199254740992 kB makes (avail*1024)-reserve come out
# at +9223372034707292160, which clears the minimum-headroom check with no
# headroom at all. So every externally-sourced number is bounded BEFORE it
# reaches arithmetic, not after.
#
# The bound is a DIGIT COUNT rather than a numeric limit, because comparing an
# out-of-range value numerically is the same trap one layer down: `[ huge -gt x ]`
# exits 2, and an `&&` list continues straight past it. Counting digits cannot
# overflow and cannot error. Any value of at most 18 digits is below 10^18, so
# two of them sum and difference well inside int64. The kB bound is 14 rather
# than 15 so that a legal kB value stays legal AFTER the x1024 every caller
# applies: 15 digits x 1024 is a 19-digit byte value, which the byte bound
# would then reject — the two limits have to compose, not merely each hold.
# Both ceilings sit astronomically above real hardware (10^18 B is 888 PiB).
# This generalises the length guard the v1 cgroup branch above already uses.
_GENESIS_MEM_MAX_DIGITS=18
_GENESIS_MEM_MAX_KB_DIGITS=14

# A nonnegative decimal integer small enough that the admission arithmetic
# cannot wrap. Leading zeros are stripped so "0000000008" is judged as one
# digit, not ten.
_genesis_uint_bounded() {
    local v="${1:-}" limit="${2:-$_GENESIS_MEM_MAX_DIGITS}"
    # No leading zeros: $(( )) reads those as OCTAL while `[` reads them as
    # decimal, so "0100" would mean 100 to one and 64 to the other, and "08"
    # aborts the script under `set -u` with a raw bash error rather than a
    # structured refusal. Rejecting the spelling is cheaper than teaching every
    # arithmetic site to write 10#.
    [[ "$v" =~ ^(0|[1-9][0-9]*)$ ]] || return 1
    [ "${#v}" -le "$limit" ]
}


# The container's own ceiling. cgroup v2 first (what an LXC/Docker limit shows
# up as), then v1, then MemTotal. "max" means unlimited, so it is not a ceiling.
_genesis_mem_ceiling() {
    local raw=""
    # A SEAM, not a convenience. Without it the refusal path below is
    # unreachable on any box big enough to run the job, so the branch that
    # protects small installs could only ever be verified by owning a small
    # install. It doubles as the operator override when a container limit is
    # not discoverable.
    if [ -n "${CODE_INTEL_MEM_CEILING_BYTES:-}" ]; then
        _genesis_uint_bounded "$CODE_INTEL_MEM_CEILING_BYTES" || return 1
        printf '%s' "$CODE_INTEL_MEM_CEILING_BYTES"
        return
    fi
    if [ -r /sys/fs/cgroup/memory.max ]; then
        raw="$(cat /sys/fs/cgroup/memory.max 2>/dev/null)"
    elif [ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]; then
        raw="$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null)"
    fi
    if [ -n "$raw" ] && [ "$raw" != "max" ] && _genesis_uint_bounded "$raw" \
        && [ "$raw" -gt 0 ] 2>/dev/null; then
        # A v1 "unlimited" is a huge sentinel rather than a word; anything at or
        # above MemTotal is not a container limit worth honouring.
        local total_kb total_b
        total_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null)"
        _genesis_uint_bounded "${total_kb:-0}" "$_GENESIS_MEM_MAX_KB_DIGITS" || total_kb=0
        total_b=$(( ${total_kb:-0} * 1024 ))
        if [ "$total_b" -gt 0 ] && [ "$raw" -lt "$total_b" ]; then
            printf '%s' "$raw"
            return
        fi
        [ "$total_b" -gt 0 ] && { printf '%s' "$total_b"; return; }
        printf '%s' "$raw"
        return
    fi
    local total_kb meminfo
    meminfo="${CODE_INTEL_MEMINFO:-/proc/meminfo}"
    total_kb="$(awk '/^MemTotal:/ {print $2}' "$meminfo" 2>/dev/null)"
    _genesis_uint_bounded "$total_kb" "$_GENESIS_MEM_MAX_KB_DIGITS" \
        && printf '%s' "$(( total_kb * 1024 ))"
}


# Convert a cgroup's total charge into a conservative working-set estimate.
# memory.current includes clean filesystem cache, which the kernel can reclaim
# before an OOM. Counting every cached byte as permanently occupied starves the
# indexer on a cache-heavy box even when anon+kernel memory is small. Do NOT use
# anon alone: shmem, dirty/writeback pages and kernel memory still consume the
# parent cgroup. Mirror read_container_memory_reclaimable(): the file LRU
# counters exclude tmpfs/shmem, unlike the broad `file` counter. Discount only
# clean LRU cache above a retained floor. Missing/malformed statistics fail
# closed to the raw charge.
_genesis_mem_working_set_from() {
    local current="${1:-}" stat_path="${2:-}"
    # Bounded in place rather than through _genesis_uint_bounded: the test suite
    # extracts THIS FUNCTION ALONE and sources it in isolation, so it cannot
    # reach the shared validator. Both call sites bound `current` before calling,
    # making this the local belt to their braces.
    [[ "$current" =~ ^(0|[1-9][0-9]*)$ ]] || return 1
    [ "${#current}" -le "${_GENESIS_MEM_MAX_DIGITS:-18}" ] || return 1
    [ -r "$stat_path" ] || { printf '%s' "$current"; return; }

    local fields inactive active dirty writeback v1_inactive v1_active v1_dirty v1_writeback reserve reclaimable discount
    fields="$(awk '
        $1 == "inactive_file" { inactive = $2 }
        $1 == "active_file" { active = $2 }
        $1 == "file_dirty" { dirty = $2 }
        $1 == "file_writeback" { writeback = $2 }
        $1 == "total_inactive_file" { v1_inactive = $2 }
        $1 == "total_active_file" { v1_active = $2 }
        $1 == "total_dirty" { v1_dirty = $2 }
        $1 == "total_writeback" { v1_writeback = $2 }
        END { printf "%s|%s|%s|%s|%s|%s|%s|%s", inactive, active, dirty, writeback, v1_inactive, v1_active, v1_dirty, v1_writeback }
    ' "$stat_path" 2>/dev/null)" || { printf '%s' "$current"; return; }
    IFS='|' read -r inactive active dirty writeback v1_inactive v1_active v1_dirty v1_writeback <<< "$fields"
    # A v1 memory.stat includes both local unprefixed counters and hierarchical
    # total_* counters. memory.usage_in_bytes is hierarchical too, so choose
    # the matching total_* schema before considering a v2 schema.
    if [[ "$v1_inactive" =~ ^[0-9]+$ && "$v1_active" =~ ^[0-9]+$ && "$v1_dirty" =~ ^[0-9]+$ && "$v1_writeback" =~ ^[0-9]+$ ]]; then
        inactive="$v1_inactive"; active="$v1_active"; dirty="$v1_dirty"; writeback="$v1_writeback"
    elif [[ "$inactive" =~ ^[0-9]+$ && "$active" =~ ^[0-9]+$ && "$dirty" =~ ^[0-9]+$ && "$writeback" =~ ^[0-9]+$ ]]; then
        :  # cgroup v2 schema
    else
        printf '%s' "$current"
        return
    fi
    for fields in "$inactive" "$active" "$dirty" "$writeback"; do
        [[ "$fields" =~ ^(0|[1-9][0-9]*)$ ]] \
            && [ "${#fields}" -le "${_GENESIS_MEM_MAX_DIGITS:-18}" ] \
            || { printf '%s' "$current"; return; }
    done

    reserve="${CODE_INTEL_FILE_CACHE_RESERVE_BYTES:-$(( 2 * 1024 * 1024 * 1024 ))}"
    [[ "$reserve" =~ ^(0|[1-9][0-9]*)$ ]] \
        || { printf '%s' "$current"; return; }
    [ "${#reserve}" -le "${_GENESIS_MEM_MAX_DIGITS:-18}" ] \
        || { printf '%s' "$current"; return; }
    reclaimable=$(( inactive + active ))
    [ "$(( dirty + writeback ))" -lt "$reclaimable" ] \
        || { printf '%s' "$current"; return; }
    reclaimable=$(( reclaimable - dirty - writeback ))
    [ "$reclaimable" -gt "$reserve" ] || { printf '%s' "$current"; return; }
    discount=$(( reclaimable - reserve ))
    [ "$discount" -lt "$current" ] || { printf '%s' "$current"; return; }
    printf '%s' "$(( current - discount ))"
}

# What everything on this box is using RIGHT NOW, job included — the fixed
# reserve below is a floor for a machine whose live usage cannot be read. A
# container where Genesis, Qdrant and the sessions already exceed that floor
# would otherwise get a cap computed as if the headroom were free, and the
# parent cgroup takes the kill anyway. cgroup v2 first, then v1, then
# MemTotal-MemAvailable. Unreadable means the caller falls back to the floor.
_genesis_mem_current() {
    if [ -n "${CODE_INTEL_MEM_CURRENT_BYTES:-}" ]; then
        _genesis_uint_bounded "$CODE_INTEL_MEM_CURRENT_BYTES" || return 1
        printf '%s' "$CODE_INTEL_MEM_CURRENT_BYTES"
        return
    fi
    local raw="" stat_path=""
    if [ -n "${CODE_INTEL_MEM_RAW_CURRENT_BYTES:-}" ]; then
        raw="$CODE_INTEL_MEM_RAW_CURRENT_BYTES"
        stat_path="${CODE_INTEL_MEM_STAT_PATH:-/sys/fs/cgroup/memory.stat}"
    elif [ -r /sys/fs/cgroup/memory.current ]; then
        raw="$(cat /sys/fs/cgroup/memory.current 2>/dev/null)"
        stat_path="${CODE_INTEL_MEM_STAT_PATH:-/sys/fs/cgroup/memory.stat}"
    elif [ -r /sys/fs/cgroup/memory/memory.usage_in_bytes ]; then
        raw="$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null)"
        stat_path="${CODE_INTEL_MEM_STAT_PATH:-/sys/fs/cgroup/memory/memory.stat}"
    fi
    if [ -n "$raw" ] && _genesis_uint_bounded "$raw" && [ "$raw" -gt 0 ] 2>/dev/null; then
        _genesis_mem_working_set_from "$raw" "$stat_path"
        return
    fi
    local total_kb avail_kb meminfo
    meminfo="${CODE_INTEL_MEMINFO:-/proc/meminfo}"
    total_kb="$(awk '/^MemTotal:/ {print $2}' "$meminfo" 2>/dev/null)"
    avail_kb="$(awk '/^MemAvailable:/ {print $2}' "$meminfo" 2>/dev/null)"
    _genesis_uint_bounded "$total_kb" "$_GENESIS_MEM_MAX_KB_DIGITS" \
        && _genesis_uint_bounded "$avail_kb" "$_GENESIS_MEM_MAX_KB_DIGITS" \
        && [ "$total_kb" -gt "$avail_kb" ] \
        && printf '%s' "$(( (total_kb - avail_kb) * 1024 ))"
}

#: Left for everything that is NOT this job -- Genesis, Qdrant, the session that
#: launched it. Below this the box is not able to host a rebuild safely.
CODE_INTEL_SIBLING_RESERVE_BYTES="${CODE_INTEL_SIBLING_RESERVE_BYTES:-$(( 2 * 1024 * 1024 * 1024 ))}"
#: MEASURED 2026-09-16: a forced full rebuild peaked at 4,874,166,272 bytes
#: (4.54 GiB). A cap below the working set does not protect anything, it just
#: relocates the kill, so refuse instead of pretending.
CODE_INTEL_GITNEXUS_MIN_BYTES="${CODE_INTEL_GITNEXUS_MIN_BYTES:-$(( 4874166272 ))}"
# MEASURED 2026-09-09: Codebase Memory's clean fast index peaked at roughly
# 2,836 MiB RSS. MemoryMax also charges file cache, kernel memory, and the
# supervising shell, so the safe floor adds a non-RSS allowance to the measured
# workload instead of treating the RSS peak as a sufficient cgroup ceiling.
CODE_INTEL_CBM_WORKLOAD_CHARGE_BYTES="${CODE_INTEL_CBM_WORKLOAD_CHARGE_BYTES:-$(( 128 * 1024 * 1024 ))}"
# Validated HERE rather than in the chain below, because this value is an
# OPERAND of the very next line's sum: a bound applied afterwards inspects a
# result that has already wrapped. MEASURED during review of the first version
# of this fix, which did exactly that — a crafted charge drove the admission
# floor to 98,305 bytes instead of 2.9 GiB with no refusal raised, re-opening
# the wrap-into-admission hole this change exists to close. The value is reset
# to the default so the sum below stays computable; the refusal is carried in
# a separate variable and raised by the chain.
_GENESIS_CHARGE_REFUSE=""
if ! _genesis_uint_bounded "$CODE_INTEL_CBM_WORKLOAD_CHARGE_BYTES"; then
    _GENESIS_CHARGE_REFUSE="CODE_INTEL_CBM_WORKLOAD_CHARGE_BYTES is not a nonnegative integer below 10^$_GENESIS_MEM_MAX_DIGITS"
    CODE_INTEL_CBM_WORKLOAD_CHARGE_BYTES=$(( 128 * 1024 * 1024 ))
fi
CODE_INTEL_CBM_MIN_BYTES="${CODE_INTEL_CBM_MIN_BYTES:-$(( 2836 * 1024 * 1024 + CODE_INTEL_CBM_WORKLOAD_CHARGE_BYTES ))}"

# THREE refusal scopes, not one. A refusal must reach exactly the legs whose
# arithmetic the bad value feeds. An earlier revision of this change put every
# constant in the shared string, which meant a malformed CBM-only constant
# refused GitNexus (and vice versa) — a leg with a valid cap and real headroom
# was skipped because of a variable it never reads.
#
# SHARED holds only what BOTH legs consume: the ceiling override, live usage,
# and the sibling reserve.
_genesis_bad_uint() {  # name -> refusal message, or nothing
    printf '%s is not a nonnegative integer below 10^%s' "$1" "$_GENESIS_MEM_MAX_DIGITS"
}

GENESIS_MEM_ENV_REFUSE=""
if [ -n "${CODE_INTEL_MEM_CEILING_BYTES:-}" ] \
    && ! _genesis_uint_bounded "$CODE_INTEL_MEM_CEILING_BYTES"; then
    # The ceiling override belongs in the SHARED chain: refusing it only on the
    # cbm leg left the gitnexus leg unable to tell "refused" from "no ceiling
    # discoverable" — both are an empty string there — so it skipped admission
    # entirely, a refusal path failing open on one of two legs.
    GENESIS_MEM_ENV_REFUSE="$(_genesis_bad_uint CODE_INTEL_MEM_CEILING_BYTES)"
elif [ -n "${CODE_INTEL_MEM_CURRENT_BYTES:-}" ] \
    && ! _genesis_uint_bounded "$CODE_INTEL_MEM_CURRENT_BYTES"; then
    GENESIS_MEM_ENV_REFUSE="$(_genesis_bad_uint CODE_INTEL_MEM_CURRENT_BYTES)"
elif ! _genesis_uint_bounded "$CODE_INTEL_SIBLING_RESERVE_BYTES"; then
    GENESIS_MEM_ENV_REFUSE="$(_genesis_bad_uint CODE_INTEL_SIBLING_RESERVE_BYTES)"
fi

# CBM-only. The workload charge is an operand of the minimum-bytes sum above,
# so its refusal was captured before that sum was computed.
GENESIS_CBM_ENV_REFUSE=""
if [ -n "$_GENESIS_CHARGE_REFUSE" ]; then
    GENESIS_CBM_ENV_REFUSE="$_GENESIS_CHARGE_REFUSE"
elif ! _genesis_uint_bounded "$CODE_INTEL_CBM_MIN_BYTES"; then
    GENESIS_CBM_ENV_REFUSE="$(_genesis_bad_uint CODE_INTEL_CBM_MIN_BYTES)"
fi

# GitNexus-only.
GENESIS_GITNEXUS_ENV_REFUSE=""
if ! _genesis_uint_bounded "$CODE_INTEL_GITNEXUS_MIN_BYTES"; then
    GENESIS_GITNEXUS_ENV_REFUSE="$(_genesis_bad_uint CODE_INTEL_GITNEXUS_MIN_BYTES)"
fi
_genesis_ceiling_b="$(_genesis_mem_ceiling)"
_genesis_want_b="$(_genesis_mem_bytes "$GITNEXUS_MEM_MAX")"
GITNEXUS_MEM_REFUSE=""
if [ -n "$GENESIS_MEM_ENV_REFUSE" ]; then
    GITNEXUS_MEM_REFUSE="$GENESIS_MEM_ENV_REFUSE"
elif [ -n "$GENESIS_GITNEXUS_ENV_REFUSE" ]; then
    GITNEXUS_MEM_REFUSE="$GENESIS_GITNEXUS_ENV_REFUSE"
elif [ -z "$_genesis_want_b" ]; then
    # Fail closed: an unparseable cap must not reach MemoryMax, and skipping the
    # admission check silently would run the job unbounded.
    GITNEXUS_MEM_REFUSE="CODE_INTEL_GITNEXUS_MEMORY_MAX='${GITNEXUS_MEM_MAX}' is not a parseable memory value — refusing rather than running unbounded"
elif [ "$_genesis_want_b" -lt "$CODE_INTEL_GITNEXUS_MIN_BYTES" ]; then
    # A configured cap below the measured working set cannot bite: on a large
    # host the rebuild would still run and be killed by its own cgroup, which
    # reads as a flaky index failure instead of the refusal it should be.
    GITNEXUS_MEM_REFUSE="configured cap ${GITNEXUS_MEM_MAX} is below the $(( CODE_INTEL_GITNEXUS_MIN_BYTES / 1024 / 1024 ))M a measured full rebuild needs — a cap that cannot bite only relocates the kill"
elif [ -n "$_genesis_ceiling_b" ]; then
    # Live usage plus the reserve as growth headroom when the kernel can tell
    # us the real figure; the reserve alone when it cannot.
    _genesis_live_b="$(_genesis_mem_current)"
    _genesis_siblings_b="$CODE_INTEL_SIBLING_RESERVE_BYTES"
    if [ -n "$_genesis_live_b" ]; then
        _genesis_siblings_b=$(( _genesis_live_b + CODE_INTEL_SIBLING_RESERVE_BYTES ))
    fi
    _genesis_spare_b=$(( _genesis_ceiling_b - _genesis_siblings_b ))
    if [ "$_genesis_spare_b" -lt "$CODE_INTEL_GITNEXUS_MIN_BYTES" ]; then
        GITNEXUS_MEM_REFUSE="this install has $(( _genesis_ceiling_b / 1024 / 1024 ))M total; live usage and the reserve claim $(( _genesis_siblings_b / 1024 / 1024 ))M of it, leaving $(( _genesis_spare_b / 1024 / 1024 ))M, below the $(( CODE_INTEL_GITNEXUS_MIN_BYTES / 1024 / 1024 ))M a measured full rebuild needs"
    elif [ "$_genesis_spare_b" -lt "$_genesis_want_b" ]; then
        # Bytes, not a rounded-down MiB: admission already proved this exact
        # spare is safe, and rounding it can cross below the measured working
        # set only to admit a cap that cannot bite. systemd accepts integer
        # byte counts and the rlimit fallback parses them.
        GITNEXUS_MEM_MAX="$_genesis_spare_b"
    fi
fi
IO_WEIGHT="${CODE_INTEL_INDEX_IO_WEIGHT:-20}"
CPU_QUOTA="${CODE_INTEL_INDEX_CPU_QUOTA:-200%}"
PERSISTENCE="${CODE_INTEL_INDEX_PERSISTENCE:-true}"

# The kill-switch path resolves through the ONE shared site (same override
# semantics the launcher enforces). An override that is relative, or begins
# with a ~/ that no HOME can expand, would make `-e` silently read as
# "not disabled" — an UNRESOLVABLE path instead refuses the cbm leg below,
# never indexing a tool the machine may have switched off.
CBM_DISABLE_FILE=""
CBM_DISABLE_UNRESOLVED=1
# %/* not dirname(1): minimal-PATH invocations (stripped-env services) may not
# have dirname, and a resolver that cannot be found fails the leg closed.
_cbm_disable_lib="${BASH_SOURCE[0]%/*}/cbm_disable_file.sh"
[ "$_cbm_disable_lib" = "${BASH_SOURCE[0]}/cbm_disable_file.sh" ] \
    && _cbm_disable_lib="./cbm_disable_file.sh"
if [ -r "$_cbm_disable_lib" ]; then
    # shellcheck source=cbm_disable_file.sh
    . "$_cbm_disable_lib"
    if declare -F genesis_cbm_disable_file >/dev/null \
        && CBM_DISABLE_FILE="$(genesis_cbm_disable_file 2>/dev/null)"; then
        CBM_DISABLE_UNRESOLVED=""
    fi
fi

_GITNEXUS_PIN_READY=0
_gitnexus_pin_file="$(dirname "${BASH_SOURCE[0]}")/gitnexus_version.sh"
if [ -r "$_gitnexus_pin_file" ]; then
    # shellcheck source=gitnexus_version.sh
    if . "$_gitnexus_pin_file"; then
        if [[ "${GENESIS_GITNEXUS_VERSION:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
            && declare -F genesis_gitnexus_node_supported >/dev/null \
            && declare -F genesis_gitnexus_resolve_binary >/dev/null \
            && declare -F genesis_gitnexus_installed_version >/dev/null \
            && declare -F genesis_gitnexus_installed_is_pinned >/dev/null; then
            _GITNEXUS_PIN_READY=1
        fi
    fi
fi

_log() { printf '[code-intel-index] %s\n' "$*"; }

# Shared load/iowait sampler for the pressure watchdog. Best-effort: if it's
# missing (older checkout), the watchdog degrades to a wall-clock cap only.
_PROC_PRESSURE="$(dirname "${BASH_SOURCE[0]}")/proc_pressure.sh"
# shellcheck source=proc_pressure.sh
[ -f "$_PROC_PRESSURE" ] && . "$_PROC_PRESSURE"

# Pressure-watchdog knobs (env-overridable for tests). The watchdog is the ONLY
# I/O throttle that works on this host: ionice is inert (disk scheduler none)
# and IOWeight is inert in user scopes (no io controller delegated), so a full
# index otherwise reads at the container's entire throttle and storms it. It
# pauses the index (cgroup freeze, or SIGSTOP on the fallback path) under
# pressure and kills a run that can never make headway.
_WD_INTERVAL="${CODE_INTEL_WATCHDOG_INTERVAL:-15}"        # steady-state sample gap (s)
_WD_WARMUP_INTERVAL="${CODE_INTEL_WATCHDOG_WARMUP_INTERVAL:-5}"  # tighter during the burst window
_WD_WARMUP_S="${CODE_INTEL_WATCHDOG_WARMUP_S:-30}"        # freeze-on-1-bad for the first N s
_WD_LOAD_MAX="${CODE_INTEL_WATCHDOG_LOAD_MAX:-4}"         # loadavg1 above this = pressure
_WD_IOWAIT_MAX="${CODE_INTEL_WATCHDOG_IOWAIT_MAX:-25}"    # iowait% above this = pressure
_WD_BAD_SAMPLES="${CODE_INTEL_WATCHDOG_BAD_SAMPLES:-2}"   # consecutive bad samples before pause
_WD_CONT_PAUSE_MAX="${CODE_INTEL_WATCHDOG_CONT_PAUSE_MAX:-600}"  # kill if paused this long CONTINUOUSLY
_WD_WALL_FAST="${CODE_INTEL_WATCHDOG_WALL_FAST:-3600}"    # wall cap for fast/moderate (s)
_WD_WALL_FULL="${CODE_INTEL_WATCHDOG_WALL_FULL:-14400}"   # wall cap for full (s) — cbm can't resume

if [ "${CODE_INTEL_INDEX_DISABLE:-0}" = "1" ]; then
    _log "disabled via CODE_INTEL_INDEX_DISABLE — skipping"
    exit 0
fi

if [ -z "$REPO_PATH" ] || [ ! -d "$REPO_PATH" ]; then
    _log "ERROR: repo path missing or not a directory: '$REPO_PATH'"
    exit 1
fi
case "$TOOLS" in cbm|gitnexus|both) ;; *)
    _log "ERROR: tool must be cbm|gitnexus|both, got '$TOOLS'"; exit 1 ;;
esac
case "$MODE" in fast|moderate|full) ;; *)
    _log "ERROR: mode must be fast|moderate|full, got '$MODE'"; exit 1 ;;
esac

# Physical path (-P): the single-flight lock is keyed on this, and a symlinked
# spelling of the same repo must not get a second lock (= second concurrent index).
REPO_PATH="$(unset CDPATH; cd "$REPO_PATH" && pwd -P)"

# ── 1. Worktree skip ────────────────────────────────────────────────────
# In a linked worktree, <root>/.git is a FILE (gitdir pointer), not a dir.
if [ -f "$REPO_PATH/.git" ]; then
    _log "skip: $REPO_PATH is a linked git worktree (never indexed — use Serena there)"
    exit 0
fi

# ── 2. Single-flight lock (per repo path) ───────────────────────────────
LOCK_DIR="${GENESIS_HOME:-$HOME/.genesis}/locks"
mkdir -p "$LOCK_DIR" 2>/dev/null || LOCK_DIR="${TMPDIR:-/tmp}"
LOCK_FILE="$LOCK_DIR/code-intel-$(printf '%s' "$REPO_PATH" | sha1sum | cut -c1-16).lock"

# Take the lock only when flock AND a writable lock file are both available.
# If either is missing, proceed UNLOCKED with a warning — a missing lock tool
# must degrade to "no dedup", never to "silently skip indexing" (a bare
# `flock -n 9` failure is indistinguishable from "lock held" otherwise).
if command -v flock >/dev/null 2>&1 && { exec 9>"$LOCK_FILE"; } 2>/dev/null; then
    if ! flock -n 9; then
        # Lock held: either a concurrent index, or the managed genesis-code-intel-freeze
        # user unit (scripts/code_intel_freeze.sh) holding THIS flock as a kill-switch.
        # Default rc 0 (back-compat); the runner sets CODE_INTEL_INDEX_LOCK_SKIP_RC=75
        # so it can tell "frozen — keep the marker" apart from a real success. The lock
        # ACQUISITION above is byte-unchanged, so the freeze keeps neutralizing every
        # trigger regardless.
        _log "skip: an index for $REPO_PATH is already running (lock held)"
        exit "${CODE_INTEL_INDEX_LOCK_SKIP_RC:-0}"
    fi
else
    _log "WARNING: flock or lock file unavailable ($LOCK_FILE) — proceeding UNLOCKED"
fi

# ── 3. Resource-capped runner ───────────────────────────────────────────
# Probe systemd-run exactly like .claude/mcp/run-codebase-memory does: the
# probe must create a real scope, because CC-spawned / hook-spawned contexts
# sometimes cannot reach the user manager even when systemd-run exists.
_GN_SCOPE_OK=0
_CBM_SCOPE_OK=0
_probe_scope() {
    local -a slice_args=()
    [ "$2" = "cbm" ] && slice_args=(--slice-inherit)
    /usr/bin/systemd-run --user --scope "${slice_args[@]}" --quiet \
        -p "MemoryMax=$1" -p "MemorySwapMax=0" \
        -p "IOWeight=${IO_WEIGHT}" -p "CPUQuota=${CPU_QUOTA}" \
        -- /bin/true 2>/dev/null
}
if [ -x /usr/bin/systemd-run ]; then
    if [ "$TOOLS" = "gitnexus" ] || [ "$TOOLS" = "both" ]; then
        _probe_scope "$GITNEXUS_MEM_MAX" gitnexus && _GN_SCOPE_OK=1
    fi
    if [ "$TOOLS" = "cbm" ] || [ "$TOOLS" = "both" ]; then
        _probe_scope "$CBM_MEM_MAX" cbm && _CBM_SCOPE_OK=1
    fi
fi

_run_capped() {
    if [ "$_SCOPE_OK" = "1" ]; then
        local -a slice_args=()
        [ "${_CI_SCOPE_INHERIT:-0}" = "1" ] && slice_args=(--slice-inherit)
        # _CI_SCOPE_UNIT (set by _run_with_watchdog) gives the scope a
        # deterministic name so the watchdog can freeze/thaw/stop it by unit.
        /usr/bin/systemd-run --user --scope "${slice_args[@]}" --quiet \
            ${_CI_SCOPE_UNIT:+--unit="$_CI_SCOPE_UNIT"} \
            -p "MemoryMax=${MEM_MAX}" -p "MemorySwapMax=0" \
            -p "IOWeight=${IO_WEIGHT}" -p "CPUQuota=${CPU_QUOTA}" \
            --description "code-intel index: $REPO_PATH" \
            -- /usr/bin/env \
                "CODE_INTEL_CHILD_CAP_BYTES=${CODE_INTEL_CHILD_ADMIT_CAP_BYTES:-}" \
                "CODE_INTEL_CHILD_RESERVE_BYTES=$CODE_INTEL_SIBLING_RESERVE_BYTES" \
                "CODE_INTEL_CHILD_SCOPE_UNIT=${_CI_SCOPE_UNIT:-}" \
                "CODE_INTEL_CHILD_REFUSAL_MARKER=${CODE_INTEL_CHILD_REFUSAL_MARKER:-}" \
                /bin/bash "$_CODE_INTEL_ENTRYPOINT" --exec-indexer-with-oom-adj "$@"
    else
        # Fallback: polite scheduling + soft address-space cap. Mirrors the
        # run-codebase-memory launcher's degradation (never block on missing
        # systemd — CI and minimal containers must still work).
        local mem_kb="" mem_b=""
        mem_b="$(_genesis_mem_bytes "$MEM_MAX" 2>/dev/null || true)"
        if [ -n "$mem_b" ]; then
            mem_kb=$(( (mem_b + 1023) / 1024 ))
        else
            _log "WARNING: cannot parse '$MEM_MAX' for the rlimit fallback — running memory-uncapped (nice/ionice only)"
        fi
        (
            [ -n "$mem_kb" ] && ulimit -v "$mem_kb" 2>/dev/null
            if command -v ionice >/dev/null 2>&1; then
                exec nice -n 19 ionice -c 3 \
                    /bin/bash "$_CODE_INTEL_ENTRYPOINT" --exec-indexer-with-oom-adj "$@"
            else
                exec nice -n 19 \
                    /bin/bash "$_CODE_INTEL_ENTRYPOINT" --exec-indexer-with-oom-adj "$@"
            fi
        )
    fi
}

# ── Pressure watchdog ────────────────────────────────────────────────────
_wall_cap() { case "$MODE" in full) printf '%s' "$_WD_WALL_FULL" ;; *) printf '%s' "$_WD_WALL_FAST" ;; esac; }

# Pause / resume / kill primitives. kind=scope -> cgroup freeze the named scope
# (stops ALL descendants, escape-proof, the only working throttle here);
# kind=pgid -> SIGSTOP/CONT/KILL the isolated process group; kind=nosig ->
# can't pause, only wall-cap kill of the single job pid.
_wd_pause()  { case "$1" in scope) systemctl --user freeze "$2.scope" 2>/dev/null ;; pgid) kill -STOP -- "-$2" 2>/dev/null ;; esac; }
_wd_resume() { case "$1" in scope) systemctl --user thaw   "$2.scope" 2>/dev/null ;; pgid) kill -CONT -- "-$2" 2>/dev/null ;; esac; }
_wd_kill()   {
    case "$1" in
        scope) systemctl --user stop "$2.scope" 2>/dev/null ;;
        pgid)  kill -CONT -- "-$2" 2>/dev/null; kill -KILL -- "-$2" 2>/dev/null ;;
        *)     kill -KILL "$3" 2>/dev/null ;;
    esac
}

# _watchdog <kind> <target> <job_pid>: babysit a running index; pause under
# pressure, resume when calm, kill a run that can't make headway.
_watchdog() {
    local kind="$1" target="$2" job_pid="$3"
    local wall start now bad=0 paused=0 pause_start=0
    wall="$(_wall_cap)"; start="$(date +%s)"
    local have_sampler=1
    command -v pressure_loadavg1 >/dev/null 2>&1 || have_sampler=0
    while kill -0 "$job_pid" 2>/dev/null; do
        now="$(date +%s)"
        if [ "$(( now - start ))" -ge "$wall" ]; then
            _log "watchdog: wall cap ${wall}s reached (mode=$MODE) — killing index"
            _wd_kill "$kind" "$target" "$job_pid"; return 0
        fi
        if [ "$paused" = 1 ] && [ "$(( now - pause_start ))" -ge "$_WD_CONT_PAUSE_MAX" ]; then
            _log "watchdog: paused ${_WD_CONT_PAUSE_MAX}s continuously (system never calmed) — killing index"
            _wd_kill "$kind" "$target" "$job_pid"; return 0
        fi
        local interval="$_WD_INTERVAL" need="$_WD_BAD_SAMPLES"
        if [ "$(( now - start ))" -lt "$_WD_WARMUP_S" ]; then
            interval="$_WD_WARMUP_INTERVAL"; need=1   # burst window: freeze on first bad sample
        fi
        if [ "$have_sampler" = 1 ] && [ "$kind" != "nosig" ]; then
            local load iow
            load="$(pressure_loadavg1)"; iow="$(pressure_iowait_pct)"
            if pressure_gt "$load" "$_WD_LOAD_MAX" || pressure_gt "$iow" "$_WD_IOWAIT_MAX"; then
                bad="$(( bad + 1 ))"
                if [ "$bad" -ge "$need" ] && [ "$paused" = 0 ]; then
                    _log "watchdog: pressure (load=$load iowait=$iow%) — pausing index"
                    _wd_pause "$kind" "$target"; paused=1; pause_start="$now"
                fi
            else
                bad=0
                if [ "$paused" = 1 ]; then
                    _log "watchdog: calm (load=$load iowait=$iow%) — resuming index"
                    _wd_resume "$kind" "$target"; paused=0
                fi
            fi
        fi
        sleep "$interval"
    done
    return 0
}

# _run_with_watchdog <tool_label> <command...>: run one tool under the watchdog.
_run_with_watchdog() {
    local label="$1"; shift
    local _SCOPE_OK="$_GN_SCOPE_OK"
    local scope_inherit=0
    [ "$label" = "cbm" ] && _SCOPE_OK="$_CBM_SCOPE_OK"
    [ "$label" = "cbm" ] && scope_inherit=1
    if [ "$_SCOPE_OK" = "1" ]; then
        local unit; unit="code-intel-$(printf '%s' "$REPO_PATH" | sha1sum | cut -c1-12)-${label}-$$"
        _CI_SCOPE_UNIT="$unit" _CI_SCOPE_INHERIT="$scope_inherit" _run_capped "$@" &
        local job_pid=$!
        _watchdog scope "$unit" "$job_pid"
        wait "$job_pid"; return $?
    fi
    # Fallback (no systemd scope): isolate a process group so SIGSTOP/KILL can
    # target the whole tool subtree — NEVER our own group (that would freeze the
    # watchdog itself and never thaw). If job control didn't isolate it, degrade
    # to wall-cap-only (kind=nosig) rather than risk signalling ourselves.
    set -m 2>/dev/null || true
    _run_capped "$@" &
    local job_pid=$!
    set +m 2>/dev/null || true
    local pgid self_pgid
    pgid="$(ps -o pgid= -p "$job_pid" 2>/dev/null | tr -d ' ')"
    self_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"
    if [[ "$pgid" =~ ^[0-9]+$ ]] && [ "$pgid" -gt 1 ] && [ "$pgid" != "$self_pgid" ]; then
        _watchdog pgid "$pgid" "$job_pid"
    else
        _log "watchdog: could not isolate a process group (pgid='$pgid' self='$self_pgid') — wall-cap only"
        _watchdog nosig "" "$job_pid"
    fi
    wait "$job_pid"; return $?
}

RC=0
MISSING=""  # requested-but-absent tools — makes a no-op run rc=3, not a false success
CBM_RAN=0
GN_RAN=0

# A tool's RAW exit status must never reach the runner: the runner reads
# 3/4/5/75 as OUTCOME PROTOCOL codes (missing / leg-incomplete / lock-held),
# so a leg that happens to fail with one of them is silently misclassified —
# rc 4 after a cbm failure would CONSUME the failed leg's request and even
# stamp the shared full clock. Remap those statuses to 111 at capture; the
# classification below then sees a genuine "ran and failed" either way.
_leg_failed() {
    RC=$?
    case "$RC" in
        3|4|5|75) RC=111 ;;
    esac
}

if [ "$TOOLS" = "cbm" ] || [ "$TOOLS" = "both" ]; then
    if [ -n "$CBM_DISABLE_UNRESOLVED" ]; then
        _log "cbm kill-switch path unresolvable — refusing cbm leg (fail closed)"
        MISSING="${MISSING}cbm "
    elif [ -e "$CBM_DISABLE_FILE" ]; then
        _log "codebase-memory-mcp disabled by $CBM_DISABLE_FILE — skipped"
        MISSING="${MISSING}cbm "
    elif command -v codebase-memory-mcp >/dev/null 2>&1; then
        CBM_MEM_REFUSE=""
        _cbm_want_b="$(_genesis_mem_bytes "$CBM_MEM_MAX")"
        if [ -n "$GENESIS_CBM_ENV_REFUSE" ]; then
            CBM_MEM_REFUSE="$GENESIS_CBM_ENV_REFUSE"
        elif [ -z "$_cbm_want_b" ]; then
            CBM_MEM_REFUSE="CODE_INTEL_CBM_MEMORY_MAX='${CBM_MEM_MAX}' is not a parseable memory value"
        elif [ "$_cbm_want_b" -lt "$CODE_INTEL_CBM_MIN_BYTES" ]; then
            CBM_MEM_REFUSE="configured cap ${CBM_MEM_MAX} is below the measured $(( CODE_INTEL_CBM_MIN_BYTES / 1024 / 1024 ))M Codebase Memory workload"
        fi

        if [ -z "$CBM_MEM_REFUSE" ] && [ "$_CBM_SCOPE_OK" != "1" ]; then
            CBM_MEM_REFUSE="cannot establish a bounded systemd user scope"
        fi
        if [ -n "$CBM_MEM_REFUSE" ]; then
            _log "SKIP cbm: $CBM_MEM_REFUSE"
            _log "      verify the requested cap, systemd scope and available headroom"
            MISSING="${MISSING:+$MISSING }cbm"
        else
            _log "indexing (codebase-memory-mcp, mode=$MODE): $REPO_PATH"
            # Flag form (cbm >=0.9): --mode selects the pipeline depth (default here is
            # fast — no similarity/semantic edges); --persistence writes the shareable
            # .codebase-memory/graph.db.zst artifact so a wiped cache restores from it
            # instead of a full 0->100 re-index.
            _cbm_refusal_marker="$(mktemp "$LOCK_DIR/cbm-admission.XXXXXXXX" 2>/dev/null)"
            if [ -z "$_cbm_refusal_marker" ]; then
                _log "SKIP cbm: cannot create admission outcome marker"
                MISSING="${MISSING:+$MISSING }cbm"
            else
                if MEM_MAX="$CBM_MEM_MAX" CODE_INTEL_CHILD_ADMIT_CAP_BYTES="$_cbm_want_b" \
                    CODE_INTEL_CHILD_REFUSAL_MARKER="$_cbm_refusal_marker" \
                    _run_with_watchdog cbm codebase-memory-mcp cli index_repository \
                    --repo-path "$REPO_PATH" --mode "$MODE" --persistence "$PERSISTENCE"; then
                    CBM_RAN=1
                else
                    _cbm_run_rc=$?
                    if [ -s "$_cbm_refusal_marker" ]; then
                        _log "SKIP cbm: scope admission refused before indexing"
                        MISSING="${MISSING:+$MISSING }cbm"
                    else
                        RC=$_cbm_run_rc
                        case "$RC" in 3|4|5|75) RC=111 ;; esac
                    fi
                fi
                rm -f -- "$_cbm_refusal_marker"
            fi
        fi
    else
        _log "codebase-memory-mcp not on PATH — skipped"
        MISSING="${MISSING}cbm "
    fi
fi

if [ "$TOOLS" = "gitnexus" ] || [ "$TOOLS" = "both" ]; then
    _GN=""
    if [ "$_GITNEXUS_PIN_READY" -ne 1 ]; then
        _log "GitNexus pin metadata unavailable — refusing dynamic resolver"
    elif ! genesis_gitnexus_node_supported; then
        _log "GitNexus ${GENESIS_GITNEXUS_VERSION} does not support Node $(node --version 2>/dev/null || echo unavailable) — refusing index"
    elif genesis_gitnexus_installed_is_pinned; then
        _GN="$(genesis_gitnexus_resolve_binary)"
    elif genesis_gitnexus_resolve_binary >/dev/null; then
        _log "GitNexus version $(genesis_gitnexus_installed_version 2>/dev/null || echo unknown) does not match expected pinned ${GENESIS_GITNEXUS_VERSION} — refusing index"
    fi
    if [ -n "$_GN" ]; then
        # gitnexus analyze is already incremental (only -f forces a full re-parse)
        # and quiet by default (-v opts into verbose), so no mode plumbing here.
        # NOTE: the `--quiet` flag added in #910 does NOT exist in gitnexus 1.6.x
        # ("error: unknown option '--quiet'" -> rc 1 on EVERY run); it silently
        # broke every entrypoint-driven gitnexus index since #910. Dropped.
        _log "indexing (gitnexus analyze): $REPO_PATH"
        if [ -n "$GITNEXUS_MEM_REFUSE" ]; then
            # REFUSED, not silently skipped: a cap that cannot bite is worse
            # than no job, because the parent cgroup takes the kill instead.
            _log "SKIP gitnexus: $GITNEXUS_MEM_REFUSE"
            _log "      raise CODE_INTEL_GITNEXUS_MEMORY_MAX / lower CODE_INTEL_SIBLING_RESERVE_BYTES to override"
            MISSING="${MISSING:+$MISSING }gitnexus"
        else
            ( cd "$REPO_PATH" && MEM_MAX="$GITNEXUS_MEM_MAX" _run_with_watchdog gitnexus "$_GN" analyze ) \
                && GN_RAN=1 || _leg_failed
        fi
    else
        _log "gitnexus not available — skipped"
        MISSING="${MISSING}gitnexus "
    fi
fi

# B1: a requested tool absent from PATH means NOTHING was indexed for it. Never
# report that as success (rc 0) — the idle runner would consume the marker and
# stamp a fresh full-index timestamp, silently disabling indexing until someone
# notices the graph is stale. Per-leg outcome codes so a completed leg is
# consumable and a skipped/refused leg never stamps cbm's shared full clock:
#   rc 3: nothing indexed — at least one requested tool missing or refused
#   rc 4: cbm leg completed, gitnexus leg did not (missing, refused, or failed)
#   rc 5: gitnexus leg completed, cbm leg did not (missing, skipped, or failed)
# A leg that ran AND FAILED still counts as "did not complete": when the other
# leg succeeded, reporting the raw failure rc makes the runner restore the
# combined marker and rebuild the completed leg on every retry.
_cbm_wanted=0; _gn_wanted=0
{ [ "$TOOLS" = "cbm" ] || [ "$TOOLS" = "both" ]; } && _cbm_wanted=1
{ [ "$TOOLS" = "gitnexus" ] || [ "$TOOLS" = "both" ]; } && _gn_wanted=1
if [ "$RC" = "0" ] && [ -n "$MISSING" ] && [ "$CBM_RAN" != "1" ] && [ "$GN_RAN" != "1" ]; then
    _log "ERROR: requested tool(s) missing or refused: ${MISSING%% } — nothing indexed (rc=3)"
    RC=3
elif [ "$_gn_wanted" = "1" ] && [ "$GN_RAN" != "1" ] && [ "$CBM_RAN" = "1" ]; then
    _log "cbm leg done; gitnexus leg did not complete (${MISSING:-failed rc=$RC}) — partial (rc=4)"
    RC=4
elif [ "$_cbm_wanted" = "1" ] && [ "$CBM_RAN" != "1" ] && [ "$GN_RAN" = "1" ]; then
    _log "gitnexus leg done; cbm leg did not complete — partial, cbm full clock NOT stamped (rc=5)"
    RC=5
fi

_log "done (rc=$RC): $REPO_PATH"
exit "$RC"
