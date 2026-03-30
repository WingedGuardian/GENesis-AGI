# Genesis Survivable Architecture

**Status:** V4 Step 1 implemented | **Last updated:** 2026-03-26


> **Status**: V4 Step 1 (Guardian) implemented. Layer 2 (host VM guardian)
> is built and ready for deployment. Layer 3 (sandbox) deferred to V5.
> **Origin**: 2026-03-16 incident — a test bug (`os.killpg(1, SIGKILL)`)
> killed every process in the container multiple times. All monitoring,
> resilience, and recovery infrastructure died with it. Recovery required
> manual VM-level intervention.
> **Scope**: V3 design + backup hardening. V4 guardian implementation +
> sandbox design. V5 full sandbox implementation.

## Problem Statement

Genesis runs inside a single container. All resilience infrastructure —
circuit breakers, deferred work queue, health monitoring, the watchdog —
runs inside that same container. When the container dies, monitoring dies
with it. The fire alarm is inside the furnace.

This is not a theoretical risk. On 2026-03-16:

1. A test called `os.killpg(1, SIGKILL)`, killing every user process
2. The watchdog died. The bridge died. The health MCP died.
3. Nobody was notified. The system was silently dead.
4. Recovery required SSH from the host VM and manual diagnosis.
5. This happened **multiple times** because the root cause wasn't
   immediately identified.

An autonomous agent that can modify its own execution environment will
inevitably cause self-harm — not maliciously, but through emergent
unintended consequences. This is not a matter of *if* but *when*. The
architecture must survive it.

## Three-Layer Architecture

```
┌─────────────────────────────────────────────────┐
│  HOST VM (Layer 1: Guardian)                    │
│  - Monitors container health (multi-signal)     │
│  - Takes pre-change Incus snapshots             │
│  - Diagnoses failures & recovers automatically  │
│  - Independent Telegram channel for alerts      │
│  - Self-monitored by dead-simple systemd timer  │
│                                                 │
│  ┌─────────────────────────────────────────┐    │
│  │  CONTAINER (Layer 2: Genesis Core)      │    │
│  │  - All real work happens here           │    │
│  │  - Existing resilience (inner defense)  │    │
│  │  - Heartbeat canary (awareness loop)    │    │
│  │                                         │    │
│  │  ┌─────────────────────────────────┐    │    │
│  │  │  NESTED CONTAINER (Layer 3)     │    │    │
│  │  │  - Sandboxed code testing       │    │    │
│  │  │  - Full Genesis instance        │    │    │
│  │  │  - Stress/chaos test battery    │    │    │
│  │  │  - Always-on, Genesis on-demand │    │    │
│  │  └─────────────────────────────────┘    │    │
│  └─────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
```

**Installation model**: Genesis is one codebase, two deployment targets.
Installing Genesis means installing components on both the host VM and
inside the container. They are equally essential parts of the system —
like an immune system is part of the body.

## Layer 1: Host VM Guardian

### Purpose

The guardian is Genesis's immune system. It runs outside the blast radius
of any container-level failure. It is an intelligent Claude Code session
(not a dumb script) that can reason about failures, diagnose root causes,
and recover the system — or escalate to the user when it can't.

### Detection: Multi-Signal Health Monitoring

The guardian monitors the container through multiple independent signals,
each catching different failure modes:

| Signal | What it catches | How |
|--------|----------------|-----|
| **Heartbeat canary** | Genesis runtime dead | HTTP endpoint tied to awareness loop — if the canary sings, the full stack underneath is alive |
| **Container existence** | Container crashed/stopped | `incus list` or `incus info genesis` from host |
| **Network reachability** | Network partition, frozen container | ICMP ping to container IP |
| **Service probe** | Partial failure, hung process | HTTP GET to health API endpoint |
| **Log freshness** | Silent death, infinite loop | Check journal timestamp recency via `incus exec` |

**Why multi-signal**: A frozen container responds to ping but has no
heartbeat. A network partition fails ping but the container is fine.
A dead awareness loop stops the heartbeat but services may still respond.
Multiple signals let the guardian distinguish failure modes and avoid
false positives.

**Signals as diagnostic breadcrumbs**: The multi-signal approach serves
a dual purpose — not just detection but diagnosis. The *sequence and
timing* of signal failures tells a story. If the heartbeat died 30s
before the service probe, the failure propagated from the runtime
outward. If the service probe died first, it's a network or HTTP layer
issue. If all signals drop simultaneously, it's a container-level crash.
The more channels the guardian monitors, the richer the forensic trail
for root cause analysis — especially for non-obvious failures where the
system is degraded but not cleanly dead.

**The heartbeat canary**: This is the primary signal. It should be the
*last thing to die* — tied to the awareness loop, which depends on the
runtime, database, event bus, and scheduler. If the canary is singing,
the full stack must be working. If it stops, something in the chain is
dead. The guardian doesn't need to know what — the silence is the alarm.

### Confirmation: Don't False-Positive

When a signal drops, the guardian does NOT immediately initiate recovery.
It follows a confirmation protocol:

1. **Wait and re-check** — Transient blips (network hiccup, GC pause)
   resolve within seconds. Wait 30s, re-check all signals.
2. **Escalate probe depth** — If heartbeat is down but ping succeeds,
   try `incus exec` to check process state inside the container.
3. **Survey the situation** — Read recent journal logs, check for OOM
   kills, inspect running processes, check disk/memory state.
4. **Confirm death** — Only after multi-signal confirmation + survey
   does the guardian declare Genesis dead and begin recovery.

### Diagnosis: Identify Root Cause Before Recovery

**Critical**: The guardian must diagnose what went wrong BEFORE restoring.
Blindly restoring may restore the bug that caused the failure.

Diagnosis steps:
1. Read container journal for error messages, OOM kills, segfaults
2. Check `git log` for recent commits — was a code change deployed?
3. Check `git diff` for uncommitted changes
4. Inspect running/zombie processes for clues
5. Check cgroup memory/IO pressure files
6. Review the last few awareness tick events in the database
7. Form a hypothesis with confidence level

If the root cause appears to be a code change:
- Identify the commit or uncommitted change
- Determine if it can be reverted before restoring
- If it was committed and pushed, note it for post-recovery cleanup

### Pre-Change Snapshots

Before any autonomous code change is applied to live Genesis, the
guardian takes an Incus container snapshot:

```
Genesis prepares change (worktree, feature branch)
  → Signals guardian via heartbeat channel: "change incoming"
  → Guardian takes Incus snapshot (incremental, fast)
  → Guardian acknowledges
  → Genesis applies change
  → If Genesis dies → Guardian rolls back to snapshot
  → If Genesis survives → Snapshot retained as rollback point
```

**Incremental snapshots** minimize overhead. Incus supports ZFS/btrfs
snapshots which are copy-on-write and nearly instant. Only changed
blocks are stored. This should add <5 seconds of latency per change.

Snapshot retention policy: keep last N snapshots (e.g., 10), auto-prune
oldest. Always keep the "last known healthy" snapshot.

### Recovery Protocol

When the guardian confirms Genesis is dead:

1. **Notify user** — Telegram alert: "Code red: Genesis is down.
   Automatic recovery initiated. Suspected cause: [diagnosis]"
2. **Attempt in-place recovery first**:
   - If root cause is identified code change → revert it
   - Restart the container / user manager / bridge
   - Check if Genesis comes back (heartbeat resumes)
3. **If in-place fails → snapshot rollback**:
   - Roll back to last known healthy Incus snapshot
   - Start container, verify heartbeat
4. **If rollback fails → backup restore**:
   - Restore from latest backup (SQLite + Qdrant + config)
   - This is the last resort with highest data loss
5. **Post-recovery verification**:
   - Confirm all signals healthy
   - Run a quick health check suite
   - Verify database integrity
6. **Notify user** — "Service restored. [Synopsis: what went wrong,
   what was lost, suspected root cause, what to avoid]"
7. **If recovery fails entirely** — "Unable to recover automatically.
   Manual intervention required. Here's what I found: [full diagnosis]"

### Communication: Independent Telegram Channel

The guardian has its OWN Telegram bot token, completely independent of
the container's Genesis bot. Under normal operations, this channel is
silent. It only activates during incidents.

Options:
- **Separate bot, same group** — "Genesis Guardian" bot posts in the
  same Telegram group as Genesis. User sees guardian messages in context.
- **Separate bot, separate chat** — Dedicated "Genesis Alerts" channel
  that only lights up during incidents.

The guardian's Telegram access must work even when the container is
completely dead — it runs on the host VM with its own API keys.

### Self-Monitoring: Who Watches the Watchman?

The guardian CC session can itself die. Two layers of protection:

1. **Primary**: Genesis (the container) monitors the guardian via the
   same heartbeat protocol in reverse. If Genesis detects the guardian
   is down, it logs a warning and attempts to restart it (via network
   command to host, or alerting the user).

2. **Fallback**: A dead-simple systemd timer on the host VM checks if
   the guardian process is alive every 60 seconds. If dead, restart it.
   No intelligence — just process existence check and restart.

This creates a mutual watchman system: each layer monitors the other.
Under normal operation, both are healthy. If one dies, the other detects
it. If both die simultaneously (host VM crash), that's a hardware-level
failure outside our control.

### What Lives on the Host VM

Minimal footprint — the host VM is a dedicated machine for this purpose:

- Claude Code CLI (for intelligent guardian sessions)
- Genesis host-side codebase (pulled from same repo)
- Telegram bot client (lightweight, for alerts)
- Incus CLI (for container management, snapshots)
- systemd timer (guardian self-monitor)
- SSH access to container (fallback diagnostic path)
- Backup restoration scripts

## Layer 2: Container (Genesis Core)

This is the existing Genesis system. All real work happens here:
awareness loop, reflection engine, memory operations, outreach,
conversation handling, learning.

The existing resilience infrastructure (circuit breakers, deferred work,
health monitoring, watchdog) remains as the **inner defense ring**. It
handles transient failures, provider outages, and recoverable errors.
The guardian (Layer 1) handles catastrophic failures that the inner ring
can't survive.

### Heartbeat Canary

The container exposes a heartbeat endpoint that the guardian monitors.
This should be:
- Tied to the awareness loop (deepest health indicator)
- Lightweight (no database queries in the heartbeat itself)
- Accessible from the host network
- Returns a timestamp + tick count so the guardian can detect staleness
  vs. complete failure

### Change Signaling

Before applying autonomous code changes, Genesis signals the guardian:
- Write to a shared status channel (network endpoint or filesystem)
- Include: what's changing, which files, commit hash
- Wait for guardian acknowledgment (snapshot taken)
- Apply changes
- Signal completion

If Genesis dies mid-change (no completion signal), the guardian knows
exactly what was being changed and can roll back.

## Layer 3: Sandboxed Execution

### Purpose

When Genesis writes code, it should test it in a real environment that
isn't the live system. Code review and static analysis miss runtime bugs
(as proven on 2026-03-16). The sandbox catches them safely.

### Architecture

An always-on nested container (Incus-in-Incus) inside the main Genesis
container:
- **Container is always running** — reduces spin-up latency
- **Genesis is NOT normally running** — saves resources
- **Codebase is incrementally synced** — not a full clone each time

### Flow

```
1. Genesis writes code in a worktree (normal flow)
2. Incremental sync to nested container's codebase
   (rsync or git pull — only changed files)
3. Bring nested Genesis online (start the runtime)
4. Run the chaos test battery (see below)
5. Observe health metrics during and after tests
6. Live Genesis evaluates results intelligently
7. If clean → merge to live. If not → report failures.
8. Shut down nested Genesis (container stays up)
```

### Chaos Test Battery

Rather than passive soak time, the sandbox runs an aggressive stress
test designed to expose failures quickly (target: <60 seconds):

- Send rapid-fire messages across all channels
- Trigger awareness loop ticks
- Force reflection cycles
- Hit every MCP tool endpoint
- Run the full pytest suite
- Force circuit breaker transitions
- Simulate provider failures
- Check for memory leaks (compare RSS before/after)
- Verify database integrity
- Check for orphaned processes

If the system survives 60 seconds of this, it's more validated than
30 minutes of idle operation.

### Compute Routing for Stress Tests

The chaos battery tests the *codebase*, not LLM quality. All LLM calls
inside the nested container should route through surplus compute
providers (Groq, Mistral, Deepseek) or free-tier models. The existing
`genesis.routing` infrastructure supports this — the nested container
gets a routing config that points all call sites to surplus providers.

This means stress testing costs nothing. The code under test is
identical — only the LLM responses differ, and LLM responses aren't
what break things. Code paths, process management, database operations,
signal handling — those are what the chaos battery validates.

### Data Realism

The nested container starts with a clean database by default. To catch
data-dependent bugs, restore the latest backup data before testing:
- SQLite dump from backup.sh
- Qdrant snapshots
- Provides realistic data patterns without exposing the live database

**Acknowledged limitation**: The backup data is up to 6 hours stale.
Some data-dependent bugs may only manifest with very recent data. This
is an accepted tradeoff — catching 90% of data-dependent bugs with
backup data is far better than catching 0% with an empty database.

### Resource Management

The nested container must not starve the live system:
- CPU: capped at 50% of available cores
- Memory: capped at 8GB (live system gets priority)
- Disk I/O: lower priority than live container
- The nested Genesis only runs during active testing, not continuously

If the host VM needs more resources to support this (e.g., 64GB RAM),
that's an acceptable infrastructure investment given the safety value.

## Guardian Diagnostic Checklist

Blanket instructions like "analyze the situation" don't work — the host
VM CC session that diagnosed the 2026-03-16 I/O storm initially missed
the root cause because it looked at current I/O limits (a symptom) and
proposed raising them, instead of looking at temporal data that showed
I/O was near zero until 20 minutes before the crash. The user had to
manually point out that this was a sudden storm, not chronic pressure.

The guardian MUST follow a concrete, ordered checklist — not aspirational
guidance. Each step produces specific data that feeds the next.

### Step 1: Collect All Infrastructure Metrics (Programmatic)

Before any LLM reasoning, a data collector script gathers everything
into a structured snapshot. The LLM does NOT hunt for data — it receives
a complete picture.

```
Collect NOW:
  - Container status (incus info genesis)
  - Container process list (incus exec -- ps aux --sort=-%mem)
  - Memory: current, max, pressure (cgroup files)
  - I/O: current pressure, read/write bytes (cgroup io.stat, io.pressure)
  - Disk: usage per mount (/, /tmp, /home)
  - CPU: usage, pressure (cpu.stat, cpu.pressure)
  - Network: can ping container? can reach health API?
  - Journal: last 100 lines from user services
  - Systemd: status of bridge, watchdog, qdrant, user manager
  - Running processes: count, any zombies, any D-state
  - Open file descriptors: count per process
  - Git: last commit, any uncommitted changes, recent git log

Collect HISTORICAL (from DB / logs / snapshots):
  - Health signal history: last 6-24 hours of awareness tick data
  - I/O pressure history (io.pressure avg10/avg60/avg300)
  - Memory usage history (from awareness tick signals)
  - Disk usage history (from awareness tick signals)
  - /tmp usage history
  - Recent CC session activity (what was running?)
  - Recent backup log (did last backup succeed?)
  - Recent journal errors (last 1 hour, last 6 hours)
```

### Step 2: Temporal Analysis (LLM)

The LLM receives the full snapshot and MUST answer:
1. When did the problem start? (Compare current metrics to historical
   baseline — is this a sudden spike or gradual degradation?)
2. What changed at that time? (git log, CC session activity, cron jobs)
3. Which metric deviated FIRST? (Sequence matters — the first metric
   to go abnormal is closest to the root cause)

### Step 3: Root Cause Hypothesis (LLM)

Only AFTER temporal analysis, form a hypothesis:
1. State the hypothesis with confidence percentage
2. State what data supports it
3. State what data contradicts it
4. State what would disprove it
5. If confidence < 70%, collect more data before acting

### Step 4: Recovery Decision (LLM)

Based on diagnosis:
- If code change caused it → revert specific change
- If resource exhaustion → clear the resource, then investigate cause
- If unknown → snapshot rollback (safest option)
- NEVER raise limits or work around symptoms without understanding cause

## Known Failure Mode Inventory

A catalog of infrastructure failure modes Genesis has encountered or
could encounter. The infrastructure forecast LLM receives this as
context so it knows what to watch for — and is told to think about
what's NOT in this list.

### Category: Filesystem

| Failure Mode | Trigger | Impact | Detection | Known? |
|---|---|---|---|---|
| /tmp fills up | Large file writes (clone, download) | Kills ALL CC sessions | tmpfs_free_pct signal | 2026-03-15 |
| Root disk full | Log growth, DB growth, pip cache | Everything fails | disk_free_pct signal | Not yet hit |
| SQLite corruption | Concurrent writes, crash during write | Data loss, bootstrap failure | DB probe in health check | Not yet hit |
| Journal fills disk | Excessive logging, crash-loops | Same as root disk full | Disk monitoring | Not yet hit |

### Category: Memory

| Failure Mode | Trigger | Impact | Detection | Known? |
|---|---|---|---|---|
| Container OOM | Memory accumulation, ghost page cache | Cgroup kills processes | container_memory_pct signal | 2026-03-16 |
| Page cache I/O storm | Aggressive cache reclaim | D-state processes, freezes | io.pressure avg10 | 2026-03-16 |
| Memory leak | Long-running process with leak | Gradual OOM | RSS trend over time | Not yet hit |
| Zombie accumulation | Orphaned subprocesses | Memory + PID exhaustion | Process count monitoring | 2026-03-16 (ACP spike) |

### Category: Process / Signals

| Failure Mode | Trigger | Impact | Detection | Known? |
|---|---|---|---|---|
| killpg with bad PGID | Mock PID coercion, unvalidated args | Kills ALL user processes | Code guard + behavioral linter | 2026-03-16 |
| Systemd user manager death | OOM kill of systemd, cascading failure | All services die silently | Guardian heartbeat | 2026-03-16 |
| Bridge crash-loop | Code bug on startup | Bridge restarts every 60s | StartLimitBurst detection | 2026-03-16 |
| File descriptor exhaustion | Too many open files/sockets | New connections fail | /proc/pid/fd count | Not yet hit |

### Category: Network / External

| Failure Mode | Trigger | Impact | Detection | Known? |
|---|---|---|---|---|
| Ollama unreachable | Network partition to ${OLLAMA_HOST:-localhost} | Embeddings fail, awareness degrades | Ollama health probe | Not yet hit |
| Telegram API outage | Telegram servers down | No user communication | Telegram polling errors | Not yet hit |
| Qdrant down | OOM, crash, port conflict | Memory ops fail | Qdrant health probe | Briefly during OOM |
| DNS failure | Resolver misconfiguration | All external APIs fail | DNS resolution check | Not yet hit |
| Tailscale/SSH loss | Network change, tailscale restart | User can't reach system | Not detectable internally | Occasional |

### Category: Code / Config

| Failure Mode | Trigger | Impact | Detection | Known? |
|---|---|---|---|---|
| pip install -e to worktree | Editable install redirect | All processes load wrong code | PreToolUse hook blocks | 2026-03-16 |
| Python venv corruption | Bad pip install, version conflict | Import errors, startup failure | Bootstrap manifest | Not yet hit |
| Config drift | Live systemd unit != git version | Behavior mismatch | Config comparison check | 2026-03-16 |
| Clock skew | NTP failure, container time drift | TLS errors, bad timestamps | Time comparison check | Not yet hit |

### Category: Data

| Failure Mode | Trigger | Impact | Detection | Known? |
|---|---|---|---|---|
| Qdrant data deletion | Tests without delete guard | Production data lost | Delete guard + backup verify | 2026-03-16 |
| Backup failure | Passphrase not set, push failure | No recovery point | Backup log monitoring | Ongoing (secrets) |
| Cron job failure | Path error, permission issue | Backups/inbox sync stop | Cron output monitoring | Not yet hit |

### What's NOT in this list (prompt for forecast LLM)

The forecast LLM should be explicitly told:
> "This inventory is incomplete. Think about what failure modes we
> haven't encountered yet. What could fill up, run out, get corrupted,
> or stop working that isn't listed here? What assumptions are we
> making about the environment that could be wrong?"

## Gaps and Open Questions

### Network as the failure mode
If the host VM's network goes down, the guardian can't reach the
container AND can't send Telegram alerts. Mitigation: the guardian
should have a local recovery playbook that works without network.

### Observation window calibration
The 60-second chaos battery is a starting point. Real-world experience
will reveal whether some failure modes need longer observation. The
duration should be configurable and tunable based on what breaks.

### Guardian intelligence level
The guardian CC session requires a Claude subscription and network
access. If the subscription lapses or Anthropic's API is down, the
guardian falls back to the dumb systemd timer (restart container, hope
for the best). The intelligent layer is best-effort; the dumb layer is
the safety net.

### Incremental snapshot overhead
Incus ZFS/btrfs snapshots are fast, but taking one before every code
change could accumulate storage. Need a retention policy and monitoring
of snapshot disk usage.

### Nested container bootstrapping
First-time setup of the nested container requires creating the Incus
container, installing dependencies, cloning the repo. This is a one-time
cost, but the setup script must be robust and idempotent.

## Version Scoping

### V3 (Current — design + backup hardening + infrastructure forecast)
- This design document
- Fix `GENESIS_BACKUP_PASSPHRASE` (secrets not being backed up)
- Consider increasing backup frequency (6h → 2h or 1h)
- Implement Infrastructure Forecast surplus compute call site
- Document the guardian concept for V4 implementation

### V4 (Guardian implementation + sandbox design)
- Host VM guardian: heartbeat monitor, diagnosis, recovery, Telegram
- Pre-change Incus snapshots
- Container↔host communication protocol
- Sandbox design doc (detailed)
- Mutual watchman protocol

### V5 (Full sandbox implementation)
- Nested container infrastructure
- Incremental codebase sync
- Chaos test battery
- Automated test-before-merge pipeline
- Resource management and monitoring

## Relationship to Existing Systems

### Existing resilience (inner defense ring)
Circuit breakers, deferred work queue, health monitoring, the watchdog,
and the page cache reclaimer remain the first line of defense. They
handle transient failures, provider outages, and recoverable errors.
The guardian only activates when this inner ring has failed.

### Backup system evolution
- `backup.sh` continues as-is for data backups
- Incus snapshots (guardian-managed) provide full system state backup
- Together they cover both data recovery and system state recovery
- Secrets backup needs `GENESIS_BACKUP_PASSPHRASE` to be set

### Autonomy subsystem integration
The autonomy manager's action classification can feed into the
pre-change snapshot decision:
- L1-L2 actions (reading, suggesting): no snapshot needed
- L3 actions (writing, modifying): snapshot before applying
- L4 actions (system changes, deployments): snapshot + sandbox test

This creates a proportional safety response — low-risk actions are fast,
high-risk actions get full validation.

### Infrastructure Forecast (new surplus compute call site)

The awareness loop collects point-in-time signals every 5 minutes. The
health MCP reports current state. But no existing subsystem reviews
*trends over time*. The /tmp filesystem filling up on 2026-03-15 was
detectable hours before it crashed CC sessions — the signals were being
collected, but nobody was watching the trajectory.

**Infrastructure Forecast** is a new call site that reviews accumulated
health signals and the known failure mode inventory to spot concerning
trends and anticipate problems.

**Frequency: Adaptive (1-6 hours)**. Tied to the awareness loop's
activity detection. During active periods (messages, reflections, code
changes, CC sessions), monitor every 1-2 hours. During idle periods,
every 6 hours. This matches real usage patterns — most work is bursty,
and that's when failures are most likely.

**Compute routing: Surplus with critical fallback**. This is surplus
compute by default (Gemini, Groq, Mistral — any model can analyze
metrics). But it's too important to skip if surplus is exhausted. If
no surplus compute is available, fall back to a paid provider (cheapest
adequate model). This call site MUST fire on schedule regardless.

**Data collection: Programmatic first, then LLM**. A data collector
script gathers ALL metrics into a structured snapshot before the LLM
call. The LLM does not hunt for data — it receives a complete picture:

- Disk fill rate (current usage + rate of change → time to full)
- /tmp and tmpfs consumption (current + history)
- RAM consumption trajectory (growing baseline = leak)
- I/O pressure trends (sustained increase = degradation)
- Qdrant/SQLite size growth
- Process count trends (orphan accumulation)
- Page cache pressure history
- Network connectivity to external services
- Cron job status (did last backup succeed?)
- All signals from the known failure mode inventory

The LLM receives:
1. The current snapshot
2. Historical snapshots (last 6-24 hours for trend analysis)
3. The failure mode inventory (what to watch for)
4. A prompt: "What's getting worse? What's approaching a limit?
   What failure modes from the inventory are showing early signs?
   What failure modes NOT in the inventory should we worry about?"

**What this catches**: Gradual degradation — disk filling over days, RAM
growing over hours, I/O pressure creeping up. It does NOT catch sudden
events (like cloning a massive repo into /tmp — that goes from 50MB to
full in seconds). Sudden events are caught by the awareness loop's
real-time signals and the guardian's monitoring. The forecast catches
the slow-motion crashes.

**Honest correction on the /tmp issue**: The 2026-03-15 /tmp incident
was NOT a gradual trend. CC decided to clone large repos into /tmp and
it filled instantly. Trend analysis would not have caught it. What would
have caught it was knowing the failure mode existed — knowing /tmp is
512MB, knowing filling it kills CC. The failure mode inventory is the
right tool for that class of problem, not trend analysis.

---

## Related Documents

- [genesis-v3-resilience-architecture.md](genesis-v3-resilience-architecture.md) — System-level resilience design
- [genesis-v3-self-healing-design.md](genesis-v3-self-healing-design.md) — Self-healing server extension
