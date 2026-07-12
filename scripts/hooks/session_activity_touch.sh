#!/usr/bin/env bash
# session_activity_touch.sh — mark the nearest ancestor `claude` process active.
#
# Registered as a Claude Code hook on UserPromptSubmit + PostToolUse. Walks the
# parent-PID chain from this hook process up to the nearest process whose comm
# is exactly `claude`, then touches ~/.genesis/session-activity/<pid>. That
# marker is the activity signal the Genesis process reaper reads: a `claude`
# PID whose marker is fresher than the idle window is never reaped, so an
# interactive session that has been active within the window survives even
# past the 7-day age floor (the 2026-07-11 incident: active sessions killed
# purely on age).
#
# Contract: must be fast and must NEVER block or fail the session — it always
# exits 0, swallows every error, and does no network / DB work.

marker_dir="${HOME}/.genesis/session-activity"
pid=$PPID

for _ in $(seq 1 20); do
    [ -n "$pid" ] || break
    [ "$pid" -gt 1 ] 2>/dev/null || break
    comm_file="/proc/${pid}/comm"
    [ -r "$comm_file" ] || break
    comm=$(cat "$comm_file" 2>/dev/null)
    if [ "$comm" = "claude" ]; then
        mkdir -p "$marker_dir" 2>/dev/null
        touch "${marker_dir}/${pid}" 2>/dev/null
        break
    fi
    # Ascend: ppid is field 4 of /proc/<pid>/stat. The comm field (2) is
    # parenthesised and may contain spaces/')', so split after the LAST ')'.
    stat=$(cat "/proc/${pid}/stat" 2>/dev/null) || break
    after=${stat##*') '}
    # after = "<state> <ppid> <pgrp> ..." → field 2 is ppid.
    # shellcheck disable=SC2086
    set -- $after
    pid=$2
done

exit 0
