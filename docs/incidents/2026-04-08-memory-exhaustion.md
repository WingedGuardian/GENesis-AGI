# Incident Report: VM Memory Exhaustion & Hard Restart

**Date:** 2026-04-08  
**Duration:** Gradual buildup over ~2-3 weeks, culminating at ~22:03 UTC  
**Impact:** Full VM hard-restart required  
**Investigated by:** Guardian (host VM Claude Code session)

---

## Summary

The host VM (66 GiB RAM, Ubuntu 24.04/Proxmox) was hard-restarted after the Genesis container's memory climbed to 83-84% (20 GiB / 24 GiB) over ~2-3 weeks of continuous operation. Genesis had told the user the growth was "just Linux kernel caching" and harmless. Investigation found this explanation was misleading — while page cache was a factor, the real problem was a combination of monotonic Python memory accumulation, broken cache reclaim, and a dead zone in the cgroup configuration where no reclaim mechanism could act.

The VM itself was likely NOT out of memory. The apparent whole-VM memory exhaustion was normal Linux page cache behavior (Linux fills all available RAM with cache). The actual crisis was localized to Genesis's cgroup, but cascading IO pressure from forced reclaim made everything feel system-wide.

---

## Timeline (UTC, April 8)

| Time | Event |
|------|-------|
| ~21:32 | Guardian heartbeat probe gets 401 Unauthorized (auth misconfiguration — `/api/genesis/heartbeat` not in auth-exempt list) |
| ~21:37 | `auth.py` patched to add heartbeat exemption; genesis serve restarted |
| 22:00:13 | Watchdog: "Container memory HIGH: 83% (19.9/24.0 GiB) — reclaiming cache" |
| 22:00:13 | Watchdog: "Page cache reclaim failed: sudo: The no new privileges flag is set" |
| 22:01:20 | Watchdog: "Container memory HIGH: 84% (20.1/24.0 GiB)" — reclaim fails again |
| ~22:03:47 | User presses power key — hard restart |
| 22:05:42 | VM boots, containers restart cleanly |
| 22:06+ | All healthy — Genesis at 4.6% memory (1.12 GiB), 23/23 subsystems OK |

---

## Root Cause Analysis

### 1. Genesis Container: Memory Growth Over 2-3 Weeks

**Pre-restart state:** 20 GiB used (83-84% of 24 GiB limit)  
**Post-restart state:** 3.1 GiB used (13%)

Breakdown at 12 minutes post-restart (fresh baseline):
- **anon (process memory):** 1.55 GiB — non-reclaimable
- **file (page cache):** 1.13 GiB — reclaimable in theory
- **kernel/slab:** 375 MB (340 MB reclaimable dentry/inode cache)

Estimated pre-restart breakdown (extrapolating 2-3 weeks of growth):
- **anon:** 8-12 GiB — NOT reclaimable (Python memory accumulation)
- **file cache:** 6-9 GiB — reclaimable, but reclaim was broken
- **kernel/slab:** 1-3 GiB — partially reclaimable

**Why anon memory grows monotonically:**
- CPython's memory allocator holds freed blocks and does not release them back to the OS
- Each awareness loop tick (every 5 min), each Claude Code session, each MCP server invocation accumulates resident memory
- No periodic service restart or memory limit per process exists
- Current processes (12 min after restart): genesis serve (331 MB), 2x Claude Code (~700 MB), Qdrant (293 MB), 4x MCP servers (~800 MB), SearXNG (104 MB)

### 2. Broken Cache Reclaim — The Toothless Watchdog

The Genesis watchdog correctly detected high memory at 83% and attempted to reclaim page cache via `echo 3 > /proc/sys/vm/drop_caches`. This **failed** because the container's security policy (`security.privileged=false`, no-new-privileges flag) prevents sudo.

Meanwhile, the cgroup `memory.high` soft limit is set at **90% (21.6 GiB)**. The kernel's automatic pressure-based reclaim doesn't kick in until that threshold.

This creates a **dead zone between 83% and 90%** where:
- The watchdog is alarmed but powerless (drop_caches blocked)
- The kernel is not yet applying back-pressure (below memory.high)
- Memory continues growing unchecked

### 3. The Cascade

1. Genesis fills toward 24 GiB limit over weeks (anon growth + cache accumulation)
2. At 83%: watchdog fires, can't reclaim, logs warnings
3. At 90% (21.6 GiB): cgroup `memory.high` triggers, kernel throttles allocations → latency
4. Latency causes heartbeat timeouts → Guardian detects "failure"
5. Guardian spawns Claude Code on host for diagnosis (~350 MB per instance)
6. IO pressure from aggressive cgroup reclaim hits shared disk → all containers slow
7. System feels frozen → user hard-restarts

### 4. Ollama Container: High Memory, No CPU — Explained

Ollama loads model weights via **malloc (anonymous memory), NOT mmap**. Confirmed from logs:
```
load_tensors: loading model tensors (mmap = false)
load_tensors: CPU model buffer size = 1834.82 MiB
```

With `OLLAMA_MAX_LOADED_MODELS=2` and `OLLAMA_KEEP_ALIVE=5m`:
- Both models (qwen2.5:3b at 1.9 GB + qwen3-embedding:0.6b-fp16 at 1.2 GB) = 3+ GiB committed
- Genesis polls `/api/tags` every ~15s (lightweight, doesn't keep models warm)
- But periodic `/api/embed` calls from the awareness loop reset the keep-alive timer
- **High memory + no CPU = models loaded but idle** (not actively inferring)

Ollama was likely at 3-5 GiB (of 16 GiB limit) — NOT a major contributor to the incident.

### 5. Host VM: The Red Herring

The VM has 66 GiB. Containers are hard-limited to 24 + 16 = 40 GiB. Host processes used ~4-8 GiB. The remaining ~18-22 GiB was **host-level page cache** — normal Linux behavior of filling available RAM with file cache.

From Proxmox's dashboard, this looks like 100% memory used. It wasn't — that cache is freely reclaimable. **The VM was never actually out of memory.** Evidence:
- Zero OOM kills in kernel logs, container logs, or systemd accounting
- Zero allocation stalls (`allocstall_*` counters all 0)
- Clean shutdown (user-initiated power key press, not kernel panic)
- Host service peaks: incusd 341 MB, snapd 271 MB, Guardian 52 MB

### 6. Was Genesis's "Just Caching" Explanation Correct?

**Partially true, but misleading:**

| Claim | Reality |
|-------|---------|
| "It's just page cache" | Some was cache, but estimated 8-12 GiB was anon (non-reclaimable) |
| "It's harmless" | Cgroup counts cache toward the limit — it's not free headroom |
| "The kernel will reclaim it" | The only reclaim mechanism (drop_caches) was broken by security policy |
| "No memory pressure" | Genesis's own reflection signals flagged 0.805 memory usage as concerning |

---

## Secondary Issues Discovered

| Issue | Severity | Detail |
|-------|----------|--------|
| **Broken page cache reclaim** | HIGH | Watchdog can't `drop_caches` due to no-new-privileges security flag |
| **Dead zone (83-90%)** | HIGH | No reclaim mechanism operates between watchdog threshold and cgroup memory.high |
| **Guardian checks wrong service** | MEDIUM | `collector.py:344` checks `genesis-bridge` (deprecated) instead of `genesis-server` (active) |
| **Uncommitted auth.py fix** | MEDIUM | `/api/genesis/heartbeat` auth-exempt patch not committed — will regress on git operations |
| **No Python memory hygiene** | MEDIUM | Long-running processes accumulate memory indefinitely with no periodic restart |
| **Qdrant polls every ~15s** | LOW | `episodic_memory` queried frequently, expanding page cache. Normal but additive. |

---

## Recommended Actions

### Immediate (prevent recurrence)
1. **Fix page cache reclaim** — either:
   - Run `incus exec genesis -- sh -c "echo 3 > /proc/sys/vm/drop_caches"` from a host-side timer (bypasses no-new-privileges)
   - Or lower `memory.high` to 70-75% (16.8-18 GiB) so the kernel reclaims earlier
2. **Commit the auth.py fix** — prevent regression
3. **Update Guardian collector.py** — change `genesis-bridge` → `genesis-server`

### Short-term (memory hygiene)
4. **Periodic service restart** — weekly or threshold-based restart of genesis-server to reset Python memory
5. **Review OLLAMA_MAX_LOADED_MODELS** — set to 1 if only one model used at a time (saves ~1.2 GiB)
6. **Lower memory.high thresholds** — 75% for both containers instead of 90%

### Monitoring
7. **Log anon vs file vs slab separately** — "83% memory" is meaningless without knowing what's reclaimable
8. **Track anon memory trend** — monotonic growth = leak; alert on growth rate, not just total

---

## Verification Checklist

After implementing fixes, monitor over 48-72 hours:
- [ ] `cat /sys/fs/cgroup/memory.stat` — track anon growth in both containers
- [ ] `cat /proc/pressure/memory` — host-level memory pressure stays near 0
- [ ] Watchdog logs show successful cache reclaim when triggered
- [ ] Guardian correctly identifies genesis-server as the active service
- [ ] No heartbeat false alarms from auth issues

---

## Key Takeaway

The incident was not a sudden failure but a slow-motion collapse over 2-3 weeks. The combination of Python's memory accumulation behavior, broken cache reclaim, and a permissive cgroup soft limit created conditions where Genesis gradually filled its memory allocation with no mechanism to self-correct. The apparent VM-wide memory exhaustion was a red herring — normal Linux cache behavior misinterpreted as a crisis. The actual problem was entirely within Genesis's 24 GiB cgroup.
