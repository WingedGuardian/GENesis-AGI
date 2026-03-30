# Genesis v3 — Self-Healing Server Design

**Status:** Active | **Last updated:** 2026-03-21


> Extends the existing resilience architecture (`genesis-v3-resilience-architecture.md`)
> with automatic remediation — mapping health signals to corrective actions under
> governance controls. This document is the source of truth for all self-healing
> behavior.

## 1. Overview

Genesis already has substantial health monitoring infrastructure: probe-based
health checks, a 4-axis resilience state machine with flapping protection,
a recovery orchestrator for post-outage draining, a watchdog with exponential
backoff for bridge restarts, and proactive memory pressure reclamation. What it
lacks is a unified **remediation layer** that connects arbitrary health signals
to corrective actions with cooldowns, governance enforcement, and escalation.

This design introduces a `RemediationRegistry` that sits between health probes
and corrective actions, enforcing safety invariants that prevent cascading
failures, oscillation, and unsupervised destructive operations.

## 2. Existing Infrastructure (Verified by Code Audit)

Every item below was verified against the actual source. File paths are relative
to the repository root.

### 2.1 Bridge Restart (Watchdog)

`src/genesis/autonomy/watchdog.py` — `WatchdogChecker`

- Reads `~/.genesis/status.json` (written every awareness tick by `StatusFileWriter`)
- Staleness threshold: 300s (configurable via `config/autonomy.yaml` `watchdog.staleness_threshold_seconds`)
- Exponential backoff: 10s initial, doubles per failure, capped at 300s
- Max 5 consecutive restart attempts before `WatchdogAction.NOTIFY`
- Pre-restart config validation: checks `secrets.env` exists with `TELEGRAM_BOT_TOKEN`, compiles bridge module for syntax errors
- Restart via `systemctl --user restart genesis-bridge.service` — never `os.kill()`
- State persisted in `~/.genesis/watchdog_state.json` (survives process restarts)
- Also checks `systemctl --user is-active genesis-bridge.service` as a fast-path before staleness check

`src/genesis/autonomy/watchdog_runner.py` — systemd oneshot entry point called by `genesis-watchdog.timer` every 60s.

### 2.2 Memory Pressure Reclamation

`src/genesis/autonomy/watchdog.py` — `_check_memory_pressure()`, `reclaim_page_cache()`, `get_container_memory()`

- Runs every watchdog cycle (60s)
- Reads cgroup v2 memory usage via `/sys/fs/cgroup/memory.current` and `memory.max`
- 80% threshold: reclaim 128M page cache
- 90% threshold: reclaim 256M page cache
- Reclaim target capped at 256M (larger reclaims cause I/O death spirals — incident 2026-03-16)
- I/O pressure guard: skips reclaim if `/sys/fs/cgroup/io.pressure` `full avg10 > 10%`
- Cooldown: max once per 300s via monotonic clock
- Writes to `/sys/fs/cgroup/user.slice/memory.reclaim` via `sudo tee` with 5s timeout

### 2.3 Status Heartbeat

`src/genesis/resilience/status_writer.py` — `StatusFileWriter`

- Writes `~/.genesis/status.json` every awareness tick
- Atomic write via `tempfile.mkstemp()` + `os.replace()` to prevent partial reads
- Contains: timestamp, 4-axis resilience state, queue depths (deferred, dead letter, pending embeddings), human summary
- Watchdog uses staleness of this file as the primary bridge liveness signal

### 2.4 Recovery Orchestrator

`src/genesis/resilience/recovery.py` — `RecoveryOrchestrator`

- Triggered when any resilience axis improves (detected via `should_recover()`)
- Confirmation: runs N consecutive health probes at intervals before committing to recovery
- Recovery sequence: expire stale deferred work, drain pending embeddings, drain deferred work by priority, replay dead letters
- Queue overflow detection: emits `WARNING` event if deferred work exceeds threshold (default 1000)

### 2.5 Health Probes

`src/genesis/observability/health.py` — four async probes, each returning `ProbeResult`:

| Probe | Target | Healthy Condition |
|-------|--------|-------------------|
| `probe_db()` | SQLite `SELECT 1` | Query succeeds |
| `probe_qdrant()` | `http://localhost:6333/healthz` | HTTP 200 |
| `probe_ollama()` | `http://${OLLAMA_URL:-localhost:11434}/api/tags` | HTTP 200 (also extracts model list) |
| `probe_scheduler()` | APScheduler instance | `scheduler.running == True` |

`ProbeResult` and `ProbeStatus` defined in `src/genesis/observability/types.py`.

### 2.6 Resilience State Machine

`src/genesis/resilience/state.py` — `ResilienceStateMachine`

- 4 independent axes: `CloudStatus`, `MemoryStatus`, `EmbeddingStatus`, `CCStatus`
- Flapping protection: >3 transitions in 15 minutes triggers 10-minute stabilization hold at worse state
- During stabilization: transitions to better states are suppressed, worse states are accepted and extend the hold
- Legacy mapping via `to_legacy_degradation_level()` for backward compatibility

### 2.7 Autonomy Governance

`src/genesis/autonomy/classification.py` — `ActionClassifier`

- Maps `(ActionClass, autonomy_level)` to `ApprovalDecision` (ACT/PROPOSE/BLOCK)
- V3 policy: REVERSIBLE=ACT, COSTLY_REVERSIBLE=PROPOSE, IRREVERSIBLE=PROPOSE
- Config: `config/autonomy.yaml` `approval_policy`

`src/genesis/autonomy/types.py` — domain types including `ActionClass`, `ApprovalDecision`, `AutonomyLevel` (L1-L4), `WatchdogAction`

### 2.8 Embedding Fallback in Memory Store

`src/genesis/memory/store.py` — `MemoryStore.store()`

- Catches `EmbeddingUnavailableError` from `EmbeddingProvider.embed()`
- Falls back to FTS5-only storage + queues embedding via `pending_embeddings.create()`
- Does NOT catch Qdrant connection errors from `upsert_point()` (Gap 1 below)

### 2.9 Health Outreach Bridge

`src/genesis/outreach/health_outreach.py` — `HealthOutreachBridge`

- Bridges health alerts to outreach requests
- Filters to WARNING+ severity
- Deduplicates by alert ID within 6-hour window
- Creates `OutreachRequest` with category `BLOCKER` for critical issues

### 2.10 Agent Zero Service Management

`~/.config/systemd/user/agent-zero.service` — systemd user service

- Manages the AZ Flask web server (port 5000) which hosts the Genesis dashboard
- `Restart=on-failure`, `RestartSec=10`, `StartLimitBurst=4`
- Watchdog checks `systemctl --user is-active agent-zero.service` every 60s
- Separate backoff state in `~/.genesis/watchdog_az_state.json` (independent from bridge)
- Max 3 restart attempts (conservative — AZ restart kills dashboard + all Genesis subsystems)
- Dashboard provides manual restart button (`POST /api/genesis/restart/agent-zero`)

## 3. Signal-to-Remediation Table

Each row defines a health signal, its trigger condition, the corrective action,
and the governance controls that gate it.

| Health Signal | Condition | Remediation | Reversibility | Governance | Cooldown | Max Attempts |
|---|---|---|---|---|---|---|
| Bridge status stale | `status.json` >300s old | `systemctl --user restart genesis-bridge.service` | REVERSIBLE | L2 (auto) | 10s exponential backoff to 300s | 5 (existing) |
| Bridge process inactive | `systemctl is-active` returns non-active | Same as above | REVERSIBLE | L2 (auto) | Same backoff | 5 (existing) |
| Qdrant probe DOWN | >2 consecutive `probe_qdrant()` failures | `systemctl restart qdrant` | REVERSIBLE | L2 (auto) | 300s | 3 |
| `/tmp` usage >80% | `os.statvfs('/tmp')` shows >80% used | Remove stale CC artifacts in `/tmp/claude-*` | REVERSIBLE | L2 (auto) | 600s | 5 |
| Disk usage >90% | `os.statvfs('/')` shows >90% used | Rotate logs + propose cleanup via outreach | COSTLY_REVERSIBLE | L3 (confirm) | 3600s | 1 |
| Memory >80% | cgroup utilization >80% | Reclaim 128M page cache (existing) | REVERSIBLE | L2 (auto) | 300s | unbounded (existing) |
| Memory >90% | cgroup utilization >90% | Reclaim 256M page cache (existing) | REVERSIBLE | L2 (auto) | 300s | unbounded (existing) |
| Awareness tick overdue | >2x tick interval since last awareness heartbeat | Cancel + re-create awareness loop task | REVERSIBLE | L2 (auto) | 300s | 3 |
| Ollama unreachable | `probe_ollama()` returns DOWN | Alert only (external service, cannot restart) | N/A | L4 (alert) | 3600s | -- |
| Watchdog max retries | 5 consecutive bridge restart failures | Emit outreach BLOCKER to Telegram | N/A | L4 (alert) | -- | -- |
| AZ process inactive | `systemctl is-active agent-zero.service` returns non-active | `systemctl --user restart agent-zero.service` | REVERSIBLE | L2 (auto) | 10s exponential backoff to 300s | 3 (conservative — AZ restart kills dashboard + all subsystems) |
| AZ max retries | 3 consecutive AZ restart failures | Log ERROR, stop retrying (manual intervention needed) | N/A | L4 (alert) | -- | -- |

### Governance level semantics

- **L2 (auto)**: `ActionClass.REVERSIBLE` maps to `ApprovalDecision.ACT` — execute without user approval.
- **L3 (confirm)**: `ActionClass.COSTLY_REVERSIBLE` maps to `ApprovalDecision.PROPOSE` — queue outreach request describing the proposed action and wait for user confirmation.
- **L4 (alert)**: No remediation possible. Emit an `OutreachRequest` with category `BLOCKER` through the outreach pipeline so the user is notified via Telegram or other available channel.

## 4. Architecture

### 4.1 RemediationRegistry

Module: `src/genesis/autonomy/remediation.py` (placed in `autonomy/` because
remediation is an autonomous action, not a resilience state transition).

```
@dataclass(frozen=True)
class RemediationAction:
    name: str                    # Human-readable identifier
    probe_name: str              # Which health probe triggers this
    condition: str               # Human-readable trigger condition
    command: list[str]           # Shell command to execute
    governance_level: int        # 2=auto, 3=confirm via outreach, 4=alert only
    reversible: bool
    cooldown_s: int              # Min seconds between executions
    max_attempts: int            # Max consecutive attempts before giving up

@dataclass
class RemediationOutcome:
    action: RemediationAction
    triggered: bool
    executed: bool
    success: bool | None
    message: str
```

Core behavior:

- **Async mutex**: `asyncio.Lock` ensures only one remediation executes at a time. The lock wraps the entire evaluation loop — all actions are serialized within a single `check_and_remediate` call.
- **Per-action cooldown**: Tracks `_last_run` per action name. If `now - last_run < cooldown_s`, skip with reason "In cooldown".
- **Per-action attempt counter**: Tracks consecutive failures. If `failures >= max_attempts`, skip with reason "Max attempts reached" and call the outreach callback if provided.
- **Governance via integer levels**: L2 (auto-execute), L3 (propose via outreach callback), L4 (alert only via outreach). This is a V3 simplification — the full `ActionClassifier.classify()` integration is deferred to V4 when governance levels become dynamic.
- **Stabilization cooldown** (in watchdog, not registry): After any restart attempt, the failure counter is only reset if the action has not been attempted within `stabilization_s` (600s). This prevents oscillation.

**V4 enhancement**: Integrate with `ActionClassifier` for dynamic governance levels, add per-action mutex skip semantics, CC session awareness check before service restarts.

```python
class RemediationRegistry:
    def __init__(self, outreach_fn=None):
        self._actions: list[RemediationAction] = []
        self._last_run: dict[str, float] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._outreach_fn = outreach_fn

    def register(self, action: RemediationAction) -> None: ...

    async def check_and_remediate(self, probe_results: dict) -> list[RemediationOutcome]:
        """Evaluate all registered actions against probe results.
        Returns outcomes for logging/observability."""

    async def _execute_one(self, action, probe_results) -> RemediationOutcome:
        """Check condition → governance → cooldown → mutex → execute."""
```

### 4.2 Integration Points

The registry does not replace existing infrastructure. It wraps and extends it.

**WatchdogChecker** (existing, `src/genesis/autonomy/watchdog.py`):
- Bridge staleness and memory pressure remain in the watchdog (they predate the registry and are battle-tested).
- The watchdog runner (`watchdog_runner.py`) gains a new code path: when `WatchdogAction.NOTIFY` is returned, it calls `RemediationRegistry.escalate()` to emit an outreach BLOCKER instead of just logging and exiting non-zero.

**Awareness loop** (existing, wired into `GenesisRuntime`):
- After each awareness tick's probe cycle, the tick handler calls `RemediationRegistry.check_and_remediate(probe_results)`.
- This is where Qdrant restart, `/tmp` cleanup, awareness restart, and Ollama alerts fire.
- The watchdog handles bridge-level concerns separately (it runs outside the bridge process).

**OutreachPipeline** (existing, `src/genesis/outreach/pipeline.py`):
- L3/L4 actions create `OutreachRequest` objects and submit them through the pipeline.
- Category is `BLOCKER` for max-retry escalations, `ALERT` for degraded-but-functional signals.

**RecoveryOrchestrator** (existing, `src/genesis/resilience/recovery.py`):
- After a successful remediation (e.g., Qdrant restart), the orchestrator's `confirm_recovery()` + `run_recovery()` sequence handles draining deferred work and replaying dead letters.
- The remediation registry does NOT duplicate recovery logic — it triggers the condition, the orchestrator handles the aftermath.

**HealthOutreachBridge** (existing, `src/genesis/outreach/health_outreach.py`):
- Continues to independently bridge health alerts to outreach. The remediation registry's escalations are additive — they cover cases the bridge doesn't (e.g., watchdog max-retry, `/tmp` full).

### 4.3 Probe-to-Remediation Flow

Two independent loops feed the remediation system:

#### Loop A: Watchdog (runs outside the bridge, every 60s)

```
genesis-watchdog.timer (systemd, 60s)
  → watchdog_runner.main()
    → WatchdogChecker.check()
      → _check_memory_pressure()          # Existing: 80%/90% reclaim
      → _is_bridge_active()               # Existing: systemctl check
      → _read_status() + staleness check  # Existing: status.json age
      → _restart_if_allowed()             # Existing: backoff + validation + restart
    → If NOTIFY:
      → RemediationRegistry.escalate("bridge_max_retries")
        → OutreachPipeline.process(BLOCKER request)
    → If RESTART:
      → restart_bridge()                  # Existing: systemctl restart
```

#### Loop B: Awareness Tick (runs inside the bridge, every ~5 min)

```
Awareness tick fires
  → Run health probes: probe_db(), probe_qdrant(), probe_ollama(), probe_scheduler()
  → Run filesystem probes: probe_tmp(), probe_disk()
  → StatusFileWriter.write()              # Existing: updates status.json
  → RemediationRegistry.check_and_remediate(probe_results)
    → For each registered action:
      → action.condition(probe_results)?  # Does this signal fire?
      → Check cooldown                    # Too soon since last attempt?
      → Check max_attempts                # Exhausted retries?
      → Check governance level            # ACT / PROPOSE / BLOCK?
      → Acquire mutex                     # Another remediation running?
      → action.handler(probe_results)     # Execute corrective action
    → Return list[RemediationOutcome]
  → Log outcomes, emit events
  → If any action exhausted max_attempts:
    → OutreachPipeline.process(BLOCKER request)
```

### 4.4 New Health Probes

Two new probes to be added to `src/genesis/observability/health.py`:

**`probe_tmp()`** — Filesystem probe for `/tmp` (512M tmpfs).

```python
async def probe_tmp(
    path: str = "/tmp",
    threshold_pct: float = 80.0,
    *,
    clock=None,
) -> ProbeResult:
    """Probe /tmp filesystem usage via os.statvfs()."""
    stat = os.statvfs(path)
    used_pct = (1 - stat.f_bavail / stat.f_blocks) * 100
    status = ProbeStatus.HEALTHY if used_pct < threshold_pct else ProbeStatus.DEGRADED
    return ProbeResult(
        name="tmp_fs",
        status=status,
        latency_ms=0.0,
        message=f"{used_pct:.0f}% used",
        details={"used_pct": round(used_pct, 1), "path": path},
    )
```

**`probe_disk()`** — Root filesystem probe.

```python
async def probe_disk(
    path: str = "/",
    threshold_pct: float = 90.0,
    *,
    clock=None,
) -> ProbeResult:
    """Probe root filesystem usage via os.statvfs()."""
    stat = os.statvfs(path)
    used_pct = (1 - stat.f_bavail / stat.f_blocks) * 100
    status = ProbeStatus.HEALTHY if used_pct < threshold_pct else ProbeStatus.DEGRADED
    return ProbeResult(
        name="disk_root",
        status=status,
        latency_ms=0.0,
        message=f"{used_pct:.0f}% used",
        details={"used_pct": round(used_pct, 1), "path": path},
    )
```

Both are synchronous operations (no I/O to external services) but maintain the async signature for consistency with the probe interface.

## 5. Safety Invariants

These are non-negotiable. Violation of any invariant is a bug.

### 5.1 Single-Remediation Mutex

**Never auto-remediate two things simultaneously.** The `RemediationRegistry` holds an `asyncio.Lock()` that wraps the entire `check_and_remediate` evaluation loop. All actions within a single call are serialized. Concurrent calls from different coroutines block until the first completes. Rationale: concurrent restarts can create cascading failures.

**V4 enhancement:** Add per-action mutex skip semantics and lock timeout (60s).

### 5.2 CC Session Awareness (V4)

**V3 status: NOT IMPLEMENTED.** In V4, before executing any remediation that restarts a service the bridge depends on, check whether a CC session is in progress via the session manager. V3 relies on cooldown + max-attempts to limit blast radius. Memory reclamation is exempt.

### 5.3 Per-Action Cooldown

**Maximum rate: 1 execution per cooldown period per action.** Tracked via monotonic clock. The cooldown starts when execution begins (not when it completes), preventing thundering-herd retries if the action hangs.

### 5.4 Remediation Circuit Breaker

**Max-attempts failures trigger escalation, not retry.** When an action reaches `max_attempts` consecutive failures, the registry stops executing it and emits an outreach `BLOCKER` to the user. The counter resets only after manual acknowledgment or after `stabilization_s` elapses without any attempt.

### 5.5 Stabilization Cooldown

**Don't reset the failure counter for `stabilization_s` after the last restart attempt.** This prevents the oscillation pattern:
1. Bridge is broken, watchdog restarts it
2. Bridge starts, writes a fresh `status.json`
3. Watchdog sees fresh status, resets `consecutive_failures` to 0
4. Bridge crashes again
5. Repeat from step 1 indefinitely

With stabilization: after a restart attempt, the failure counter is only reset if no attempt has occurred within the stabilization window (default 600s). This means the bridge must stay healthy for 10 minutes before the watchdog considers it recovered.

**Implemented**: `WatchdogChecker._reset_state()` now checks `last_restart_at` from watchdog state. If a restart was attempted within `_stabilization_s` (default 600s), the counter is NOT reset — the bridge must stay healthy for the full cooldown before being considered recovered.

### 5.6 Process Kill Safety

**Never kill processes by PID. Always use `systemctl`.** This is a project-wide invariant (see CLAUDE.md). The remediation registry must never call `os.kill()`, `os.killpg()`, or `signal.send_signal()`. All process management goes through `systemctl --user restart <unit>` or `systemctl restart <unit>`.

### 5.7 No Blind Deletion

**Filesystem cleanup actions must use allowlists, not glob-and-delete.** The `/tmp` cleanup handler removes only files matching known CC artifact patterns (`/tmp/claude-*`, `/tmp/.claude-*`) older than 1 hour. It never uses `rm -rf` on directories. The disk cleanup handler only rotates logs it owns (`logs/*.log`) and proposes other cleanup via outreach.

## 6. Gaps Fixed by This Design

### Gap 1: Qdrant Mid-Operation Crash in `store.py`

**File**: `src/genesis/memory/store.py`, `MemoryStore.store()` method

**Problem**: The `upsert_point()` call (line 65) is wrapped in a `try/except` that only catches `EmbeddingUnavailableError`. If Qdrant itself is down (connection refused, timeout), the `upsert_point()` call raises an unhandled `qdrant_client` exception, which propagates up and kills the store operation entirely. The FTS5 write (line 89) never executes.

**Fix**: Wrap `upsert_point()` with a broader exception handler that catches Qdrant connection errors (`qdrant_client.http.exceptions.UnexpectedResponse`, `httpx.ConnectError`, `httpx.TimeoutException`). On Qdrant failure, fall back to the same FTS5 + `pending_embeddings` queue path already used for embedding unavailability.

**Pattern**: Identical to the existing `EmbeddingUnavailableError` handler — set `embedding_ok = False`, log at WARNING, continue to FTS5 write, queue for later embedding.

### Gap 2: NOTIFY Goes Unnoticed

**File**: `src/genesis/autonomy/watchdog_runner.py`, `main()` function

**Problem**: When `WatchdogAction.NOTIFY` is returned (max restarts reached, or status file missing), the runner logs an error and exits with code 1. systemd records the failure, but no notification reaches the user. The watchdog has determined the bridge is unrecoverably broken and the user needs to intervene, but the only evidence is a systemd journal entry.

**Fix**: On `NOTIFY`, call into the outreach pipeline (or a lightweight Telegram API call if the pipeline is unavailable — since the bridge is down, the full pipeline may not be functional). Emit an `OutreachRequest` with category `BLOCKER`, topic "Bridge unrecoverable", and context including the failure count and last reason from watchdog state.

**Fallback**: If the outreach pipeline is unreachable (bridge is down), the watchdog runner falls back to a direct `httpx` POST to the Telegram Bot API using the token from `secrets.env`. This is the only place in the system where direct Telegram API access is permitted outside the outreach pipeline.

### Gap 3: State Reset Oscillation

**File**: `src/genesis/autonomy/watchdog.py`, `WatchdogChecker._reset_state()` (line 278)

**Problem**: When status.json is fresh (staleness < threshold), `_reset_state()` unconditionally writes `consecutive_failures: 0`. If the bridge was just restarted and momentarily writes a fresh status.json before crashing again, the failure counter resets. This allows infinite restart loops — each restart produces a brief fresh status, resetting the counter, allowing another restart when it goes stale again.

**Fix**: Add a stabilization check to `_reset_state()`. Before resetting, read the last restart attempt timestamp from watchdog state. If `now - last_attempt_at < stabilization_s` (default 600s), do NOT reset the counter. Only reset after the bridge has been continuously healthy for the stabilization window.

```python
def _reset_state(self) -> None:
    state = self._load_state()
    last_attempt = state.get("next_attempt_after")  # Set by _record_failure
    if last_attempt is not None:
        # Check stabilization window
        elapsed = time.time() - last_attempt
        if elapsed < self._stabilization_s:
            logger.debug(
                "Skipping state reset — stabilization active (%.0fs remaining)",
                self._stabilization_s - elapsed,
            )
            return
    # Safe to reset
    self._state_path.write_text(json.dumps({
        "consecutive_failures": 0,
        "next_attempt_after": None,
        "last_reason": None,
        "last_check_at": datetime.now(UTC).isoformat(),
    }))
```

## 7. Configuration

Remediation settings extend the existing `config/resilience.yaml`:

```yaml
# Added to config/resilience.yaml

remediation:
  enabled: true
  mutex_timeout_s: 60                    # Max time to hold the remediation lock
  default_stabilization_s: 600           # Per-action override available

  actions:
    qdrant_restart:
      cooldown_s: 300
      max_attempts: 3
      consecutive_failures_trigger: 2    # Fire after 2 consecutive probe failures

    tmp_cleanup:
      cooldown_s: 600
      max_attempts: 5
      threshold_pct: 80.0
      artifact_patterns:
        - "/tmp/claude-*"
        - "/tmp/.claude-*"
      min_age_s: 3600                    # Only clean files older than 1 hour

    disk_cleanup:
      cooldown_s: 3600
      max_attempts: 1                    # Propose once, then wait for user
      threshold_pct: 90.0
      log_rotate_paths:
        - "logs/*.log"

    awareness_restart:
      cooldown_s: 300
      max_attempts: 3
      overdue_multiplier: 2.0            # Fire at 2x tick interval
```

## 8. Verification Plan

### Unit Tests

All tests in `tests/test_remediation.py`:

1. **Condition evaluation**: Each remediation fires only when its specific condition is met.
2. **Cooldown enforcement**: Action is skipped if fired within cooldown window.
3. **Max attempt tracking**: Action is skipped and BLOCKER emitted after max attempts.
4. **Mutex exclusion**: Second concurrent remediation is skipped with `skipped_reason="mutex"`.
5. **Governance enforcement**: COSTLY_REVERSIBLE actions produce PROPOSE outreach, not execution.
6. **Stabilization cooldown**: Failure counter is NOT reset within stabilization window even if status.json is fresh.
7. **Oscillation prevention**: Simulate bridge crash-restart-crash cycle, verify max_attempts is reached and escalation fires.

### Integration Tests

1. **Watchdog → remediation escalation**: `WatchdogAction.NOTIFY` triggers outreach BLOCKER creation.
2. **Awareness tick → remediation cycle**: Probe results flow through registry, correct actions fire in priority order.
3. **Remediation → recovery orchestrator**: After successful Qdrant restart, recovery orchestrator drains pending embeddings and deferred work.

### Manual Verification

1. Stop Qdrant (`systemctl stop qdrant`), verify remediation fires after 2 consecutive failed probes, verify Qdrant restarts, verify recovery orchestrator drains pending items.
2. Fill `/tmp` past 80%, verify cleanup fires, verify only CC artifacts older than 1 hour are removed.
3. Stop the bridge, verify watchdog reaches max retries, verify Telegram BLOCKER notification is received.

## 9. Observability

Every remediation attempt emits a `GenesisEvent` via the event bus:

- **Subsystem**: `Subsystem.HEALTH`
- **Severity**: `INFO` for successful remediation, `WARNING` for skipped (cooldown/mutex), `ERROR` for failed remediation, `CRITICAL` for max-attempts escalation
- **Event type**: `remediation.<action_name>.<outcome>` (e.g., `remediation.qdrant_restart.success`, `remediation.tmp_cleanup.skipped_cooldown`)

The health MCP server (`src/genesis/mcp/health_mcp.py`) exposes remediation history through `health_status()` so the neural monitor dashboard can display recent remediation activity.

Remediation outcomes are also written to `~/.genesis/status.json` under a new `last_remediation` key, enabling the watchdog to see what the awareness loop has been doing and vice versa.

## 10. Relationship to Existing Architecture Documents

| Document | Relationship |
|---|---|
| `genesis-v3-resilience-architecture.md` | Parent doc. This design extends section 3 (Failure Mode Responses) with automated remediation. |
| `genesis-v3-resilience-patterns.md` | Building-blocks reference. Deferred work, dead letters, embedding recovery are consumed here, not duplicated. |
| `genesis-v3-autonomous-behavior-design.md` | Awareness loop and reflection engine. Remediation fires within awareness ticks. |
| `genesis-v3-build-phases.md` | Self-healing was not a named build phase. It extends Phase 9 (Basic Autonomy) infrastructure. |

## 11. Future Extensions (V4)

These are explicitly out of scope for V3.

- **Predictive remediation**: Detect trends (memory creeping toward threshold, Qdrant latency increasing) and remediate before failure. Requires time-series data not currently collected.
- **Remediation learning**: Track which remediations succeed and which fail, adjust strategy (e.g., if Qdrant restart fails 80% of the time, escalate sooner). Requires outcome tracking over weeks.
- **Cross-service dependency graph**: Restart services in correct order based on dependency relationships (e.g., don't restart the bridge if Qdrant is down — fix Qdrant first). V3 uses the mutex as a coarse approximation.
- **Remediation dry-run mode**: Execute condition checks and governance gates without actually firing handlers. Useful for testing new remediation actions in production.

---

## Related Documents

- [genesis-v3-resilience-architecture.md](genesis-v3-resilience-architecture.md) — System-level resilience design
- [genesis-v3-resilience-patterns.md](genesis-v3-resilience-patterns.md) — Tactical resilience patterns
