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

# ── Load config ──────────────────────────────────────────────
load_config() {
    if [[ -f "$CONF_FILE" ]]; then
        # shellcheck source=/dev/null
        source "$CONF_FILE"
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
    find "$1" -depth -not -type s -delete 2>/dev/null || true
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
    local -A live=()
    local -A dirs=()
    # Always scan cc-tmp's own socket dir, even when no listener points there:
    # on a fully-severed install every listener path is already gone from disk,
    # and that is exactly when the leftovers still need counting. `%/` because
    # watchgod.conf is re-sourced every poll and a trailing slash there would
    # otherwise build keys that never match the ones `find` produces.
    dirs["${CC_TMP_DIR%/}/cc-socks"]=1

    local path
    while IFS= read -r path; do
        [[ -z "$path" ]] && continue
        listeners=$(( listeners + 1 ))
        live["$path"]=1
        dirs["$(dirname "$path")"]=1
        # -S is "exists AND is a socket": a path replaced by an ordinary file is
        # no more reachable than a missing one, so it counts as severed too.
        [[ -S "$path" ]] || severed=$(( severed + 1 ))
        # NOTE: `ss -xlp` lists every user's sockets. On a shared box another
        # user's cc-socks path would be counted here, and an unstattable one
        # would read as severed. Genesis is single-user by design, so this is
        # left as a known limitation rather than engineered around.
    done < <(awk '$4 ~ /\/cc-socks\/.*\.sock$/ {print $4}' <<<"$raw" | sort -u)

    local d f
    for d in "${!dirs[@]}"; do
        [[ -d "$d" ]] || continue
        while IFS= read -r f; do
            [[ -z "$f" ]] && continue
            [[ -n "${live[$f]:-}" ]] || stale=$(( stale + 1 ))
        done < <(find "$d" -maxdepth 1 -type s -name '*.sock' 2>/dev/null)
    done

    if (( listeners == 0 && stale == 0 )); then
        echo "empty:0:0:0"
        return 0
    fi
    echo "ok:${severed}:${stale}:${listeners}"
}

# Should this control-plane reading page, and at what level is the plane now
# "reported"? Pure arithmetic, kept out of the poll loop so the two rules below
# can be tested without running the daemon.
#
#   CONFIRMATION — the SAME elevated count must appear on two consecutive polls,
#   which is stricter than "elevated twice": a count still climbing (1 -> 2 -> 3)
#   stays silent until it settles, and one that oscillates never pages at all.
#   Severance is permanent — nothing re-binds — so a real one always settles,
#   usually within a poll or two. What this buys is the false positive: a session
#   exiting between ss's snapshot and its own unlink reads as one severed listener
#   for a single poll, and a monitor that cries wolf gets ignored.
#
#   HIGH WATER THAT FOLLOWS DOWN — `paged` is the level already reported, and it
#   drops with the count. Without that, sessions being restarted (4 -> 0) would
#   leave the bar at 4 and a later, smaller severance would never page.
#
# Args: <prev-count> <already-paged-level> <current-count>.
# Echoes "<0|1 page>:<new already-paged level>".
control_plane_page_decision() {
    local prev="$1" paged="$2" severed="$3"
    if (( severed < paged )); then
        paged=$severed
    fi
    if (( severed > paged )) && (( severed == prev )); then
        echo "1:${severed}"
    else
        echo "0:${paged}"
    fi
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

    # Which workspace is ACTIVE — by the newest file anywhere inside it, not by a
    # directory's own mtime.
    #
    # This is the same defect the YELLOW reap had, in the tier where getting it
    # wrong costs the most, and fixing it in only one of the two places would have
    # left the class alive in the nuclear one. Depth 2 is the PROJECT directory,
    # and its mtime moves only when a session dir is created or removed directly
    # under it — never when a live session writes. So the sort was ranking
    # projects by "when did a session last start here", and preserving the winner.
    #
    # MEASURED on a live install (2026-09-07): the truly-active project's newest
    # FILE was 3 days newer than its own directory mtime, and that directory led a
    # DORMANT project's by 10 minutes. One more session started in the dormant
    # project and RED would have preserved that one and reaped the active
    # project's 54 session workspaces — while logging "preserving active session".
    #
    # `%T@ %p` over files, taking the max per project: a project with no files at
    # all cannot win, which is correct — there is nothing there to preserve.
    local newest_session=""
    newest_session=$(find "$CC_TMP_DIR" -mindepth 3 -type f -path "*/claude-*" \
        -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | awk '{print $2}') || true
    if [[ -n "$newest_session" ]]; then
        # Reduce the winning FILE to its project dir (…/claude-<uid>/<project>),
        # which is the unit the sweeps below exclude. Deliberately NOT narrowed to
        # the single session dir, which is what the review that found this
        # suggested: that would make RED delete the other sessions of the active
        # project, i.e. MORE destruction in the nuclear tier, and this change is
        # about preserving the right thing rather than preserving less of it.
        local _rel="${newest_session#"$CC_TMP_DIR"/}"
        local _uid="${_rel%%/*}"
        local _proj="${_rel#*/}"
        _proj="${_proj%%/*}"
        newest_session="$CC_TMP_DIR/$_uid/$_proj"
    fi

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

check_oom_events() {
    # $1 = previous baseline count. Echoes the (possibly-updated) baseline so the
    # caller can carry it to the next tick. On an increment: durable snapshot +
    # one dedup'd page. Never touches stdout except the final baseline echo.
    local prev="$1" cur
    cur=$(_read_oom_kill) || { printf '%s' "$prev"; return 0; }
    [[ -z "$cur" ]] && { printf '%s' "$prev"; return 0; }
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
        # Emergency tier (pages): an OOM kill is a discrete serious event — the
        # usual reason a CC session vanishes with no crash message — not routine
        # tier pressure, so unlike ORANGE it warrants a proactive page (per the
        # 2026-08-19 decision). Deduped per distinct oom_kill total.
        queue_alert emergency "watchgod:oom" "cgroup OOM kill(s) detected" \
            "${n} process(es) OOM-killed in the container cgroup (oom_kill ${prev}->${cur}). A CC session vanishing with no crash message is often this. Snapshot: ${OOM_LOG}" \
            "watchgod:oom:${cur}"
        # Bound the OOM log (retention discipline — matches cc_exit/log rotation);
        # keep the most recent ~1000 lines so a thrashing container can't leak it.
        local oom_lines
        oom_lines=$(wc -l < "$OOM_LOG" 2>/dev/null || echo 0)
        if (( ${oom_lines:-0} > 1000 )); then
            tail -n 1000 "$OOM_LOG" > "${OOM_LOG}.tmp" 2>/dev/null && mv "${OOM_LOG}.tmp" "$OOM_LOG" 2>/dev/null || true
        fi
    fi
    printf '%s' "$cur"
}

# ── Main loop ────────────────────────────────────────────────
main() {
    mkdir -p "$(dirname "$LOG_FILE")" "$ALERT_DIR"
    log INFO "Watchgod starting (poll=${POLL_INTERVAL}s, budget=${CC_TMP_BUDGET_MB}MB, sacred=${SACRED_GROUND_MB}MB)"

    # Baseline the OOM counter at startup so we only page on NEW kills (never the
    # cumulative-since-boot history). Empty baseline = monitoring unavailable.
    local oom_baseline
    oom_baseline=$(_read_oom_kill) || oom_baseline=""
    if [[ -z "$oom_baseline" ]]; then
        log INFO "OOM event capture unavailable (no readable ${OOM_EVENTS_FILE}) — OOM monitoring off"
    else
        log INFO "OOM event capture armed (baseline oom_kill=${oom_baseline})"
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
    local cp_prev_severed=-1 cp_last=""
    local cp_paged_file="$ALERT_DIR/control_plane_paged"
    local cp_paged_severed
    cp_paged_severed=$(cat "$cp_paged_file" 2>/dev/null) || cp_paged_severed=""
    [[ "$cp_paged_severed" =~ ^[0-9]+$ ]] || cp_paged_severed=0

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

        local cp_status cp_severed cp_stale cp_listeners
        IFS=: read -r cp_status cp_severed cp_stale cp_listeners <<<"$cp_result"

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
                "$cp_prev_severed" "$cp_paged_severed" "$cp_severed")
            cp_should_page="${cp_decision%%:*}"
            cp_paged_severed="${cp_decision##*:}"
            printf '%s' "$cp_paged_severed" > "$cp_paged_file" 2>/dev/null || true
            if [[ "$cp_should_page" == "1" ]]; then
                log WARN "control plane SEVERED: ${cp_severed} of ${cp_listeners} session(s) unreachable"
                # `warning` is the honest severity for the event, but note it does
                # NOT buy a quieter delivery: the container drain submits every
                # queued entry at one category and salience regardless of this
                # argument, so this pages exactly like the RED emergency does.
                # That is intended here (the owner asked to be paged on a NEW
                # severance) — recorded so nobody infers a tier that does not exist.
                queue_alert warning "watchgod:control-plane" \
                    "CC control plane severed (${cp_severed} session(s) unreachable)" \
                    "${cp_severed} of ${cp_listeners} live Claude Code session(s) are listening on a socket whose PATH no longer exists, so peers get ENOENT and cannot reach them. Nothing re-binds after startup — the only remedy is restarting those sessions. Check what deleted the paths under cc-socks." \
                    "watchgod:control_plane:${cp_severed}"
            fi
            cp_prev_severed=$cp_severed
        else
            # Neither `unknown` nor `empty` is a recovery: forget the previous
            # count so the next readable one needs its own confirmation, and leave
            # the already-paged level alone so a blind spell cannot silently
            # re-arm a page for a severance that was already reported.
            cp_prev_severed=-1
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
