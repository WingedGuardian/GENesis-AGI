# Phase 1: Awareness Loop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the Awareness Loop — a 5-minute tick scheduler that collects signals, computes urgency scores, classifies reflection depth, and persists results.

**Architecture:** APScheduler v3 `AsyncIOScheduler` drives a tick every 5 minutes. Each tick runs a pipeline: collect 9 signals → score urgency per depth → classify highest triggered depth → store tick + optional observation. All async, all tested, no LLM calls.

**Tech Stack:** Python 3.12, APScheduler 3.11.2, aiosqlite, pytest + pytest-asyncio

**Design doc:** `docs/plans/2026-03-03-phase1-awareness-loop-design.md`

---

### Task 1: Add APScheduler dependency

**Files:**
- Modify: `pyproject.toml:5-10`

**Step 1: Add apscheduler to dependencies**

In `pyproject.toml`, add `"apscheduler>=3.11,<4"` to the `dependencies` list:

```toml
dependencies = [
    "aiosqlite",
    "aiohttp",
    "apscheduler>=3.11,<4",
    "python-telegram-bot>=21.0",
    "qdrant-client",
]
```

**Step 2: Install and verify**

Run: `source ~/agent-zero/.venv/bin/activate && pip install -e ~/genesis && python -c "from apscheduler.schedulers.asyncio import AsyncIOScheduler; print('OK')"`
Expected: `OK`

**Step 3: Verify existing tests still pass**

Run: `cd ~/genesis && ruff check . && pytest -v --tb=short 2>&1 | tail -5`
Expected: `229 passed`, `All checks passed!`

**Step 4: Commit**

```bash
cd ~/genesis
git add pyproject.toml
git commit -m "feat(phase1): add APScheduler dependency"
```

---

### Task 2: Schema — awareness_ticks + depth_thresholds tables

**Files:**
- Modify: `src/genesis/db/schema.py:252-276` (add to TABLES dict, before `drive_weights`)
- Modify: `src/genesis/db/schema.py:326` (add new indexes to INDEXES list)
- Modify: `src/genesis/db/schema.py:347` (add seed data after DRIVE_WEIGHTS_SEED)
- Modify: `src/genesis/db/schema.py:365-379` (add seed insert to `seed_data()`)
- Test: `tests/test_db/test_schema.py`

**Step 1: Write the failing test**

Add to `tests/test_db/test_schema.py`:

```python
async def test_awareness_ticks_table_exists(db):
    """awareness_ticks table was created by create_all_tables."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='awareness_ticks'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_depth_thresholds_table_exists(db):
    """depth_thresholds table was created by create_all_tables."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='depth_thresholds'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_depth_thresholds_seeded(db):
    """depth_thresholds has seed data for all four depths."""
    cursor = await db.execute("SELECT depth_name FROM depth_thresholds ORDER BY depth_name")
    rows = await cursor.fetchall()
    names = [r["depth_name"] for r in rows]
    assert names == ["Deep", "Light", "Micro", "Strategic"]
```

**Step 2: Run tests to verify they fail**

Run: `cd ~/genesis && pytest tests/test_db/test_schema.py -v --tb=short 2>&1 | tail -10`
Expected: 3 FAILED (table does not exist)

**Step 3: Add table DDL to schema.py**

Add these entries to the `TABLES` dict in `src/genesis/db/schema.py` (after `drive_weights`, before the closing `}`):

```python
    "awareness_ticks": """
        CREATE TABLE IF NOT EXISTS awareness_ticks (
            id               TEXT PRIMARY KEY,
            source           TEXT NOT NULL CHECK (source IN ('scheduled', 'critical_bypass')),
            signals_json     TEXT NOT NULL,
            scores_json      TEXT NOT NULL,
            classified_depth TEXT,
            trigger_reason   TEXT,
            created_at       TEXT NOT NULL
        )
    """,
    "depth_thresholds": """
        CREATE TABLE IF NOT EXISTS depth_thresholds (
            depth_name              TEXT PRIMARY KEY,
            threshold               REAL NOT NULL,
            floor_seconds           INTEGER NOT NULL,
            ceiling_count           INTEGER NOT NULL,
            ceiling_window_seconds  INTEGER NOT NULL
        )
    """,
```

Add these indexes to the `INDEXES` list:

```python
    # awareness loop
    "CREATE INDEX IF NOT EXISTS idx_ticks_depth ON awareness_ticks(classified_depth)",
    "CREATE INDEX IF NOT EXISTS idx_ticks_created ON awareness_ticks(created_at)",
```

Add seed data constant after `DRIVE_WEIGHTS_SEED`:

```python
DEPTH_THRESHOLDS_SEED = [
    # (depth_name, threshold, floor_seconds, ceiling_count, ceiling_window_seconds)
    ("Micro", 0.50, 1800, 2, 3600),         # floor 30min, max 2/hr
    ("Light", 0.80, 21600, 1, 3600),         # floor 6h, max 1/hr
    ("Deep", 0.55, 172800, 1, 86400),        # floor 48h, max 1/day
    ("Strategic", 0.40, 604800, 1, 604800),  # floor 7d, max 1/wk
]
```

Add to `seed_data()` function:

```python
    await db.executemany(
        """INSERT OR IGNORE INTO depth_thresholds
           (depth_name, threshold, floor_seconds, ceiling_count, ceiling_window_seconds)
           VALUES (?, ?, ?, ?, ?)""",
        DEPTH_THRESHOLDS_SEED,
    )
```

**Step 4: Run tests to verify they pass**

Run: `cd ~/genesis && pytest tests/test_db/test_schema.py -v --tb=short`
Expected: ALL PASSED (including the 3 new tests)

Run: `cd ~/genesis && ruff check . && pytest -v --tb=short 2>&1 | tail -5`
Expected: All 232+ tests pass, ruff clean

**Step 5: Commit**

```bash
cd ~/genesis
git add src/genesis/db/schema.py tests/test_db/test_schema.py
git commit -m "feat(phase1): add awareness_ticks and depth_thresholds tables with seed data"
```

---

### Task 3: CRUD modules — awareness_ticks + depth_thresholds

**Files:**
- Create: `src/genesis/db/crud/awareness_ticks.py`
- Create: `src/genesis/db/crud/depth_thresholds.py`
- Test: `tests/test_db/test_awareness_ticks.py`
- Test: `tests/test_db/test_depth_thresholds.py`

**Step 1: Write failing tests for awareness_ticks CRUD**

Create `tests/test_db/test_awareness_ticks.py`:

```python
"""Tests for awareness_ticks CRUD module."""

import json
from datetime import datetime, UTC

import pytest

from genesis.db.crud import awareness_ticks


@pytest.fixture
def tick_row():
    return {
        "id": "tick-001",
        "source": "scheduled",
        "signals_json": json.dumps([{"name": "software_error_spike", "value": 0.8}]),
        "scores_json": json.dumps([{"depth": "Micro", "final_score": 0.9, "triggered": True}]),
        "classified_depth": "Micro",
        "trigger_reason": "error spike",
        "created_at": datetime.now(UTC).isoformat(),
    }


async def test_create_and_get(db, tick_row):
    await awareness_ticks.create(db, **tick_row)
    row = await awareness_ticks.get_by_id(db, "tick-001")
    assert row is not None
    assert row["source"] == "scheduled"
    assert row["classified_depth"] == "Micro"


async def test_get_nonexistent(db):
    row = await awareness_ticks.get_by_id(db, "nope")
    assert row is None


async def test_query_by_depth(db, tick_row):
    await awareness_ticks.create(db, **tick_row)
    rows = await awareness_ticks.query(db, classified_depth="Micro")
    assert len(rows) == 1
    assert rows[0]["id"] == "tick-001"


async def test_query_by_source(db, tick_row):
    await awareness_ticks.create(db, **tick_row)
    rows = await awareness_ticks.query(db, source="scheduled")
    assert len(rows) == 1


async def test_count_in_window(db, tick_row):
    """Count ticks at a given depth within a time window."""
    await awareness_ticks.create(db, **tick_row)
    count = await awareness_ticks.count_in_window(
        db, depth="Micro", window_seconds=3600
    )
    assert count == 1


async def test_count_in_window_empty(db):
    count = await awareness_ticks.count_in_window(
        db, depth="Micro", window_seconds=3600
    )
    assert count == 0


async def test_last_tick_at_depth(db, tick_row):
    await awareness_ticks.create(db, **tick_row)
    last = await awareness_ticks.last_at_depth(db, "Micro")
    assert last is not None
    assert last["id"] == "tick-001"


async def test_last_tick_at_depth_empty(db):
    last = await awareness_ticks.last_at_depth(db, "Deep")
    assert last is None


async def test_delete(db, tick_row):
    await awareness_ticks.create(db, **tick_row)
    deleted = await awareness_ticks.delete(db, "tick-001")
    assert deleted is True
    assert await awareness_ticks.get_by_id(db, "tick-001") is None
```

**Step 2: Run to verify failures**

Run: `cd ~/genesis && pytest tests/test_db/test_awareness_ticks.py -v --tb=short 2>&1 | tail -15`
Expected: FAILED (ModuleNotFoundError)

**Step 3: Implement awareness_ticks CRUD**

Create `src/genesis/db/crud/awareness_ticks.py`:

```python
"""CRUD operations for awareness_ticks table."""

from __future__ import annotations

from datetime import datetime, UTC

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    source: str,
    signals_json: str,
    scores_json: str,
    created_at: str,
    classified_depth: str | None = None,
    trigger_reason: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO awareness_ticks
           (id, source, signals_json, scores_json, classified_depth,
            trigger_reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (id, source, signals_json, scores_json, classified_depth,
         trigger_reason, created_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM awareness_ticks WHERE id = ?", (id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def query(
    db: aiosqlite.Connection,
    *,
    source: str | None = None,
    classified_depth: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM awareness_ticks WHERE 1=1"
    params: list = []
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    if classified_depth is not None:
        sql += " AND classified_depth = ?"
        params.append(classified_depth)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def count_in_window(
    db: aiosqlite.Connection, *, depth: str, window_seconds: int
) -> int:
    """Count ticks at a given depth within the last window_seconds."""
    cutoff = datetime.now(UTC).isoformat()
    # SQLite datetime comparison works on ISO strings
    cursor = await db.execute(
        """SELECT COUNT(*) as cnt FROM awareness_ticks
           WHERE classified_depth = ?
           AND created_at >= datetime('now', ?)""",
        (depth, f"-{window_seconds} seconds"),
    )
    row = await cursor.fetchone()
    return row["cnt"] if row else 0


async def last_at_depth(db: aiosqlite.Connection, depth: str) -> dict | None:
    """Get the most recent tick at a given depth."""
    cursor = await db.execute(
        """SELECT * FROM awareness_ticks
           WHERE classified_depth = ?
           ORDER BY created_at DESC LIMIT 1""",
        (depth,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute(
        "DELETE FROM awareness_ticks WHERE id = ?", (id,)
    )
    await db.commit()
    return cursor.rowcount > 0
```

**Step 4: Write failing tests for depth_thresholds CRUD**

Create `tests/test_db/test_depth_thresholds.py`:

```python
"""Tests for depth_thresholds CRUD module."""

from genesis.db.crud import depth_thresholds


async def test_get_existing(db):
    row = await depth_thresholds.get(db, "Micro")
    assert row is not None
    assert row["threshold"] == 0.50
    assert row["floor_seconds"] == 1800


async def test_get_nonexistent(db):
    row = await depth_thresholds.get(db, "Nonexistent")
    assert row is None


async def test_list_all(db):
    rows = await depth_thresholds.list_all(db)
    assert len(rows) == 4
    names = [r["depth_name"] for r in rows]
    assert "Micro" in names
    assert "Strategic" in names


async def test_update_threshold(db):
    ok = await depth_thresholds.update_threshold(db, "Deep", new_threshold=0.60)
    assert ok is True
    row = await depth_thresholds.get(db, "Deep")
    assert row["threshold"] == 0.60
```

**Step 5: Implement depth_thresholds CRUD**

Create `src/genesis/db/crud/depth_thresholds.py`:

```python
"""CRUD operations for depth_thresholds table."""

from __future__ import annotations

import aiosqlite


async def get(db: aiosqlite.Connection, depth_name: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM depth_thresholds WHERE depth_name = ?", (depth_name,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_all(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM depth_thresholds ORDER BY depth_name"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def update_threshold(
    db: aiosqlite.Connection, depth_name: str, *, new_threshold: float
) -> bool:
    cursor = await db.execute(
        "UPDATE depth_thresholds SET threshold = ? WHERE depth_name = ?",
        (new_threshold, depth_name),
    )
    await db.commit()
    return cursor.rowcount > 0
```

**Step 6: Run all tests**

Run: `cd ~/genesis && ruff check . && pytest -v --tb=short 2>&1 | tail -15`
Expected: All pass (229 existing + 3 schema + ~13 new CRUD tests), ruff clean

**Step 7: Commit**

```bash
cd ~/genesis
git add src/genesis/db/crud/awareness_ticks.py src/genesis/db/crud/depth_thresholds.py \
        tests/test_db/test_awareness_ticks.py tests/test_db/test_depth_thresholds.py
git commit -m "feat(phase1): add awareness_ticks and depth_thresholds CRUD modules"
```

---

### Task 4: types.py — Depth enum and data classes

**Files:**
- Create: `src/genesis/awareness/__init__.py`
- Create: `src/genesis/awareness/types.py`
- Test: `tests/test_awareness/__init__.py`
- Test: `tests/test_awareness/test_types.py`

**Step 1: Create package directories**

Run: `mkdir -p ~/genesis/src/genesis/awareness ~/genesis/tests/test_awareness`

**Step 2: Write failing tests**

Create `tests/test_awareness/__init__.py` (empty).

Create `tests/test_awareness/test_types.py`:

```python
"""Tests for awareness loop data types."""

from genesis.awareness.types import Depth, SignalReading, DepthScore, TickResult


def test_depth_enum_values():
    """Depth names must match DB seed data exactly."""
    assert Depth.MICRO.value == "Micro"
    assert Depth.LIGHT.value == "Light"
    assert Depth.DEEP.value == "Deep"
    assert Depth.STRATEGIC.value == "Strategic"


def test_depth_enum_all_values():
    assert len(Depth) == 4


def test_signal_reading_creation():
    sr = SignalReading(
        name="software_error_spike",
        value=0.8,
        source="health_mcp",
        collected_at="2026-03-03T12:00:00+00:00",
    )
    assert sr.name == "software_error_spike"
    assert sr.value == 0.8


def test_signal_reading_immutable():
    sr = SignalReading(
        name="test", value=0.5, source="test", collected_at="2026-03-03T12:00:00+00:00"
    )
    try:
        sr.value = 0.9
        assert False, "Should be immutable"
    except AttributeError:
        pass


def test_depth_score_triggered():
    ds = DepthScore(
        depth=Depth.MICRO,
        raw_score=0.6,
        time_multiplier=1.0,
        final_score=0.6,
        threshold=0.5,
        triggered=True,
    )
    assert ds.triggered is True


def test_tick_result_no_trigger():
    tr = TickResult(
        tick_id="t-001",
        timestamp="2026-03-03T12:00:00+00:00",
        source="scheduled",
        signals=[],
        scores=[],
        classified_depth=None,
        trigger_reason=None,
    )
    assert tr.classified_depth is None
```

**Step 3: Run to verify failure**

Run: `cd ~/genesis && pytest tests/test_awareness/test_types.py -v --tb=short 2>&1 | tail -10`
Expected: FAILED (ModuleNotFoundError)

**Step 4: Implement types.py**

Create `src/genesis/awareness/__init__.py`:

```python
"""Genesis Awareness Loop — the system's heartbeat."""
```

Create `src/genesis/awareness/types.py`:

```python
"""Data types for the Awareness Loop."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Depth(str, Enum):
    """Reflection depth levels. Values match DB seed data in signal_weights.feeds_depths."""

    MICRO = "Micro"
    LIGHT = "Light"
    DEEP = "Deep"
    STRATEGIC = "Strategic"


@dataclass(frozen=True)
class SignalReading:
    """A single signal measurement."""

    name: str
    value: float  # 0.0–1.0 normalized
    source: str
    collected_at: str  # ISO datetime


@dataclass(frozen=True)
class DepthScore:
    """Urgency score for one depth level."""

    depth: Depth
    raw_score: float
    time_multiplier: float
    final_score: float  # raw_score × time_multiplier
    threshold: float
    triggered: bool  # final_score >= threshold


@dataclass(frozen=True)
class TickResult:
    """Complete result of one awareness tick."""

    tick_id: str
    timestamp: str  # ISO datetime
    source: str  # "scheduled" | "critical_bypass"
    signals: list[SignalReading]
    scores: list[DepthScore]
    classified_depth: Depth | None
    trigger_reason: str | None
```

**Step 5: Run tests**

Run: `cd ~/genesis && ruff check . && pytest tests/test_awareness/test_types.py -v --tb=short`
Expected: ALL PASSED

**Step 6: Commit**

```bash
cd ~/genesis
git add src/genesis/awareness/__init__.py src/genesis/awareness/types.py \
        tests/test_awareness/__init__.py tests/test_awareness/test_types.py
git commit -m "feat(phase1): add Depth enum and awareness data types"
```

---

### Task 5: signals.py — SignalCollector protocol + implementations

**Files:**
- Create: `src/genesis/awareness/signals.py`
- Test: `tests/test_awareness/test_signals.py`

**Step 1: Write failing tests**

Create `tests/test_awareness/test_signals.py`:

```python
"""Tests for signal collectors."""

from genesis.awareness.signals import (
    SignalCollector,
    ConversationCollector,
    TaskQualityCollector,
    OutreachEngagementCollector,
    ReconFindingsCollector,
    MemoryBacklogCollector,
    BudgetCollector,
    ErrorSpikeCollector,
    CriticalFailureCollector,
    StrategicTimerCollector,
    collect_all,
)
from genesis.awareness.types import SignalReading


async def test_stub_collector_returns_zero():
    """Phase 1 stub collectors return 0.0."""
    c = ConversationCollector()
    reading = await c.collect()
    assert isinstance(reading, SignalReading)
    assert reading.value == 0.0
    assert reading.name == "conversations_since_reflection"


async def test_all_collectors_have_correct_signal_names():
    """Each collector's signal_name matches the DB seed data."""
    expected = {
        "conversations_since_reflection",
        "task_completion_quality",
        "outreach_engagement_data",
        "recon_findings_pending",
        "unprocessed_memory_backlog",
        "budget_pct_consumed",
        "software_error_spike",
        "critical_failure",
        "time_since_last_strategic",
    }
    collectors = [
        ConversationCollector(),
        TaskQualityCollector(),
        OutreachEngagementCollector(),
        ReconFindingsCollector(),
        MemoryBacklogCollector(),
        BudgetCollector(),
        ErrorSpikeCollector(),
        CriticalFailureCollector(),
        StrategicTimerCollector(),
    ]
    names = {c.signal_name for c in collectors}
    assert names == expected


async def test_collect_all_returns_nine_readings():
    collectors = [
        ConversationCollector(),
        TaskQualityCollector(),
        OutreachEngagementCollector(),
        ReconFindingsCollector(),
        MemoryBacklogCollector(),
        BudgetCollector(),
        ErrorSpikeCollector(),
        CriticalFailureCollector(),
        StrategicTimerCollector(),
    ]
    readings = await collect_all(collectors)
    assert len(readings) == 9
    assert all(isinstance(r, SignalReading) for r in readings)


async def test_collect_all_tolerates_failure():
    """A failing collector should not break collect_all."""

    class FailingCollector:
        signal_name = "broken"
        async def collect(self) -> SignalReading:
            raise RuntimeError("boom")

    collectors = [ConversationCollector(), FailingCollector()]
    readings = await collect_all(collectors)
    # Should get 2 readings: one real, one fallback 0.0 for the failure
    assert len(readings) == 2
    values = {r.name: r.value for r in readings}
    assert values["broken"] == 0.0
    assert values["conversations_since_reflection"] == 0.0
```

**Step 2: Run to verify failure**

Run: `cd ~/genesis && pytest tests/test_awareness/test_signals.py -v --tb=short 2>&1 | tail -10`
Expected: FAILED (ImportError)

**Step 3: Implement signals.py**

Create `src/genesis/awareness/signals.py`:

```python
"""Signal collectors for the Awareness Loop.

Each collector reads one signal source and returns a normalized 0.0–1.0 value.
Phase 1: all return 0.0 (stubs). Later phases update implementations to query
real data from MCP servers and DB tables.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, UTC
from typing import Protocol, runtime_checkable

from genesis.awareness.types import SignalReading

logger = logging.getLogger(__name__)


@runtime_checkable
class SignalCollector(Protocol):
    """Protocol for signal collectors."""

    signal_name: str

    async def collect(self) -> SignalReading: ...


def _stub_reading(name: str, source: str) -> SignalReading:
    """Create a zero-value reading for Phase 1 stubs."""
    return SignalReading(
        name=name, value=0.0, source=source,
        collected_at=datetime.now(UTC).isoformat(),
    )


class ConversationCollector:
    signal_name = "conversations_since_reflection"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "agent_zero")


class TaskQualityCollector:
    signal_name = "task_completion_quality"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "agent_zero")


class OutreachEngagementCollector:
    signal_name = "outreach_engagement_data"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "outreach_mcp")


class ReconFindingsCollector:
    signal_name = "recon_findings_pending"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "recon_mcp")


class MemoryBacklogCollector:
    signal_name = "unprocessed_memory_backlog"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "memory_mcp")


class BudgetCollector:
    signal_name = "budget_pct_consumed"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "health_mcp")


class ErrorSpikeCollector:
    signal_name = "software_error_spike"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "health_mcp")


class CriticalFailureCollector:
    signal_name = "critical_failure"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "health_mcp")


class StrategicTimerCollector:
    signal_name = "time_since_last_strategic"

    async def collect(self) -> SignalReading:
        # GROUNDWORK(strategic-timer): In later phases, query awareness_ticks
        # table for last Strategic tick and normalize elapsed time to 0.0–1.0.
        return _stub_reading(self.signal_name, "clock")


async def collect_all(collectors: list) -> list[SignalReading]:
    """Run all collectors concurrently. Failures return 0.0, never propagate."""

    async def _safe_collect(c) -> SignalReading:
        try:
            return await c.collect()
        except Exception:
            logger.warning("Signal collector %s failed, returning 0.0", c.signal_name)
            return SignalReading(
                name=c.signal_name, value=0.0, source="error",
                collected_at=datetime.now(UTC).isoformat(),
            )

    return list(await asyncio.gather(*[_safe_collect(c) for c in collectors]))
```

**Step 4: Run tests**

Run: `cd ~/genesis && ruff check . && pytest tests/test_awareness/test_signals.py -v --tb=short`
Expected: ALL PASSED

**Step 5: Commit**

```bash
cd ~/genesis
git add src/genesis/awareness/signals.py tests/test_awareness/test_signals.py
git commit -m "feat(phase1): add SignalCollector protocol and 9 stub implementations"
```

---

### Task 6: scorer.py — Urgency scoring with time multipliers

**Files:**
- Create: `src/genesis/awareness/scorer.py`
- Test: `tests/test_awareness/test_scorer.py`

**Step 1: Write failing tests**

Create `tests/test_awareness/test_scorer.py`:

```python
"""Tests for urgency scorer."""

import json

from genesis.awareness.scorer import compute_time_multiplier, compute_scores
from genesis.awareness.types import Depth, SignalReading, DepthScore


# ─── Time multiplier curve tests ─────────────────────────────────────────────

def test_micro_multiplier_at_zero():
    """Micro: 0.3x at 0 minutes elapsed."""
    assert compute_time_multiplier(Depth.MICRO, elapsed_seconds=0) == 0.3


def test_micro_multiplier_at_floor():
    """Micro: 1.0x at 30 minutes (1800s)."""
    assert compute_time_multiplier(Depth.MICRO, elapsed_seconds=1800) == 1.0


def test_micro_multiplier_at_overdue():
    """Micro: 2.5x at 60 minutes (3600s)."""
    assert compute_time_multiplier(Depth.MICRO, elapsed_seconds=3600) == 2.5


def test_micro_multiplier_interpolated():
    """Micro: halfway between 0min (0.3) and 30min (1.0) should be ~0.65."""
    result = compute_time_multiplier(Depth.MICRO, elapsed_seconds=900)
    assert abs(result - 0.65) < 0.01


def test_light_multiplier_at_zero():
    assert compute_time_multiplier(Depth.LIGHT, elapsed_seconds=0) == 0.5


def test_light_multiplier_at_3h():
    """Light: 1.0x at 3 hours."""
    assert compute_time_multiplier(Depth.LIGHT, elapsed_seconds=10800) == 1.0


def test_light_multiplier_at_6h():
    """Light: 1.5x at 6 hours (floor)."""
    assert compute_time_multiplier(Depth.LIGHT, elapsed_seconds=21600) == 1.5


def test_light_multiplier_at_12h():
    """Light: 3.0x at 12 hours (alarm)."""
    assert compute_time_multiplier(Depth.LIGHT, elapsed_seconds=43200) == 3.0


def test_deep_multiplier_at_zero():
    assert compute_time_multiplier(Depth.DEEP, elapsed_seconds=0) == 0.3


def test_deep_multiplier_at_48h():
    """Deep: 1.0x at 48 hours (floor)."""
    assert compute_time_multiplier(Depth.DEEP, elapsed_seconds=172800) == 1.0


def test_deep_multiplier_at_72h():
    """Deep: 1.5x at 72 hours."""
    assert compute_time_multiplier(Depth.DEEP, elapsed_seconds=259200) == 1.5


def test_deep_multiplier_at_96h():
    """Deep: 2.5x at 96 hours (overdue)."""
    assert compute_time_multiplier(Depth.DEEP, elapsed_seconds=345600) == 2.5


def test_strategic_multiplier_at_zero():
    assert compute_time_multiplier(Depth.STRATEGIC, elapsed_seconds=0) == 0.2


def test_strategic_multiplier_at_7d():
    """Strategic: 1.0x at 7 days (floor)."""
    assert compute_time_multiplier(Depth.STRATEGIC, elapsed_seconds=604800) == 1.0


def test_strategic_multiplier_at_14d():
    """Strategic: 2.0x at 14 days."""
    assert compute_time_multiplier(Depth.STRATEGIC, elapsed_seconds=1209600) == 2.0


def test_strategic_multiplier_at_21d():
    """Strategic: 3.0x at 21 days."""
    assert compute_time_multiplier(Depth.STRATEGIC, elapsed_seconds=1814400) == 3.0


def test_multiplier_caps_at_max():
    """Beyond the last defined point, multiplier should not exceed the max."""
    result = compute_time_multiplier(Depth.MICRO, elapsed_seconds=99999)
    assert result == 2.5


# ─── Score computation tests ─────────────────────────────────────────────────

async def test_compute_scores_basic(db):
    """Known signals and weights produce expected scores."""
    signals = [
        SignalReading(name="software_error_spike", value=1.0,
                      source="health_mcp", collected_at="2026-03-03T12:00:00+00:00"),
    ]
    # software_error_spike feeds ["Micro", "Light"] with weight 0.70
    # With no prior ticks, elapsed time is large → high multiplier
    scores = await compute_scores(db, signals, now="2026-03-03T12:00:00+00:00")
    micro_score = next(s for s in scores if s.depth == Depth.MICRO)
    # raw = 1.0 * 0.70 = 0.70, multiplier at max elapsed = 2.5
    assert micro_score.raw_score == 0.70


async def test_compute_scores_returns_all_depths(db):
    """Should return a score for each depth."""
    signals = []
    scores = await compute_scores(db, signals, now="2026-03-03T12:00:00+00:00")
    depths = {s.depth for s in scores}
    assert depths == {Depth.MICRO, Depth.LIGHT, Depth.DEEP, Depth.STRATEGIC}


async def test_compute_scores_zero_signals(db):
    """With no signal readings, raw scores should be 0."""
    scores = await compute_scores(db, [], now="2026-03-03T12:00:00+00:00")
    for s in scores:
        assert s.raw_score == 0.0
```

**Step 2: Run to verify failure**

Run: `cd ~/genesis && pytest tests/test_awareness/test_scorer.py -v --tb=short 2>&1 | tail -10`
Expected: FAILED (ImportError)

**Step 3: Implement scorer.py**

Create `src/genesis/awareness/scorer.py`:

```python
"""Urgency scoring for the Awareness Loop.

Implements: urgency_score(depth) = Σ(signal_value × weight) × time_multiplier(depth)
"""

from __future__ import annotations

import json
from datetime import datetime, UTC

import aiosqlite

from genesis.awareness.types import Depth, DepthScore, SignalReading
from genesis.db.crud import awareness_ticks, depth_thresholds, signal_weights

# ─── Time multiplier curves ──────────────────────────────────────────────────
# Each curve is a list of (elapsed_seconds, multiplier) breakpoints.
# Between breakpoints: linear interpolation. Beyond last: clamp to last value.

_TIME_CURVES: dict[Depth, list[tuple[int, float]]] = {
    Depth.MICRO: [
        (0, 0.3),       # just happened
        (1800, 1.0),    # 30min — floor
        (3600, 2.5),    # 60min — overdue
    ],
    Depth.LIGHT: [
        (0, 0.5),
        (10800, 1.0),   # 3h
        (21600, 1.5),   # 6h — floor
        (43200, 3.0),   # 12h — alarm
    ],
    Depth.DEEP: [
        (0, 0.3),
        (172800, 1.0),  # 48h — floor
        (259200, 1.5),  # 72h
        (345600, 2.5),  # 96h — overdue
    ],
    Depth.STRATEGIC: [
        (0, 0.2),
        (604800, 1.0),  # 7d — floor
        (1209600, 2.0), # 14d
        (1814400, 3.0), # 21d — overdue
    ],
}


def compute_time_multiplier(depth: Depth, *, elapsed_seconds: int) -> float:
    """Piecewise linear interpolation on the time-multiplier curve."""
    curve = _TIME_CURVES[depth]
    if elapsed_seconds <= curve[0][0]:
        return curve[0][1]
    if elapsed_seconds >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t0, m0 = curve[i]
        t1, m1 = curve[i + 1]
        if t0 <= elapsed_seconds <= t1:
            ratio = (elapsed_seconds - t0) / (t1 - t0)
            return round(m0 + ratio * (m1 - m0), 4)
    return curve[-1][1]  # fallback (unreachable)


async def compute_scores(
    db: aiosqlite.Connection,
    signals: list[SignalReading],
    *,
    now: str | None = None,
) -> list[DepthScore]:
    """Compute urgency scores for all four depths."""
    if now is None:
        now = datetime.now(UTC).isoformat()

    signal_map = {s.name: s.value for s in signals}
    thresholds = {r["depth_name"]: r for r in await depth_thresholds.list_all(db)}
    results = []

    for depth in Depth:
        # Get signals + weights that feed this depth
        weights = await signal_weights.list_by_depth(db, depth.value)
        raw_score = 0.0
        for w in weights:
            val = signal_map.get(w["signal_name"], 0.0)
            raw_score += val * w["current_weight"]

        # Elapsed time since last tick at this depth
        last_tick = await awareness_ticks.last_at_depth(db, depth.value)
        if last_tick is not None:
            last_dt = datetime.fromisoformat(last_tick["created_at"])
            now_dt = datetime.fromisoformat(now)
            elapsed = int((now_dt - last_dt).total_seconds())
        else:
            # No prior tick — treat as maximally overdue
            elapsed = _TIME_CURVES[depth][-1][0]

        multiplier = compute_time_multiplier(depth, elapsed_seconds=elapsed)
        final = round(raw_score * multiplier, 4)

        threshold_val = thresholds[depth.value]["threshold"]
        results.append(DepthScore(
            depth=depth,
            raw_score=round(raw_score, 4),
            time_multiplier=multiplier,
            final_score=final,
            threshold=threshold_val,
            triggered=final >= threshold_val,
        ))

    return results
```

**Step 4: Run tests**

Run: `cd ~/genesis && ruff check . && pytest tests/test_awareness/test_scorer.py -v --tb=short`
Expected: ALL PASSED

**Step 5: Commit**

```bash
cd ~/genesis
git add src/genesis/awareness/scorer.py tests/test_awareness/test_scorer.py
git commit -m "feat(phase1): add urgency scorer with time multiplier curves"
```

---

### Task 7: classifier.py — Depth classification with floor/ceiling

**Files:**
- Create: `src/genesis/awareness/classifier.py`
- Test: `tests/test_awareness/test_classifier.py`

**Step 1: Write failing tests**

Create `tests/test_awareness/test_classifier.py`:

```python
"""Tests for depth classifier."""

import json
from datetime import datetime, UTC, timedelta

from genesis.awareness.classifier import classify_depth
from genesis.awareness.types import Depth, DepthScore
from genesis.db.crud import awareness_ticks


def _score(depth, final, threshold, triggered):
    return DepthScore(
        depth=depth, raw_score=final, time_multiplier=1.0,
        final_score=final, threshold=threshold, triggered=triggered,
    )


async def test_highest_depth_wins(db):
    """When multiple depths trigger, return the highest (Deep > Light > Micro)."""
    scores = [
        _score(Depth.MICRO, 0.6, 0.5, True),
        _score(Depth.LIGHT, 0.9, 0.8, True),
        _score(Depth.DEEP, 0.6, 0.55, True),
        _score(Depth.STRATEGIC, 0.3, 0.4, False),
    ]
    result = await classify_depth(db, scores)
    assert result.depth == Depth.DEEP


async def test_nothing_triggered(db):
    """When no depth triggers, return None."""
    scores = [
        _score(Depth.MICRO, 0.3, 0.5, False),
        _score(Depth.LIGHT, 0.4, 0.8, False),
        _score(Depth.DEEP, 0.2, 0.55, False),
        _score(Depth.STRATEGIC, 0.1, 0.4, False),
    ]
    result = await classify_depth(db, scores)
    assert result is None


async def test_ceiling_blocks_trigger(db):
    """If a depth is at ceiling, skip to next lower depth."""
    # Insert 2 Micro ticks in the last hour (ceiling = 2/hr)
    now = datetime.now(UTC)
    for i in range(2):
        await awareness_ticks.create(
            db,
            id=f"ceiling-test-{i}",
            source="scheduled",
            signals_json="[]",
            scores_json="[]",
            classified_depth="Micro",
            created_at=(now - timedelta(minutes=i * 5)).isoformat(),
        )

    scores = [
        _score(Depth.MICRO, 0.6, 0.5, True),    # triggered but at ceiling
        _score(Depth.LIGHT, 0.4, 0.8, False),    # not triggered
        _score(Depth.DEEP, 0.2, 0.55, False),
        _score(Depth.STRATEGIC, 0.1, 0.4, False),
    ]
    result = await classify_depth(db, scores)
    assert result is None  # Micro blocked by ceiling, nothing else triggered


async def test_bypass_ceiling_on_critical(db):
    """force_tick bypass_ceiling=True ignores ceiling limits."""
    now = datetime.now(UTC)
    for i in range(2):
        await awareness_ticks.create(
            db,
            id=f"bypass-test-{i}",
            source="scheduled",
            signals_json="[]",
            scores_json="[]",
            classified_depth="Micro",
            created_at=(now - timedelta(minutes=i * 5)).isoformat(),
        )

    scores = [
        _score(Depth.MICRO, 0.6, 0.5, True),
        _score(Depth.LIGHT, 0.4, 0.8, False),
        _score(Depth.DEEP, 0.2, 0.55, False),
        _score(Depth.STRATEGIC, 0.1, 0.4, False),
    ]
    result = await classify_depth(db, scores, bypass_ceiling=True)
    assert result is not None
    assert result.depth == Depth.MICRO
```

**Step 2: Run to verify failure**

Run: `cd ~/genesis && pytest tests/test_awareness/test_classifier.py -v --tb=short 2>&1 | tail -10`
Expected: FAILED (ImportError)

**Step 3: Implement classifier.py**

Create `src/genesis/awareness/classifier.py`:

```python
"""Depth classification for the Awareness Loop.

Selects the highest triggered depth that isn't blocked by ceiling constraints.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from genesis.awareness.types import Depth, DepthScore
from genesis.db.crud import awareness_ticks, depth_thresholds

# Priority order: highest to lowest
_DEPTH_PRIORITY = [Depth.STRATEGIC, Depth.DEEP, Depth.LIGHT, Depth.MICRO]


@dataclass(frozen=True)
class DepthDecision:
    """Result of depth classification."""

    depth: Depth
    score: DepthScore
    reason: str


async def classify_depth(
    db: aiosqlite.Connection,
    scores: list[DepthScore],
    *,
    bypass_ceiling: bool = False,
) -> DepthDecision | None:
    """Select the highest triggered depth not blocked by ceiling constraints.

    Returns None if nothing triggered or all triggered depths are at ceiling.
    """
    score_map = {s.depth: s for s in scores}
    thresholds = {r["depth_name"]: r for r in await depth_thresholds.list_all(db)}

    for depth in _DEPTH_PRIORITY:
        ds = score_map.get(depth)
        if ds is None or not ds.triggered:
            continue

        # Check ceiling unless bypassed (critical event)
        if not bypass_ceiling:
            cfg = thresholds[depth.value]
            recent = await awareness_ticks.count_in_window(
                db,
                depth=depth.value,
                window_seconds=cfg["ceiling_window_seconds"],
            )
            if recent >= cfg["ceiling_count"]:
                continue  # At ceiling — try next lower depth

        reason = f"{depth.value} triggered: score {ds.final_score:.3f} >= {ds.threshold:.3f}"
        if bypass_ceiling:
            reason = f"CRITICAL BYPASS — {reason}"
        return DepthDecision(depth=depth, score=ds, reason=reason)

    return None
```

**Step 4: Run tests**

Run: `cd ~/genesis && ruff check . && pytest tests/test_awareness/test_classifier.py -v --tb=short`
Expected: ALL PASSED

**Step 5: Commit**

```bash
cd ~/genesis
git add src/genesis/awareness/classifier.py tests/test_awareness/test_classifier.py
git commit -m "feat(phase1): add depth classifier with ceiling enforcement"
```

---

### Task 8: loop.py — AwarenessLoop orchestrator

**Files:**
- Create: `src/genesis/awareness/loop.py`
- Test: `tests/test_awareness/test_loop.py`

**Step 1: Write failing tests**

Create `tests/test_awareness/test_loop.py`:

```python
"""Tests for the AwarenessLoop orchestrator.

Tests the tick pipeline directly (perform_tick) without relying on
APScheduler timing. Scheduler integration is tested separately.
"""

import json

from genesis.awareness.loop import perform_tick
from genesis.awareness.signals import ConversationCollector, ErrorSpikeCollector
from genesis.awareness.types import Depth, SignalReading
from genesis.db.crud import awareness_ticks, observations


async def test_perform_tick_no_trigger(db):
    """Tick with zero signals writes to awareness_ticks but not observations."""
    collectors = [ConversationCollector()]
    result = await perform_tick(db, collectors, source="scheduled")

    assert result.classified_depth is None

    # Should be stored in awareness_ticks
    ticks = await awareness_ticks.query(db)
    assert len(ticks) == 1
    assert ticks[0]["source"] == "scheduled"
    assert ticks[0]["classified_depth"] is None

    # Should NOT create an observation
    obs = await observations.query(db, type="awareness_tick")
    assert len(obs) == 0


async def test_perform_tick_with_trigger(db):
    """Tick that triggers a depth writes to both awareness_ticks and observations."""

    class HotSignal:
        signal_name = "software_error_spike"
        async def collect(self):
            return SignalReading(
                name="software_error_spike", value=1.0,
                source="health_mcp", collected_at="2026-03-03T12:00:00+00:00",
            )

    result = await perform_tick(db, [HotSignal()], source="scheduled")

    # software_error_spike (weight 0.70) at value 1.0, max elapsed → high multiplier
    # Should trigger at least Micro (threshold 0.50)
    assert result.classified_depth is not None

    # Check observation was created
    obs = await observations.query(db, type="awareness_tick")
    assert len(obs) == 1
    assert obs[0]["source"] == "awareness_loop"


async def test_perform_tick_critical_bypass(db):
    """Critical bypass ticks are flagged correctly."""
    result = await perform_tick(
        db, [ConversationCollector()],
        source="critical_bypass", reason="test emergency",
    )
    ticks = await awareness_ticks.query(db, source="critical_bypass")
    assert len(ticks) == 1


async def test_perform_tick_idempotent_ids(db):
    """Each tick gets a unique ID."""
    collectors = [ConversationCollector()]
    r1 = await perform_tick(db, collectors, source="scheduled")
    r2 = await perform_tick(db, collectors, source="scheduled")
    assert r1.tick_id != r2.tick_id
```

**Step 2: Run to verify failure**

Run: `cd ~/genesis && pytest tests/test_awareness/test_loop.py -v --tb=short 2>&1 | tail -10`
Expected: FAILED (ImportError)

**Step 3: Implement loop.py**

Create `src/genesis/awareness/loop.py`:

```python
"""AwarenessLoop — the system's heartbeat.

Orchestrates the tick pipeline: collect signals → score → classify → store.
APScheduler drives the 5-minute interval. perform_tick() is the testable core.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, UTC

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from genesis.awareness.classifier import classify_depth
from genesis.awareness.scorer import compute_scores
from genesis.awareness.signals import SignalCollector, collect_all
from genesis.awareness.types import DepthScore, TickResult
from genesis.db.crud import awareness_ticks, observations

logger = logging.getLogger(__name__)


async def perform_tick(
    db: aiosqlite.Connection,
    collectors: list,
    *,
    source: str = "scheduled",
    reason: str | None = None,
) -> TickResult:
    """Execute one awareness tick. Testable without the scheduler."""
    now = datetime.now(UTC).isoformat()
    tick_id = str(uuid.uuid4())

    # 1. Collect signals
    signals = await collect_all(collectors)

    # 2. Score urgency per depth
    scores = await compute_scores(db, signals, now=now)

    # 3. Classify depth
    bypass = source == "critical_bypass"
    decision = await classify_depth(db, scores, bypass_ceiling=bypass)

    classified_depth = decision.depth if decision else None
    trigger_reason = decision.reason if decision else reason

    result = TickResult(
        tick_id=tick_id,
        timestamp=now,
        source=source,
        signals=signals,
        scores=scores,
        classified_depth=classified_depth,
        trigger_reason=trigger_reason,
    )

    # 4. Store tick result
    await awareness_ticks.create(
        db,
        id=tick_id,
        source=source,
        signals_json=json.dumps([
            {"name": s.name, "value": s.value, "source": s.source,
             "collected_at": s.collected_at}
            for s in signals
        ]),
        scores_json=json.dumps([
            {"depth": s.depth.value, "raw_score": s.raw_score,
             "time_multiplier": s.time_multiplier, "final_score": s.final_score,
             "threshold": s.threshold, "triggered": s.triggered}
            for s in scores
        ]),
        classified_depth=classified_depth.value if classified_depth else None,
        trigger_reason=trigger_reason,
        created_at=now,
    )

    # 5. If triggered, also create an observation
    if decision is not None:
        obs_id = str(uuid.uuid4())
        await observations.create(
            db,
            id=obs_id,
            source="awareness_loop",
            type="awareness_tick",
            content=json.dumps({
                "tick_id": tick_id,
                "depth": classified_depth.value,
                "reason": trigger_reason,
                "scores": {s.depth.value: s.final_score for s in scores},
            }),
            priority="high" if classified_depth in (
                classified_depth.DEEP, classified_depth.STRATEGIC
            ) else "medium",
            created_at=now,
        )

    return result


class AwarenessLoop:
    """The metronome — drives the 5-minute awareness tick via APScheduler."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        collectors: list[SignalCollector],
        *,
        interval_minutes: int = 5,
    ):
        self._db = db
        self._collectors = list(collectors)
        self._interval = interval_minutes
        self._scheduler = AsyncIOScheduler()
        self._tick_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the scheduler with the tick job."""
        self._scheduler.add_job(
            self._on_tick,
            IntervalTrigger(minutes=self._interval),
            id="awareness_tick",
            max_instances=1,
            misfire_grace_time=60,
        )
        self._scheduler.start()
        logger.info("Awareness Loop started (interval=%dm)", self._interval)

    async def stop(self) -> None:
        """Stop the scheduler, waiting for any running tick to finish."""
        self._scheduler.shutdown(wait=True)
        logger.info("Awareness Loop stopped")

    async def force_tick(self, reason: str) -> TickResult:
        """Critical event bypass — immediate out-of-cycle tick."""
        async with self._tick_lock:
            logger.info("Force tick triggered: %s", reason)
            return await perform_tick(
                self._db, self._collectors,
                source="critical_bypass", reason=reason,
            )

    async def _on_tick(self) -> None:
        """Scheduled tick callback."""
        async with self._tick_lock:
            try:
                result = await perform_tick(
                    self._db, self._collectors, source="scheduled",
                )
                if result.classified_depth:
                    logger.info(
                        "Tick triggered %s: %s",
                        result.classified_depth.value, result.trigger_reason,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Awareness tick failed")

    # GROUNDWORK(category-2-rhythms): add_rhythm(name, interval, callback)
    # GROUNDWORK(category-3-crons): add_cron(name, cron_expr, callback)
```

**Step 4: Run tests**

Run: `cd ~/genesis && ruff check . && pytest tests/test_awareness/test_loop.py -v --tb=short`
Expected: ALL PASSED

Note: there is a subtle bug in `perform_tick` in the priority assignment — `classified_depth.DEEP` references the enum via the value instead of `Depth.DEEP`. Fix during implementation:

```python
            priority="high" if classified_depth in (Depth.DEEP, Depth.STRATEGIC) else "medium",
```

(Need to import `Depth` — it's already imported via types.)

**Step 5: Commit**

```bash
cd ~/genesis
git add src/genesis/awareness/loop.py tests/test_awareness/test_loop.py
git commit -m "feat(phase1): add AwarenessLoop orchestrator with perform_tick pipeline"
```

---

### Task 9: Integration test — end-to-end tick pipeline

**Files:**
- Create: `tests/test_awareness/test_integration.py`

**Step 1: Write the integration test**

Create `tests/test_awareness/test_integration.py`:

```python
"""End-to-end integration test for the Awareness Loop.

Verifies the full pipeline: signals → score → classify → store,
with real DB seed data and realistic signal values.
"""

import json
from datetime import datetime, UTC

from genesis.awareness.loop import perform_tick
from genesis.awareness.signals import (
    ConversationCollector,
    TaskQualityCollector,
    OutreachEngagementCollector,
    ReconFindingsCollector,
    MemoryBacklogCollector,
    BudgetCollector,
    ErrorSpikeCollector,
    CriticalFailureCollector,
    StrategicTimerCollector,
)
from genesis.awareness.types import Depth, SignalReading
from genesis.db.crud import awareness_ticks, observations


async def test_full_pipeline_quiet(db):
    """All stub collectors (0.0 values) → nothing triggers."""
    collectors = [
        ConversationCollector(), TaskQualityCollector(),
        OutreachEngagementCollector(), ReconFindingsCollector(),
        MemoryBacklogCollector(), BudgetCollector(),
        ErrorSpikeCollector(), CriticalFailureCollector(),
        StrategicTimerCollector(),
    ]
    result = await perform_tick(db, collectors, source="scheduled")

    # All signals are 0.0, but time multipliers are maxed (no prior ticks)
    # 0.0 * any_weight = 0.0, 0.0 * any_multiplier = 0.0 → nothing triggers
    assert result.classified_depth is None
    assert len(result.signals) == 9
    assert len(result.scores) == 4

    # Tick stored
    ticks = await awareness_ticks.query(db)
    assert len(ticks) == 1

    # No observation
    obs = await observations.query(db, type="awareness_tick")
    assert len(obs) == 0


async def test_full_pipeline_error_spike(db):
    """A saturated error spike signal should trigger Micro or Light."""

    class HotError:
        signal_name = "software_error_spike"
        async def collect(self):
            return SignalReading(
                name="software_error_spike", value=1.0,
                source="health_mcp", collected_at=datetime.now(UTC).isoformat(),
            )

    collectors = [HotError(), ConversationCollector()]
    result = await perform_tick(db, collectors, source="scheduled")

    # error_spike feeds Micro (weight 0.70) and Light (weight 0.70)
    # value=1.0, weight=0.70, max elapsed multiplier → well above thresholds
    assert result.classified_depth is not None
    assert result.classified_depth in (Depth.MICRO, Depth.LIGHT)

    # Observation created for the trigger
    obs = await observations.query(db, type="awareness_tick")
    assert len(obs) == 1
    content = json.loads(obs[0]["content"])
    assert "tick_id" in content


async def test_full_pipeline_critical_bypass(db):
    """force_tick path works end-to-end."""

    class CritSignal:
        signal_name = "critical_failure"
        async def collect(self):
            return SignalReading(
                name="critical_failure", value=1.0,
                source="health_mcp", collected_at=datetime.now(UTC).isoformat(),
            )

    result = await perform_tick(
        db, [CritSignal()],
        source="critical_bypass", reason="cascading failure",
    )

    ticks = await awareness_ticks.query(db, source="critical_bypass")
    assert len(ticks) == 1


async def test_sequential_ticks_track_history(db):
    """Multiple ticks create a history that affects time multipliers."""
    collectors = [ConversationCollector()]

    r1 = await perform_tick(db, collectors, source="scheduled")
    r2 = await perform_tick(db, collectors, source="scheduled")

    ticks = await awareness_ticks.query(db)
    assert len(ticks) == 2
    assert ticks[0]["id"] != ticks[1]["id"]
```

**Step 2: Run the integration test**

Run: `cd ~/genesis && ruff check . && pytest tests/test_awareness/test_integration.py -v --tb=short`
Expected: ALL PASSED

**Step 3: Run the full test suite**

Run: `cd ~/genesis && ruff check . && pytest -v --tb=short 2>&1 | tail -10`
Expected: All tests pass (229 existing + ~30 new awareness tests), ruff clean

**Step 4: Commit**

```bash
cd ~/genesis
git add tests/test_awareness/test_integration.py
git commit -m "test(phase1): add awareness loop integration tests"
```

---

### Task 10: Update documentation

**Files:**
- Modify: `src/genesis/db/schema.py` — verify all changes from Task 2 are correct
- Modify: `.claude/docs/build-phase-current.md` — update to Phase 1 in progress
- Modify: `docs/architecture/genesis-v3-build-phases.md` — check Phase 0 verification boxes
- No new docs files (design doc already committed in Task 0)

**Step 1: Update build-phase-current.md**

Replace contents of `.claude/docs/build-phase-current.md` with:

```markdown
# Current Build Phase: Phase 1 — The Metronome (Awareness Loop)

**Status:** IN PROGRESS
**Risk:** LOW
**Dependencies:** Phase 0 (COMPLETE)

## What Phase 1 Delivers

- APScheduler-driven 5-minute tick scheduler
- 9 signal collectors (protocol + stub implementations)
- Urgency scorer with piecewise linear time multiplier curves
- Depth classifier with threshold, floor, and ceiling enforcement
- Critical event bypass (force_tick)
- awareness_ticks table (tick measurement history)
- depth_thresholds table (configurable per-depth thresholds)
- Observation written on trigger for downstream phases

## What Comes Next

Phase 2 (Compute Routing) and Phase 3 (Surplus Infrastructure) can start
in parallel — both depend only on Phase 0.

Phase 4 (Perception) depends on Phase 1 + Phase 2.
```

**Step 2: Run full test suite one final time**

Run: `cd ~/genesis && ruff check . && pytest -v --tb=short 2>&1 | tail -10`
Expected: ALL PASSED, ruff clean

**Step 3: Commit**

```bash
cd ~/genesis
git add .claude/docs/build-phase-current.md
git commit -m "docs: update build phase tracker to Phase 1 in progress"
```

---

### Task 11: Final verification and push

**Step 1: Run full verification**

Run: `cd ~/genesis && ruff check . && pytest -v 2>&1 | tail -20`
Expected: All tests pass, ruff clean

**Step 2: Review git log**

Run: `cd ~/genesis && git log --oneline -10`
Expected: ~8 new commits on main since the design doc commit

**Step 3: Push to GitHub**

Run: `cd ~/genesis && git push origin main`
Expected: Success

---

## Summary

| Task | What | Tests Added |
|------|------|-------------|
| 1 | APScheduler dependency | 0 (verify existing) |
| 2 | Schema: awareness_ticks + depth_thresholds | 3 |
| 3 | CRUD: awareness_ticks + depth_thresholds | ~13 |
| 4 | types.py: Depth enum + dataclasses | 6 |
| 5 | signals.py: SignalCollector protocol + 9 stubs | 4 |
| 6 | scorer.py: urgency scoring + time multipliers | ~20 |
| 7 | classifier.py: depth classification + ceiling | 4 |
| 8 | loop.py: AwarenessLoop + perform_tick | 4 |
| 9 | Integration tests | 4 |
| 10 | Documentation updates | 0 |
| 11 | Final verification + push | 0 |

**Total new tests:** ~58
**Existing tests preserved:** 229+
**New files:** 10 (5 src + 5 test)
**Modified files:** 3 (schema.py, pyproject.toml, build-phase-current.md)
