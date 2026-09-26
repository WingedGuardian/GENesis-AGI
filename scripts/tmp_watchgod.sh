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

session_path_has_numeric_uid() {
    # $1 = a path under $2 = the sweep root. True when the first component
    # below the root is `claude-` followed by an ALL-NUMERIC uid.
    #
    # This rule does not belong in the find predicate. `-path` takes a GLOB, and
    # `[0-9]*` in a glob is ONE digit followed by anything — not an all-digit
    # suffix. REPRODUCED: `claude-1cache/proj/item` matched and would have been
    # reaped, which is the sibling-cache data loss this function exists to
    # prevent, re-created by the predicate meant to prevent it. fnmatch cannot
    # express "one or more digits" at all. GNU `-regextype posix-egrep -regex`
    # CAN — measured — but it is not the better shape: it would trade escaping
    # the root for a glob into escaping it for an ERE, a strictly larger
    # metacharacter set, and it is GNU-specific. So the find pattern stays
    # broad and the digit rule is enforced here, where it can be stated
    # exactly and tested directly.
    local rest="${1#"$2"/}"
    [[ "${rest%%/*}" =~ ^claude-[0-9]+$ ]]
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
    local snapshot="$2" root="$3"
    _wg_out=()
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

reap_stale_session_dirs() {
    # Reap per SESSION, judging staleness by the session's CONTENTS.
    #
    # WHAT THIS REPLACES, and why it was data loss. The old predicate was
    #
    #     find "$CC_TMP_DIR" -mindepth 2 -maxdepth 2 -type d \
    #          -path "*/claude-*/???*" -mtime +7 -exec rm -rf {} +
    #
    # which is wrong on all three counts:
    #
    #   * DEPTH 2 is the PROJECT directory, so one `rm -rf` took every session
    #     under it, not the stale one.
    #   * `-mtime +7` on a DIRECTORY tests the directory's OWN mtime, and a
    #     project directory's mtime moves only when a session dir is created or
    #     removed directly under it — never when a live session writes inside
    #     one. So it answers "when did a session last START here", not "is
    #     anything here still in use".
    #   * `claude-*` is unanchored and also matches sibling caches such as
    #     `claude-skills` (see #1878, which absorbed #2297).
    #
    # REPRODUCED against the unfixed predicate: a project whose newest file was
    # written SECONDS ago, with its own directory mtime backdated 30 days, was
    # selected for deletion. Work in one project for a month without starting a
    # new session there and it ages out with every session it holds.
    #
    # FAILS CLOSED. The freshness probe distinguishes "nothing inside is fresh"
    # from "could not look" by EXIT STATUS, not by empty output — the two are
    # the same string, and conflating them is how #2342 shipped a fail-open
    # twice in one review. MEASURED on GNU findutils 4.9.0, which is what the
    # daemon resolves `find` to: rc=0 for a match AND for no-match, rc=1 for a
    # missing or unreadable directory. An unreadable session is SPARED and said
    # so out loud.
    local root="$1" age_days="$2"
    shift 2
    local -a excl=("$@")

    # VALIDATE the age before it becomes a date string. `find -newermt` does not
    # reject garbage — GNU parse_datetime REINTERPRETS most of it, usually as a
    # timezone, and returns SUCCESS. MEASURED 2026-09-25 against GNU findutils
    # 4.9.0, with today at 2026-09-25:
    #
    #     '7 days ago'   -> 2026-09-18   rc=0   (intended)
    #     '7d days ago'  -> 2026-09-23   rc=0   <- 2 days, not 7
    #     ' days ago'    -> 2026-09-24   rc=0   <- today
    #     'X days ago'   -> 2026-09-24   rc=0   <- today
    #     'abc days ago' -> UNPARSEABLE  rc=1
    #
    # Every rc=0 row moves the cutoff FORWARD, so a caller passing "7d", or an
    # unset variable, reaps nearly every session in the tree — silently, with
    # the fail-closed path never firing because the status is 0, and with the
    # log line still reading "newer than 7d". Only the last row errors.
    # The sole caller hardcodes 7 today; this is here so that stays safe when
    # it becomes configurable.
    if ! [[ "$age_days" =~ ^[1-9][0-9]*$ ]]; then
        log WARN "YELLOW session reap: age_days='${age_days}' is not a positive integer — refusing to reap (a malformed age silently widens the cutoff instead of erroring)"
        return 0
    fi
    local cutoff="${age_days} days ago"
    local sess probe reaped=0 spared_live=0 spared_blind=0
    local -a blind_paths=()

    # ANCHOR THE GLOB TO $root. `-path`'s `*` matches `/`, so the unanchored
    # `*/claude-[0-9]*/*/*` is satisfied by a `claude-<digit>` component in the
    # ROOT'S OWN PATH — after which the pattern stops discriminating and every
    # depth-3 directory under the root is eligible. MEASURED: with a root of
    # `…/claude-1000/fakeroot`, the unanchored form matched (and would have
    # reaped) `pip-unpack-xyz/wheels/numpy` and `tsx-cache/v1/build`; the
    # anchored form matches nothing. Reachable on any install whose $HOME
    # contains such a component, and this ships to every clone. Same bug class
    # as the `claude-*` defect this function exists to fix — that one was fixed
    # at the instance and left open one level up.
    # NORMALISE AND ESCAPE THE ROOT before it becomes part of a glob. `-path`
    # interprets its whole argument as a pattern even though the expansion is
    # quoted, so a bracket or question mark anywhere in $HOME silently turns
    # these predicates into non-matching patterns: every enumeration returns
    # ZERO with status 0, stale sessions survive, and no warning fires because
    # nothing failed. A trailing slash does the same through a doubled
    # separator. REPRODUCED under a root containing `[r]`.
    local canon_root="$root"
    while [[ "$canon_root" == */ && "$canon_root" != "/" ]]; do canon_root="${canon_root%/}"; done
    local esc_root
    esc_root="$(glob_escape "$canon_root")"

    # THE STATUS COMES FROM THE ENUMERATION THAT ACTUALLY FEEDS THE LOOP.
    # It used to come from a separate no-output walk, which is a PROXY: if the
    # second find failed or partially traversed after the first had succeeded —
    # permissions or the tree changing between the two — sessions were left
    # unexamined while enum_rc stayed 0 and the warning never fired.
    # MEASURED 2026-09-26: bash sets $! for a process substitution and `wait`
    # returns its real exit status (3 from a deliberately failing arm, 0 from
    # the control, both having read the same records), so one invocation can
    # carry both the output and the status. `$(...)` still cannot: bash drops
    # NUL bytes from a command substitution, destroying the separator.
    # `wait` on a process substitution needs bash 5.0 (bash NEWS: 4.4 makes the
    # procsub visible as $!, 5.0 lets `wait` take it). Guard the version rather
    # than assume it: under `set -u` — which this script sets — reading an
    # unset $! does not warn, it ABORTS, and a dead watchgod is how cc-tmp
    # fills and CC sessions get killed. Below 5.0 the status is simply not
    # collected, which is the behaviour before this fix, not a new failure.
    local enum_rc=0 enum_pid=0

    while IFS= read -r -d '' sess; do
        # The find pattern cannot express the all-digits rule; see the helper.
        session_path_has_numeric_uid "$sess" "$canon_root" || continue
        if ! probe=$(find "$sess" -newermt "$cutoff" -print -quit 2>/dev/null); then
            spared_blind=$((spared_blind + 1))
            (( ${#blind_paths[@]} < 3 )) && blind_paths+=("$sess")
            continue
        fi
        if [[ -n "$probe" ]]; then
            spared_live=$((spared_live + 1))
            continue
        fi
        rm -rf -- "$sess" 2>/dev/null && reaped=$((reaped + 1))
    done < <(find "$canon_root" -mindepth 3 -maxdepth 3 -type d \
                  -path "$esc_root/claude-*/*/*" \
                  ${excl[@]+"${excl[@]}"} -print0 2>/dev/null)
    if (( BASH_VERSINFO[0] >= 5 )); then
        enum_pid=$!
        wait "$enum_pid" || enum_rc=1
    fi

    # A project directory left with no sessions is an empty shell; remove it so
    # the tree does not accumulate them. `-empty` is exact — MEASURED: `-delete`
    # uses rmdir semantics and refuses a non-empty directory — so this can never
    # take a project that still holds a session.
    #
    # `-mmin +60` is NOT redundant with `-empty`. A project directory is EMPTY
    # for the instant between its own mkdir and its first session's mkdir, and
    # YELLOW polls every 30s whenever cc-tmp is over half its budget. MEASURED:
    # a project created that instant was deleted by this pass. The live-writer
    # exclusions cannot help — a directory created a millisecond ago holds no
    # open descriptor. An empty shell is never urgent, so it gets the same hour
    # of grace the sibling temp-file sweep already gives.
    # A loop rather than -delete, because the all-digits rule has to be applied
    # per candidate. rmdir keeps the guarantee -delete gave: it refuses a
    # non-empty directory, so this can never take a project holding a session.
    local proj
    while IFS= read -r -d '' proj; do
        session_path_has_numeric_uid "$proj" "$canon_root" || continue
        # find(1) rather than rmdir(1): -maxdepth 0 -empty -delete has the same
        # refuse-if-non-empty semantics without adding a new PATH dependency to
        # a sweep that would otherwise become a silent no-op if rmdir were
        # missing or shadowed.
        find "$proj" -maxdepth 0 -empty -delete 2>/dev/null || true
    done < <(find "$canon_root" -mindepth 2 -maxdepth 2 -type d \
                  -path "$esc_root/claude-*/*" \
                  -empty -mmin +60 ${excl[@]+"${excl[@]}"} -print0 2>/dev/null)
    # The SAME status lesson, applied to the second walk. Taking it for the
    # session enumeration and not this one would leave exactly the defect the
    # WARN below claims to have closed — an unreadable project producing an
    # entirely empty log — alive one loop over.
    if (( BASH_VERSINFO[0] >= 5 )); then
        enum_pid=$!
        wait "$enum_pid" || enum_rc=1
    fi

    # Both blind spots are LOUD. An earlier version made the per-session probe
    # loud and left the ENUMERATION silent, so an unreadable PROJECT produced an
    # entirely empty log — indistinguishable from "nothing to reclaim", which is
    # the failure `zone_a_live_exclusions` already argues against in this file.
    if (( enum_rc != 0 )); then
        log WARN "YELLOW session reap: enumeration hit unreadable subtrees under ${root} — some sessions were never examined this sweep"
    fi
    if (( spared_blind > 0 )); then
        log WARN "YELLOW session reap: SPARED ${spared_blind} session dir(s) whose freshness could not be determined (unreadable) — failing closed, not deleting; first: ${blind_paths[*]}"
    fi
    if (( reaped > 0 || spared_live > 0 )); then
        log INFO "YELLOW session reap: reaped ${reaped} stale session dir(s), spared ${spared_live} with content newer than ${age_days}d"
    fi
}

clean_cc_yellow() {
    # Same in-flight exclusions the tiers above use. YELLOW fires at 50% of
    # budget — more often than either — and deletes *.tmp older than 60min: a
    # long download's still-open .tmp is in-flight work by RED's own standard.
    # Snapshot cost ~50-100ms per poll, measured.
    local -a _yellow_excl=()
    zone_a_live_exclusions _yellow_excl "$(live_open_paths)" "$CC_TMP_DIR"
    log INFO "Zone A YELLOW — cleaning stale session dirs and temp files"

    # Reap stale SESSIONS by their contents. See reap_stale_session_dirs for
    # why the old per-project, directory-mtime form was data loss.
    reap_stale_session_dirs "$CC_TMP_DIR" 7 ${_yellow_excl[@]+"${_yellow_excl[@]}"}

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

    # Delete claude-skills cache (~35MB, CC re-clones on demand)
    find "$CC_TMP_DIR" -type d -name "claude-skills" \
        ${live_excl[@]+"${live_excl[@]}"} -exec rm -rf {} + 2>/dev/null || true

    # Delete tsx cache (~1.2MB, rebuilt automatically)
    find "$CC_TMP_DIR" -type d -name "tsx-*" \
        ${live_excl[@]+"${live_excl[@]}"} -exec rm -rf {} + 2>/dev/null || true

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
    zone_a_live_exclusions live_excl "$live_paths" "$CC_TMP_DIR"

    # Reap every depth-1 dir except the newest session's ancestor and any
    # directory a live process is writing into — object-level and
    # socket-sparing (see reap_dir_sparing_sockets); loose files are the
    # separate sweep below. `|| true` matches the file's find idiom: a
    # transient find error must not abort the daemon mid-RED under
    # set -euo pipefail.
    local dir
    while IFS= read -r dir; do
        # Skip if this contains the active session. Deliberately NOT added to
        # any exclusion list: this spares for a DIFFERENT reason than the
        # live-writer branch, and the loose sweep below already carries its own
        # deliberately narrower -not -path "$newest_session/*". Promoting this
        # to the depth-1 parent would exclude every sibling project tree under
        # claude-<uid>/ from the sweep (MEASURED: 7 trees, 1 of them live).
        # Below the oxygen floor even this sparing goes: the active session's
        # tree is as reclaimable as anything else when the alternative is
        # ENOSPC for every session including that one.
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
    done < <(find "$CC_TMP_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null) || true

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
    local cache_dir
    while IFS= read -r cache_dir; do
        [[ -d "$cache_dir" ]] || continue   # an outer match may have taken it
        reap_dir_sparing_sockets "$cache_dir"
    done < <(find "$CC_TMP_DIR" -type d \( -name "claude-skills" -o -name "tsx-*" \) \
        ${live_excl[@]+"${live_excl[@]}"} \
        ${session_excl[@]+"${session_excl[@]}"} \
        2>/dev/null) || true

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
