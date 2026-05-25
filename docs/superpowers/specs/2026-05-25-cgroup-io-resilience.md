# Cgroup v2 I/O Resilience + Guardian/Sentinel Coordination

**Author:** Genesis (design session with user, 2026-05-25)
**Status:** Draft
**Incident context:** 2026-05-25 I/O death spiral -- 7 CC sessions saturated host io.max,
D-state freeze, 4 restarts in 23 minutes.

---

## Problem Statement

CC sessions (Claude Code subprocesses) can saturate the container's I/O bandwidth,
hitting the host-level io.max ceiling and causing D-state process freezes. When this
happens, everything inside the container -- including Sentinel -- freezes. Only Guardian
(host-side) can recover, but Guardian's diagnostic CC session competes for the same
constrained resources.

Three interconnected problems:
1. No I/O isolation between critical processes (server, Sentinel) and background work (CC sessions)
2. Guardian doesn't respect Sentinel's approval-gated timeline -- uses a fixed 5-minute ETA
3. Recovery tools (GROUNDWORK) exist but aren't wired into the escalation path

---

## Design 1: Container-Side Cgroup v2 I/O Isolation

### Architecture

Internal cgroup v2 is **preventive, not curative.** It prevents CC sessions from
ever reaching the host io.max ceiling. Once D-state occurs, only Guardian (host-side)
can help.

```
/sys/fs/cgroup/user.slice/.../app.slice/
  +-- genesis-critical/          <-- Server, awareness loop, Sentinel process
  |     io.weight = 500          (5x priority over background)
  |     memory.min = 2G          (reserved, never reclaimed)
  |
  +-- genesis-background/        <-- CC sessions, surplus compute
  |     io.weight = 100          (best-effort)
  |     io.max = <device> rbps=<50% of host limit> wbps=<50% of host limit>
  |     memory.high = 20G        (soft limit, triggers reclaim before OOM)
  |
  +-- genesis-server.service     <-- systemd-managed (current, migrates to critical)
```

### Verification (2026-05-25)

Cgroup v2 I/O delegation confirmed working inside the Genesis container:
- io controller available: cpuset cpu io memory hugetlb pids rdma misc
- Delegation through user.slice hierarchy: confirmed (requires sudo)
- Sub-cgroup creation under app.slice: confirmed
- io.weight, io.max, io.pressure all available in sub-cgroups
- Setting io.weight = 500: confirmed working
- Caveat: delegation does NOT persist across container restart

### Key Principles

1. **Host io.max is a safety boundary -- never touch it.** The host io.max exists to
   prevent disk corruption from I/O storms. No Genesis code may remove or raise it.
   relieve_io_max() must be removed from the recovery path.

2. **Internal io.max on genesis-background prevents D-state.** By capping CC session
   I/O at 50% of the host-allowed bandwidth, they can never push total container I/O
   to the host limit. Critical processes always have headroom.

3. **io.weight provides proportional fairness within the allowed bandwidth.** When CC
   sessions are active, genesis-critical gets 5x their proportional share.

4. **Sentinel's CC sessions go to genesis-background.** The Sentinel Python process
   stays in genesis-critical (it needs to remain responsive). But its child CC session
   is placed in genesis-background -- it's still a CC session competing for resources.

### Implementation Details

**Cgroup delegation persistence:** The +io delegation through the user slice
hierarchy does not survive container restart. A systemd oneshot unit must run at
boot to propagate +io through:
```
user.slice -> user-1000.slice -> user@1000.service -> app.slice
```

**CC invoker changes (src/genesis/cc/invoker.py):**
After create_subprocess_exec(), move the child PID into genesis-background:
```python
Path(GENESIS_BACKGROUND_CGROUP / "cgroup.procs").write_text(str(pid))
```

**Server process migration:** On startup, the server moves its own PID into
genesis-critical. This must happen early in bootstrap, before the awareness
loop or scheduler start.

**Device detection for io.max:** The internal io.max on genesis-background
requires the block device major:minor number. Auto-detect via:
```python
os.stat("/").st_dev  # -> major:minor of root filesystem device
```

### What This Solves

- CC sessions can't saturate I/O to the point of D-state
- Server stays responsive during I/O pressure -> health API answers Guardian
- Sentinel stays responsive -> can detect and act on I/O pressure
- Guardian dialogue gets real answers instead of silence

### What This Does NOT Solve

- D-state caused by the server process itself (genesis-critical is the offender)
- Container-level freezes from host-side operations (Incus maintenance, etc.)
- Memory exhaustion without I/O pressure (needs separate memory limits)

### Memory Limits on genesis-background

In addition to I/O isolation, genesis-background gets memory.high (soft limit):
```
memory.high = 20G   # triggers kernel reclaim before OOM
memory.max  = 24G   # hard ceiling -- OOM killer targets background first
```

genesis-critical gets reserved memory:
```
memory.min = 2G     # kernel will not reclaim this, even under pressure
```

This prevents the scenario where 10 concurrent CC sessions exhaust all 36GB
of container memory and trigger a system-wide OOM that kills the server.

### CC Session Pressure Management

When genesis-background approaches its memory.high limit, Genesis must manage
session count rather than let the kernel randomly OOM-kill sessions.

**Background sessions (Genesis-managed):**
- Monitor genesis-background memory.current vs memory.high
- When usage > 80% of memory.high: stop accepting new background dispatches
- When usage > 90% of memory.high: kill the oldest/lowest-priority background
  session. Genesis owns these sessions and can make informed priority decisions.
- This is autonomous -- no user approval needed for managing background sessions
  Genesis started.

**Foreground sessions (user-managed):**
- When memory pressure is elevated and foreground session count > threshold:
  alert user via Telegram: "You have N foreground CC sessions open. Container
  memory at X%. Consider closing some."
- Genesis does NOT kill foreground sessions. Those belong to the user.

### Failure Modes Introduced

- **Cgroup delegation lost on restart** if boot unit fails -> CC sessions run
  uncontrolled (same as today, not worse)
- **Invoker race condition** -- brief window between fork and cgroup.procs write
  where CC session is in the parent cgroup. Mitigated by writing ASAP after fork.
- **io.max too restrictive** -> CC sessions take forever. Needs tuning based on
  actual host io.max value. Start conservative (50%), adjust based on telemetry.

---

## Design 2: Guardian-Sentinel Coordination Fix

### Problem

Guardian uses a fixed eta_s (default 300s / 5 min) when Genesis says "Sentinel
dispatched." But Sentinel has TWO approval gates:
1. sentinel_dispatch -- permission to spawn CC session
2. sentinel_action -- permission to run fixes

User could be asleep for 8 hours. 5-minute timeout is meaningless.

### Fix

Replace fixed-ETA timeout with **state-aware standing:**

Guardian checks Sentinel state on every 30s tick (via the dialogue endpoint or
shared filesystem). Stand down as long as Sentinel is in any active state:
- INVESTIGATING -- actively diagnosing
- REMEDIATING -- actively fixing
- AWAITING_DISPATCH_APPROVAL -- parked on user approval
- AWAITING_ACTION_APPROVAL -- parked on user approval

Guardian proceeds only when:
- Sentinel transitions to ESCALATED (explicitly gave up)
- Sentinel returns to HEALTHY but health probes still fail (fixed wrong thing)
- Genesis becomes completely unreachable (dialogue endpoint itself dies)

**No wall-clock timeout.** If Sentinel is parked on approval and the user is asleep
for 8 hours, Guardian waits 8 hours. User sovereignty is absolute -- neither
Guardian nor Sentinel should take major actions just because time passed. The
approval gate is the designed behavior, not a bottleneck to work around.

### Implementation

Modify _handle_awaiting_self_heal() in check.py:
1. On each tick, query Sentinel state via /api/genesis/sentinel/status (new endpoint)
   or read ~/.genesis/shared/sentinel/sentinel_state.json from shared mount
2. If Sentinel active -> stay in AWAITING_SELF_HEAL, reset internal timer
3. If Sentinel escalated/failed -> transition to CONFIRMED_DEAD
4. If dialogue endpoint unreachable -> transition to CONFIRMED_DEAD

Modify dialogue endpoint to return Sentinel state with each response so Guardian
doesn't need a separate query.

---

## Design 3: IO_TRIAGE Recovery Action

### Position in Escalation Ladder

```
1. RESTART_SERVICES          (lightest -- restart genesis-bridge)
2. IO_TRIAGE       <- NEW    (kill top I/O consumer, collect diagnostics)
3. RESOURCE_CLEAR            (clear /tmp, drop page cache)
4. REVERT_CODE               (git stash + revert HEAD)
5. RESTART_CONTAINER         (incus restart)
6. SNAPSHOT_ROLLBACK         (incus snapshot restore)
7. ESCALATE                  (alert user, stop)
```

### Behavior

**Triggered when:** I/O pressure PSI full avg10 > 50% (the same threshold that
sets alive=False on the io_saturation probe).

**Flow:**
1. **Collect diagnostics** -- find_top_io_pids() -> log top 5 consumers with
   PID, comm, read_bytes, write_bytes
2. **Assess PSI trend** -- compare avg10 vs avg60 vs avg300:
   - If avg10 > avg60 > avg300: accelerating -> intervene
   - If avg10 > avg60 but avg300 dropping: recent spike, may recover -> wait one more cycle
   - If avg10 dropping: recovering -> log and stand down
3. **Kill top consumer** -- kill_pid(top_pid, container=container_name) with
   cgroup membership validation
4. **Wait 30s, re-probe** -- if PSI improving, done. If not, fall through to
   RESOURCE_CLEAR.

### What IO_TRIAGE Does NOT Do

- **Never touches io.max.** relieve_io_max() is removed from the recovery path.
  The function remains as GROUNDWORK for future manual intervention only.
- **Never kills PID 1 or the server process.** Kill candidates are filtered to
  genesis-background cgroup members only (once cgroup isolation is in place).
  Before cgroup isolation, filter by known CC session PIDs from the invoker.
- **Does not kill more than 1 process per cycle.** Kill one, reassess. If
  still saturated, the next cycle kills the next top consumer.

### Grace Period (Tiered Patience)

- **0-5 min:** Sentinel has first crack (via dialogue dispatch). Guardian collects
  diagnostics and logs PSI readings every 30s tick.
- **5-15 min:** Guardian observes and assesses PSI trend. If trending down, continue
  waiting. If flat or accelerating, mark as "intervention candidate."
- **15+ min:** If still saturated and not trending down, run IO_TRIAGE.

This 15-minute grace period respects the observation from the 2026-05-25 incident
where D-state resolved after ~20-30 minutes of heavy reads. The trade-off: we
accept up to 15 minutes of degradation in exchange for not killing legitimate
long-running work. If the user wants faster intervention, the threshold is
configurable.

---

## Design 4: Snapshot Space Gating

### Problem

Fixed 80% pool threshold doesn't account for actual snapshot size. 80% of a
300GB pool is 240GB used / 60GB free -- currently we're at 42GB used, so this
threshold would never trigger. Meanwhile, 80% of a 10GB pool would be too tight.

### Fix: Headroom-Based Gating

```python
def safe_to_snapshot(pool_free_bytes, pool_total_bytes, history):
    """Check if there's enough headroom for a snapshot.

    Required headroom = max(5 GB, 2x average of last 3 snapshot sizes)
    Fallback when no history: require at least 10% of pool free.
    """
    if not history:
        return pool_free_bytes > pool_total_bytes * 0.10  # 10% fallback
    avg_size = sum(history[-3:]) / len(history[-3:])
    required = max(5 * 1024**3, 2 * avg_size)
    return pool_free_bytes > required
```

### History Storage

Store last 3 snapshot sizes in Guardian state file (already persisted per check
cycle). After each successful snapshot, record its size via:
```
btrfs subvolume show <snapshot_path> | grep "Exclusive"
```
Or estimate from incus info snapshot metadata.

### Fallback Behavior

- No history -> 10% of pool free (safe default)
- History available -> headroom check
- Pool detection fails (non-BTRFS) -> always allow (current fail-open behavior)

---

## Execution Order

1. **Cgroup v2 delegation** -- boot unit + invoker changes (highest impact)
2. **Guardian-Sentinel coordination** -- ETA fix (highest safety improvement)
3. **IO_TRIAGE wiring** -- recovery action with grace period
4. **Snapshot gating** -- headroom-based check (lowest risk)

Each is independently deployable. 1 and 3 have the most synergy (cgroup isolation
makes IO_TRIAGE safer because kill targets are scoped to genesis-background).

---

## Architecture Documentation Updates

Update the following after implementation:
- docs/architecture/genesis-v3-survivable-architecture.md -- Layer model
- docs/architecture/genesis-v3-resilience-architecture.md -- Resilience state machine
- Guardian and Sentinel CLAUDE.md sections
- config/guardian.yaml -- new IO_TRIAGE and coordination settings
