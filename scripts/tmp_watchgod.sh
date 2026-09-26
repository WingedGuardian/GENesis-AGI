#!/usr/bin/env bash
# tmp_watchgod.sh — Dual-zone temp directory protection.
#
# Runs as a standalone systemd user service, independent of Genesis.
# Monitors two zones:
#   Zone A: CC temp (~/.genesis/cc-tmp/) — tiered budget enforcement
#   Zone B: System /tmp — gentle time-based housekeeping
#
# Reads config from ~/.genesis/config/watchgod.conf
# Writes status to ~/.genesis/watchgod_state.json
# Logs to ~/.genesis/logs/tmp_watchgod.log

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

POLL_INTERVAL=30
CONF_FILE="$HOME/.genesis/config/watchgod.conf"
STATE_FILE="$HOME/.genesis/watchgod_state.json"
LOG_FILE="$HOME/.genesis/logs/tmp_watchgod.log"
ALERT_DIR="$HOME/.genesis/alerts"

# OOM event capture: the container cgroup-v2 cumulative oom_kill counter, and a
# durable log for the snapshots. OOM_EVENTS_FILE is overridable so tests can
# point it at a fixture file. OOM_LOG lives beside the watchgod log (NOT in
# cc-tmp) so it survives cleanup.
OOM_EVENTS_FILE="${OOM_EVENTS_FILE:-/sys/fs/cgroup/memory.events}"
# The trigger discriminator (Codex P1, #1790): a unit's `oom_kill` count records
# WHICH process died, not WHOSE limit fired — when the container (or any
# ancestor) hits its limit, the kernel can pick a high-RSS victim inside a
# contained child scope, and the journal then names that child. Only the cgroup
# whose OWN limit was hit records a LOCAL `oom` event, so the container root's
# memory.events.local `oom` counter distinguishes container pressure from a
# contained cap doing its job. Readable since kernel 4.19 wherever
# memory.events is; unreadable here means the trigger cannot be verified and
# the kill PAGES (attribution never silences on missing evidence).
# Deliberately NOT resolved here. An explicit override wins, but the DEFAULT is
# derived from OOM_EVENTS_FILE at the moment it is read (see
# _read_oom_local_trigger), because resolving it at source time freezes it
# against whatever OOM_EVENTS_FILE happened to be then — and anything that
# reassigns OOM_EVENTS_FILE afterwards leaves this pointing at the real
# /sys/fs/cgroup while believing otherwise. That is silent, and it is
# environment-dependent: it reads correct on a host that has the file and
# inverts every suppression decision on one that does not.
OOM_EVENTS_LOCAL_FILE="${OOM_EVENTS_LOCAL_FILE:-}"
OOM_LOG="$(dirname "$LOG_FILE")/oom_events.log"

# Durable alert queue (F.3) — emergency-tier events page Telegram via the
# container drainer. Guarded: if the lib is ever not co-located, degrade to a
# no-op so `set -e` can never take the service down over an alert.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$_SCRIPT_DIR/lib/alert_queue.sh" ]]; then
    # shellcheck source=scripts/lib/alert_queue.sh
    source "$_SCRIPT_DIR/lib/alert_queue.sh"
else
    queue_alert() { :; }
fi

# Defaults (overridden by config)
CC_TMP_DIR="$HOME/.genesis/cc-tmp"
CC_TMP_BUDGET_MB=500
SACRED_GROUND_MB=150
# Units whose OOM kill is CONTAINMENT WORKING, not a container emergency: they
# run inside their own MemoryMax scope on purpose (issue #1775 — 11 emergency
# pages for the code-intel indexer dying at its own 2G cap, attributed to "the
# container" and blamed on CC sessions, while `free` showed 17.8 GB available).
# Space-separated unit-name prefixes; override in watchgod.conf or the env.
# cbm-mcp- = the codebase-memory MCP wrapper (.claude/mcp/run-codebase-memory),
# capped and NAMED for exactly this classification (issue #1792).
OOM_CONTAINED_UNIT_PREFIXES="${OOM_CONTAINED_UNIT_PREFIXES:-code-intel- cbm-mcp-}"

# ── Load config ──────────────────────────────────────────────
load_config() {
    if [[ -f "$CONF_FILE" ]]; then
        # shellcheck source=/dev/null
        source "$CONF_FILE"
    fi
    # Headroom can never exceed capacity, so a sacred ground at or above the
    # volume's capacity makes `headroom < SACRED_GROUND_MB` true forever: the
    # oxygen floor is pinned ON and the in-flight guard is bypassed on every
    # RED run, silently and permanently. That is the same failure a capacity of
    # 0 produces, from the other knob — cc_tmp_capacity_mb already rejects
    # that one, and leaving this side open would be arbitrary. Clamp loudly
    # rather than fail closed: a wrong sacred ground must not stop the daemon.
    # Conditioned on the default being a SANE sacred ground for this capacity:
    # on a volume genuinely smaller than 150MB every value is >= capacity, the
    # floor being permanently on is the honest answer, and clamping would only
    # trade a real signal for a warning on every poll.
    local _cap
    _cap="$(cc_tmp_capacity_mb)"
    if [[ "${SACRED_GROUND_MB:-}" =~ ^[0-9]+$ ]] && (( _cap > 150 && SACRED_GROUND_MB >= _cap )); then
        log WARN "watchgod.conf: SACRED_GROUND_MB=${SACRED_GROUND_MB} >= cc-tmp capacity ${_cap}MB — that pins the oxygen floor permanently ON and bypasses the in-flight guard on every RED run; clamping to 150"
        SACRED_GROUND_MB=150
    fi
}

# ── Logging ──────────────────────────────────────────────────
log() {
    local level="$1"; shift
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [$level] $*" >> "$LOG_FILE"
}

# ── Helpers ──────────────────────────────────────────────────
dir_usage_mb() {
    # Total disk usage of a directory in MB
    # NOTE: capture first, then default — pipefail + '|| echo 0' appends
    # a spurious '0' when du exits non-zero but awk already emitted output.
    local result
    result=$(du -sm "$1" 2>/dev/null | awk '{print $1}') || true
    echo "${result:-0}"
}

tmp_usage_pct() {
    if df -T /tmp 2>/dev/null | grep -q tmpfs; then
        # tmpfs: filesystem percentage is meaningful
        local result
        result=$(df --output=pcent /tmp 2>/dev/null | tail -1 | tr -d ' %') || true
        echo "${result:-0}"
    else
        # Not tmpfs (/tmp on root disk): use absolute free space thresholds.
        # Danger is the same regardless of disk size — CC sessions need ~60MB
        # each, sacred ground is 150MB.  Percentage-based thresholds are
        # meaningless when measuring the whole root filesystem.
        local free_mb
        free_mb=$(df -BM --output=avail /tmp 2>/dev/null | tail -1 | tr -d ' M') || true
        free_mb="${free_mb:-9999}"
        if (( free_mb > 2048 )); then echo 0       # >2GB free  → green
        elif (( free_mb > 1024 )); then echo 60     # 1-2GB free → yellow
        elif (( free_mb > 500 )); then echo 75      # 500M-1GB  → orange
        else echo 90                                 # <500MB    → red
        fi
    fi
}

fs_free_mb() {
    # Free space on the filesystem containing the given path, in MB
    local result
    result=$(df -BM --output=avail "$1" 2>/dev/null | tail -1 | tr -d ' M') || true
    echo "${result:-999999}"
}

fs_total_mb() {
    # Total size of the filesystem containing the given path, in MB. After the
    # cc-tmp blast-radius split this reports the dedicated volume's size, so the
    # state file (and dashboard) can show the volume's capacity/headroom, not
    # the rootfs's.
    local result
    result=$(df -BM --output=size "$1" 2>/dev/null | tail -1 | tr -d ' M') || true
    echo "${result:-0}"
}

glob_escape() {
    # Escape the four characters find(1)'s -path GLOB treats specially, so an
    # exclusion built from a real directory name matches that name literally.
    # MEASURED 2026-09-23: without this, a directory named `sp[a]re` is NOT
    # excluded by -not -path ".../sp[a]re/*" and is reaped anyway — a silent
    # fail-OPEN in the exact direction this guard exists to prevent.
    printf '%s' "$1" | sed 's/[][*?\\]/\\&/g'
}

live_open_paths() {
    # Every filesystem path a live process currently holds OPEN, one per line.
    #
    # This is the signal the reap needs, and mtime is not: a directory being
    # written RIGHT NOW is indistinguishable by mtime from a cache written a
    # second ago and already closed, so a recency threshold either destroys
    # in-flight work or refuses to reclaim fresh junk.
    #
    # It is a STRICTLY BETTER signal, not a complete one. A writer that opens,
    # writes and closes each file in turn — a shell loop calling curl per file,
    # an extractor that closes each member, a write-then-rename — holds no
    # descriptor at the instant of the sweep and is reaped exactly as before.
    # Do not read this as a biconditional.
    #
    # NEWLINE-delimited, and that is a CONSTRAINT rather than a choice. A path
    # containing a newline splits into two records, neither of which matches —
    # a silent fail-OPEN. The obvious fix is NUL delimiting, and it is not
    # available here: MEASURED 2026-09-23, bash command substitution STRIPS NUL
    # bytes outright (it warns "ignored null byte in input"), so a NUL-delimited
    # snapshot arrives in the variable as one concatenated blob with no
    # separators at all — strictly worse. awk itself handles RS="\0" fine; the
    # shell variable is the limit. Accepted: every directory name this sweep
    # meets in practice comes from mktemp, pip, or a CC session UUID.
    #
    # Same-uid processes only: another uid's /proc/<pid>/fd is EACCES and is
    # skipped. cc-tmp is mode 700, so its writers are normally this user's own
    # processes — "normally" because a root process writing there would be
    # invisible here, which is one of the states the caller's self-test exists
    # to notice.
    #
    # One find for the whole sweep rather than one per candidate directory.
    # MEASURED 2026-09-23 on a live install: ~2,000 descriptors across ~170
    # processes in a single pass (47-118ms), and identical counts inside a
    # transient systemd --user unit (the service's own context) as in an
    # interactive shell — this unit sets no ProtectProc/ProcSubset.
    find /proc/[0-9]*/fd -maxdepth 1 -type l -printf '%l\n' 2>/dev/null || true
}

dir_has_live_writer() {
    # 0 when the snapshot ($2) holds an open path under directory $1. Used for
    # the depth-1 REAP decision, where the unit is the whole directory: reaping
    # only the quiet part of a tree being written leaves its writer a
    # partially-deleted directory.
    #
    # The needle goes through the environment rather than `awk -v`, which
    # expands backslash escapes in the value — MEASURED: a directory named
    # `ta\tb` silently fails to match under -v, and fail-OPEN follows.
    # Prefix-matched on "$1/" so a sibling sharing a name prefix
    # (pip-unpack-a beside pip-unpack-abc) cannot match the wrong directory.
    local dir="$1" snapshot="$2"
    [[ -n "$snapshot" ]] || return 1
    # No early exit on match, deliberately: awk quitting mid-stream leaves
    # printf writing into a closed pipe, and under pipefail the pipeline then
    # returns 141 (SIGPIPE) — which the caller reads as "no live writer", so a
    # FOUND writer produced a reap. MEASURED 2026-09-23 at snapshots >~379KB
    # (mawk's read buffer; implementation-dependent). Draining the whole
    # stream costs ~60ms at 800KB and makes the status always awk's own.
    #
    # The " (deleted)" skip: an unlinked inode's directory reclaims nothing
    # and would be spared forever. A real filename ending in that literal
    # string is indistinguishable (/proc does not escape) and would hide its
    # writer — accepted: cc-tmp is mode 700 and same-uid, so an actor who can
    # craft that name can already delete the tree directly.
    printf '%s\n' "$snapshot" | _wg_needle="$dir/" awk '
        / \(deleted\)$/ { next }
        index($0, ENVIRON["_wg_needle"]) == 1 { found = 1 }
        END { exit !found }
    '
}

live_writer_units() {
    # For each live open file under $2, the WORK UNIT its writer owns —
    # "U <dir>" lines — plus the unit's ancestor NODES up to (never including)
    # the root — "A <dir>" lines. Deduplicated.
    #
    # The unit question is where both review rounds' fixes conflicted, each
    # correct about its own measured case: excluding the file's DEEPEST
    # directory misses quiet siblings one level up (a pip unpack with a writer
    # in a subdir lost already-downloaded files beside that subdir), while
    # excluding the whole ANCESTOR CHAIN as subtrees turns one live session
    # under claude-<uid>/ into a verdict about all of it (MEASURED live: 7
    # project trees, 93 session dirs, 1 with live fds — a disabled reaper).
    #
    # Resolution — the unit is what the writer plausibly OWNS:
    #   * an ordinary depth-1 dir (pip-unpack-*, tmpXXXX, tsx-*): the whole
    #     depth-1 tree. mktemp-style dirs are single-owner by construction.
    #   * under the claude-<uid>/ container: the SESSION tree
    #     (claude-<uid>/<project>/<session>), the same 2-level layout
    #     newest_session above already encodes. Siblings stay reclaimable.
    #   * a loose file at the ROOT: no unit at all. CC points TMPDIR here, so
    #     root-level held temp files are routine (227 measured live) — deriving
    #     the root itself as a unit excluded EVERYTHING from every sweep.
    #
    # Unit subtrees are excluded whole ("U": node + contents); ancestors are
    # excluded as NODES only ("A": the directory itself survives rm -rf /
    # -delete, its OTHER children remain sweepable).
    local snapshot="$1" root="$2"
    [[ -n "$snapshot" ]] || return 0
    printf '%s\n' "$snapshot" | _wg_root="$root/" awk '
        / \(deleted\)$/ { next }
        index($0, ENVIRON["_wg_root"]) != 1 { next }
        {
            rootlen = length(ENVIRON["_wg_root"])
            rel = substr($0, rootlen + 1)
            if (rel !~ /\//) {
                # A loose file at the ROOT gets no directory unit (the C3
                # floor: deriving the root dir excluded EVERYTHING) — but the
                # FILE itself is still in-flight work, so protect exactly that
                # one path, node-only. Bounded: one entry per held root file.
                if (!(("F" rel) in seen)) { seen["F" rel] = 1; print "F " $0 }
                next
            }
            d = rel
            sub(/\/[^\/]*$/, "", d)       # dirname, root-relative
            n = split(d, comp, "/")
            # The container test is anchored to the NUMERIC-uid form on
            # purpose: a bare /^claude-/ also matched claude-skills — the
            # CACHE this same file deletes by name at two tiers — and
            # reclassified it as a container, narrowing its unit to depth 2
            # and re-creating for a neighbouring name the exact quiet-sibling
            # loss the unit model exists to prevent (round-3 finding,
            # MEASURED).
            if (comp[1] ~ /^claude-[0-9]+$/) {
                if (n < 2) {
                    # A loose file directly under the container would derive
                    # the container ITSELF as a unit — the C3 disabled-reaper
                    # one level down (round-3 finding, MEASURED: one held
                    # lockfile stopped every sibling tree being reclaimed).
                    # Same remedy as the root floor: protect exactly the file.
                    if (!(("F" rel) in seen)) { seen["F" rel] = 1; print "F " $0 }
                    next
                }
                depth = (n < 3 ? n : 3)   # session tree, or shallower dirname
            } else {
                depth = 1                 # ordinary temp: depth-1 owns it
            }
            unit = comp[1]
            for (i = 2; i <= depth; i++) unit = unit "/" comp[i]
            if (!(("U" unit) in seen)) {
                seen["U" unit] = 1
                print "U " ENVIRON["_wg_root"] unit
            }
            anc = ""
            for (i = 1; i < depth; i++) {
                anc = (i == 1 ? comp[1] : anc "/" comp[i])
                if (!(("A" anc) in seen)) {
                    seen["A" anc] = 1
                    print "A " ENVIRON["_wg_root"] anc
                }
            }
        }
    '
}

zone_a_live_exclusions() {
    # Populate the array named by $1 with find(1) predicates protecting every
    # live writer's WORK UNIT (see live_writer_units): "U" units as node +
    # subtree, "A" ancestors as node only.
    #
    # ONE builder, consumed by EVERY Zone A deletion site. The alternative —
    # each site matching in its own way — is what let a directory be spared by
    # the reap loop, logged as spared, and then deleted by the cache sweep
    # twenty lines later.
    local -n _wg_out="$1"
    local snapshot="$2" root="$3" floor="${4:-0}"
    _wg_out=()

    # THE SNAPSHOT CANNOT REPRESENT A NEWLINE, so no exclusion built from it
    # can protect a path containing one: live_open_paths renders /proc targets
    # one per LINE, and a held `dir-a<LF>b/part.whl` arrives as two records,
    # the first truncated at the newline. Every consumer of this array —
    # YELLOW's two sweeps, ORANGE's, RED's file sweep and both cache loops —
    # would otherwise walk straight into such a tree with no valid liveness
    # exclusion and unlink work in flight. Skipping only the directory-level
    # reaper, as the first version of this guard did, left those file sweeps
    # traversing the same tree.
    #
    # MEASURED 2026-09-26: find's -path glob DOES match a newline through `*`,
    # so one predicate covers the node and its whole subtree; a control arm
    # with plain-named siblings confirmed they are still swept.
    #
    # Added BEFORE the loop deliberately, so the 512-entry truncation below
    # cannot drop it.
    #
    # BELOW THE OXYGEN FLOOR IT DOES NOT APPLY, and that is not an oversight.
    # The floor bypasses every liveness guard by design — only zero-byte
    # sockets survive — so a newline-named, directory-heavy tree left immortal
    # there would hold the volume at ENOSPC while the daemon kept killing
    # sessions to no effect. An unverifiable writer loses to a certain outage.
    #
    # THE ROOT IS TESTED FIRST, and skipping that test is a total, silent
    # failure. `-path`'s leading `*` matches `/`, so `*<LF>*` is satisfied by
    # the SEARCH ROOT's own path whenever the root itself contains a newline —
    # every candidate then matches the exclusion and Zone A stops reclaiming
    # ANYTHING. VERIFIED with a control: under a newline-named root the aged
    # temp sweep reclaimed nothing, while the identical tree under a plain
    # root reclaimed normally. This file already names the class at the C3
    # floor; the check was missing here.
    #
    # Anchoring the pattern to the root would also avoid the everything-match,
    # but it would then sweep such a tree BLIND and say nothing, because under
    # a newline root no pattern can separate the representable paths from the
    # unrepresentable ones — there are none of the former. So the root case is
    # handled explicitly instead: warn_paths_defeating_liveness_guard reports
    # it once and the sweep proceeds. That is the degrade-OPEN-and-LOUD stance
    # the in-flight guard already takes, and for the same reason — refusing to
    # reap lets cc-tmp fill, and a full cc-tmp is what kills the sessions this
    # service exists to protect. With the root cleared, the pattern needs no
    # anchor: every candidate is under the root by construction.
    if (( ! floor )) && ! root_defeats_liveness_guard "$root"; then
        _wg_out+=(-not -path "*${_WG_NL}*")
    fi

    local line tag d esc count=0
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        tag="${line:0:1}"
        d="${line:2}"
        # N4: an absurdly large exclusion set would push find's argv past
        # ARG_MAX, and `2>/dev/null || true` would swallow the failure — the
        # sweep silently never runs, indistinguishable from "nothing to
        # reclaim". Cap it LOUDLY; ~500 units is far beyond any real state
        # (baseline: ~2,000 descriptors system-wide, 0 under cc-tmp).
        if (( ++count > 512 )); then
            log WARN "Zone A — live-writer exclusion set exceeded 512 entries; truncating (further live writers are NOT protected this sweep)"
            break
        fi
        esc="$(glob_escape "$d")"
        # "U" unit subtree: node + contents. "A" ancestor and "F" root-level
        # held file: node only — an "F" exclusion is a single exact path, so
        # it can never widen into the everything-match the C3 floor prevents.
        _wg_out+=(-not -path "$esc")
        if [[ "$tag" == "U" ]]; then
            _wg_out+=(-not -path "$esc/*")
        fi
    done < <(live_writer_units "$snapshot" "$root")
}

cc_tmp_capacity_mb() {
    # The cc-tmp volume's TRUE capacity in MB: min(what statfs reports, the
    # configured cap). Two backends, verified live 2026-09-23 on two installs:
    # on an LVM pool the volume is a real 2GiB block device and statfs tells
    # the truth (min picks it); on a btrfs pool the volume is a subvolume
    # whose 2GiB quota lives in a qgroup statfs CANNOT see — df reports the
    # whole shared pool (349GB measured), so the configured number is the only
    # true one (min picks it). A statfs failure (0) falls back to the config.
    local fs_total conf
    fs_total="$(fs_total_mb "$CC_TMP_DIR")"
    conf="${CC_TMP_CAPACITY_MB:-2048}"
    # A hand-edited conf value like "2G" would kill the daemon at the
    # arithmetic below under set -e; degrade to the default instead. ZERO is
    # rejected by the same test on purpose: a capacity of 0 makes every
    # headroom negative, which pins the oxygen floor permanently ON and so
    # bypasses the in-flight guard forever — the failure direction this whole
    # helper exists to avoid.
    [[ "$conf" =~ ^[1-9][0-9]*$ ]] || conf=2048
    if (( fs_total > 0 && fs_total < conf )); then
        echo "$fs_total"
    else
        echo "$conf"
    fi
}

cc_tmp_headroom_mb() {
    # Headroom in MB — how much more cc-tmp can grow before it hits its own
    # configured ceiling. ONE term, deliberately: capacity - used.
    #
    # fs_free is DELIBERATELY NOT folded in, and this is the third position on
    # that question — the first two were wrong and both are recorded here so
    # the cap does not get re-added a fourth time.
    #
    # The floor is a statement about CC-TMP'S OWN ceiling. fs_free answers a
    # different question — how full is the filesystem cc-tmp happens to sit on
    # — and on every backend this install supports that filesystem is SHARED:
    #   * plain directory (unsupported pool, failed create/attach, bare metal)
    #     -> the host root filesystem;
    #   * btrfs subvolume (the isolated case) -> the whole pool. MEASURED
    #     2026-09-24: cc-tmp and / report the SAME device and the SAME 200641MB
    #     avail, while cc-tmp is genuinely its own mount point. So an
    #     is-it-its-own-mount test — the obvious discriminator, and the one
    #     tried second — passes on btrfs and lets the shared number straight
    #     back in.
    # Folding it in therefore makes "the host disk is full" indistinguishable
    # from "cc-tmp is full", and the floor's response (spare nothing, delete
    # everything) is correct for the second while being destructive AND futile
    # for the first: it would wipe every in-flight write inside a near-empty
    # cc-tmp, on every poll, without freeing a byte that moves the host disk.
    #
    # The concern that motivated the cap — that capacity minus usage can
    # overstate what is actually writable — is already answered where it can be
    # answered honestly: cc_tmp_capacity_mb takes min(fs_total, configured), so
    # on a backend whose statfs tells the truth the capacity is already the
    # device's real size. And a genuine host-level disk emergency keeps its own
    # independent RED trigger in check_cc_tmp; it simply does not license the
    # bypass.
    #
    # $1 is the already-measured usage, because du of cc-tmp is not free and
    # check_cc_tmp has measured it one line earlier; a direct call measures it.
    local used="${1:-}" capacity
    [[ "$used" =~ ^[0-9]+$ ]] || used="$(dir_usage_mb "$CC_TMP_DIR")"
    capacity="$(cc_tmp_capacity_mb)"
    echo "$(( capacity - used ))"
}

# A newline in a path DEFEATS THE LIVENESS GUARD, so a candidate carrying one
# is spared rather than reaped. This is not fastidiousness — it is the direct
# consequence of NUL-delimiting the reap loops.
#
# MEASURED: `live_open_paths` renders /proc/*/fd targets one per LINE, so a
# descriptor held on `<cc-tmp>/pip-a<LF>b/part.whl` appears in the snapshot as
# two records, the first truncated to `<cc-tmp>/pip-a`. `dir_has_live_writer`
# then searches those records for the directory prefix and cannot match — it
# reports NO live writer for a directory that demonstrably has one, while
# reporting correctly for its plain-named sibling.
#
# Before the loops were NUL-delimited, `read -r` split such a name and the
# candidate was never reached, so it survived BY ACCIDENT. Reaching it without
# fixing the snapshot would convert that accident into a deletion with the
# guard silently answering the wrong question. Bash cannot hold a NUL in a
# variable, so a NUL-safe snapshot is a larger change to a different function;
# until then this sparing is the honest position, and it is LOUD rather than
# implicit.
_WG_NL=$'\n'

root_defeats_liveness_guard() {
    # True when the SWEEP ROOT itself contains a newline. Then every path
    # beneath it is unrepresentable in the liveness snapshot, so there is no
    # exclusion to build — only a report to make.
    [[ "$1" == *"$_WG_NL"* ]]
}

path_defeats_liveness_guard() {
    # True when live_open_paths cannot represent this path, so no exclusion
    # built from that snapshot can protect it. See the builder for why.
    [[ "$1" == *"$_WG_NL"* ]]
}

warn_paths_defeating_liveness_guard() {
    # Say OUT LOUD which paths the liveness guard structurally cannot see, so
    # the limitation is visible in the log rather than implicit in a sweep
    # that quietly walked around them. Sparing silently would just be the old
    # accident with extra steps.
    #
    # This is a DEDICATED pass rather than a branch inside a reap loop, and
    # that is the round-2 correction: a loop only ever logs the candidates it
    # enumerates, so the file sweeps — which have no loop — could never report
    # what they had skipped. One pass covers every consumer.
    #
    # Called once per cleaner invocation: from clean_cc_yellow (which
    # clean_cc_orange runs first, so ORANGE inherits it) and from clean_cc_red
    # above the oxygen floor. Below the floor nothing is spared, so there is
    # nothing to report.
    local root="$1" p count=0
    if root_defeats_liveness_guard "$root"; then
        # The whole tree is unrepresentable. Say so once and sweep anyway.
        log WARN "Zone A — the sweep root itself contains a newline, which live_open_paths cannot represent, so the in-flight guard is DEGRADED for the ENTIRE tree; sweeping anyway, because refusing would let cc-tmp fill and a full cc-tmp is what kills sessions"
        return 0
    fi
    while IFS= read -r -d '' p; do
        # Bound the log, not the sparing: the exclusion is a single find
        # predicate and covers every such path regardless of how many there
        # are. Only the per-path reporting is capped.
        if (( ++count > 8 )); then
            log WARN "Zone A — more than 8 paths contain a newline; the rest are spared too but not listed individually"
            break
        fi
        local _wg_q
        printf -v _wg_q '%q' "$p"
        log WARN "Zone A — sparing ${_wg_q}: its name contains a newline, which live_open_paths cannot represent, so this daemon cannot establish whether a process is writing into it"
        # -prune, so the cap counts offending NAMES rather than descendants.
        # Every child of a newline-named directory also contains the newline,
        # so one bad name floods the report: MEASURED, one directory holding
        # 20 files produced 21 records without -prune and 1 with it.
    done < <(find "$root" -path "*${_WG_NL}*" -prune -print0 2>/dev/null) || true
}

reap_dir_sparing_sockets() {
    # Object-level deletion that NEVER removes unix sockets. CC binds one
    # socket per live session under cc-tmp (cross-session messaging); they are
    # 0 bytes, so deleting them reclaims nothing and silently severs the local
    # coordination plane — sessions keep listening on bound-but-unlinked
    # sockets and inbound connects fail ENOENT (measured, 2026-09-05 RED
    # incident). Deletes everything else depth-first; a socket's ancestor dirs
    # stay non-empty so they survive; a dir holding no sockets is removed
    # entirely, exactly like rm -rf. -delete failures on non-empty dirs are
    # expected and suppressed; GNU find continues past them.
    find "$1" -depth -not -type s -delete 2>/dev/null || true
}

write_state() {
    local cc_tier="$1" cc_used="$2" sys_tier="$3" sys_pct="$4"
    local is_tmpfs="false"
    if df -T /tmp 2>/dev/null | grep -q tmpfs; then
        is_tmpfs="true"
    fi
    # Filesystem headroom for cc-tmp's mount. Post-split these describe the
    # dedicated volume; pre-split they describe the rootfs. Consumers read them
    # via .get(..) so an older state file (without these keys) stays valid.
    local cc_fs_free cc_fs_total
    cc_fs_free=$(fs_free_mb "$CC_TMP_DIR")
    cc_fs_total=$(fs_total_mb "$CC_TMP_DIR")
    local tmp="${STATE_FILE}.tmp"
    cat > "$tmp" <<EOF
{
  "cc_tmp": {"tier": "$cc_tier", "used_mb": $cc_used, "budget_mb": $CC_TMP_BUDGET_MB, "sacred_mb": $SACRED_GROUND_MB, "fs_free_mb": $cc_fs_free, "fs_total_mb": $cc_fs_total},
  "system_tmp": {"tier": "$sys_tier", "used_pct": $sys_pct, "is_tmpfs": $is_tmpfs},
  "poll_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
    mv "$tmp" "$STATE_FILE"
}

# ── Zone A: CC Temp ──────────────────────────────────────────
#
# Tiers (% of budget):
#   Green  : < 50%  — no action
#   Yellow : > 50%  — clean stale session dirs + old temp files
#   Orange : > 75%  — yellow + delete caches + record a stuck tier (NO kill) + alert
#   Red    : > 90% OR fs free < sacred — nuclear cleanup + emergency alert

clean_cc_yellow() {
    # Same in-flight exclusions the tiers above use. YELLOW fires at 50% of
    # budget — more often than either — and deletes *.tmp older than 60min: a
    # long download's still-open .tmp is in-flight work by RED's own standard.
    # Snapshot cost ~50-100ms per poll, measured.
    local -a _yellow_excl=()
    zone_a_live_exclusions _yellow_excl "$(live_open_paths)" "$CC_TMP_DIR"
    log INFO "Zone A YELLOW — cleaning stale session dirs and temp files"
    warn_paths_defeating_liveness_guard "$CC_TMP_DIR"

    # Clean session dirs with mtime > 7 days
    find "$CC_TMP_DIR" -mindepth 2 -maxdepth 2 -type d -path "*/claude-*/???*" \
        -mtime +7 ${_yellow_excl[@]+"${_yellow_excl[@]}"} \
        -exec rm -rf {} + 2>/dev/null || true

    # Clean old temp files (*.tmp, *.env, *.yaml) > 1 hour old
    find "$CC_TMP_DIR" -type f \( -name "*.tmp" -o -name "*.env" -o -name "*.yaml" \) \
        -mmin +60 ${_yellow_excl[@]+"${_yellow_excl[@]}"} \
        -delete 2>/dev/null || true
}

clean_cc_orange() {
    clean_cc_yellow
    log WARN "Zone A ORANGE — deleting caches, then re-measuring to see if the tier resolved"

    # Same in-flight exclusions RED uses, for the same two names. ORANGE fires
    # at 75% of budget — MORE often than RED — so guarding only RED would leave
    # the incident class open on the tier that actually runs. A tsx-* directory
    # is written into while its process runs; "rebuilt automatically" is true
    # of a cache nobody is mid-write on, and says nothing about one that is.
    #
    # No probe self-test here, unlike RED: this tier deletes two named caches
    # rather than reaping arbitrary directories, so a blind guard costs a
    # rebuildable cache rather than a writer's work, and the scan runs on a
    # far more frequent tier. RED carries the diagnostic.
    local -a live_excl=()
    zone_a_live_exclusions live_excl "$(live_open_paths)" "$CC_TMP_DIR"

    # Delete the two rebuildable caches (claude-skills ~35MB, CC re-clones on
    # demand; tsx-* ~1.2MB, rebuilt automatically) — routed through
    # reap_dir_sparing_sockets rather than `rm -rf`, for the SAME reason RED's
    # cache sweep already is. The argument is the paragraph directly above this
    # one, which this tier made about the live-writer exclusions and never
    # applied to sockets: guarding only RED leaves the incident class open on
    # the tier that actually runs.
    #
    # It was open. REPRODUCED against the `rm -rf` form on the default branch:
    # a live socket placed inside claude-skills was DESTROYED by this tier.
    # That is the 2026-09-05 severance shape — sessions left listening on
    # bound-but-unlinked sockets while inbound connects fail ENOENT — fixed at
    # RED (90%) and left standing at ORANGE (75%), which is the tier that fires
    # first and far more often. `rm -rf` has no socket predicate; the primitive
    # does, and it costs nothing here: sockets are 0 bytes, so sparing them
    # reclaims exactly as much as deleting them.
    # NUL-delimited, and that is load-bearing rather than tidy. `find` prints
    # newline-terminated records, so a directory whose NAME contains a newline
    # splits into two: the head (not a directory, skipped by the guard below)
    # and a TAIL that is a RELATIVE path — resolved against the daemon's cwd,
    # which is the invoking user's home, NOT cc-tmp. MEASURED: a cache named
    # "tsx-a<LF>b" yields 2 records from -print, the second a bare "b" that a
    # reap would follow out of the tree entirely; -print0 yields the 1 correct
    # record. (The NUL caveat recorded for live_open_paths concerns COMMAND
    # substitution, which drops NUL bytes. This is PROCESS substitution into
    # `read -d ''`, where they pass through — measured, opposite conclusion.)
    local cache_dir
    while IFS= read -r -d '' cache_dir; do
        [[ -d "$cache_dir" ]] || continue   # an outer match may have taken it
        reap_dir_sparing_sockets "$cache_dir"
    done < <(find "$CC_TMP_DIR" -type d \( -name "claude-skills" -o -name "tsx-*" \) \
        ${live_excl[@]+"${live_excl[@]}"} -print0 2>/dev/null) || true

    mkdir -p "$ALERT_DIR"
    touch "$ALERT_DIR/tmp_warning"

    # LOOP-BREAK: re-measure AFTER the cleanup above, and record the stuck
    # state ONLY if we are still over the ORANGE line. This closes the runaway
    # of 2026-08-19: the tier that dispatched us here was measured BEFORE
    # cleanup, so without a re-measure the daemon re-enters ORANGE every poll
    # and re-runs this tail forever while the real filler (a pytest tree the
    # cache-evict never touches) sits untouched.
    # Use dir_usage_mb (du) — it drops immediately after rm; df can lag on
    # held-open deleted fds.
    local used_after threshold_orange
    used_after=$(dir_usage_mb "$CC_TMP_DIR")
    threshold_orange=$(( CC_TMP_BUDGET_MB * 75 / 100 ))
    if (( used_after <= threshold_orange )); then
        log INFO "ORANGE resolved by cache cleanup (used=${used_after}MB <= ${threshold_orange}MB)"
        rm -f "$ALERT_DIR/tmp_orange_stuck" 2>/dev/null || true
        return 0
    fi

    log WARN "ORANGE persists after cleanup (used=${used_after}MB > ${threshold_orange}MB) — nothing further this tier can safely reclaim"

    # NO SESSION KILL AT THIS TIER — removed deliberately, 2026-09.
    #
    # ORANGE used to reap unattached CC tmux sessions idle >2h at this point.
    # The loop never fired, and could not have helped if it had. MEASURED over
    # two independent log windows (2026-08-19 → 09-07, and 2026-09-22 →
    # 09-25): 1,385 ORANGE polls, ZERO kills. The re-measure comment above
    # already carried the reason — sessions are not the filler. One episode is
    # the demonstration: a live install sat ORANGE for 2h45m on 263MB of a
    # third-party tool's index cache plus 160MB of live session trees, and
    # every byte of that would have survived a tmux kill untouched.
    #
    # RESIDUAL, stated rather than hidden: a kill was not strictly a no-op for
    # space. Killing a process closes its descriptors, which releases any
    # unlinked-but-held blocks it was pinning — precisely the space a du-based
    # reclaim figure cannot see (MEASURED: 64MB of unlinked-but-held blocks
    # reads as 0MB to `du -sm`).
    #
    # But that reclaim was never reachable from THIS tier, for a reason the
    # deleted predicate hid. It judged idleness with tmux's
    # `#{session_activity}`, which does not track a process at all — MEASURED
    # 2026-09-25: a session writing 8MB/s to disk advanced it by 0 seconds over
    # a 6-second window. So "idle >2h" never meant "not writing"; it meant "not
    # printing", and a session running a long silent job — a build, a clone, a
    # redirected test run — was a KILL CANDIDATE while actively writing into
    # cc-tmp. The loop's best case was releasing blocks the tier could not see,
    # and its worst case was reaping a working session. Note also that age does
    # not bound size: a process that unlinked a 300MB temp three hours ago pins
    # 300MB right now.
    #
    # RED (90%) still kills every unattached cc- session and is UNCHANGED. Be
    # precise about what that escape hatch covers: RED's triggers are du-derived
    # except the `free_mb < SACRED_GROUND_MB` statvfs arm, and on a shared-pool
    # backend that arm reports the whole pool (see the backend note above), so
    # descriptor-pinned space may not escalate there either. The hatch is real
    # and narrower than "RED will catch it".

    # Stuck-ORANGE: cleanup did not resolve it, and this tier has nothing safe
    # left to do. Per design D2 (ORANGE is dashboard/log only — only RED pages)
    # this does NOT page; it records the stuck state ONCE (dedupe flag) in the
    # log instead of silently re-polling forever, so the condition is
    # discoverable. If cc-tmp keeps filling it escalates to RED, which DOES
    # page. The flag is cleared (main loop) whenever cc-tmp LEAVES ORANGE
    # (green/yellow/red), so one episode is logged exactly once.
    if [[ ! -f "$ALERT_DIR/tmp_orange_stuck" ]]; then
        log WARN "cc-tmp STUCK ORANGE (used=${used_after}MB, budget=${CC_TMP_BUDGET_MB}MB): cache eviction freed nothing and this tier has nothing else it can safely delete — non-reclaimable data is filling cc-tmp (see cc_tmp_top snapshots). Dashboard/log-only per D2; RED will page if it escalates."
        touch "$ALERT_DIR/tmp_orange_stuck"
    fi
}

clean_cc_red() {
    log WARN "Zone A RED — NUCLEAR cleanup, preserving active session"

    # Find the most recently modified session UUID dir (the active workspace)
    local newest_session=""
    newest_session=$(find "$CC_TMP_DIR" -mindepth 2 -maxdepth 2 -type d -path "*/claude-*" \
        -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | awk '{print $2}') || true

    # ── In-flight guard ──────────────────────────────────────
    # Degrades OPEN AND LOUD. Reaping without the guard is today's behaviour
    # and risks one writer's work; refusing to reap would spare everything and
    # let cc-tmp fill, and a full cc-tmp is what kills CC sessions — the
    # outcome this whole service exists to prevent. Open is the lesser harm.
    # Silent is not an option in either direction.
    #
    # The check is a POSITIVE SELF-TEST, not an emptiness test. An empty
    # snapshot is only the TOTAL failure; a partial /proc read, a writer owned
    # by another uid, or a CC_TMP_DIR spelling the kernel does not use all
    # yield a NON-empty snapshot with a structurally blind guard. Holding a
    # descriptor open on a probe inside CC_TMP_DIR and requiring the snapshot
    # to show it exercises every one of those at once — including the root
    # spelling, which is the case no amount of reading would have caught.
    # ── THE OXYGEN FLOOR — the invariant this whole tier exists for ──
    # When true headroom falls under sacred ground, cc-tmp is about to hit its
    # real ceiling, and a full cc-tmp is what kills CC sessions. At that point
    # NOTHING is spared: the in-flight guard is not consulted, no descriptor
    # and no guard defect can block reclamation. Keyed on cc_tmp_capacity_mb
    # (not statfs) because on a btrfs backend df cannot see the volume quota —
    # the deployed sacred-ground check read 190GB free while the real ceiling
    # was 2GiB away, so it could never have fired before ENOSPC.
    # check_cc_tmp has already measured usage and headroom to decide this tier;
    # it passes the number in so the du is not paid twice and the dispatcher and
    # the cleaner can never disagree about which side of the floor we are on. A
    # direct call (tests, a manual run) measures it here instead.
    local headroom="${1:-}"
    [[ "$headroom" =~ ^-?[0-9]+$ ]] || headroom="$(cc_tmp_headroom_mb)"

    local floor=0
    local live_paths="" probe="" probe_snapshot=""
    local probe_fd   # assigned by `exec {probe_fd}>` below, never by hand
    if (( headroom < SACRED_GROUND_MB )); then
        floor=1
        # Log the COMPONENTS, not just the result. An operator seeing only the
        # difference cannot tell a capacity that is wrong in the config from a
        # cc-tmp that is genuinely full, and those have opposite remedies. The
        # extra du costs one call on a path that fires only in an emergency.
        local _cap _used
        _cap="$(cc_tmp_capacity_mb)"
        _used="$(dir_usage_mb "$CC_TMP_DIR")"
        log WARN "Zone A RED — OXYGEN FLOOR: true headroom ${headroom}MB < sacred ${SACRED_GROUND_MB}MB (capacity ${_cap}MB - used ${_used}MB; filesystem free space is deliberately NOT part of this — it measures a SHARED pool on every supported backend). EVERY discretionary exclusion is bypassed — the in-flight guard, the 60-second freshness window, and the active session's own tree. Unix sockets are the ONLY thing that survives, anywhere in the tree (0 bytes: deleting them reclaims nothing and severs the control plane)."
    else
        probe="$(mktemp "$CC_TMP_DIR/.wg-probe.XXXXXX" 2>/dev/null)" || probe=""
        if [[ -n "$probe" ]]; then
            # Two hazards in one line, both MEASURED, both silent:
            #   * a failing `exec {fd}>` EXITS a non-interactive shell under
            #     set -e even mid-function — an unguarded open would take the
            #     daemon down at the exact tier where fd pressure makes open()
            #     likeliest to fail. Hence the `if`.
            #   * an `exec` with NO COMMAND applies every redirection to the
            #     shell PERMANENTLY, so a bare `exec {fd}>"$probe" 2>/dev/null`
            #     sends the daemon's own stderr to /dev/null for the rest of
            #     its life — the journal silently loses bash errors and set -e
            #     aborts from then on. The brace group scopes the suppression
            #     while probe_fd, opened in the current shell, outlives it.
            # MEASURED 2026-09-24, 4 forms x 2 outcomes with a no-redirect
            # oracle arm: this form keeps stderr AND still guards.
            if { exec {probe_fd}>"$probe"; } 2>/dev/null; then
                probe_snapshot="$(live_open_paths)"
                exec {probe_fd}>&-
            fi
            rm -f "$probe"
        fi
        # ONE scan, and it is the scan the self-test validated. An earlier
        # draft validated the probe snapshot and then took a SECOND snapshot to
        # protect with — so the checked one was discarded and the one that
        # actually guarded was unchecked. live_open_paths suppresses find
        # failures, so a failed or partial second read returns empty, which is
        # indistinguishable from "nothing is live" and reaps every active
        # directory while the self-test reports healthy.
        #
        # Positive self-test, not an emptiness test: a partial /proc read,
        # another uid's writer, or a CC_TMP_DIR spelling the kernel does not
        # use all yield a NON-empty snapshot with a structurally blind guard.
        # Exact-path match, not a prefix test: a prefix test answers "is
        # ANYTHING open under cc-tmp", which any other session's descriptor
        # satisfies (6 measured live at review time) — masking exactly the
        # partial read this names first (MEASURED: the prefix form passed with
        # the probe wholly invisible).
        # Whole-line containment in pure bash — NOT `printf | grep -qxF`.
        # `grep -q` exits on its first match, which SIGPIPEs the printf still
        # feeding it, and under `set -o pipefail` the pipeline then reports 141
        # and the self-test reads as FAILED while the probe was in fact found.
        # MEASURED 2026-09-24, sweeping snapshot size with the needle first:
        # 33,901 B -> rc 0, 67,901 B -> rc 141. The boundary is the 64 KiB pipe
        # buffer, and a live snapshot here is ~118 KB — so the piped form was
        # failing its own self-test on EVERY real RED run while passing in
        # small test fixtures. Same class as the awk early-exit above; the pipe
        # is the hazard, so this form removes the pipe rather than working
        # around it. Both operands are quoted, which makes the needle a LITERAL
        # inside the pattern, and the \n fences make it an exact-line test.
        if [[ -n "$probe" && $'\n'"$probe_snapshot"$'\n' == *$'\n'"$probe"$'\n'* ]]; then
            # Drop the probe's own record — it is closed and unlinked by now,
            # so a unit derived from it would exclude a path that cannot exist.
            # grep -v has no early exit, so it drains its input and cannot
            # SIGPIPE the printf the way the -q form above did.
            live_paths="$(printf '%s\n' "$probe_snapshot" | grep -vxF -- "$probe")" || live_paths=""
        else
            # Degrade LOUD — but KEEP the snapshot. Discarding it here would
            # be a policy change riding along with the single-snapshot fix,
            # and a strictly worse one: a snapshot that failed a POSITIVE
            # self-test is still better evidence than the empty string. Every
            # failure mode this test catches (a partial /proc read, a
            # CC_TMP_DIR spelling the kernel does not use, another uid's
            # writer) makes the snapshot INCOMPLETE, never fictional — /proc
            # fd links cannot name a path nobody has open — so using it can
            # only under-protect, which is the same direction blanking goes,
            # while blanking additionally throws away the real writers it DID
            # see. The point is sharp here: the SIGPIPE defect fixed just above
            # made this very branch fire on every real RED run, and under
            # blanking that false negative would have deleted every live
            # writer's tree rather than costing a log line.
            log WARN "Zone A RED — in-flight guard DEGRADED (self-test failed: this process's own open probe under $CC_TMP_DIR is not visible in the /proc snapshot); proceeding with the unverified snapshot, which may be incomplete — an active writer's directory may be deleted"
            live_paths="$probe_snapshot"
        fi
    fi

    # One exclusion set, built once, consumed by every deletion below.
    local -a live_excl=()
    zone_a_live_exclusions live_excl "$live_paths" "$CC_TMP_DIR" "$floor"
    (( floor )) || warn_paths_defeating_liveness_guard "$CC_TMP_DIR"

    # Reap every depth-1 dir except the newest session's ancestor and any
    # directory a live process is writing into — object-level and
    # socket-sparing (see reap_dir_sparing_sockets); loose files are the
    # separate sweep below. `|| true` matches the file's find idiom: a
    # transient find error must not abort the daemon mid-RED under
    # set -euo pipefail.
    # NUL-delimited, and that is load-bearing rather than tidy. `find` prints
    # newline-terminated records, so a directory whose NAME contains a newline
    # splits into two: the head (not a directory, skipped by the guard below)
    # and a TAIL that is a RELATIVE path — resolved against the daemon's cwd,
    # which is the invoking user's home, NOT cc-tmp. MEASURED: a cache named
    # "tsx-a<LF>b" yields 2 records from -print, the second a bare "b" that a
    # reap would follow out of the tree entirely; -print0 yields the 1 correct
    # record. (The NUL caveat recorded for live_open_paths concerns COMMAND
    # substitution, which drops NUL bytes. This is PROCESS substitution into
    # `read -d ''`, where they pass through — measured, opposite conclusion.)
    local dir
    while IFS= read -r -d '' dir; do
        # Skip if this contains the active session. Deliberately NOT added to
        # any exclusion list: this spares for a DIFFERENT reason than the
        # live-writer branch, and the loose sweep below already carries its own
        # deliberately narrower -not -path "$newest_session/*". Promoting this
        # to the depth-1 parent would exclude every sibling project tree under
        # claude-<uid>/ from the sweep (MEASURED: 7 trees, 1 of them live).
        # Below the oxygen floor even this sparing goes: the active session's
        # tree is as reclaimable as anything else when the alternative is
        # ENOSPC for every session including that one.
        # This loop is the ONE Zone A deletion site whose enumeration is not
        # filtered by live_excl — it walks every depth-1 directory and defers
        # to dir_has_live_writer per candidate — so the builder's newline
        # exclusion cannot reach it and the skip has to be here. The two cache
        # loops take live_excl on their own find, which already drops these
        # ABOVE THE FLOOR; below it nothing is dropped, by design.
        # Silent by design: warn_paths_defeating_liveness_guard above has
        # already named every such path once, and repeating it per loop would
        # report the same limitation three times per RED pass.
        if (( ! floor )) && path_defeats_liveness_guard "$dir"; then
            continue
        fi
        if (( ! floor )) && [[ -n "$newest_session" && "$newest_session" == "$dir/"* ]]; then
            continue
        fi
        # The REAP unit is the whole directory: reaping only the quiet part of
        # a tree being written leaves its writer a partially-deleted directory,
        # which breaks it just as surely as removing the whole thing.
        if dir_has_live_writer "$dir" "$live_paths"; then
            log INFO "Zone A RED — sparing $dir (a live process is writing into it)"
            continue
        fi
        reap_dir_sparing_sockets "$dir"
    done < <(find "$CC_TMP_DIR" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null) || true

    # The active session's own subtree, spared by both sweeps below — but ONLY
    # when newest_session actually resolved. Spelled unconditionally, an empty
    # newest_session makes the predicate `-not -path "/*"`, which matches every
    # absolute path and so deletes NOTHING AT ALL: RED would run, log normally
    # and reclaim zero bytes on any cc-tmp with no claude-<uid>/<project> dir
    # at depth 2 (MEASURED: 0 of 2 files swept). Pre-existing on the default
    # branch; it has to be right here because the floor path depends on these
    # sweeps actually sweeping.
    local -a session_excl=()
    if (( ! floor )) && [[ -n "$newest_session" ]]; then
        session_excl=(-not -path "$newest_session/*")
    fi

    # Delete all reclaimable loose files except those modified in the last 60s.
    # This sweep has no -maxdepth, so it walks the WHOLE tree — including the
    # directories spared above. Without the exclusions it would delete the
    # quiet files inside a directory the reap loop deliberately kept, which is
    # the same partial-deletion failure by another route.
    #
    # Below the floor the freshness window goes too. It is the one exclusion
    # that survives an emptied live_paths, and a root-level file being written
    # fast enough to cause the emergency is precisely the file whose mtime is
    # always current — it would survive every sweep, all the way to ENOSPC.
    local -a fresh_excl=()
    (( floor )) || fresh_excl=(-not -newermt '60 seconds ago')
    find "$CC_TMP_DIR" -type f ${fresh_excl[@]+"${fresh_excl[@]}"} \
        ${session_excl[@]+"${session_excl[@]}"} ${live_excl[@]+"${live_excl[@]}"} \
        -delete 2>/dev/null || true

    # Delete caches — except where a live process is writing, and except
    # inside the active session's own tree: a tsx-*/claude-skills dir INSIDE
    # the newest session is that session's in-flight tooling state, and the
    # descriptor guard alone cannot vouch for it (its writer may be between
    # opens). Reclaim of DEAD caches is pinned by its own test arm. Without
    # the live exclusions this sweep deleted a directory the reap loop had
    # just spared AND logged as spared, which is worse than not sparing it:
    # the operator is told a directory survived that did not.
    #
    # Routed through reap_dir_sparing_sockets rather than `rm -rf`, for the
    # SAME reason the depth-1 reap is: rm -rf has no socket predicate, so a
    # socket living inside a cache directory was deleted here twenty lines
    # after the reap loop deliberately kept its parent BECAUSE it holds a
    # socket (MEASURED: of four sockets placed across the tree, the two inside
    # cache directories were destroyed) — the exact spared-then-deleted
    # failure the paragraph above says this sweep exists to avoid, and the
    # reason the floor's log line could not honestly say sockets survive.
    # Sockets are 0 bytes, so keeping them costs no reclaimed space; their
    # ancestor directories stay non-empty and survive with them, which is the
    # same outcome the depth-1 reap already produces.
    # NUL-delimited, and that is load-bearing rather than tidy. `find` prints
    # newline-terminated records, so a directory whose NAME contains a newline
    # splits into two: the head (not a directory, skipped by the guard below)
    # and a TAIL that is a RELATIVE path — resolved against the daemon's cwd,
    # which is the invoking user's home, NOT cc-tmp. MEASURED: a cache named
    # "tsx-a<LF>b" yields 2 records from -print, the second a bare "b" that a
    # reap would follow out of the tree entirely; -print0 yields the 1 correct
    # record. (The NUL caveat recorded for live_open_paths concerns COMMAND
    # substitution, which drops NUL bytes. This is PROCESS substitution into
    # `read -d ''`, where they pass through — measured, opposite conclusion.)
    local cache_dir
    while IFS= read -r -d '' cache_dir; do
        [[ -d "$cache_dir" ]] || continue   # an outer match may have taken it
        reap_dir_sparing_sockets "$cache_dir"
    done < <(find "$CC_TMP_DIR" -type d \( -name "claude-skills" -o -name "tsx-*" \) \
        ${live_excl[@]+"${live_excl[@]}"} \
        ${session_excl[@]+"${session_excl[@]}"} \
        -print0 2>/dev/null) || true

    # Report the surviving control plane — counted AFTER every sweep above,
    # so the line is true by construction whatever any sweep did. Sockets
    # are 0 bytes: deleting them reclaims nothing and silently severs
    # cross-session messaging (measured, 2026-09-05 incident — this line's
    # absence is what made that invisible).
    local sock_count
    sock_count=$(find "$CC_TMP_DIR" -type s 2>/dev/null | wc -l) || sock_count=0
    if (( sock_count > 0 )); then
        log INFO "RED preserved ${sock_count} unix socket(s) under cc-tmp — control plane, 0 bytes reclaimable"
    fi

    # Kill ALL idle CC sessions (log each — so it's clear which terminals were reaped)
    while IFS= read -r sname; do
        [[ -z "$sname" ]] && continue
        if [[ "$sname" =~ ^cc- ]]; then
            log WARN "RED killing idle CC session: $sname"
            tmux kill-session -t "$sname" 2>/dev/null || true
        fi
    done < <(tmux list-sessions -F '#{session_name}:#{session_attached}' 2>/dev/null \
             | grep ':0$' | cut -d: -f1 || true)

    # Emergency alert — queue a page ONLY on the transition INTO red (flag not
    # yet set), so a sustained red episode does not re-page every 30s poll.
    mkdir -p "$ALERT_DIR"
    if [[ ! -f "$ALERT_DIR/tmp_emergency" ]]; then
        queue_alert emergency "watchgod:cc" "CC temp CRITICAL (RED)" \
            "cc-tmp blew its budget (${CC_TMP_BUDGET_MB}MB) — nuclear cleanup ran to protect active CC sessions. Investigate what filled it." \
            "watchgod:tmp_emergency"
    fi
    touch "$ALERT_DIR/tmp_emergency"
    log WARN "Zone A RED — nuclear cleanup complete"
}

# Record cc-tmp pressure + top consumers BEFORE a cleanup runs — so a filled-folder
# incident is diagnosable afterward (the nuclear cleanup erases the evidence otherwise).
# The snapshot goes under the log dir (NOT cc-tmp), so it survives the cleanup.
_log_cc_pressure() {
    local tier="$1" used="$2" free="$3"
    local stamp snap top
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    snap="$(dirname "$LOG_FILE")/cc_tmp_top_${stamp}.txt"
    # `|| true` inside the substitution: an empty cc-tmp (e.g. the sacred-ground RED path,
    # disk-full but cc-tmp empty) makes the glob literal → du fails → set -e would abort the
    # whole daemon. Tolerate it; `top` is just empty then.
    top=$(du -sm "$CC_TMP_DIR"/* 2>/dev/null | sort -rn | head -8 || true)
    {
        echo "# cc-tmp pressure ${stamp}  tier=${tier} used=${used}MB free=${free}MB budget=${CC_TMP_BUDGET_MB}MB"
        echo "$top"
    } > "$snap" 2>/dev/null || true
    log WARN "cc-tmp ${tier^^}: used=${used}MB free=${free}MB budget=${CC_TMP_BUDGET_MB}MB — top consumers → ${snap}"
    # Bound the snapshot count — a sustained ORANGE/RED episode would otherwise accumulate
    # these unbounded on the very filesystem we're protecting. Keep the 20 most recent.
    ls -1t "$(dirname "$LOG_FILE")"/cc_tmp_top_*.txt 2>/dev/null | tail -n +21 | xargs -r rm -f 2>/dev/null || true
}

check_cc_tmp() {
    mkdir -p "$CC_TMP_DIR"
    local used_mb
    used_mb=$(dir_usage_mb "$CC_TMP_DIR")
    local free_mb
    free_mb=$(fs_free_mb "$CC_TMP_DIR")

    local threshold_yellow=$(( CC_TMP_BUDGET_MB * 50 / 100 ))
    local threshold_orange=$(( CC_TMP_BUDGET_MB * 75 / 100 ))
    local threshold_red=$(( CC_TMP_BUDGET_MB * 90 / 100 ))

    local tier="green"

    # TRUE headroom against the volume's real ceiling, computed HERE rather
    # than inside clean_cc_red alone. The budget thresholds above are a
    # configured number and the statfs check below is blind on a btrfs backend,
    # so a budget set larger than the volume lets both stay green while the
    # volume runs out: with a 1 GiB volume and CC_TMP_BUDGET_MB=2000, budget-RED
    # does not start until 1800 MB and df reports the shared pool, so nothing
    # would ever call the only function that evaluates the real ceiling.
    local headroom
    headroom=$(cc_tmp_headroom_mb "$used_mb")

    # After the cc-tmp blast-radius split, free_mb measures the DEDICATED
    # volume, so this sacred-ground trigger guards that volume (not the rootfs).
    # On a 2 GiB volume it is a pure backstop behind the 450 MiB budget-red
    # above; rootfs free-space monitoring lives in Zone B (/tmp) below.
    if (( used_mb > threshold_red )) || (( free_mb < SACRED_GROUND_MB )) \
        || (( headroom < SACRED_GROUND_MB )); then
        tier="red"
        _log_cc_pressure red "$used_mb" "$free_mb"   # capture BEFORE the nuclear cleanup erases it
        clean_cc_red "$headroom"
    elif (( used_mb > threshold_orange )); then
        tier="orange"
        _log_cc_pressure orange "$used_mb" "$free_mb"
        clean_cc_orange
    elif (( used_mb > threshold_yellow )); then
        tier="yellow"
        log INFO "cc-tmp YELLOW: used=${used_mb}MB free=${free_mb}MB budget=${CC_TMP_BUDGET_MB}MB"
        clean_cc_yellow
    fi

    echo "$tier:$used_mb"
}

# ── Zone B: System /tmp ──────────────────────────────────────
#
# Tiers (% of filesystem):
#   Green  : < 50%  — no action
#   Yellow : 50-70% — clean files not accessed in 7+ days
#   Orange : 70-85% — clean files not accessed in 3+ days + alert
#   Red    : > 85%  — aggressive cleanup + emergency alert

clean_sys_yellow() {
    log INFO "Zone B YELLOW — cleaning /tmp files not accessed in 7+ days"
    find /tmp -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -path "*/claude-*" -not -name "*.sock" \
        -atime +7 -delete 2>/dev/null || true
    find /tmp -mindepth 1 -type d -empty -not -path "*/tmux-*" -not -path "*/pytest-*" -not -path "*/claude-*" \
        -delete 2>/dev/null || true
}

clean_sys_orange() {
    clean_sys_yellow
    log WARN "Zone B ORANGE — cleaning /tmp files not accessed in 3+ days"
    find /tmp -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -path "*/claude-*" -not -name "*.sock" \
        -atime +3 -delete 2>/dev/null || true
    mkdir -p "$ALERT_DIR"
    touch "$ALERT_DIR/tmp_warning"
}

clean_sys_red() {
    log WARN "Zone B RED — aggressive /tmp cleanup"
    # Files not accessed in 1+ day
    find /tmp -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -path "*/claude-*" -not -name "*.sock" \
        -atime +1 -delete 2>/dev/null || true

    # If still critical, remove all regular files except last 1h, sockets, tmux, pytest, claude
    local pct_after
    pct_after=$(tmp_usage_pct)
    if (( pct_after > 85 )); then
        find /tmp -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -path "*/claude-*" -not -name "*.sock" \
            -mmin +60 -delete 2>/dev/null || true
    fi

    # Emergency alert — transition-only (see clean_cc_red). Zones A/B share the
    # tmp_emergency flag, so a red episode pages once regardless of which zone
    # tripped first — intentional (one page per episode, not per zone).
    mkdir -p "$ALERT_DIR"
    if [[ ! -f "$ALERT_DIR/tmp_emergency" ]]; then
        queue_alert emergency "watchgod:sys" "System /tmp CRITICAL (RED)" \
            "/tmp usage exceeded 85% — aggressive cleanup ran. Something is filling /tmp." \
            "watchgod:tmp_emergency"
    fi
    touch "$ALERT_DIR/tmp_emergency"
    log WARN "Zone B RED — aggressive cleanup complete"
}

check_sys_tmp() {
    local pct
    pct=$(tmp_usage_pct)
    local tier="green"

    if (( pct > 85 )); then
        tier="red"
        clean_sys_red
    elif (( pct > 70 )); then
        tier="orange"
        clean_sys_orange
    elif (( pct > 50 )); then
        tier="yellow"
        clean_sys_yellow
    fi

    echo "$tier:$pct"
}

# ── OOM event capture (best-effort, cgroup v2) ───────────────
# A cgroup OOM kill silently collapses a CC session (tmux `exec claude` → claude
# is reaped → the last pane dies → the session ends) and leaves no durable trace:
# the kernel dmesg ring cycles and the kernel journal is usually unreadable from
# inside the container. This samples the container cgroup's CUMULATIVE oom_kill
# counter each poll and, on a NEW kill since the daemon started, records a
# timestamped snapshot (memory + top-RSS processes) and pages once. Read-only —
# it never kills or reclaims anything. Degrades to a no-op when the cgroup-v2
# interface file is absent/unreadable (older layouts / non-cgroup2 hosts).

_read_oom_kill() {
    # Echo the current cumulative oom_kill count; non-zero return if unavailable.
    [[ -r "$OOM_EVENTS_FILE" ]] || return 1
    awk '/^oom_kill /{print $2; found=1} END{exit !found}' "$OOM_EVENTS_FILE" 2>/dev/null
}

_read_oom_local_trigger() {
    # Echo the container root's LOCAL `oom` count (limit invocations charged to
    # this cgroup itself, descendants excluded); non-zero return if unavailable.
    local _local_file="${OOM_EVENTS_LOCAL_FILE:-$(dirname "$OOM_EVENTS_FILE")/memory.events.local}"
    [[ -r "$_local_file" ]] || return 1
    awk '/^oom /{print $2; found=1} END{exit !found}' "$_local_file" 2>/dev/null
}

# Attribution reads the systemd journal because the killer cgroup is usually a
# TRANSIENT scope, deleted with its job — every surviving cgroup shows the kill
# only as an inherited aggregate (measured: local=0 at every level) — while the
# journal names the unit and outlives the scope. The query window is a CURSOR:
# each successful read advances a durable epoch marker, and the next read asks
# only for lines SINCE it. That is what keeps attribution honest during a
# thrashing contained job: without it, a contained kill's line from the
# PREVIOUS increment still inside a fixed lookback could account for a NEW
# kill that left no line of its own (a non-main process dying inside a
# surviving scope writes no unit-failure line) and silence a page. A missing
# cursor (first run) falls back to a short lookback computed from the LIVE
# poll interval; a failed query does not advance the cursor. Every failure
# direction lands on the unattributed PAGE, never on silence.
_OOM_CURSOR_FILE="$(dirname "$LOG_FILE")/.oom_journal_cursor"

_oom_killed_units() {
    # Echo unit names the user journal says were oom-killed since the cursor,
    # one per line.
    # rc!=0 = journal UNAVAILABLE (no journalctl, or the query failed) — the
    # caller degrades to the unattributed page. rc=0 with empty output =
    # journal readable, no oom-kill record (also unattributed).
    command -v journalctl >/dev/null 2>&1 || return 1
    # Computed per call, not at load time: load_config re-sources watchgod.conf
    # every tick and may change POLL_INTERVAL — a frozen window shorter than
    # one poll gap would miss every contained kill and re-open the false pages.
    local _fallback_s=$(( POLL_INTERVAL * 2 + 60 ))
    local _cursor out rc=0
    _cursor=$(cat "$_OOM_CURSOR_FILE" 2>/dev/null) || _cursor=""
    # A REAL journal cursor, not a timestamp. `--since` is a TIMESTAMP filter and
    # is INCLUSIVE at its boundary, so an entry landing exactly on the stored
    # second is re-read on the next tick; `--after-cursor` is a POSITION filter
    # and starts strictly AFTER the named entry, so every record is seen exactly
    # once. That distinction is load-bearing now that the caller reconciles
    # RECORD COUNT against kill deltas: a double-counted boundary entry would
    # inflate the count. `--show-cursor` appends a trailing `-- cursor: s=…`
    # line, stripped below.
    #
    # NOTE (adversarial audit, #1790): journalctl REFUSES to combine
    # --after-cursor with --since/--cursor ("Please specify only one of" —
    # verified on systemd 255), and these records' timestamps are PID 1's
    # EMISSION time, not the kernel kill time — so a time window can neither
    # compose with the position filter nor bound a late record anyway. The
    # late/pre-baseline record problem is therefore closed by the caller's
    # DEFICIT reconciliation (see check_oom_events), not here.
    if [[ "$_cursor" == s=* ]]; then
        out=$(journalctl --user --after-cursor "$_cursor" --no-pager --show-cursor -o cat 2>/dev/null) || rc=$?
    else
        # First run, or a cursor file written by an older version (epoch digits):
        # fall back to the time window. Never trust a malformed value as a cursor.
        out=$(journalctl --user --since "-${_fallback_s} seconds" --no-pager --show-cursor -o cat 2>/dev/null) || rc=$?
    fi
    [[ $rc -ne 0 ]] && return 1
    # Advance the cursor only on a SUCCESSFUL read (this function runs in a
    # command substitution, but file writes escape the subshell). If the read
    # returned no cursor line (an empty journal window), KEEP the old cursor
    # rather than clearing it — clearing would re-read the whole window next
    # tick and double-count.
    local _newcur
    _newcur=$(printf '%s\n' "$out" | sed -n 's/^-- cursor: //p' | tail -1)
    # Through the single verified writer, like every other advance. On failure
    # the OLD cursor is removed rather than left behind: a stale value is
    # indistinguishable from a fresh anchor to anything that merely checks the
    # file exists, and `drain` is cleared on exactly that check.
    if [[ -n "$_newcur" ]]; then
        _oom_persist_cursor "$_newcur" || rm -f "$_OOM_CURSOR_FILE" 2>/dev/null || true
    fi
    # `-o cat` renders systemd's line as `<unit>: Failed with result 'oom-kill'.`
    # A unit name can legally contain ':' (template instances); cut would then
    # truncate it, and a truncated name cannot match a contained prefix — so a
    # pathological name mis-classifies toward PAGING, the safe direction.
    # NOT `sort -u`: the caller compares this list's RECORD COUNT against the
    # kill delta, and de-duplicating collapses two kills of the same unit name
    # into one line — which would under-count and suppress a page for a kill
    # nothing accounted for. Cardinality is the point; the display string
    # de-duplicates separately. (The `-- cursor:` line carries no oom-kill
    # phrase, so grep drops it here.)
    printf '%s
' "$out"         | { grep -F ": Failed with result 'oom-kill'" || true; }         | cut -d: -f1
}

_oom_units_all_contained() {
    # $1 = newline-separated non-empty unit list. rc 0 = EVERY unit matches a
    # contained prefix; any unmatched unit → rc 1 (one uncontained kill pages).
    local u p ok
    while IFS= read -r u; do
        [[ -z "$u" ]] && continue
        ok=0
        for p in $OOM_CONTAINED_UNIT_PREFIXES; do
            [[ "$u" == "$p"* ]] && { ok=1; break; }
        done
        [[ $ok -eq 1 ]] || return 1
    done <<<"$1"
    return 0
}

check_oom_events() {
    # $1 = the carried baseline spec
    #   counter:local_oom:deficit:deficit_ts:drain
    # (a bare counter from an older caller is accepted: local unverified,
    # deficit 0). Echoes the refreshed spec for the next tick. Never touches
    # stdout except the final spec echo.
    #
    # The spec carries FIVE facts because suppression needs all of them
    # (#1790 review round):
    #   counter     — the oom_kill aggregate (was there a kill?)
    #   local_oom   — the container root's LOCAL oom count (WHOSE limit fired:
    #                 the journal names the victim unit, and a container-limit
    #                 kill can victimise a contained child scope)
    #   deficit     — kills we already paged for whose journal records have not
    #                 been seen yet. systemd's record of a unit failure can be
    #                 emitted AFTER the poll that observed the counter
    #                 increment, and the record's timestamp is PID 1's EMISSION
    #                 time (measured on the live journal, #1790 audit) — so NO
    #                 time window can exclude a late record from the next
    #                 kill's batch. The only sound correlation is
    #                 reconciliation: records returned by a query first retire
    #                 the owed deficit, and only the REST may account for the
    #                 current delta. A late record can therefore never cover a
    #                 kill it does not belong to (Codex P1 / Devin, #1790).
    #   deficit_ts  — when the deficit last GREW. A kill that never writes a
    #                 record (a non-main process dying inside a surviving
    #                 scope) leaves a deficit nothing can retire; it expires
    #                 after 20 poll intervals so contained kills are not
    #                 spuriously paged forever. Residue, accepted: a journald
    #                 outage LONGER than the TTL, ending with a query whose
    #                 backlogged records exactly equal deficit+n, can
    #                 mis-attribute once. systemd's emission lag is
    #                 milliseconds in every measurement we have; the TTL is
    #                 generous against it.
    #   drain       — set when arming could not advance the journal cursor
    #                 (journalctl absent/failing at startup): the FIRST
    #                 resolution must page and re-anchor, because the fallback
    #                 window would return pre-baseline records that could
    #                 otherwise "account" for a post-startup kill.
    local prev_spec="$1" prev prev_local prev_deficit prev_deficit_ts prev_drain
    prev="${prev_spec%%:*}"
    local _r1="" _r2="" _r3="" _r4=""
    [[ "$prev_spec" == *:* ]] && _r1="${prev_spec#*:}"
    prev_local="${_r1%%:*}"
    [[ "$_r1" == *:* ]] && _r2="${_r1#*:}"
    prev_deficit="${_r2%%:*}"
    [[ "$_r2" == *:* ]] && _r3="${_r2#*:}"
    prev_deficit_ts="${_r3%%:*}"
    [[ "$_r3" == *:* ]] && _r4="${_r3#*:}"
    prev_drain="${_r4%%:*}"
    # Numeric hygiene: a malformed element degrades to "unknown", never to a
    # bash arithmetic error under set -e (audit NOTE, #1790).
    [[ "$prev" =~ ^[0-9]+$ ]] || prev=""
    [[ "$prev_local" =~ ^[0-9]+$ ]] || prev_local=""
    [[ "$prev_deficit" =~ ^[0-9]+$ ]] || prev_deficit=0
    [[ "$prev_deficit_ts" =~ ^[0-9]+$ ]] || prev_deficit_ts=0
    [[ "$prev_drain" == "1" ]] || prev_drain=0
    local cur loc_oom now_epoch
    cur=$(_read_oom_kill) || { printf '%s' "$prev_spec"; return 0; }
    [[ "$cur" =~ ^[0-9]+$ ]] || { printf '%s' "$prev_spec"; return 0; }
    loc_oom=$(_read_oom_local_trigger) || loc_oom=""
    [[ "$loc_oom" =~ ^[0-9]+$ ]] || loc_oom=""
    now_epoch=$(date +%s)
    # Expire an unretireable deficit (see the spec comment above). Computed
    # PER CALL, like _oom_killed_units' fallback window: load_config re-sources
    # watchgod.conf every tick and may change POLL_INTERVAL.
    local _deficit_ttl=$(( POLL_INTERVAL * 20 ))
    if (( prev_deficit > 0 && prev_deficit_ts > 0 )) \
        && (( now_epoch - prev_deficit_ts > _deficit_ttl )); then
        prev_deficit=0
    fi
    # LATE ARM (Codex P1, #1790). `main` calls `_oom_arm_baseline` exactly once,
    # at startup. If `memory.events` was unreadable at that moment the spec came
    # back empty -- "monitoring unavailable" -- and the journal cursor was never
    # anchored, because arming returns before it gets that far. The baseline is
    # then actually established HERE, on the first tick where the counter reads,
    # and emitting drain=0 from that path would hand the next kill's query a
    # fallback time window in which a PRE-BASELINE record can explain it away.
    # A malformed spec lands here too, and for the same reason: we do not know
    # what the cursor points at, so we re-anchor or refuse to trust it.
    if [[ -z "$prev" ]]; then
        _oom_arm_cursor || prev_drain=1
        # `main` already told the operator monitoring was off. It is not, from
        # here on, and a log that never retracts a scary line is how someone
        # concludes the monitor is dead while it is running.
        log INFO "OOM event capture armed late (baseline oom_kill=${cur}${prev_drain:+, drain=${prev_drain}})"
    fi
    if [[ -n "$prev" ]] && (( cur > prev )); then
        local n=$(( cur - prev )) stamp
        stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        {
            echo "# OOM event ${stamp}: cgroup oom_kill ${prev} -> ${cur} (+${n})"
            echo "## memory (MB):"; free -m 2>/dev/null | head -2
            echo "## top RSS:"; ps -eo pid,rss,comm --sort=-rss 2>/dev/null | head -12
            echo
        } >> "$OOM_LOG" 2>/dev/null || true
        log WARN "cgroup OOM kill detected (oom_kill ${prev} -> ${cur}); snapshot → ${OOM_LOG}"
        # ATTRIBUTE before paging (issue #1775): the root counter aggregates
        # oom_kill from every descendant cgroup, so a by-design kill inside a
        # resource-capped child scope reads identically to genuine container
        # pressure. Ask the journal which unit died; when EVERY killed unit is
        # a known contained scope, the cap did its job — record it (snapshot +
        # WARN log stay either way) and do not page. Anything else — a
        # non-contained unit, no record, or no journal — pages exactly as
        # before: attribution can only ever DOWNGRADE a known-contained kill,
        # never silence an unknown one.
        #
        # TRIGGER CHECK FIRST (Codex P1, #1790): the journal names the VICTIM,
        # not the cgroup whose limit fired. When the container root's LOCAL
        # oom counter moved, the container's own limit triggered the kill —
        # the victim can still be a contained child, so journal attribution
        # must never suppress this. LIMIT OF THE MECHANISM, stated honestly:
        # an ancestor ABOVE the container root (the host/VM slice) firing and
        # victimising a contained child is indistinguishable from a contained
        # kill at this layer and can still be suppressed — no container-side
        # signal names that trigger. Unreadable local counter = unverifiable
        # trigger = the same fail direction (page).
        local _container_trigger=0
        if [[ -n "$loc_oom" && -n "$prev_local" ]] && (( loc_oom > prev_local )); then
            _container_trigger=1
        fi
        local _oom_units="" _oom_who="unattributed" _oom_n=0 _oom_query_ok=0
        if _oom_units=$(_oom_killed_units); then
            _oom_query_ok=1
            if [[ -n "$_oom_units" ]]; then
                _oom_who=$(printf '%s\n' "$_oom_units" | sort -u | paste -sd, -)
                _oom_n=$(printf '%s\n' "$_oom_units" | grep -c . || true)
            fi
        else
            _oom_units=""
        fi
        # RECONCILE (see the spec comment): the obligations are the owed
        # deficit PLUS this tick's delta; the records returned retire them.
        # Suppress only when records FULLY account for every obligation and
        # every named unit is contained. Anything else pages, and the
        # unaccounted remainder carries forward as the new deficit — which is
        # why a LATE record (returned by a later query) can never cover a kill
        # it does not belong to: by then its own kill is already an
        # obligation. EVERY observed kill must be accounted for, not merely
        # SOME of them (the partially attributed batch was the original
        # fail-open here).
        local _obligations=$(( prev_deficit + n )) _new_deficit
        _new_deficit=$(( _obligations - _oom_n ))
        (( _new_deficit < 0 )) && _new_deficit=0
        if (( prev_drain == 0 && _container_trigger == 0 )) \
            && [[ -n "$loc_oom" && -n "$prev_local" && -n "$_oom_units" ]] \
            && (( _oom_n == _obligations )) \
            && _oom_units_all_contained "$_oom_units"; then
            log WARN "OOM kill contained in [${_oom_who}] — its own resource cap fired, not container pressure; not paging (snapshot kept)"
        else
            # Emergency tier (pages): an OOM kill is a discrete serious event —
            # the usual reason a CC session vanishing with no crash message —
            # not routine tier pressure, so unlike ORANGE it warrants a
            # proactive page (per the 2026-08-19 decision). Deduped per
            # distinct oom_kill total.
            local _why="killed unit(s): ${_oom_who}"
            if (( prev_drain == 1 )); then
                _why="journal cursor could not be armed at startup; killed unit(s): ${_oom_who}"
            elif [[ "$_container_trigger" -eq 1 ]]; then
                _why="container-level trigger (memory.events.local oom ${prev_local} -> ${loc_oom}); killed unit(s): ${_oom_who}"
            elif [[ -z "$loc_oom" || -z "$prev_local" ]]; then
                _why="trigger unverifiable (memory.events.local unreadable); killed unit(s): ${_oom_who}"
            fi
            queue_alert emergency "watchgod:oom" "cgroup OOM kill(s) detected" \
                "${n} process(es) OOM-killed in the container cgroup (oom_kill ${prev}->${cur}; ${_why}). A CC session vanishing with no crash message is often this. Snapshot: ${OOM_LOG}" \
                "watchgod:oom:${cur}"
        fi
        # The deficit clock only restarts when the deficit GROWS; retirements
        # keep the original timestamp so a shrinking deficit cannot live
        # forever by halves.
        if (( _new_deficit > prev_deficit )); then
            prev_deficit_ts=$now_epoch
        fi
        prev_deficit=$_new_deficit
        # drain clears ONLY on a successful query: the flag means "the cursor
        # was never anchored", and a FAILED resolution neither re-anchors it
        # nor returns records — clearing on failure would let the next tick's
        # fallback window offer pre-baseline records as attribution (audit
        # BLOCKER, #1790 round 2).
        # A SUCCESSFUL QUERY IS NOT AN ANCHORED CURSOR, and conflating them
        # gave back exactly what drain was added to prevent. MEASURED with the
        # cursor path unwritable: the arm correctly set drain=1, the next kill
        # paged and cleared drain, its re-anchor silently failed, and the kill
        # after that -- a genuine one whose own record was never written -- was
        # accounted for by the first kill's record, still inside the fallback
        # window, and suppressed. Two kills, one page. Clear drain only when the
        # cursor is verifiably on disk.
        # BOTH DIRECTIONS. `drain` means "the cursor is not anchored", so the
        # cursor decides it -- clearing it on success while never SETTING it left
        # the inverse open, and the inverse is reachable from the ordinary
        # drain=0 state: a re-anchor that cannot persist deletes the cursor
        # (:549) and leaves drain=0 behind, so the next query falls back to the
        # relative window and re-reads the record this tick just counted.
        # MEASURED from `4:0:0:0:0` with the cursor path unwritable: a contained
        # kill, then a real kill that wrote no record of its own -- TWO kills,
        # ZERO pages. Setting drain from the cursor closes it in one place
        # rather than at each site that can fail to write.
        # Clearing needs BOTH: the query succeeded AND the cursor on disk is
        # anchored. The anchored check alone is syntax -- a stale file from a
        # dead epoch passes it -- and on a failed query nothing re-anchored, so
        # clearing there trusts a position nobody verified. Setting needs only
        # the anchor to be missing.
        if ! _oom_cursor_is_anchored; then
            prev_drain=1
        elif (( _oom_query_ok == 1 )); then
            prev_drain=0
        fi
        # Bound the OOM log (retention discipline — matches cc_exit/log rotation);
        # keep the most recent ~1000 lines so a thrashing container can't leak it.
        local oom_lines
        oom_lines=$(wc -l < "$OOM_LOG" 2>/dev/null || echo 0)
        if (( ${oom_lines:-0} > 1000 )); then
            tail -n 1000 "$OOM_LOG" > "${OOM_LOG}.tmp" 2>/dev/null && mv "${OOM_LOG}.tmp" "$OOM_LOG" 2>/dev/null || true
        fi
    fi
    # A transient unreadable local counter must not DISARM future trigger
    # verification: carry the last known local baseline forward (the tick
    # itself still pages — the current value is unknown — and a jump observed
    # once the file is readable again correctly reads as a container trigger).
    printf '%s' "${cur}:${loc_oom:-$prev_local}:${prev_deficit}:${prev_deficit_ts}:${prev_drain}"
}

_oom_persist_cursor() {
    # $1 = a cursor value (with or without the leading `s=`). rc 0 ONLY when the
    # cursor file now verifiably holds it.
    #
    # THE SINGLE WRITER. Every path that advances the cursor goes through here,
    # because a cursor that was not persisted is the one state the whole
    # suppression mechanism cannot survive: queries fall back to a TIME WINDOW,
    # where a record written before the baseline can account for a kill that
    # happened after it.
    #
    # The write's exit status is not enough on its own -- a full filesystem
    # reports the failure on close, leaving an empty or truncated file behind --
    # so the value is read BACK and compared. At the point this matters an
    # unreadable cursor and an absent one are the same thing, and they get the
    # same answer.
    local _want="s=${1#s=}" _back=""
    [[ "$_want" != "s=" ]] || return 1
    # The write's own status is checked, and then the value is read back and
    # compared to what we MEANT to write. The comparison subsumes the status
    # check -- MEASURED: reverting this `|| return 1` to `|| true` turns no test
    # red, because a failed write leaves either nothing (read-back fails) or the
    # OLD value (read-back mismatches). It is kept as the cheap early exit and
    # because a future refactor that weakened the read-back to a bare `s=*`
    # prefix test would make it load-bearing again.
    printf '%s' "$_want" > "$_OOM_CURSOR_FILE" 2>/dev/null || return 1
    _back=$(cat "$_OOM_CURSOR_FILE" 2>/dev/null) || return 1
    [[ "$_back" == "$_want" ]] || return 1
    return 0
}

_oom_cursor_is_anchored() {
    # rc 0 when a usable cursor is on disk. This is the fact `drain` denies, so
    # nothing may clear drain without it.
    local _back=""
    _back=$(cat "$_OOM_CURSOR_FILE" 2>/dev/null) || return 1
    [[ "$_back" == s=* ]] || return 1
    return 0
}

_oom_arm_cursor() {
    # Advance the journal cursor to the current tail and PERSIST it.
    #
    # rc 0 ONLY when the cursor file now holds that position. rc 1 for every
    # other outcome -- journalctl absent, the query failing, the write failing,
    # or a write that reported success and left nothing readable behind. The
    # caller must carry drain=1 on rc 1, because an unarmed cursor is not a
    # cosmetic gap: the next query falls back to a TIME WINDOW, where a record
    # written BEFORE the baseline can account for a kill that happened after it
    # and suppress a real page.
    #
    # `-n 0 --show-cursor` prints the tail cursor with no entries, so this
    # advances the position without consuming anything.
    command -v journalctl >/dev/null 2>&1 || return 1
    local _tail_cursor=""
    _tail_cursor=$(journalctl --user -n 0 --show-cursor --no-pager -o cat 2>/dev/null \
        | sed -n 's/^-- cursor: //p' | tail -1) || _tail_cursor=""
    [[ -n "$_tail_cursor" ]] || return 1
    _oom_persist_cursor "$_tail_cursor"
}

_oom_arm_baseline() {
    # Echo the initial OOM baseline spec
    # ("counter:local_oom:deficit:deficit_ts:drain"; empty = monitoring
    # unavailable) and advance the journal cursor to the current tail (Codex
    # P1, #1790): records of kills that predate the baseline — written before
    # this (re)start, or left behind the cursor by a kill that landed in the
    # gap — must never account for a post-startup kill. `-n 0 --show-cursor`
    # prints the tail cursor with no entries, so this advances the position
    # without consuming anything. When the cursor cannot be armed (journalctl
    # absent or failing), the spec carries drain=1: the first resolution then
    # pages and re-anchors rather than trusting the fallback window's
    # pre-baseline records.
    local base="" loc="" drain=0
    base=$(_read_oom_kill) || base=""
    [[ -z "$base" ]] && { printf '%s' ""; return 0; }
    loc=$(_read_oom_local_trigger) || loc=""
    if ! _oom_arm_cursor; then
        drain=1
        # A cursor file from a PREVIOUS daemon epoch must not survive a failed
        # arm. It passes the anchored check on syntax, but its POSITION predates
        # this baseline -- a query from it returns pre-baseline records, and one
        # of those can account for a post-baseline kill. MEASURED: with a stale
        # file surviving, drain=0 and an expired deficit, one real line-less
        # kill produced ZERO pages. Absent file -> queries use the bounded
        # fallback window and drain=1 covers the first resolution.
        rm -f "$_OOM_CURSOR_FILE" 2>/dev/null || true
    fi
    printf '%s' "${base}:${loc}:0:0:${drain}"
}

# ── Main loop ────────────────────────────────────────────────
main() {
    mkdir -p "$(dirname "$LOG_FILE")" "$ALERT_DIR"
    log INFO "Watchgod starting (poll=${POLL_INTERVAL}s, budget=${CC_TMP_BUDGET_MB}MB, sacred=${SACRED_GROUND_MB}MB)"

    # Baseline the OOM counter at startup so we only page on NEW kills (never the
    # cumulative-since-boot history). Empty baseline = monitoring unavailable.
    local oom_baseline
    oom_baseline=$(_oom_arm_baseline)
    if [[ -z "$oom_baseline" ]]; then
        log INFO "OOM event capture unavailable (no readable ${OOM_EVENTS_FILE}) — OOM monitoring off"
    else
        log INFO "OOM event capture armed (baseline oom_kill=${oom_baseline%%:*})"
    fi

    while true; do
        load_config

        local cc_result sys_result
        cc_result=$(check_cc_tmp)
        sys_result=$(check_sys_tmp)

        local cc_tier="${cc_result%%:*}"
        local cc_used="${cc_result##*:}"
        local sys_tier="${sys_result%%:*}"
        local sys_pct="${sys_result##*:}"

        write_state "$cc_tier" "$cc_used" "$sys_tier" "$sys_pct"

        # Durable OOM capture — snapshot + page on any NEW cgroup OOM kill.
        oom_baseline=$(check_oom_events "$oom_baseline")

        # Clear the shared cc+sys alert flags only when BOTH zones are green:
        # tmp_warning/tmp_emergency are touched by both the cc AND sys handlers
        # (one dedupe key across zones), so a red episode in either zone must
        # keep them set.
        if [[ "$cc_tier" == "green" && "$sys_tier" == "green" ]]; then
            rm -f "$ALERT_DIR/tmp_warning" "$ALERT_DIR/tmp_emergency" 2>/dev/null || true
        fi
        # tmp_orange_stuck is cc-tmp-SPECIFIC (only clean_cc_orange sets it), so
        # clear it whenever cc-tmp is no longer ORANGE — independent of the sys
        # tier. Otherwise a cc episode that falls to YELLOW while /tmp stays
        # non-green leaves a stale flag that suppresses the once-per-episode STUCK
        # record of a later, distinct cc-tmp ORANGE episode.
        if [[ "$cc_tier" != "orange" ]]; then
            rm -f "$ALERT_DIR/tmp_orange_stuck" 2>/dev/null || true
        fi

        # Log rotation — truncate when > 1MB
        local log_size
        log_size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
        if (( log_size > 1048576 )); then
            tail -100 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
        fi

        sleep "$POLL_INTERVAL"
    done
}

# Run the poll loop only when executed directly — sourcing (e.g. from tests) loads the
# functions without starting the daemon.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
