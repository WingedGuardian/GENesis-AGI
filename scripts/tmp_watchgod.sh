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
# Below this many MB reclaimed, a RED pass is logged as having freed ~nothing.
#
# THIS NUMBER IS A JUDGEMENT, NOT A MEASUREMENT — argue with it rather than
# inheriting it. It is set at the noise floor of the measurement that feeds it:
# `fs_free_mb` reads a SHARED btrfs pool, so an unrelated writer elsewhere on
# the volume moves it, and repeated idle readings here varied by ~1MB. 5MB
# gives that a few times' margin while staying ~1% of the 500MB budget, so a
# pass that genuinely helped is never called futile.
#
# It errs toward SILENCE: too high and a real futile pass logs as a success,
# too low and a trickle reads as progress. Log-only today, so either error
# costs a log line — 3b makes this number load-bearing and should re-derive it
# rather than adopt it.
CC_RECLAIM_FLOOR_MB=5
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
    # A non-numeric CC_RECLAIM_FLOOR_MB would be evaluated as 0 inside (( )),
    # silently turning "freed ~nothing" into "freed < 0" — which is never true,
    # so the futility log would go permanently quiet with no error. Restore the
    # default loudly instead; this is a log-only signal, so never fail closed.
    if ! [[ "${CC_RECLAIM_FLOOR_MB:-}" =~ ^(0|[1-9][0-9]*)$ ]]; then
        log WARN "watchgod.conf: CC_RECLAIM_FLOOR_MB='${CC_RECLAIM_FLOOR_MB:-}' is not a non-negative integer — the futility signal would go silent; restoring default 5"
        CC_RECLAIM_FLOOR_MB=5
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
    # Validate, do not merely default. `${x:-0}` fires only on EMPTY, so a
    # NON-EMPTY non-integer passes straight through to a caller's (( )) — see
    # fs_free_mb below for the measured abort that costs.
    [[ "$result" =~ ^[0-9]+$ ]] || result=0
    echo "$result"
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
    # Free space on the filesystem containing the given path, in MB.
    #
    # ⚠ VALIDATE, DO NOT MERELY DEFAULT — `${result:-999999}` fires only when the
    # pipeline produced NOTHING, so a NON-EMPTY non-integer sailed through to
    # every caller's arithmetic. MEASURED 2026-09-25, on the real script:
    #
    #   df prints `not-a-number` -> fs_free_mb returns it -> `$(( a - b ))`
    #   parses it as the expression `not - a - number`, `not` is an unset name,
    #   and `set -u` aborts the DAEMON. Reproduced end-to-end: the poll died
    #   mid-cleanup with `line 924: not: unbound variable`.
    #
    # `-` is the realistic trigger, not a contrived one: some pseudo-filesystems
    # report `-` in the avail column, `tr -d ' M'` leaves it, and `$(( x - - ))`
    # is a syntax error — also fatal.
    #
    # This is a PRE-EXISTING exposure (check_cc_tmp has done arithmetic on this
    # value since long before the futility signal); it is fixed here rather than
    # at the new call site because the primitive is where the class lives. The
    # validate-or-fallback shape is already this file's own idiom — see
    # clean_cc_red's `[[ "$headroom" =~ ^-?[0-9]+$ ]] || headroom=...`.
    local result
    result=$(df -BM --output=avail "$1" 2>/dev/null | tail -1 | tr -d ' M') || true
    [[ "$result" =~ ^[0-9]+$ ]] || result=999999
    echo "$result"
}

fs_free_mb_strict() {
    # Same reading, but reports UNMEASURABLE as an EMPTY string instead of
    # substituting a plausible number.
    #
    # `fs_free_mb`'s 999999 fallback keeps existing callers safe, and for a
    # THRESHOLD question ("is there enough room?") an optimistic default is the
    # right failure direction. For a DELTA it is the wrong one: 999999 minus a
    # real 300 reads as `freed=999699MB`, i.e. a fabricated SUCCESS that
    # silences the very signal the delta exists to raise. A caller computing a
    # difference must be able to tell "no reading" from "a big number".
    local result
    result=$(df -BM --output=avail "$1" 2>/dev/null | tail -1 | tr -d ' M') || true
    [[ "$result" =~ ^[0-9]+$ ]] || result=""
    echo "$result"
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

cc_reclaim_snapshot() {
    # One reading of BOTH space measures for "how much did that pass actually
    # reclaim": `<du_mb>:<free_mb>`. Taken before and after a cleanup, the pair
    # answers a question neither half can answer alone.
    #
    # WHY BOTH, and why `free` is the authoritative one here. MEASURED
    # 2026-09-25 on this btrfs backend, writing 500 MB then unlinking it while a
    # descriptor stayed open:
    #
    #   state            du     df-used
    #   baseline           0     151752
    #   after 500MB      500     152253
    #   unlinked+held      0     152253   <-- du says 500 MB freed; NOTHING was
    #   after close        0     151752
    #
    # `du` walks the visible tree, so an unlinked-but-held file leaves it the
    # instant the name goes away — while the blocks stay allocated until the
    # holder closes. A du-based before/after therefore reports a phantom
    # reclaim on EXACTLY the pass this signal exists to catch: cleanup unlinked
    # something a live process still holds, and freed nothing. `df` sees those
    # blocks and is correct.
    #
    # This is NOT a contradiction of clean_cc_orange's "use du" comment, and
    # that comment should not be read as applying here — it is answering a
    # DIFFERENT question ("am I still over the line, should I kill sessions?"),
    # where exiting the loop early is the safe direction. For "was this pass
    # futile?", exiting early is the failure.
    #
    # The `free` half has two known limits, and BOTH BIAS TOWARD A FALSE
    # FUTILE — the loud direction, not the quiet one. An earlier draft of this
    # comment claimed the opposite; it was wrong on both limbs:
    #   * on btrfs this measures the SHARED pool, so a concurrent writer
    #     elsewhere CONSUMES free space, shrinking the delta -> toward futile;
    #   * it settles lazily, so a reading taken right after a delete has not
    #     yet risen, also shrinking the delta -> toward futile.
    # That matters because the regime where it fires — a RED episode with
    # sessions writing hard — is exactly when a concurrent writer is likeliest.
    # Erring loud is the acceptable direction for a log-only signal, but "a
    # check that cries wolf gets silenced", so 3b must not inherit this as a
    # safety argument without measuring the busy case.
    #
    # ⚠ The 5MB floor's basis is IDLE jitter (~1MB across repeated readings on
    # an idle pool; an independent review measured 0MB across six consecutive
    # readings). Nobody has measured the busy case. Deriving a load-bearing
    # threshold from the idle regime is deriving it in the wrong regime — see
    # CC_RECLAIM_FLOOR_MB.
    #
    # Uses fs_free_mb_STRICT: an unmeasurable reading must come back empty, not
    # as 999999, because 999999 minus a real number renders as a huge fabricated
    # reclaim and silences the signal.
    local path="$1"
    printf '%s:%s' "$(dir_usage_mb "$path")" "$(fs_free_mb_strict "$path")"
}

live_open_paths_with_pid() {
    # Open-descriptor targets WITH the holding pid: `<pid> <path>` per line.
    #
    # Deliberately NOT a change to `live_open_paths`. That one emits a bare path
    # and every consumer (dir_has_live_writer, live_writer_units) assumes the
    # path starts at column 1 — prefixing a pid there would break all of them
    # silently, with no parse error and no test failure, just a guard that stops
    # matching. A separate reader costs one extra /proc walk on a RED-only path
    # and cannot regress the existing ones.
    #
    # Emits TAB-separated `<pid>\t<deleted|live>\t<path>`.
    #
    # TAB, not space: `%l` targets can contain spaces, and a space-split `$2`
    # truncates `/tmp/my file.bin` to `/tmp/my` — a docstring that promises
    # `<pid> <path>` while silently lying for any such path.
    #
    # The `deleted` flag is the DISCRIMINATOR, and dropping it was the original
    # defect here. A futile pass has exactly two causes and they need OPPOSITE
    # remedies:
    #   * deleted-but-pinned — the sweep unlinked it and a live process holds
    #     the inode. Deleting more cannot help; the holder must exit.
    #   * spared-and-live — a guard (in-flight, freshness, active session)
    #     declined to delete it. Deleting more MIGHT help; look at the guard.
    # The kernel distinguishes them for free: an unlinked-but-held fd's target
    # reads `/path/x (deleted)`. Reporting one string for both hands the
    # operator the news and not the answer. This mirrors the reasoning already
    # in this file for the headroom components — log the COMPONENTS, not just
    # the result.
    #
    # `%h` is the dirname of the matched /proc/<pid>/fd/<n>, i.e.
    # `/proc/<pid>/fd`; the pid is extracted from it. Fails OPEN (empty) like
    # its sibling — a caller that needs a validated snapshot must self-test,
    # because an empty result here is indistinguishable from "nothing is live".
    find /proc/[0-9]*/fd -maxdepth 1 -type l -printf '%h\t%l\n' 2>/dev/null \
        | awk -F'\t' '{
              split($1, p, "/")
              d = ($2 ~ / \(deleted\)$/) ? "deleted" : "live"
              t = $2; sub(/ \(deleted\)$/, "", t)
              print p[3] "\t" d "\t" t
          }' 2>/dev/null || true
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
#   Orange : > 75%  — yellow + delete caches + kill idle sessions + alert
#   Red    : > 90% OR fs free < sacred — nuclear cleanup + emergency alert

clean_cc_yellow() {
    # Same in-flight exclusions the tiers above use. YELLOW fires at 50% of
    # budget — more often than either — and deletes *.tmp older than 60min: a
    # long download's still-open .tmp is in-flight work by RED's own standard.
    # Snapshot cost ~50-100ms per poll, measured.
    local -a _yellow_excl=()
    zone_a_live_exclusions _yellow_excl "$(live_open_paths)" "$CC_TMP_DIR"
    log INFO "Zone A YELLOW — cleaning stale session dirs and temp files"

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
    log WARN "Zone A ORANGE — deleting caches, then re-measuring before any session kill"

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

    # LOOP-BREAK: re-measure AFTER the cleanup above and kill idle sessions ONLY
    # if we're still over the ORANGE line. This closes the runaway that killed no
    # session but churned for ~4.5h on 2026-08-19: the tier that dispatched us
    # here was measured BEFORE cleanup, so without a re-measure the daemon would
    # re-enter ORANGE every poll and re-run the kill loop forever while the real
    # filler (a pytest tree the cache-evict never touches) sat untouched. Killing
    # an idle session cannot reduce cc-tmp anyway (sessions aren't the filler), so
    # a kill here is at best useless and at worst reaps an innocent bystander.
    # Use dir_usage_mb (du) — it drops immediately after rm; df can lag on
    # held-open deleted fds.
    local used_after threshold_orange
    used_after=$(dir_usage_mb "$CC_TMP_DIR")
    threshold_orange=$(( CC_TMP_BUDGET_MB * 75 / 100 ))
    if (( used_after <= threshold_orange )); then
        log INFO "ORANGE resolved by cache cleanup (used=${used_after}MB <= ${threshold_orange}MB) — no session kills"
        rm -f "$ALERT_DIR/tmp_orange_stuck" 2>/dev/null || true
        return 0
    fi

    log WARN "ORANGE persists after cleanup (used=${used_after}MB > ${threshold_orange}MB) — evaluating idle sessions"
    # Kill idle CC tmux sessions (unattached, idle > 2h)
    local killed_any=0
    while IFS= read -r session; do
        [[ -z "$session" ]] && continue
        local sname
        sname=$(echo "$session" | cut -d: -f1)
        if [[ "$sname" =~ ^cc- ]]; then
            local last_activity
            last_activity=$(tmux display-message -t "$sname" -p '#{session_activity}' 2>/dev/null || echo 0)
            local now
            now=$(date +%s)
            local idle_s=$(( now - last_activity ))
            if (( idle_s > 7200 )); then
                log WARN "Killing idle CC session: $sname (idle ${idle_s}s)"
                # Count a reap only when tmux actually killed it — if the session
                # vanished between listing and killing (or the kill fails), we
                # reclaimed nothing, so killed_any must stay 0 and the stuck
                # marker must still be recorded rather than silently skipped.
                if tmux kill-session -t "$sname" 2>/dev/null; then
                    killed_any=1
                fi
            fi
        fi
    done < <(tmux list-sessions -F '#{session_name}:#{session_attached}' 2>/dev/null | grep ':0$' || true)

    # Stuck-ORANGE: cleanup didn't resolve it AND nothing was killable → the
    # daemon has nothing safe left to do. Per design D2 (ORANGE is dashboard/log
    # only — only RED pages) this does NOT page; it records the stuck state ONCE
    # (dedupe flag) in the log instead of silently re-polling forever, so the
    # condition is discoverable. If cc-tmp keeps filling it escalates to RED,
    # which DOES page. The flag is cleared (main loop) whenever cc-tmp LEAVES
    # ORANGE (green/yellow/red) — never on a kill: reaping an idle session does
    # not reduce cc-tmp, so a kill that leaves us ORANGE keeps cc_tier==orange and
    # must not re-arm and re-log the same episode.
    if (( killed_any == 0 )) && [[ ! -f "$ALERT_DIR/tmp_orange_stuck" ]]; then
        log WARN "cc-tmp STUCK ORANGE (used=${used_after}MB, budget=${CC_TMP_BUDGET_MB}MB): reclaim freed nothing and no idle (>2h) session is killable — non-reclaimable data is filling cc-tmp (see cc_tmp_top snapshots). Dashboard/log-only per D2; RED will page if it escalates."
        touch "$ALERT_DIR/tmp_orange_stuck"
    fi
}

clean_cc_red() {
    log WARN "Zone A RED — NUCLEAR cleanup, preserving active session"

    # Futility signal, part 1 of 2: snapshot BOTH space measures before the
    # sweep. Paired with the reading at the end of this function, it turns
    # "nuclear cleanup complete" — a line this daemon printed 356 times in one
    # 2h45m episode while reclaiming zero — into a statement with a number in
    # it. LOG-ONLY: nothing below changes a verdict, a tier, or what gets
    # deleted. See cc_reclaim_snapshot for why `free` is the authoritative half.
    local _reclaim_before
    _reclaim_before=$(cc_reclaim_snapshot "$CC_TMP_DIR")

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

    # Futility signal, part 2 of 2 — LOG-ONLY, no escalation, no verdict change.
    local _reclaim_after _du_before _du_after _free_before _free_after
    local _freed_mb
    _reclaim_after=$(cc_reclaim_snapshot "$CC_TMP_DIR")
    _du_before="${_reclaim_before%%:*}";  _free_before="${_reclaim_before##*:}"
    _du_after="${_reclaim_after%%:*}";    _free_after="${_reclaim_after##*:}"
    # free RISES as space is reclaimed; du FALLS. Both deltas are reported
    # because their DISAGREEMENT is itself the diagnosis: du falling while free
    # stays flat means the sweep unlinked something a live process still holds,
    # so the name is gone and the blocks are not.
    # An UNMEASURABLE reading is its own state — never rendered as a reclaim of
    # any size. Saying "could not measure" is the honest answer; substituting a
    # number here is how one failed `df` becomes a fabricated success.
    if [[ -z "$_free_before" || -z "$_free_after" ]]; then
        log WARN "Zone A RED — nuclear cleanup complete; reclaim UNMEASURABLE (df gave no usable reading). du ${_du_before}→${_du_after}MB. $(cc_pinned_by_live_holders)"
        return 0
    fi

    _freed_mb=$(( _free_after - _free_before ))

    if (( _freed_mb < CC_RECLAIM_FLOOR_MB )); then
        log WARN "Zone A RED — nuclear cleanup complete but reclaimed ~nothing: freed=${_freed_mb}MB (floor ${CC_RECLAIM_FLOOR_MB}MB), du ${_du_before}→${_du_after}MB. $(cc_pinned_by_live_holders)"
    else
        # `${_du_before}→${_du_after}` already carries the sign unambiguously.
        # An explicit `-${_du_delta}` renders as `du --5MB` whenever cc-tmp GREW
        # during the sweep, which is the normal RED condition (a session writing
        # while it runs), so the redundant field is dropped rather than fixed.
        log WARN "Zone A RED — nuclear cleanup complete: freed=${_freed_mb}MB (du ${_du_before}→${_du_after}MB)"
    fi
}

cc_pinned_by_live_holders() {
    # One-line attribution for a futile pass: WHICH live pids hold descriptors
    # under cc-tmp. Resolved AT the moment of detection, because the holder may
    # be gone a second later and a log proving only WHEN buys another
    # occurrence.
    #
    # Reports the count of distinct holders and names the top few by descriptor
    # count. It deliberately does NOT claim how many MB each pins: mapping a
    # holder to its pinned BYTES needs the unlinked inode's size, which is not
    # available from the fd symlink target alone. Saying "pid X holds N open
    # paths here" is what this can honestly support; 3b's page needs the byte
    # figure and will have to earn it separately.
    # The needle goes through the ENVIRONMENT, not `awk -v`, which expands
    # backslash escapes in the value — this file already carries that lesson 650
    # lines up: a directory named `ta\tb` silently fails to match under -v, and
    # a fail-OPEN follows (here: "holders: none found" on a pass that has them).
    local snapshot
    snapshot=$(live_open_paths_with_pid 2>/dev/null \
        | WG_CC_DIR="$CC_TMP_DIR/" awk -F'\t' 'index($3, ENVIRON["WG_CC_DIR"]) == 1' || true)
    if [[ -z "$snapshot" ]]; then
        # Fails OPEN like its siblings, and says so rather than asserting "no
        # holders" — an unreadable /proc and an empty one are the same string.
        echo "holders: none found (an unreadable /proc reads identically — not proof of absence)"
        return 0
    fi
    local n_holders n_deleted top
    n_holders=$(printf '%s\n' "$snapshot" | awk -F'\t' '{print $1}' | sort -u | wc -l)
    # Split out the deleted-but-pinned holders: for THOSE, deleting more cannot
    # reclaim anything and the holder has to go. That is the actionable half.
    n_deleted=$(printf '%s\n' "$snapshot" | awk -F'\t' '$2 == "deleted" {print $1}' | sort -u | wc -l)
    top=$(printf '%s\n' "$snapshot" | awk -F'\t' '{print $1}' | sort | uniq -c | sort -rn \
          | awk 'NR<=3 {printf "pid %s(%s fds) ", $2, $1}')
    echo "holders: ${n_holders} live pid(s) with descriptors under cc-tmp, ${n_deleted} pinning DELETED inodes (unreclaimable by further deletion) — ${top}"
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

live_open_paths_selftested() {
    # `live_open_paths` with a POSITIVE self-test, printed on stdout. Returns 1 —
    # printing nothing — when the self-test fails.
    #
    # WHY: `live_open_paths` ends in `2>/dev/null || true`, and
    # `dir_has_live_writer` opens with `[[ -n "$snapshot" ]] || return 1`. BOTH
    # halves fail OPEN, so a failed or partial /proc read is indistinguishable
    # from "nothing is live" — and the caller is a loop that DELETES whatever it
    # believes to be dead.
    #
    # The test is POSITIVE, not an emptiness check: a partial read, another uid's
    # writer, or a root spelling the kernel does not use all yield a NON-empty
    # snapshot with a structurally blind guard. So open a descriptor we KNOW
    # about, under the same root the caller will match against, and require it
    # back.
    #
    # THE RETURN IS THE VALIDATED STRING ITSELF, and that is a fix rather than a
    # style choice. An earlier version piped it through
    # `printf ... | grep -vxF -- "$probe" || true` to drop the probe's own
    # record — and that one line reintroduced the exact fail-open this function
    # exists to close: ANY failure of that pipeline yields rc 0 with EMPTY
    # stdout, which the caller reads as a valid snapshot in which nothing is
    # alive. Written two lines below its own fix, by the same `|| true` idiom the
    # comment above condemns. The strip was also unnecessary: the probe lives at
    # "<root>/.wg-bprobe.XXXXXX" while every candidate is matched under
    # "<root>/pytest-of-*/", so the probe's record can never match a candidate.
    #
    # DUPLICATION, acknowledged: Zone A does this too (`clean_cc_red`). They are
    # not merged here because Zone A's degrade policy is deliberately the
    # opposite — it keeps an unvalidated snapshot and warns, because its floor
    # path has a reason to press on — and because folding a refactor of a
    # freshly hardened sibling into this change is how both get broken at once.
    local probe_dir="$1" probe="" probe_fd snap=""
    probe="$(mktemp "$probe_dir/.wg-bprobe.XXXXXX" 2>/dev/null)" || probe=""
    if [[ -n "$probe" ]]; then
        # Guarded `exec` inside a brace group: an unguarded failing `exec {fd}>`
        # exits the daemon under set -e, and an `exec` with no command applies
        # its redirection PERMANENTLY. Same two hazards Zone A documents.
        if { exec {probe_fd}>"$probe"; } 2>/dev/null; then
            snap="$(live_open_paths)"
            exec {probe_fd}>&-
        fi
        rm -f "$probe"
    fi
    # Whole-line containment in pure bash, never `printf | grep -q`: grep -q
    # exits on its first match and SIGPIPEs the printf, which under pipefail
    # reports 141 and reads as a FAILED self-test on every large snapshot.
    [[ -n "$probe" && $'\n'"$snap"$'\n' == *$'\n'"$probe"$'\n'* ]] || return 1
    printf '%s\n' "$snap"
}

reclaim_dead_pytest_dirs() {
    # Reclaim pytest base directories under $2 (default /tmp) that no live run
    # owns and that are older than the caller's tier gate ($1).
    #
    # WHY THIS EXISTS. Every generic sweep below carries `-not -path "*/pytest-*"`,
    # to avoid deleting files out from under a running suite. The exclusion cannot
    # tell a live run from a dead one, so it protects the garbage equally — and
    # MEASURED 2026-09-24, that is not a theoretical cost: 255 MB of dead pytest
    # trees, half of a 512 MB tmpfs, survived the emergency tier while it churned
    # every 30 seconds for 22 minutes reporting "aggressive cleanup complete".
    #
    # So the generic sweeps keep their exclusion — they must never nibble
    # individual files out of a live tree — and pytest directories are handled
    # here instead, WHOLE, by liveness rather than by name.
    #
    # ENUMERATION IS `find -P`, NOT A GLOB, and that is a containment fix rather
    # than a tidy-up. `/tmp` is mode 1777, so any uid can plant a SYMLINK named
    # `pytest-of-something` pointing anywhere. A bash glob follows it: MEASURED,
    # `for d in "$root"/pytest-of-*/*` yields paths inside the link's target, and
    # they satisfy `[[ -d ]]`, so the recursive delete below leaves $root
    # entirely. A per-leaf `[[ -L "$d" ]]` guard does not help — the leaf is a
    # real directory; the escape is the PARENT. `find -P` never follows a symlink
    # and refuses to descend a symlinked component at all (measured: zero results
    # against the same fixture), which closes the class rather than one level of
    # it. -print0 because a newline in a directory name would otherwise split one
    # record into two, and neither half would match.
    #
    # THE AGE GATE IS UNCONDITIONAL AND COMES FIRST. That ordering is the fix for
    # a defect found in this function's first draft, and it is worth stating
    # because the broken shape looks entirely reasonable: the gate lived in the
    # `elif` of the no-lock branch, so a LOCKED directory whose owner had exited
    # fell straight through to the delete with no age test at all. Since this
    # project always writes a lock, that was the common path — the tier constants
    # were cosmetic, and YELLOW (at 51% of the filesystem, every 30s) would have
    # deleted a tree the instant its suite exited. A dead owner is a NECESSARY
    # condition for reclaiming, never a sufficient one: pytest deliberately
    # retains the last `keep` runs so a developer can open the artifacts of a
    # failing run (`make_numbered_dir_with_cleanup`, _pytest/pathlib.py:363-396).
    #
    # THE LIVENESS SIGNALS, each read from pytest's own contract or from /proc.
    # Past the age gate, a directory is spared if ANY holds:
    #
    #   1. It is a symlink. pytest maintains `pytest-current` pointing at the
    #      newest run. Mirrors `ensure_deletable` (_pytest/pathlib.py:312).
    #   2. A live process holds a descriptor open underneath it — the Zone A
    #      signal, reused. Catches a live run whatever its lock says, including
    #      `--basetemp` and keep=0 runs that have no lock at all.
    #   3. Its `.lock` names a pid with a live `/proc` entry. pytest writes its
    #      own pid INTO the lock at creation (pathlib.py:254-256) and unlinks it
    #      at exit behind a fork guard (:263-279).
    #
    #      `/proc/<pid>`, NOT `kill -0`. MEASURED on bash 5.2.21: `kill -0 1`
    #      returns 1 ("Operation not permitted") and `kill -0 999999` returns 1
    #      ("No such process") — EPERM and ESRCH are indistinguishable, so
    #      signalling reads a LIVE process owned by another uid as dead.
    #
    #      The pid is SHAPE-CHECKED before it is used, which the first /proc
    #      version dropped along with the `kill` it replaced. `tr -dc` produces a
    #      digit string from anything, and two spellings then skip signal 4 and
    #      go straight to the delete: a 36-digit run of garbage (no such /proc
    #      entry, so "dead"), and a zero-padded pid like `0755` for a LIVE
    #      process 755 (no `/proc/0755`, so "dead"). Anything that is not a plain
    #      1-7 digit decimal falls through to the mtime rule instead.
    #   4. The lock exists but carries no usable pid. Fall back to pytest's own
    #      published threshold and spare until the lock is LOCK_TIMEOUT old,
    #      exactly as `ensure_deletable` does (pathlib.py:327).
    #
    # A directory with no lock at all is reclaimable once past the age gate — the
    # same answer `ensure_deletable` gives (pathlib.py:316-317). That covers a
    # project configured with retention `none`, where `if keep != 0`
    # (pathlib.py:390) means a live run holds no lock; signal 2 and the age gate
    # are what protect it.
    #
    # Nothing here ever unlinks a `.lock`. pytest's own GC does that when it
    # expires one (pathlib.py:333); a watchdog doing it would hand a second
    # cleaner a directory this one had decided to spare.
    local min_age_min="$1" root="${2:-/tmp}"
    local d lock pid reclaimed=0 failed=0 snapshot

    # pytest's own staleness threshold, from the installed _pytest/pathlib.py:46
    # (`LOCK_TIMEOUT = 60 * 60 * 24 * 3`), verified against pytest 9.0.2, in
    # MINUTES for `find -mmin`. Anything shorter is more aggressive than pytest's
    # own garbage collector and reaps a suite that legitimately runs longer.
    #
    # LOCAL, not a top-level global. `load_config` re-sources watchgod.conf on
    # every poll with no key allowlist, so a global of this name would be
    # settable from configuration — and setting it to 0 makes the daemon reap
    # trees pytest's published rule says are live. The same reasoning moved the
    # sweep root to a parameter; this is the other half of that class.
    local lock_timeout_min=4320

    if ! snapshot="$(live_open_paths_selftested "$root")" || [[ -z "$snapshot" ]]; then
        # FAIL CLOSED, on a failed self-test OR an empty result.
        #
        # THE EMPTINESS TEST IS THE GUARD; the unstripped return above is
        # simplification. Stated this way round because the obvious reading is
        # the opposite, and an earlier version of this comment asserted the
        # opposite before it was checked.
        #
        # A passing self-test proves the probe's own path is in the snapshot, so
        # with an unstripped return this branch is unreachable — the value cannot
        # be empty. It is kept for the case where it is not: the first version
        # returned `... | grep -vxF -- "$probe" || true`, whose failure mode is
        # rc 0 with EMPTY stdout, and the caller then tested only the status.
        #
        # MEASURED, and the number is the point: restoring the strip, removing
        # this test, and doing BOTH all leave the suite green. The pair is NOT
        # bound by any test here, and no fixture was found that distinguishes
        # them — an empty snapshot arises only when /proc showed nothing but the
        # probe, and in that state both variants reclaim the same directories.
        # So this is defence in depth against a future edit, not a behaviour any
        # arm observes. Do not delete it on the grounds that nothing fails.
        #
        # The limit worth knowing: the self-test is POSITIVE, not complete. A
        # partial /proc read that happens to include the probe passes it, and
        # neither guard helps there.
        #
        # Skipping one 30-second poll costs nothing; deleting on a blind reading
        # destroys a live suite's work.
        log WARN "Zone B — /proc snapshot failed its self-test under $root (this process's own open probe was not visible, or the reading came back empty); SKIPPING the pytest reclaim this poll rather than deleting on a reading that cannot see live writers"
        return 0
    fi

    while IFS= read -r -d '' d; do
        [[ -L "$d" ]] && continue
        [[ -d "$d" ]] || continue
        # (0) the caller's tier gate — unconditional, and before every signal
        [[ -z "$(find -P "$d" -maxdepth 0 -mmin "+$min_age_min" 2>/dev/null)" ]] && continue
        # (2) an open descriptor underneath is decisive, whatever the lock says
        dir_has_live_writer "$d" "$snapshot" && continue
        lock="$d/.lock"
        if [[ -f "$lock" ]]; then
            pid="$(head -c 32 "$lock" 2>/dev/null | tr -dc '0-9')"
            if [[ "$pid" =~ ^[1-9][0-9]{0,6}$ ]]; then
                [[ -d "/proc/$pid" ]] && continue      # (3) owner alive -> spare
            # (4) no usable pid -> pytest's own three-day rule
            elif [[ -z "$(find -P "$lock" -maxdepth 0 -mmin "+$lock_timeout_min" 2>/dev/null)" ]]; then
                continue
            fi
        fi
        if rm -rf -- "$d" 2>/dev/null; then
            reclaimed=$((reclaimed + 1))
        else
            failed=$((failed + 1))
        fi
    done < <(find -P "$root" -mindepth 2 -maxdepth 2 -path "$root/pytest-of-*" \
                 \( -name 'pytest-*' -o -name 'garbage-*' \) -print0 2>/dev/null)

    (( reclaimed > 0 )) && log INFO "Zone B reclaimed $reclaimed dead pytest dir(s) (age gate ${min_age_min}min)"
    # A PARTIAL delete must be loud. Silence here would reproduce the exact
    # pathology this function exists to fix: the tier reports "cleanup complete",
    # reclaims nothing, and retries every 30 seconds with nobody able to see why.
    (( failed > 0 )) && log WARN "Zone B — $failed pytest dir(s) could not be removed (possibly PARTIALLY deleted); something under them is undeletable by this uid"
    return 0
}

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
        # Emergency gate for pytest trees — 60 minutes, matching the file sweep
        # immediately above, and tighter than the tier's own 1440 gate applied
        # by check_sys_tmp before this point.
        reclaim_dead_pytest_dirs 60
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

    # The pytest reclaim is dispatched HERE, once per poll with the tier's own
    # age gate, rather than inside each clean_sys_* function. clean_sys_orange
    # calls clean_sys_yellow for its file sweep, so a reclaim living in both ran
    # twice per poll — two full /proc walks at ~50-120ms each — with the first
    # pass strictly subsumed by the second.
    if (( pct > 85 )); then
        tier="red"
        reclaim_dead_pytest_dirs 1440
        clean_sys_red
    elif (( pct > 70 )); then
        tier="orange"
        reclaim_dead_pytest_dirs 4320
        clean_sys_orange
    elif (( pct > 50 )); then
        tier="yellow"
        reclaim_dead_pytest_dirs 10080
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
