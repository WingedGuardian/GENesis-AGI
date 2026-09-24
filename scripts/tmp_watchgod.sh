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
CP_IDS_FILE="$HOME/.genesis/alerts/control_plane_severed_ids"

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

# The socket-listing tool for the control-plane check. Overridable for the same
# reason OOM_EVENTS_FILE is: it is the only way to exercise the "tool absent"
# branch without rebuilding PATH from scratch, and an operator on a box where
# iproute2 lives elsewhere can point at it.
SS_BIN="${SS_BIN:-ss}"

# Zone B's target. A literal /tmp made this whole zone untestable — a test would
# have had to sweep the real one — which is why its sweeps had no coverage at all
# until the 2026-09-07 socket-tree finding. Overridable purely as a seam; nothing
# in production sets it.
SYS_TMP_DIR="${SYS_TMP_DIR:-/tmp}"

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
    # Normalise ONCE, here, so no later site has to remember. watchgod.conf is
    # re-sourced every poll and `CC_TMP_DIR=/path/to/cc-tmp/` is a perfectly valid
    # thing to write there; a trailing slash then broke string surgery downstream
    # in one place while another had its own `%/` guard — normalised here, and
    # nowhere else, that whole class cannot recur (Codex P2, PR #1856).
    CC_TMP_DIR="${CC_TMP_DIR%/}"
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
    if df -T "$SYS_TMP_DIR" 2>/dev/null | grep -q tmpfs; then
        # tmpfs: filesystem percentage is meaningful
        local result
        result=$(df --output=pcent "$SYS_TMP_DIR" 2>/dev/null | tail -1 | tr -d ' %') || true
        echo "${result:-0}"
    else
        # Not tmpfs (/tmp on root disk): use absolute free space thresholds.
        # Danger is the same regardless of disk size — CC sessions need ~60MB
        # each, sacred ground is 150MB.  Percentage-based thresholds are
        # meaningless when measuring the whole root filesystem.
        local free_mb
        free_mb=$(df -BM --output=avail "$SYS_TMP_DIR" 2>/dev/null | tail -1 | tr -d ' M') || true
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
    #
    # The socket DIRECTORY is spared as well, because sparing the inodes is not
    # enough: an EMPTY cc-socks has no socket inside it to keep it non-empty, so
    # the depth-first pass removes it — and it is the directory the next session
    # binds into. Zone B's empty-dir sweep already spares it; this is the same
    # rule in Zone A, applied at the one function every Zone A caller goes
    # through rather than at each call site.
    #
    # `-type d -name` and NOT `-path '*/cc-socks*'`: `-path` matches the whole
    # path and its `*` crosses `/`, so the path form would also spare every
    # reclaimable FILE sitting inside the directory — which RED must still
    # reclaim, and which `test_red_reclaims_files_inside_socket_dir` pins.
    # `-name` matches the basename only and cannot widen.
    find "$1" -depth -not -type s \
        -not \( -type d -name 'cc-socks' \) \
        -not \( -type d -name 'cc-daemon-*' \) \
        -delete 2>/dev/null || true
}

# ── Control plane: severed CC messaging sockets ───────────────
#
# Claude Code binds ONE unix socket per session for cross-session messaging,
# at `$XDG_RUNTIME_DIR/cc-socks/<pid>.sock` — falling back to the process temp
# dir when XDG_RUNTIME_DIR is unset, which on a Genesis install is cc-tmp: the
# very directory this daemon reclaims.
#
# Deleting the socket PATH does not stop the listener. The process keeps the
# inode, so `ss` still reports LISTEN and the session looks healthy from the
# inside, while every peer resolves BY PATH and gets ENOENT. Nothing re-binds
# after startup, so the session stays unreachable for the rest of its life and
# cannot notice. Measured 2026-09-05: one sweep severed 3 of 4 sessions here and
# 6 of 6 on a sibling install; the only survivor had started after the sweep.
#
# The sweeps learned to spare sockets, which prevents recurrence but cannot heal
# a session that is already severed. This check makes the state VISIBLE: it
# compares live listeners against what is on disk, so a severance is reported
# instead of silent. Strictly READ-ONLY — a stale socket file is counted and
# never deleted, because deleting sockets from this daemon is what caused the
# outage in the first place.
#
# Path-agnostic on purpose: directories come from the live listeners, so this
# keeps working unchanged if the sockets move out of cc-tmp later.
#
# SCOPE. This watches the per-session MESSAGING sockets under `cc-socks/` only.
# CC also runs a separate daemon tree (`/tmp/cc-daemon-<uid>/…` — control, pty and
# rendezvous sockets for the background-spare machinery), which is deliberately
# NOT counted here: what severance MEANS there has not been established, and
# reporting on a subsystem whose failure semantics you have not verified is a
# guess wearing a number. Those sockets are protected from deletion all the same
# (see the Zone B sweeps) — protect what you do not understand, report only what
# you do.
#
# Echoes "<status>:<severed>:<stale>:<listeners>". status is one of:
#   ok      — the probe ran and saw at least one socket, live or left behind.
#   empty   — the probe ran and saw NOTHING. Usually true (no CC sessions), but
#             it is also what a detector that has gone blind looks like (an `ss`
#             output change, a netns move, sockets relocating out of cc-socks),
#             so it is NOT reported as health.
#   unknown — the probe could not run at all. Never a clean plane: "could not
#             measure" and "measured, and it is fine" must not share a value.
check_control_plane() {
    command -v "$SS_BIN" >/dev/null 2>&1 || { echo "unknown:0:0:0"; return 0; }

    local raw rc=0
    raw=$("$SS_BIN" -xlpH state listening 2>/dev/null) || rc=$?
    if (( rc != 0 )); then
        echo "unknown:0:0:0"
        return 0
    fi

    local listeners=0 severed=0 stale=0
    local severed_ids=""
    local -A live=()
    local -A dirs=()
    # Always scan cc-tmp's own socket dir, even when no listener points there:
    # on a fully-severed install every listener path is already gone from disk,
    # and that is exactly when the leftovers still need counting. The trailing
    # slash a re-sourced watchgod.conf could carry is stripped once in
    # `load_config` (a local `%/` here would be a second place to remember), so
    # this key always matches the ones `find` produces.
    dirs["$CC_TMP_DIR/cc-socks"]=1

    local path
    while IFS= read -r path; do
        [[ -z "$path" ]] && continue
        listeners=$(( listeners + 1 ))
        live["$path"]=1
        dirs["$(dirname "$path")"]=1
        # -S is "exists AND is a socket": a path replaced by an ordinary file is
        # no more reachable than a missing one, so it counts as severed too.
        if [[ ! -S "$path" ]]; then
            severed=$(( severed + 1 ))
            # Carry the IDENTITY, not just the tally. A count cannot tell "the
            # same four sessions are still severed" from "those four exited and a
            # different one broke": both read as a number going down, and the new
            # severance would never page. Basenames are `<pid>.sock`, so the field
            # is bounded by the number of live CC sessions.
            severed_ids+="${severed_ids:+ }$(basename "$path")"
        fi
        # NOTE: `ss -xlp` lists every user's sockets. On a shared box another
        # user's cc-socks path would be counted here, and an unstattable one
        # would read as severed. Genesis is single-user by design, so this is
        # left as a known limitation rather than engineered around.
        # Whichever FIELD looks like a socket path — never a fixed index. Two
        # reviewers pushed this in opposite directions (one to $4, one to $5)
        # because the column count is not stable: `ss` prints a State column for
        # unix sockets, and MEASURED here on iproute2-6.1.0 with `state listening`
        # it does not, putting the path at $4 while the man page's row shape says
        # $5. A guard whose verdict depends on which release of a tool is
        # installed is the wrong shape; matching the field by what it IS cannot
        # be wrong either way. Scoped to a field, so the `users:((...))` column
        # can never supply one.
    done < <(awk '{for (i = 1; i <= NF; i++) if ($i ~ /\/cc-socks\/[^\/]*\.sock$/) { print $i; break }}' <<<"$raw" | sort -u)

    local d f
    for d in "${!dirs[@]}"; do
        [[ -d "$d" ]] || continue
        while IFS= read -r f; do
            [[ -z "$f" ]] && continue
            [[ -n "${live[$f]:-}" ]] || stale=$(( stale + 1 ))
        done < <(find "$d" -maxdepth 1 -type s -name '*.sock' 2>/dev/null)
    done

    # Identities travel on their OWN channel, never as a fifth field. Packing them
    # into the colon-delimited string meant two readers with different arities:
    # `write_state` reads four, so bash handed it "1:123.sock" as the listener
    # count and the state file became invalid JSON exactly when a severance made
    # it worth reading (Codex P1, PR #1856). A positional protocol with a
    # variable-length tail cannot be extended safely; this one is fixed-width
    # again, and the tail has a file of its own.
    printf '%s' "$severed_ids" > "$CP_IDS_FILE" 2>/dev/null || true
    if (( listeners == 0 && stale == 0 )); then
        echo "empty:0:0:0"
        return 0
    fi
    echo "ok:${severed}:${stale}:${listeners}"
}

# Should this control-plane reading page, and which severances are now reported?
# Pure set logic, kept out of the poll loop so it can be tested without running
# the daemon.
#
# IDENTITIES, NOT A COUNT. An earlier version tracked a high-water COUNT that
# followed the number down, and a count cannot distinguish "the same four
# sessions are still severed" from "those four exited and a different one broke".
# Both read as 4 -> 1, the bar dropped to 1, and the NEW severance then never
# paged — silently losing the one alert the detector exists to send. Sets do not
# have that failure: a severance is news iff its own id has not been reported.
#
#   CONFIRMATION — an id must appear on two consecutive polls before it can page.
#   A session exiting between ss's snapshot and its own unlink reads as severed
#   for a single poll, and a monitor that cries wolf gets ignored.
#
#   FORGET WHAT RECOVERED — an id that is no longer severed drops out of the
#   reported set, so if that pid is ever severed again it is news again.
#
# Args: <prev-ids> <already-paged-ids> <current-ids>  (space separated).
# Echoes "<0|1 page>:<new already-paged ids>".
control_plane_page_decision() {
    local prev=" $1 " paged=" $2 " cur="$3"
    local id page=0 new_paged=""
    for id in $cur; do
        # Confirmed = seen on the previous poll too.
        if [[ "$prev" == *" $id "* ]]; then
            new_paged+="${new_paged:+ }$id"
            [[ "$paged" == *" $id "* ]] || page=1
        elif [[ "$paged" == *" $id "* ]]; then
            # Already reported and still severed: keep it, do not re-page.
            new_paged+="${new_paged:+ }$id"
        fi
    done
    echo "${page}:${new_paged}"
}

write_state() {
    local cc_tier="$1" cc_used="$2" sys_tier="$3" sys_pct="$4"
    # Control-plane tuple from check_control_plane, "status:severed:stale:listeners".
    # Defaulted so a caller that predates this field (the existing tier tests)
    # still writes a valid state file.
    local cp="${5:-unknown:0:0:0}"
    local cp_status cp_severed cp_stale cp_listeners
    IFS=: read -r cp_status cp_severed cp_stale cp_listeners <<<"$cp"
    local is_tmpfs="false"
    if df -T "$SYS_TMP_DIR" 2>/dev/null | grep -q tmpfs; then
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
  "control_plane": {"status": "${cp_status:-unknown}", "severed_sockets": ${cp_severed:-0}, "stale_sockets": ${cp_stale:-0}, "listeners": ${cp_listeners:-0}},
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
#   Orange : > 75%  — yellow + delete caches + warn (never kills a session)
#   Red    : > 90% OR fs free < sacred — nuclear cleanup + emergency alert

# The 7-day session reap. Two things here are load-bearing, and both were wrong
# before (measured on a live install, 2026-09-07):
#
#   DEPTH. The layout under cc-tmp is claude-<uid>/<project>/<session-uuid>/…,
#   so depth 2 is the PROJECT directory, not a session. On this install one
#   depth-2 directory held 54 session workspaces — every live session's
#   scratchpad and its running background-task output. The intended target has
#   always been the depth-3 per-SESSION directory.
#
#   STALENESS. A directory's mtime tracks only its DIRECT children, so a project
#   directory goes stale the moment no NEW session starts under it, however busy
#   the sessions inside are. Measured: the one actively-used project directory
#   carried an mtime 2.1 days old while its contents had been written seconds
#   earlier, and every dormant directory showed no divergence at all — the gap
#   appears precisely on the directory that must not be deleted. Seven quiet days
#   and a routine 50%-usage YELLOW would have reaped live workspaces at a tier
#   that pages nobody. So staleness is tested against the CONTENTS, recursively.
#
# The freshness probe fails CLOSED: a directory whose freshness cannot be
# determined counts as fresh and is kept. Deleting on an unreadable probe is how
# one broken predicate turns a housekeeping sweep into total data loss.
reap_stale_session_dirs() {
    local cutoff
    cutoff=$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || cutoff=""
    if [[ -z "$cutoff" ]]; then
        log WARN "session reap skipped — could not compute the 7-day cutoff"
        return 0
    fi

    local sdir probe rc
    while IFS= read -r sdir; do
        [[ -z "$sdir" ]] && continue
        rc=0
        probe=$(find "$sdir" -newermt "$cutoff" -print -quit 2>/dev/null) || rc=$?
        if (( rc == 0 )) && [[ -z "$probe" ]]; then
            # Socket-sparing, like every other sweep in this file. A socket's
            # mtime is its BIND time, so a session that bound one here and then
            # wrote nothing for seven days reads as stale — and a plain `rm -rf`
            # would have this daemon sever a live session in the very sweep added
            # to stop it doing that. The rmdir then succeeds only if nothing
            # survived, which is exactly the intent.
            reap_dir_sparing_sockets "$sdir"
            rmdir "$sdir" 2>/dev/null || true
        fi
    done < <(find "$CC_TMP_DIR" -mindepth 3 -maxdepth 3 -type d -path "*/claude-*/*/*" 2>/dev/null)

    # A project directory the reap emptied holds nothing, and without this they
    # accumulate. `-mtime +7` is not about staleness here — an empty directory has
    # nothing to be stale — it closes a race: CC creates <project>/ and then
    # <session-uuid>/ as two steps, and an rmdir landing between them gives the
    # starting session ENOENT. A directory created moments ago cannot match.
    find "$CC_TMP_DIR" -mindepth 2 -maxdepth 2 -type d -path "*/claude-*/*" -empty \
        -mtime +7 -delete 2>/dev/null || true
}

clean_cc_yellow() {
    log INFO "Zone A YELLOW — cleaning stale session dirs and temp files"

    reap_stale_session_dirs

    # Clean old temp files (*.tmp, *.env, *.yaml) > 1 hour old
    find "$CC_TMP_DIR" -type f \( -name "*.tmp" -o -name "*.env" -o -name "*.yaml" \) \
        -mmin +60 -delete 2>/dev/null || true
}

clean_cc_orange() {
    clean_cc_yellow
    log WARN "Zone A ORANGE — deleting caches, then re-measuring (ORANGE never kills a session)"

    # Delete claude-skills cache (~35MB, CC re-clones on demand)
    find "$CC_TMP_DIR" -type d -name "claude-skills" -exec rm -rf {} + 2>/dev/null || true

    # Delete tsx cache (~1.2MB, rebuilt automatically)
    find "$CC_TMP_DIR" -type d -name "tsx-*" -exec rm -rf {} + 2>/dev/null || true

    mkdir -p "$ALERT_DIR"
    touch "$ALERT_DIR/tmp_warning"

    # Re-measure AFTER the cleanup above. Use dir_usage_mb (du) — it drops
    # immediately after rm, where df can lag on held-open deleted fds.
    #
    # ORANGE DOES NOT KILL SESSIONS. It used to reap unattached tmux sessions
    # idle over 2h, and the loop-break comment that guarded it already made the
    # case against itself: sessions are not what fills cc-tmp, so a kill here
    # reclaims essentially nothing while destroying a session's entire context.
    # That is the exact inversion this daemon must not make — it exists to stop
    # runaway usage from killing CC, not to kill CC to tidy up. The measurement
    # settles the cost of removing it: across the whole log history
    # (2026-08-19 → 2026-09-07, 7,563 lines, 1,029 ORANGE polls) the kill loop
    # was reached ONCE and killed ZERO sessions, so removal is behaviour-neutral
    # on the record we have. RED remains the pressure valve and still reaps.
    local used_after threshold_orange
    used_after=$(dir_usage_mb "$CC_TMP_DIR")
    threshold_orange=$(( CC_TMP_BUDGET_MB * 75 / 100 ))
    if (( used_after <= threshold_orange )); then
        log INFO "ORANGE resolved by cache cleanup (used=${used_after}MB <= ${threshold_orange}MB)"
        rm -f "$ALERT_DIR/tmp_orange_stuck" 2>/dev/null || true
        return 0
    fi

    # Stuck-ORANGE: the cleanup did not resolve it and there is nothing else safe
    # to do. Per design D2 (ORANGE is dashboard/log only — only RED pages) this
    # does NOT page; it records the stuck state ONCE (dedupe flag) so the
    # condition is discoverable instead of silently re-polling forever. If cc-tmp
    # keeps filling it escalates to RED, which DOES page. The flag is cleared in
    # the main loop whenever cc-tmp LEAVES ORANGE.
    if [[ ! -f "$ALERT_DIR/tmp_orange_stuck" ]]; then
        log WARN "cc-tmp STUCK ORANGE (used=${used_after}MB, budget=${CC_TMP_BUDGET_MB}MB): reclaim freed nothing — non-reclaimable data is filling cc-tmp (see cc_tmp_top snapshots). Dashboard/log-only per D2; RED will page if it escalates."
        touch "$ALERT_DIR/tmp_orange_stuck"
    fi
}

clean_cc_red() {
    log WARN "Zone A RED — NUCLEAR cleanup, preserving active session"

    # Which workspace is ACTIVE. UNCHANGED FROM main, DELIBERATELY — see below.
    #
    # This selector is NOT part of this change, and two attempts to improve it
    # here both shipped a regression that deleted the live session workspace
    # while logging "preserving active session". Recorded so the next reader does
    # not make it three:
    #
    #   1. Widening to `-mindepth 3 -type f` (to let a file's mtime outrank a
    #      stale directory mtime) silently RE-ANCHORED the `-path` glob. `find`'s
    #      `-path` matches the WHOLE path and its `*` crosses `/`, so with the
    #      `-maxdepth` bound gone, `*/claude-*` matches a component OR BASENAME
    #      beginning `claude-` ANYWHERE in the tree. A file under an unrelated
    #      depth-1 directory then wins the sort, the reduction below names that
    #      directory as the active project, and the depth-1 loop reaps the real
    #      one. MEASURED with one decoy: main preserves the live tree, the
    #      widened selector destroys it. The shape is not hypothetical — pytest
    #      basetemps live inside cc-tmp, so the test suite plants it.
    #   2. Reducing the winning entry to `<uid>/<project>` by string surgery
    #      (`${p#$ROOT/}` then `%%/*`) turns a mis-selection into a plausible
    #      wrong answer rather than an obvious one, and inherits every
    #      normalisation bug in the configured path.
    #
    # The real fix is not a narrower glob: RED should not INFER which session is
    # live from filesystem mtimes at all, when the live set is directly
    # observable (this file already enumerates listening CC sockets in
    # `check_control_plane`). That is a redesign of the nuclear tier's preserve
    # rule, tracked in issue #1878 — it must not ride along in a change about
    # reaping sessions instead of projects.
    #
    # KNOWN LIMITATION, carried from main unchanged: depth 2 is the PROJECT
    # directory, whose mtime moves only when a session dir is created or removed
    # directly under it — never when a live session writes. So this ranks
    # projects by "when did a session last start here". MEASURED on a live
    # install (2026-09-07): the active project's newest FILE was 3 days newer
    # than its own directory mtime, and that directory led a dormant project's by
    # 10 minutes. The YELLOW reap below no longer has this defect; RED still does,
    # and closing it is the tracked redesign above.
    local newest_session=""
    newest_session=$(find "$CC_TMP_DIR" -mindepth 2 -maxdepth 2 -type d -path "*/claude-*" \
        -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | awk '{print $2}') || true

    # Reap every depth-1 dir except the newest session's ancestor —
    # object-level and socket-sparing (see reap_dir_sparing_sockets); loose
    # depth-1 files are the separate sweep below. `|| true` matches the
    # file's find idiom: a transient find error must not abort the daemon
    # mid-RED under set -euo pipefail.
    find "$CC_TMP_DIR" -mindepth 1 -maxdepth 1 -type d | while IFS= read -r dir; do
        # Skip if this contains the active session
        if [[ -n "$newest_session" && "$newest_session" == "$dir/"* ]]; then
            continue
        fi
        reap_dir_sparing_sockets "$dir"
    done || true

    # Delete all reclaimable files except those modified in last 60s
    find "$CC_TMP_DIR" -type f -not -newermt '60 seconds ago' \
        -not -path "$newest_session/*" -delete 2>/dev/null || true

    # Delete caches unconditionally
    find "$CC_TMP_DIR" -type d \( -name "claude-skills" -o -name "tsx-*" \) \
        -exec rm -rf {} + 2>/dev/null || true

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

    # After the cc-tmp blast-radius split, free_mb measures the DEDICATED
    # volume, so this sacred-ground trigger guards that volume (not the rootfs).
    # On a 2 GiB volume it is a pure backstop behind the 450 MiB budget-red
    # above; rootfs free-space monitoring lives in Zone B (/tmp) below.
    if (( used_mb > threshold_red )) || (( free_mb < SACRED_GROUND_MB )); then
        tier="red"
        _log_cc_pressure red "$used_mb" "$free_mb"   # capture BEFORE the nuclear cleanup erases it
        clean_cc_red
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
    find "$SYS_TMP_DIR" -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -path "*/claude-*" -not -name "*.sock" \
        -atime +7 -delete 2>/dev/null || true
    # The file sweeps above spare `*.sock`; this one must spare their DIRECTORIES,
    # which the socket exclusion cannot cover. MEASURED 2026-09-07: this predicate
    # matched `/tmp/cc-socks` and `/tmp/cc-daemon-1000/<id>/pty` — the latter an
    # empty rendezvous directory inside a LIVE CC daemon's tree, created up front
    # and populated later. An empty directory under a socket tree is a rendezvous
    # point, not garbage, and deleting one is the same failure class as deleting
    # the socket itself. (An earlier audit of this sweep called it safe "because
    # socket-holding dirs are non-empty" — true of the directory holding the
    # socket, false of its siblings.)
    find "$SYS_TMP_DIR" -mindepth 1 -type d -empty \
        -not -path "*/tmux-*" -not -path "*/pytest-*" -not -path "*/claude-*" \
        -not -path "*/cc-socks*" -not -path "*/cc-daemon-*" \
        -delete 2>/dev/null || true
}

clean_sys_orange() {
    clean_sys_yellow
    log WARN "Zone B ORANGE — cleaning /tmp files not accessed in 3+ days"
    find "$SYS_TMP_DIR" -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -path "*/claude-*" -not -name "*.sock" \
        -atime +3 -delete 2>/dev/null || true
    mkdir -p "$ALERT_DIR"
    touch "$ALERT_DIR/tmp_warning"
}

clean_sys_red() {
    log WARN "Zone B RED — aggressive /tmp cleanup"
    # Files not accessed in 1+ day
    find "$SYS_TMP_DIR" -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -path "*/claude-*" -not -name "*.sock" \
        -atime +1 -delete 2>/dev/null || true

    # If still critical, remove all regular files except last 1h, sockets, tmux, pytest, claude
    local pct_after
    pct_after=$(tmp_usage_pct)
    if (( pct_after > 85 )); then
        find "$SYS_TMP_DIR" -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -path "*/claude-*" -not -name "*.sock" \
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

    # Control-plane paging state. `prev` is the previous poll's count and starts
    # at -1 so no page can fire on the daemon's very first reading — an elevated
    # count must be CONFIRMED by a second consecutive poll. Without that, a
    # session exiting between ss's snapshot and its own unlink reads as one
    # severed listener for a single poll and would page spuriously.
    #
    # `paged` is the level already reported. It survives a restart in a file, the
    # way the RED tier's transition marker does: the unit is Restart=always, a
    # severance is UNHEALABLE without restarting the sessions, and an in-memory
    # level would therefore re-page the same unfixed condition after every deploy
    # or crash-restart. It still follows the count DOWN, so once the sessions ARE
    # restarted a later, smaller severance pages again.
    local cp_prev_ids="" cp_last=""
    local cp_paged_file="$ALERT_DIR/control_plane_paged"
    local cp_paged_ids
    cp_paged_ids=$(cat "$cp_paged_file" 2>/dev/null) || cp_paged_ids=""
    # Ids are socket basenames; anything else is a file from an older version
    # (which stored a count) or corruption — start clean rather than treat a
    # stray token as a reported severance.
    #
    # This pattern must stay as WIDE as the producer, which takes any basename
    # ending `.sock` (see check_control_plane). Today CC names them `<pid>.sock`,
    # but a validator narrower than its producer fails in the worst direction:
    # ONE non-conforming id would fail the whole-string match, discard every
    # reported severance, and re-page the same unhealed condition after every
    # restart — exactly what this file exists to prevent.
    [[ "$cp_paged_ids" =~ ^([^[:space:]]+\.sock( [^[:space:]]+\.sock)*)?$ ]] || cp_paged_ids=""

    while true; do
        load_config

        local cc_result sys_result cp_result
        cc_result=$(check_cc_tmp)
        sys_result=$(check_sys_tmp)
        cp_result=$(check_control_plane)

        local cc_tier="${cc_result%%:*}"
        local cc_used="${cc_result##*:}"
        local sys_tier="${sys_result%%:*}"
        local sys_pct="${sys_result##*:}"

        write_state "$cc_tier" "$cc_used" "$sys_tier" "$sys_pct" "$cp_result"

        # FOUR fields, matching the fixed-width contract check_control_plane
        # publishes. The identities come from their own channel — see the note
        # there on why a variable-length tail cannot ride a positional string.
        local cp_status cp_severed cp_stale cp_listeners cp_ids
        IFS=: read -r cp_status cp_severed cp_stale cp_listeners <<<"$cp_result"
        cp_ids=$(cat "$CP_IDS_FILE" 2>/dev/null) || cp_ids=""

        # Log only on CHANGE — this runs every poll and an unchanged plane has
        # nothing to say.
        if [[ "$cp_result" != "$cp_last" ]]; then
            case "$cp_status" in
                ok)
                    log INFO "control plane: ${cp_listeners} listener(s), ${cp_severed} severed (socket path deleted under a live listener), ${cp_stale} stale socket file(s)" ;;
                empty)
                    log INFO "control plane: no CC sockets visible — either no session is running, or this check can no longer see them" ;;
                *)
                    log INFO "control plane: unknown (${SS_BIN} unavailable or failed) — severance cannot be detected on this box" ;;
            esac
            cp_last="$cp_result"
        fi

        if [[ "$cp_status" == "ok" ]]; then
            local cp_decision cp_should_page
            cp_decision=$(control_plane_page_decision \
                "$cp_prev_ids" "$cp_paged_ids" "$cp_ids")
            cp_should_page="${cp_decision%%:*}"
            local cp_next_paged="${cp_decision#*:}"
            if [[ "$cp_should_page" == "1" ]]; then
                log WARN "control plane SEVERED: ${cp_severed} of ${cp_listeners} session(s) unreachable"
                # The dedupe key carries the IDENTITIES, for the same reason the
                # paging decision does. Keyed on the COUNT, a severance of one
                # session replaced by a severance of a different session inside
                # the drainer's 24h dedupe window is a second "count 1" alert —
                # rejected and unlinked, while this daemon has already recorded
                # the new id as paged and will never raise it again (Codex P2,
                # PR #1856).
                # `warning` is the honest severity for the event, but note it does
                # NOT buy a quieter delivery: the container drain submits every
                # queued entry at one category and salience regardless of this
                # argument, so this pages exactly like the RED emergency does.
                # That is intended here (the owner asked to be paged on a NEW
                # severance) — recorded so nobody infers a tier that does not exist.
                # Record the severance as reported ONLY once the entry is on
                # disk. `queue_alert` is best-effort by contract — it swallows
                # every failure and degrades to a no-op when its library is
                # absent — so marking first would let an unwritable queue silence
                # the alert permanently, on exactly the degraded box this
                # detector exists to expose. Count the queue instead of trusting
                # the return value.
                local _q="${_ALERT_QUEUE_ROOT:-$HOME/.genesis/alerts/queue}"
                local _before _after
                # `set -euo pipefail` is on, and `find` on a MISSING directory
                # exits nonzero — which under pipefail fails the whole
                # substitution and kills the daemon outright. On a clean install
                # the queue does not exist until queue_alert makes it, so the
                # probe added to verify delivery would have taken the service
                # down on the first severance and again after every restart
                # (Codex P1, PR #1856). Create it first, and give every count a
                # floor so no arithmetic can inherit an empty string.
                mkdir -p "$_q" 2>/dev/null || true
                _before=$( { find "$_q" -maxdepth 1 -name '*.json' 2>/dev/null || true; } | wc -l)
                queue_alert warning "watchgod:control-plane" \
                    "CC control plane severed (${cp_severed} session(s) unreachable)" \
                    "${cp_severed} of ${cp_listeners} live Claude Code session(s) are listening on a socket whose PATH no longer exists, so peers get ENOENT and cannot reach them. Nothing re-binds after startup — the only remedy is restarting those sessions. Check what deleted the paths under cc-socks." \
                    "watchgod:control_plane:${cp_ids// /,}"
                _after=$( { find "$_q" -maxdepth 1 -name '*.json' 2>/dev/null || true; } | wc -l)
                if (( _after > _before )); then
                    cp_paged_ids="$cp_next_paged"
                else
                    log WARN "control-plane alert could not be queued (${_q}) — leaving the severance UNREPORTED so a later poll retries"
                fi
            else
                cp_paged_ids="$cp_next_paged"
            fi
            printf '%s' "$cp_paged_ids" > "$cp_paged_file" 2>/dev/null || true
            cp_prev_ids="$cp_ids"
        else
            # Neither `unknown` nor `empty` is a recovery: forget the previous
            # reading so the next readable one needs its own confirmation, and
            # leave the reported set alone so a blind spell cannot silently
            # re-arm a page for a severance that was already reported.
            cp_prev_ids=""
        fi

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
