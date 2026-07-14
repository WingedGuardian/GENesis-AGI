#!/usr/bin/env bash
# proc_pressure.sh — shared system-pressure sampling for the code-intel idle
# gate (code_intel_runner.sh) and the entrypoint pressure watchdog
# (code_intel_index.sh). SOURCE this file, then call the functions below.
#
# The two callers use the SAME readings with DIFFERENT thresholds: the runner
# only STARTS an index when the box is quiet (loadavg1 < 2, iowait < 10%, no CC
# session pegging a core); the watchdog PAUSES a running index when pressure
# spikes (loadavg1 > 4 or iowait > 25%). Keeping one implementation avoids drift.
#
# Test seams (so behavioural tests need no real load):
#   CODE_INTEL_FAKE_LOADAVG     short-circuit loadavg1 with a fixed value
#   CODE_INTEL_FAKE_IOWAIT      short-circuit iowait% with a fixed value
#   CODE_INTEL_FAKE_CLAUDE_CPU  short-circuit the max-claude-CPU probe
#   CODE_INTEL_PROC_LOADAVG     path override for /proc/loadavg
#   CODE_INTEL_PROC_STAT        path override for /proc/stat

# 1-minute load average (float, e.g. "0.42").
pressure_loadavg1() {
    if [ -n "${CODE_INTEL_FAKE_LOADAVG:-}" ]; then
        printf '%s' "$CODE_INTEL_FAKE_LOADAVG"
        return
    fi
    local f="${CODE_INTEL_PROC_LOADAVG:-/proc/loadavg}"
    awk '{print $1}' "$f" 2>/dev/null || printf '0'
}

# iowait percentage over a ~1s window (integer 0-100). Two /proc/stat samples;
# iowait delta over total-jiffies delta. Returns 0 if it can't compute.
pressure_iowait_pct() {
    if [ -n "${CODE_INTEL_FAKE_IOWAIT:-}" ]; then
        printf '%s' "$CODE_INTEL_FAKE_IOWAIT"
        return
    fi
    local f="${CODE_INTEL_PROC_STAT:-/proc/stat}"
    local a b
    a="$(grep '^cpu ' "$f" 2>/dev/null)" || { printf '0'; return; }
    sleep 1
    b="$(grep '^cpu ' "$f" 2>/dev/null)" || { printf '0'; return; }
    awk -v A="$a" -v B="$b" 'BEGIN {
        na = split(A, x); nb = split(B, y)
        if (na < 6 || nb < 6) { print 0; exit }
        # fields: label user nice system idle iowait irq softirq steal ...
        dio = y[6] - x[6]
        dtot = 0
        for (i = 2; i <= (nb < na ? nb : na); i++) dtot += (y[i] - x[i])
        if (dtot <= 0) { print 0; exit }
        p = int(dio * 100 / dtot)
        if (p < 0) p = 0
        print p
    }'
}

# Highest %CPU among live `claude` processes (integer). The idle gate uses this
# so a full index never starts while a CC session is doing real work.
pressure_max_claude_cpu() {
    if [ -n "${CODE_INTEL_FAKE_CLAUDE_CPU:-}" ]; then
        printf '%s' "$CODE_INTEL_FAKE_CLAUDE_CPU"
        return
    fi
    # ps %cpu is per-process; comm match avoids matching this script's args.
    ps -eo pcpu=,comm= 2>/dev/null \
        | awk '$2 == "claude" && $1 > max { max = $1 } END { printf "%d", max + 0 }' \
        || printf '0'
}

# pressure_gt A B  → exit 0 (true) iff float A > float B. awk handles the
# float compare bash can't do natively; non-numeric A is treated as 0.
pressure_gt() {
    awk -v a="$1" -v b="$2" 'BEGIN { exit !((a + 0) > (b + 0)) }'
}
