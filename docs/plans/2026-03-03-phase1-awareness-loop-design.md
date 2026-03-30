# Phase 1 Design: The Metronome (Awareness Loop)

> **Date:** 2026-03-03
> **Status:** Approved
> **Depends on:** Phase 0 (Data Foundation) — COMPLETE
> **Risk:** LOW — Purely programmatic. No LLM, no user-facing output.

---

## Overview

The Awareness Loop is Genesis's heartbeat — a 5-minute tick that collects signals,
computes urgency scores, classifies reflection depth, and records the decision. It
is the sole authority for triggering reflection (Phases 4+). In Phase 1 it runs
standalone; wiring to the Reflection Engine happens in Phase 4.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                       AwarenessLoop                          │
│  APScheduler AsyncIOScheduler — 5-min IntervalTrigger        │
│                                                              │
│  on_tick():                                                  │
│    signals = SignalCollector.collect_all()                    │
│    scores  = UrgencyScorer.score(signals, db)                │
│    decision = DepthClassifier.classify(scores, db)           │
│    → write to awareness_ticks table                          │
│    → if triggered: also write observation                    │
│                                                              │
│  force_tick():                                               │
│    same pipeline, flagged source='critical_bypass'           │
│    bypasses cooldowns, still respects tick lock              │
│                                                              │
│  # GROUNDWORK(category-2-rhythms): hook for Genesis rhythms  │
│  # GROUNDWORK(category-3-crons): hook for user-scheduled     │
└──────────────────────────────────────────────────────────────┘
```

### Package Structure

```
src/genesis/awareness/
├── __init__.py
├── loop.py           # AwarenessLoop orchestrator
├── signals.py        # SignalCollector protocol + 9 concrete collectors
├── scorer.py         # UrgencyScorer — formula + time multipliers
├── classifier.py     # DepthClassifier — thresholds + floor/ceiling
└── types.py          # Depth enum, SignalReading, TickResult, DepthDecision

tests/test_awareness/
├── conftest.py       # Fixtures: seeded DB, mock collectors
├── test_signals.py   # Each collector returns valid SignalReading
├── test_scorer.py    # Formula correctness, time multiplier curves
├── test_classifier.py # Threshold, floor/ceiling enforcement
├── test_loop.py      # Tick lifecycle, force_tick, DB writes
└── test_integration.py # End-to-end: signals → score → classify → store
```

---

## Component Design

### types.py — Data Types

```python
from enum import Enum

class Depth(str, Enum):
    MICRO = "Micro"         # Matches DB seed: feeds_depths JSON
    LIGHT = "Light"
    DEEP = "Deep"
    STRATEGIC = "Strategic"

@dataclass(frozen=True)
class SignalReading:
    name: str               # e.g. "software_error_spike"
    value: float            # 0.0–1.0 normalized
    source: str             # e.g. "health_mcp"
    collected_at: str       # ISO datetime

@dataclass(frozen=True)
class DepthScore:
    depth: Depth
    raw_score: float        # Σ(signal × weight) before time multiplier
    time_multiplier: float  # Based on elapsed time since last tick at this depth
    final_score: float      # raw_score × time_multiplier
    threshold: float        # From DB
    triggered: bool         # final_score >= threshold

@dataclass(frozen=True)
class TickResult:
    tick_id: str            # UUID
    timestamp: str          # ISO datetime
    source: str             # "scheduled" | "critical_bypass"
    signals: list[SignalReading]
    scores: list[DepthScore]
    classified_depth: Depth | None   # Highest triggered depth, or None
    trigger_reason: str | None       # Human-readable reason if triggered
```

### signals.py — Signal Collection

**Protocol:**

```python
class SignalCollector(Protocol):
    signal_name: str
    async def collect(self) -> SignalReading: ...
```

**Nine implementations** — one per signal from the design doc:

| Collector | Signal | Phase 1 behavior |
|-----------|--------|-----------------|
| ConversationCollector | conversations_since_reflection | Returns 0.0 (no AZ integration yet) |
| TaskQualityCollector | task_completion_quality | Returns 0.0 |
| OutreachEngagementCollector | outreach_engagement_data | Returns 0.0 |
| ReconFindingsCollector | recon_findings_pending | Returns 0.0 |
| MemoryBacklogCollector | unprocessed_memory_backlog | Returns 0.0 |
| BudgetCollector | budget_pct_consumed | Returns 0.0 |
| ErrorSpikeCollector | software_error_spike | Returns 0.0 |
| CriticalFailureCollector | critical_failure | Returns 0.0 |
| StrategicTimerCollector | time_since_last_strategic | Queries awareness_ticks table for last Strategic tick |

All return 0.0 in Phase 1 except `StrategicTimerCollector`, which can compute
a real value by querying the tick history table. When MCP servers are implemented
(later phases), the collector implementations get updated to call real data
sources — the protocol interface stays the same.

**`collect_all()`** runs all collectors concurrently via `asyncio.gather()`.
Individual collector failures return `SignalReading(value=0.0)` with a logged
warning — one failing signal source must not break the tick.

### scorer.py — Urgency Scoring

Implements the design doc formula:

```
urgency_score(depth) = Σ(signal_value_i × weight_i) × time_multiplier(depth)
```

**Per-depth scoring:**
1. Query `signal_weights` via `list_by_depth(depth)` to get relevant signals + weights
2. Match collected signals by name, multiply value × weight, sum
3. Compute time multiplier from elapsed time since last tick at this depth

**Time multiplier curves** (from design doc):

| Depth | 0min/0h/0d | Floor start | Floor end | Overdue |
|-------|-----------|-------------|-----------|---------|
| Micro | 0.3x @ 0min | 1.0x @ 30min | — | 2.5x @ 60min |
| Light | 0.5x @ 0h | 1.0x @ 3h | 1.5x @ 6h (floor) | 3.0x @ 12h |
| Deep | 0.3x @ 0h | 1.0x @ 48h (floor) | 1.5x @ 72h | 2.5x @ 96h |
| Strategic | 0.2x @ 0d | 1.0x @ 7d (floor) | 2.0x @ 14d | 3.0x @ 21d |

Implementation: piecewise linear interpolation between the defined points.
Time since last tick at each depth is queried from the `awareness_ticks` table.

### classifier.py — Depth Classification

**Threshold lookup:** Read from `depth_thresholds` DB table (new — see Schema
Changes below).

**Classification logic:**
1. For each depth (Strategic → Deep → Light → Micro — highest first):
   - If `final_score >= threshold` AND floor/ceiling constraints allow → candidate
2. Return the highest triggered depth (or None)

**Floor enforcement** (minimum intervals — prevents silence):
| Depth | Floor |
|-------|-------|
| Micro | 30 min |
| Light | 6 hours |
| Deep | 48 hours |
| Strategic | 7 days |

If a depth hasn't been triggered within its floor interval, the time multiplier
naturally pushes the score above threshold. Floors are safety nets, not triggers —
the time multiplier curve is what actually enforces minimum cadence.

**Ceiling enforcement** (maximum frequency — prevents thrashing):
| Depth | Ceiling |
|-------|---------|
| Micro | 2 per hour |
| Light | 1 per hour |
| Deep | 1 per day |
| Strategic | 1 per week |

Ceiling check: count recent ticks at this depth from `awareness_ticks` table.
If at ceiling → skip this depth, check next lower.

**`force_tick()` behavior:** Critical bypass skips ceiling checks but still
respects the tick lock (no concurrent ticks ever). Floors are irrelevant for
force_tick since it's explicitly requested.

### loop.py — AwarenessLoop Orchestrator

```python
class AwarenessLoop:
    def __init__(self, db: aiosqlite.Connection, collectors: list[SignalCollector]):
        self._scheduler = AsyncIOScheduler()
        self._db = db
        self._collectors = collectors
        self._tick_lock = asyncio.Lock()  # Never concurrent ticks

    async def start(self):
        self._scheduler.add_job(
            self._on_tick, IntervalTrigger(minutes=5),
            id="awareness_tick", max_instances=1,
            misfire_grace_time=60  # Allow 60s late before skipping
        )
        self._scheduler.start()

    async def stop(self):
        self._scheduler.shutdown(wait=True)  # Wait for running tick to finish

    async def force_tick(self, reason: str):
        """Critical event bypass — immediate out-of-cycle tick."""
        async with self._tick_lock:
            await self._perform_tick(source="critical_bypass", reason=reason)

    async def _on_tick(self):
        async with self._tick_lock:
            await self._perform_tick(source="scheduled")

    async def _perform_tick(self, source: str, reason: str | None = None):
        """The actual tick pipeline — tested independently."""
        try:
            signals = await collect_all(self._collectors)
            scores = await compute_scores(self._db, signals)
            decision = await classify_depth(self._db, scores, bypass_ceiling=(source == "critical_bypass"))
            tick_result = TickResult(...)
            await store_tick(self._db, tick_result)
            if decision.classified_depth is not None:
                await store_trigger_observation(self._db, tick_result)
        except asyncio.CancelledError:
            # Graceful shutdown — don't leave partial writes
            raise
        except Exception:
            logging.exception("Awareness tick failed")
            # Tick failures are logged, never propagated — the metronome keeps ticking
```

**Key design decisions:**
- `_perform_tick()` is a standalone async function — testable without the scheduler
- `asyncio.Lock` prevents concurrent ticks (scheduled + force_tick race)
- `shutdown(wait=True)` prevents CancelledError during aiosqlite operations
- Tick failures are logged and swallowed — the loop must never stop

---

## Schema Changes

### New table: `awareness_ticks`

```sql
CREATE TABLE IF NOT EXISTS awareness_ticks (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL CHECK (source IN ('scheduled', 'critical_bypass')),
    signals_json    TEXT NOT NULL,       -- JSON: list of {name, value, source, collected_at}
    scores_json     TEXT NOT NULL,       -- JSON: list of {depth, raw, multiplier, final, threshold, triggered}
    classified_depth TEXT,               -- NULL if nothing triggered, else "Micro"/"Light"/"Deep"/"Strategic"
    trigger_reason  TEXT,
    created_at      TEXT NOT NULL
)
```

Indexes:
```sql
CREATE INDEX IF NOT EXISTS idx_ticks_depth ON awareness_ticks(classified_depth);
CREATE INDEX IF NOT EXISTS idx_ticks_created ON awareness_ticks(created_at);
```

### New table: `depth_thresholds`

```sql
CREATE TABLE IF NOT EXISTS depth_thresholds (
    depth_name     TEXT PRIMARY KEY,     -- "Micro", "Light", "Deep", "Strategic"
    threshold      REAL NOT NULL,
    floor_seconds  INTEGER NOT NULL,     -- Minimum interval in seconds
    ceiling_count  INTEGER NOT NULL,     -- Max triggers per ceiling_window
    ceiling_window_seconds INTEGER NOT NULL
)
```

Seed data:
| depth_name | threshold | floor_seconds | ceiling_count | ceiling_window_seconds |
|-----------|-----------|---------------|---------------|----------------------|
| Micro | 0.50 | 1800 (30min) | 2 | 3600 (1hr) |
| Light | 0.80 | 21600 (6h) | 1 | 3600 (1hr) |
| Deep | 0.55 | 172800 (48h) | 1 | 86400 (1day) |
| Strategic | 0.40 | 604800 (7d) | 1 | 604800 (1wk) |

### New dependency: `pyproject.toml`

Add `apscheduler>=3.11,<4` to dependencies.

### No changes to existing tables or code

All existing Phase 0 code remains untouched. New tables are additive. The only
file change outside `src/genesis/awareness/` is `pyproject.toml` (dependency)
and `src/genesis/db/schema.py` (new table DDL + seed data).

---

## Agent Zero Integration Notes

**Why APScheduler instead of AZ's built-in TaskScheduler:**

Agent Zero has `task_scheduler.py` + `job_loop.py` — a cron-based scheduler
with file persistence, running on a 60-second tick. It doesn't fit because:

1. 60-second granularity is too coarse for 5-minute precision
2. No `max_instances` control (can't prevent concurrent ticks)
3. No misfire grace period
4. No programmatic `force_tick()` — it's designed for user-facing cron tasks
5. Different problem domain: AZ schedules user tasks; Genesis schedules internal cognition

APScheduler handles Genesis-level scheduling. AZ's scheduler handles AZ-level
concerns. No overlap — they serve different masters.

**Event loop strategy:** In Phase 1, the scheduler runs on whatever loop is
available (test runner's loop, or a standalone script's loop). When integrated
into AZ (later phase), it will run in a dedicated background thread with its own
event loop, mirroring AZ's `EventLoopThread` pattern. This isolates Genesis's
tick from uvicorn's request handling.

---

## What This Does NOT Build

- Reflection Engine (Phase 4) — tick results go to DB, not to a consumer
- Real signal values from MCP servers — collectors return 0.0 (except StrategicTimer)
- Category 2 rhythms (morning report, calibration) — GROUNDWORK hooks only
- Category 3 user crons — GROUNDWORK hooks only
- Signal weight adaptation — V4 feature
- nest_asyncio interaction testing — deferred to AZ integration phase

---

## Verification Criteria

From the build phases doc + additions from adversarial review:

- [ ] Known signal + known weight → expected composite urgency score
- [ ] Time multiplier curves match design doc at all defined points
- [ ] Score thresholds correctly classify to right depth
- [ ] Critical events bypass the tick ceiling (but not the lock)
- [ ] Calendar floors enforce min intervals (via time multiplier)
- [ ] Calendar ceilings enforce max frequency
- [ ] Tick results persisted to awareness_ticks table
- [ ] Trigger events also create observations
- [ ] Non-trigger ticks do NOT create observations
- [ ] Depth enum names match DB seed data exactly
- [ ] Individual signal collector failure doesn't break the tick
- [ ] Scheduler shutdown doesn't corrupt in-flight DB writes
- [ ] All existing 229 tests still pass
- [ ] ruff clean
