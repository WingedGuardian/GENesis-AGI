# Phase 2: Compute Routing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build compute routing infrastructure — fallback chains, circuit breakers, retry with backoff, cost tracking, budget enforcement, dead-letter staging, and graceful degradation. All tested with mock delegates, no real LLM calls.

**Architecture:** Genesis-side `routing` package. A `CallDelegate` protocol abstracts the actual LLM call — mocked in tests, wired to AZ's `unified_call()` in Phase 4. Wraps existing Phase 0 CRUD for cost_events and budgets.

**Tech Stack:** Python 3.12, aiosqlite, pyyaml, pytest

**Design doc:** `docs/plans/2026-03-04-phase2-compute-routing-design.md`

---

### Task 1: Add pyyaml dependency + package skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `src/genesis/routing/__init__.py`
- Create: `tests/test_routing/__init__.py`

**Step 1: Add pyyaml to dependencies**

In `pyproject.toml`, add `"pyyaml"` to the dependencies list:

```toml
dependencies = [
    "aiosqlite",
    "aiohttp",
    "apscheduler>=3.11,<4",
    "python-telegram-bot>=21.0",
    "qdrant-client",
    "pyyaml",
]
```

**Step 2: Install**

Run: `cd ~/genesis && pip install -e .`
Expected: pyyaml installs successfully.

**Step 3: Create package skeleton**

`src/genesis/routing/__init__.py`:
```python
"""Compute routing — fallback chains, circuit breakers, cost tracking."""
```

`tests/test_routing/__init__.py`:
```python
```

**Step 4: Verify**

Run: `cd ~/genesis && ruff check . && pytest -v`
Expected: All existing tests still pass.

**Step 5: Commit**

```bash
git add pyproject.toml src/genesis/routing/__init__.py tests/test_routing/__init__.py
git commit -m "feat(routing): add pyyaml dependency and routing package skeleton"
```

---

### Task 2: Types module

**Files:**
- Create: `src/genesis/routing/types.py`
- Create: `tests/test_routing/test_types.py`

**Step 1: Write the failing test**

`tests/test_routing/test_types.py`:
```python
"""Tests for routing type definitions."""

import pytest

from genesis.routing.types import (
    BudgetStatus,
    CallResult,
    CallSiteConfig,
    DegradationLevel,
    ErrorCategory,
    ProviderConfig,
    ProviderState,
    RetryPolicy,
    RoutingConfig,
    RoutingResult,
)


class TestEnums:
    def test_provider_state_values(self):
        assert ProviderState.CLOSED == "closed"
        assert ProviderState.OPEN == "open"
        assert ProviderState.HALF_OPEN == "half_open"

    def test_error_category_values(self):
        assert ErrorCategory.TRANSIENT == "transient"
        assert ErrorCategory.DEGRADED == "degraded"
        assert ErrorCategory.PERMANENT == "permanent"

    def test_degradation_level_values(self):
        assert DegradationLevel.NORMAL == "L0"
        assert DegradationLevel.FALLBACK == "L1"
        assert DegradationLevel.REDUCED == "L2"
        assert DegradationLevel.ESSENTIAL == "L3"
        assert DegradationLevel.MEMORY_IMPAIRED == "L4"
        assert DegradationLevel.LOCAL_COMPUTE_DOWN == "L5"

    def test_budget_status_values(self):
        assert BudgetStatus.UNDER_LIMIT == "under_limit"
        assert BudgetStatus.WARNING == "warning"
        assert BudgetStatus.EXCEEDED == "exceeded"


class TestDataclasses:
    def test_provider_config_frozen(self):
        p = ProviderConfig(
            name="test", provider_type="anthropic", model_id="claude",
            is_free=False, rpm_limit=None, open_duration_s=120,
        )
        with pytest.raises(AttributeError):
            p.name = "changed"

    def test_call_site_config_defaults(self):
        cs = CallSiteConfig(id="test", chain=["a", "b"])
        assert cs.default_paid is False
        assert cs.never_pays is False
        assert cs.retry_profile == "default"

    def test_retry_policy_defaults(self):
        rp = RetryPolicy()
        assert rp.max_retries == 3
        assert rp.base_delay_ms == 500
        assert rp.max_delay_ms == 30000
        assert rp.backoff_multiplier == 2.0
        assert rp.jitter_pct == 0.25

    def test_call_result_defaults(self):
        r = CallResult(success=True)
        assert r.content is None
        assert r.input_tokens == 0
        assert r.cost_usd == 0.0

    def test_routing_result_defaults(self):
        r = RoutingResult(success=False, call_site_id="test")
        assert r.provider_used is None
        assert r.attempts == 0
        assert r.fallback_used is False
        assert r.dead_lettered is False

    def test_routing_config(self):
        cfg = RoutingConfig(providers={}, call_sites={}, retry_profiles={})
        assert cfg.providers == {}
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_routing/test_types.py -v`
Expected: FAIL — ImportError

**Step 3: Write implementation**

`src/genesis/routing/types.py`:
```python
"""Routing data types — enums, frozen dataclasses, protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class ProviderState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ErrorCategory(StrEnum):
    TRANSIENT = "transient"
    DEGRADED = "degraded"
    PERMANENT = "permanent"


class DegradationLevel(StrEnum):
    NORMAL = "L0"
    FALLBACK = "L1"
    REDUCED = "L2"
    ESSENTIAL = "L3"
    MEMORY_IMPAIRED = "L4"
    LOCAL_COMPUTE_DOWN = "L5"


class BudgetStatus(StrEnum):
    UNDER_LIMIT = "under_limit"
    WARNING = "warning"
    EXCEEDED = "exceeded"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    provider_type: str
    model_id: str
    is_free: bool
    rpm_limit: int | None
    open_duration_s: int


@dataclass(frozen=True)
class CallSiteConfig:
    id: str
    chain: list[str]
    default_paid: bool = False
    never_pays: bool = False
    retry_profile: str = "default"


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay_ms: int = 500
    max_delay_ms: int = 30000
    backoff_multiplier: float = 2.0
    jitter_pct: float = 0.25


@dataclass(frozen=True)
class CallResult:
    success: bool
    content: str | None = None
    error: str | None = None
    status_code: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    retry_after_s: float | None = None


@dataclass(frozen=True)
class RoutingResult:
    success: bool
    call_site_id: str
    provider_used: str | None = None
    model_id: str | None = None
    content: str | None = None
    attempts: int = 0
    fallback_used: bool = False
    error: str | None = None
    dead_lettered: bool = False


@dataclass(frozen=True)
class RoutingConfig:
    providers: dict[str, ProviderConfig]
    call_sites: dict[str, CallSiteConfig]
    retry_profiles: dict[str, RetryPolicy]


class CallDelegate(Protocol):
    """Pluggable call backend. Mock in tests, AZ unified_call() in production."""

    async def call(
        self, provider: str, model_id: str, messages: list[dict], **kwargs
    ) -> CallResult: ...
```

**Step 4: Run tests**

Run: `pytest tests/test_routing/test_types.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/genesis/routing/types.py tests/test_routing/test_types.py
git commit -m "feat(routing): add types module — enums, dataclasses, CallDelegate protocol"
```

---

### Task 3: Dead-letter table DDL + budget seed data

**Files:**
- Modify: `src/genesis/db/schema.py`
- Modify: `tests/test_db/test_schema.py`

**Step 1: Write the failing test**

Add to `tests/test_db/test_schema.py`:
```python
async def test_dead_letter_table_exists(db):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dead_letter'"
    )
    assert await cursor.fetchone() is not None


async def test_dead_letter_columns(db):
    cursor = await db.execute("PRAGMA table_info(dead_letter)")
    cols = {row[1] for row in await cursor.fetchall()}
    expected = {
        "id", "operation_type", "payload", "target_provider",
        "failure_reason", "created_at", "retry_count", "last_retry_at", "status",
    }
    assert expected == cols


async def test_budget_seed_data(db):
    cursor = await db.execute("SELECT * FROM budgets WHERE active = 1")
    rows = await cursor.fetchall()
    types = {dict(r)["budget_type"] for r in rows}
    assert {"daily", "weekly", "monthly"} == types
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_db/test_schema.py::test_dead_letter_table_exists tests/test_db/test_schema.py::test_budget_seed_data -v`
Expected: FAIL

**Step 3: Write implementation**

Add to `TABLES` dict in `src/genesis/db/schema.py`:
```python
    "dead_letter": """
        CREATE TABLE IF NOT EXISTS dead_letter (
            id              TEXT PRIMARY KEY,
            operation_type  TEXT NOT NULL,
            payload         TEXT NOT NULL,
            target_provider TEXT NOT NULL,
            failure_reason  TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            retry_count     INTEGER DEFAULT 0,
            last_retry_at   TEXT,
            status          TEXT DEFAULT 'pending'
        )
    """,
```

Add to `INDEXES` list:
```python
    # dead letter
    "CREATE INDEX IF NOT EXISTS idx_dead_letter_status ON dead_letter(status)",
    "CREATE INDEX IF NOT EXISTS idx_dead_letter_provider ON dead_letter(target_provider)",
```

Add seed data constant:
```python
BUDGET_SEED = [
    # (id, budget_type, limit_usd, warning_pct, created_at, updated_at)
    ("budget_daily", "daily", 2.00, 0.80, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    ("budget_weekly", "weekly", 10.00, 0.80, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    ("budget_monthly", "monthly", 30.00, 0.80, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
]
```

Add to `seed_data()`:
```python
    await db.executemany(
        """INSERT OR IGNORE INTO budgets
           (id, budget_type, limit_usd, warning_pct, active, created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, ?, ?)""",
        BUDGET_SEED,
    )
```

**Step 4: Run tests**

Run: `pytest tests/test_db/test_schema.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/genesis/db/schema.py tests/test_db/test_schema.py
git commit -m "feat(routing): add dead_letter table DDL and budget seed data"
```

---

### Task 4: Dead-letter CRUD module

**Files:**
- Create: `src/genesis/db/crud/dead_letter.py`
- Create: `tests/test_db/test_dead_letter.py`

**Step 1: Write the failing test**

`tests/test_db/test_dead_letter.py`:
```python
"""Tests for dead_letter CRUD operations."""

import pytest

from genesis.db.crud import dead_letter


async def test_create_and_get(db):
    dl_id = await dead_letter.create(
        db, id="dl-1", operation_type="memory_write",
        payload='{"key": "value"}', target_provider="qdrant",
        failure_reason="connection refused", created_at="2026-03-04T00:00:00",
    )
    assert dl_id == "dl-1"
    row = await dead_letter.get_by_id(db, "dl-1")
    assert row is not None
    assert row["operation_type"] == "memory_write"
    assert row["status"] == "pending"
    assert row["retry_count"] == 0


async def test_query_pending(db):
    await dead_letter.create(
        db, id="dl-1", operation_type="memory_write", payload="{}",
        target_provider="qdrant", failure_reason="down",
        created_at="2026-03-04T00:00:00",
    )
    await dead_letter.create(
        db, id="dl-2", operation_type="embedding", payload="{}",
        target_provider="ollama", failure_reason="timeout",
        created_at="2026-03-04T00:01:00",
    )
    qdrant_pending = await dead_letter.query_pending(db, target_provider="qdrant")
    assert len(qdrant_pending) == 1
    assert qdrant_pending[0]["id"] == "dl-1"

    all_pending = await dead_letter.query_pending(db)
    assert len(all_pending) == 2


async def test_update_status(db):
    await dead_letter.create(
        db, id="dl-1", operation_type="memory_write", payload="{}",
        target_provider="qdrant", failure_reason="down",
        created_at="2026-03-04T00:00:00",
    )
    ok = await dead_letter.update_status(db, "dl-1", status="replayed")
    assert ok is True
    row = await dead_letter.get_by_id(db, "dl-1")
    assert row["status"] == "replayed"


async def test_increment_retry(db):
    await dead_letter.create(
        db, id="dl-1", operation_type="memory_write", payload="{}",
        target_provider="qdrant", failure_reason="down",
        created_at="2026-03-04T00:00:00",
    )
    ok = await dead_letter.increment_retry(
        db, "dl-1", last_retry_at="2026-03-04T01:00:00",
    )
    assert ok is True
    row = await dead_letter.get_by_id(db, "dl-1")
    assert row["retry_count"] == 1
    assert row["last_retry_at"] == "2026-03-04T01:00:00"


async def test_count_pending(db):
    await dead_letter.create(
        db, id="dl-1", operation_type="a", payload="{}", target_provider="x",
        failure_reason="err", created_at="2026-03-04T00:00:00",
    )
    await dead_letter.create(
        db, id="dl-2", operation_type="b", payload="{}", target_provider="y",
        failure_reason="err", created_at="2026-03-04T00:01:00",
    )
    assert await dead_letter.count_pending(db) == 2
    assert await dead_letter.count_pending(db, target_provider="x") == 1


async def test_delete(db):
    await dead_letter.create(
        db, id="dl-1", operation_type="a", payload="{}", target_provider="x",
        failure_reason="err", created_at="2026-03-04T00:00:00",
    )
    assert await dead_letter.delete(db, "dl-1") is True
    assert await dead_letter.get_by_id(db, "dl-1") is None
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_db/test_dead_letter.py -v`
Expected: FAIL — ImportError

**Step 3: Write implementation**

`src/genesis/db/crud/dead_letter.py`:
```python
"""CRUD operations for dead_letter table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    operation_type: str,
    payload: str,
    target_provider: str,
    failure_reason: str,
    created_at: str,
    status: str = "pending",
) -> str:
    await db.execute(
        """INSERT INTO dead_letter
           (id, operation_type, payload, target_provider,
            failure_reason, created_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (id, operation_type, payload, target_provider,
         failure_reason, created_at, status),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM dead_letter WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def query_pending(
    db: aiosqlite.Connection,
    *,
    target_provider: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM dead_letter WHERE status = 'pending'"
    params: list = []
    if target_provider is not None:
        sql += " AND target_provider = ?"
        params.append(target_provider)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def update_status(
    db: aiosqlite.Connection, id: str, *, status: str
) -> bool:
    cursor = await db.execute(
        "UPDATE dead_letter SET status = ? WHERE id = ?", (status, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def increment_retry(
    db: aiosqlite.Connection, id: str, *, last_retry_at: str
) -> bool:
    cursor = await db.execute(
        """UPDATE dead_letter
           SET retry_count = retry_count + 1, last_retry_at = ?
           WHERE id = ?""",
        (last_retry_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def count_pending(
    db: aiosqlite.Connection, *, target_provider: str | None = None
) -> int:
    sql = "SELECT COUNT(*) FROM dead_letter WHERE status = 'pending'"
    params: list = []
    if target_provider is not None:
        sql += " AND target_provider = ?"
        params.append(target_provider)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0])


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM dead_letter WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
```

**Step 4: Run tests**

Run: `pytest tests/test_db/test_dead_letter.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/genesis/db/crud/dead_letter.py tests/test_db/test_dead_letter.py
git commit -m "feat(routing): add dead_letter CRUD module"
```

---

### Task 5: Config loader + YAML config file

**Files:**
- Create: `src/genesis/routing/config.py`
- Create: `config/model_routing.yaml`
- Create: `tests/test_routing/test_config.py`

**Step 1: Write the failing test**

`tests/test_routing/test_config.py`:
```python
"""Tests for routing config loader."""

import textwrap
from pathlib import Path

import pytest

from genesis.routing.config import load_config, load_config_from_string
from genesis.routing.types import RoutingConfig

MINIMAL_YAML = textwrap.dedent("""\
    providers:
      free-provider:
        type: mistral
        model: mistral-large-latest
        free: true
        rpm_limit: 2
        open_duration_s: 120
      paid-provider:
        type: anthropic
        model: claude-haiku-4-5-20251001
        free: false
        open_duration_s: 120

    call_sites:
      3_micro_reflection:
        chain: [free-provider, paid-provider]
        retry_profile: background
      4_light_reflection:
        chain: [paid-provider]
        default_paid: true

    retry:
      default:
        max_retries: 3
        base_delay_ms: 500
        max_delay_ms: 30000
      background:
        max_retries: 4
        base_delay_ms: 1000
        max_delay_ms: 60000
""")


def test_load_minimal_config():
    cfg = load_config_from_string(MINIMAL_YAML)
    assert isinstance(cfg, RoutingConfig)
    assert "free-provider" in cfg.providers
    assert cfg.providers["free-provider"].is_free is True
    assert cfg.providers["paid-provider"].is_free is False
    assert "3_micro_reflection" in cfg.call_sites
    assert cfg.call_sites["3_micro_reflection"].chain == ["free-provider", "paid-provider"]
    assert cfg.call_sites["4_light_reflection"].default_paid is True
    assert "default" in cfg.retry_profiles
    assert cfg.retry_profiles["background"].max_retries == 4


def test_reject_missing_provider_in_chain():
    bad_yaml = textwrap.dedent("""\
        providers:
          real-provider:
            type: anthropic
            model: claude
            free: false
            open_duration_s: 120
        call_sites:
          test_site:
            chain: [real-provider, ghost-provider]
        retry:
          default:
            max_retries: 3
            base_delay_ms: 500
            max_delay_ms: 30000
    """)
    with pytest.raises(ValueError, match="ghost-provider"):
        load_config_from_string(bad_yaml)


def test_reject_missing_retry_profile():
    bad_yaml = textwrap.dedent("""\
        providers:
          p1:
            type: anthropic
            model: claude
            free: false
            open_duration_s: 120
        call_sites:
          test_site:
            chain: [p1]
            retry_profile: nonexistent
        retry:
          default:
            max_retries: 3
            base_delay_ms: 500
            max_delay_ms: 30000
    """)
    with pytest.raises(ValueError, match="nonexistent"):
        load_config_from_string(bad_yaml)


def test_load_full_yaml(tmp_path):
    """Load the real config/model_routing.yaml and verify structure."""
    real_path = Path(__file__).resolve().parents[2] / "config" / "model_routing.yaml"
    if not real_path.exists():
        pytest.skip("Full YAML not yet written")
    cfg = load_config(real_path)
    assert len(cfg.call_sites) >= 20
    assert len(cfg.providers) >= 10
    assert "default" in cfg.retry_profiles


def test_default_retry_policy_defaults():
    cfg = load_config_from_string(MINIMAL_YAML)
    assert cfg.retry_profiles["default"].backoff_multiplier == 2.0
    assert cfg.retry_profiles["default"].jitter_pct == 0.25
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_routing/test_config.py -v`
Expected: FAIL — ImportError

**Step 3: Write config loader**

`src/genesis/routing/config.py`:
```python
"""Load and validate model_routing.yaml → RoutingConfig."""

from __future__ import annotations

from pathlib import Path

import yaml

from genesis.routing.types import (
    CallSiteConfig,
    ProviderConfig,
    RetryPolicy,
    RoutingConfig,
)


def load_config(path: str | Path) -> RoutingConfig:
    """Load routing config from a YAML file."""
    with open(path) as f:
        return _parse(yaml.safe_load(f))


def load_config_from_string(text: str) -> RoutingConfig:
    """Load routing config from a YAML string (for tests)."""
    return _parse(yaml.safe_load(text))


def _parse(raw: dict) -> RoutingConfig:
    providers = _parse_providers(raw.get("providers", {}))
    retry_profiles = _parse_retry(raw.get("retry", {}))
    call_sites = _parse_call_sites(raw.get("call_sites", {}), providers, retry_profiles)
    return RoutingConfig(
        providers=providers, call_sites=call_sites, retry_profiles=retry_profiles,
    )


def _parse_providers(raw: dict) -> dict[str, ProviderConfig]:
    result = {}
    for name, cfg in raw.items():
        result[name] = ProviderConfig(
            name=name,
            provider_type=cfg["type"],
            model_id=cfg["model"],
            is_free=cfg.get("free", False),
            rpm_limit=cfg.get("rpm_limit"),
            open_duration_s=cfg.get("open_duration_s", 120),
        )
    return result


def _parse_call_sites(
    raw: dict,
    providers: dict[str, ProviderConfig],
    retry_profiles: dict[str, RetryPolicy],
) -> dict[str, CallSiteConfig]:
    result = {}
    for site_id, cfg in raw.items():
        chain = cfg.get("chain", [])
        for provider_name in chain:
            if provider_name not in providers:
                raise ValueError(
                    f"Call site '{site_id}' references unknown provider '{provider_name}'"
                )
        profile = cfg.get("retry_profile", "default")
        if profile not in retry_profiles:
            raise ValueError(
                f"Call site '{site_id}' references unknown retry profile '{profile}'"
            )
        result[site_id] = CallSiteConfig(
            id=site_id,
            chain=chain,
            default_paid=cfg.get("default_paid", False),
            never_pays=cfg.get("never_pays", False),
            retry_profile=profile,
        )
    return result


def _parse_retry(raw: dict) -> dict[str, RetryPolicy]:
    result = {}
    for name, cfg in raw.items():
        result[name] = RetryPolicy(
            max_retries=cfg.get("max_retries", 3),
            base_delay_ms=cfg.get("base_delay_ms", 500),
            max_delay_ms=cfg.get("max_delay_ms", 30000),
            backoff_multiplier=cfg.get("backoff_multiplier", 2.0),
            jitter_pct=cfg.get("jitter_pct", 0.25),
        )
    return result
```

**Step 4: Write the full YAML config file**

`config/model_routing.yaml`:
```yaml
# Genesis v3 Model Routing Configuration
# Source of truth: docs/architecture/genesis-v3-model-routing-registry.md
# Call site 1 (signal_collection) is pure computation — not routed through LLM.
# Call sites 23-26 (code work, identity evolution) are handled outside this system.

providers:
  ollama-3b:
    type: ollama
    model: qwen2.5:3b
    free: true
    open_duration_s: 60

  ollama-embedding:
    type: ollama
    model: qwen3-embedding:0.6b
    free: true
    open_duration_s: 60

  mistral-free:
    type: mistral
    model: mistral-large-latest
    free: true
    rpm_limit: 2
    open_duration_s: 120

  groq-free:
    type: groq
    model: llama-3.3-70b-versatile
    free: true
    rpm_limit: 30
    open_duration_s: 120

  gemini-free:
    type: google
    model: gemini-2.5-flash
    free: true
    rpm_limit: 15
    open_duration_s: 120

  openrouter-free:
    type: openrouter
    model: best-free
    free: true
    rpm_limit: 20
    open_duration_s: 120

  deepseek-v4:
    type: deepseek
    model: deepseek-chat
    free: false
    open_duration_s: 120

  qwen-plus:
    type: qwen
    model: qwen3.5-plus
    free: false
    open_duration_s: 120

  gpt-5-nano:
    type: openai
    model: gpt-5-nano
    free: false
    open_duration_s: 120

  claude-haiku:
    type: anthropic
    model: claude-haiku-4-5-20251001
    free: false
    open_duration_s: 120

  claude-sonnet:
    type: anthropic
    model: claude-sonnet-4-6-20250514
    free: false
    open_duration_s: 120

  claude-opus:
    type: anthropic
    model: claude-opus-4-6-20250514
    free: false
    open_duration_s: 120

  glm5:
    type: glm
    model: glm-5
    free: false
    open_duration_s: 120

call_sites:
  2_triage:
    chain: [ollama-3b, mistral-free, groq-free, gpt-5-nano]
  3_micro_reflection:
    chain: [groq-free, mistral-free, gpt-5-nano]
    retry_profile: background
  4_light_reflection:
    chain: [claude-haiku, claude-sonnet]
    default_paid: true
  5_deep_reflection:
    chain: [claude-sonnet, claude-opus]
    default_paid: true
    retry_profile: background
  6_strategic_reflection:
    chain: [claude-opus]
    default_paid: true
    retry_profile: background
  7_task_retrospective:
    chain: [deepseek-v4, qwen-plus, gpt-5-nano]
    default_paid: true
  8_memory_consolidation:
    chain: [mistral-free, groq-free, gemini-free, openrouter-free, gpt-5-nano]
  9_fact_extraction:
    chain: [mistral-free, groq-free, gemini-free, openrouter-free, gpt-5-nano]
  10_cognitive_state:
    chain: [glm5, claude-sonnet]
    default_paid: true
  11_user_model_synthesis:
    chain: [claude-sonnet, claude-opus]
    default_paid: true
    retry_profile: background
  12_surplus_brainstorm:
    chain: [mistral-free, groq-free, gemini-free, openrouter-free]
    never_pays: true
  13_morning_report:
    chain: [mistral-free, groq-free, gemini-free, gpt-5-nano]
  14_weekly_self_assessment:
    chain: [claude-opus]
    default_paid: true
  15_triage_calibration:
    chain: [deepseek-v4, qwen-plus, gpt-5-nano]
    default_paid: true
  16_quality_calibration:
    chain: [claude-opus]
    default_paid: true
  17_fresh_eyes_review:
    chain: [gpt-5-nano, claude-sonnet]
    default_paid: true
  18_meta_prompting:
    chain: [deepseek-v4, qwen-plus, gpt-5-nano]
    default_paid: true
  19_outreach_draft:
    chain: [mistral-free, groq-free, gemini-free, openrouter-free, gpt-5-nano]
  20_adversarial_counterargument:
    chain: [gpt-5-nano, claude-sonnet]
    default_paid: true
  21_embeddings:
    chain: [ollama-embedding]
  22_tagging:
    chain: [ollama-3b, mistral-free, groq-free]
  27_pre_execution_assessment:
    chain: [claude-sonnet, claude-haiku]
    default_paid: true
    retry_profile: user_facing
  28_observation_sweep:
    chain: [deepseek-v4, claude-sonnet]
    default_paid: true

retry:
  default:
    max_retries: 3
    base_delay_ms: 500
    max_delay_ms: 30000
  user_facing:
    max_retries: 2
    base_delay_ms: 300
    max_delay_ms: 5000
  background:
    max_retries: 4
    base_delay_ms: 1000
    max_delay_ms: 60000
```

**Step 5: Run all config tests and commit**

Run: `pytest tests/test_routing/test_config.py -v`
Expected: All PASS

```bash
git add src/genesis/routing/config.py config/model_routing.yaml tests/test_routing/test_config.py
git commit -m "feat(routing): add config loader and full model_routing.yaml with 22 call sites"
```

---

### Task 6: Circuit breaker

**Files:**
- Create: `src/genesis/routing/circuit_breaker.py`
- Create: `tests/test_routing/test_circuit_breaker.py`

**Step 1: Write the failing test**

`tests/test_routing/test_circuit_breaker.py`:
```python
"""Tests for circuit breaker state machine."""

import pytest

from genesis.routing.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry
from genesis.routing.types import DegradationLevel, ErrorCategory, ProviderState


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker("test-provider")
        assert cb.state == ProviderState.CLOSED
        assert cb.is_available() is True

    def test_consecutive_failures_trip_to_open(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        for _ in range(3):
            cb.record_failure(ErrorCategory.TRANSIENT)
        assert cb.state == ProviderState.OPEN
        assert cb.is_available() is False

    def test_open_to_half_open_after_duration(self):
        t = 0.0
        cb = CircuitBreaker("test", failure_threshold=2, open_duration_s=10, clock=lambda: t)
        cb.record_failure(ErrorCategory.TRANSIENT)
        cb.record_failure(ErrorCategory.TRANSIENT)
        assert cb.state == ProviderState.OPEN
        t = 11.0
        assert cb.state == ProviderState.HALF_OPEN
        assert cb.is_available() is True

    def test_half_open_to_closed_after_successes(self):
        t = 0.0
        cb = CircuitBreaker(
            "test", failure_threshold=2, open_duration_s=5,
            success_threshold=2, clock=lambda: t,
        )
        cb.record_failure(ErrorCategory.TRANSIENT)
        cb.record_failure(ErrorCategory.TRANSIENT)
        t = 6.0
        assert cb.state == ProviderState.HALF_OPEN
        cb.record_success()
        assert cb.state == ProviderState.HALF_OPEN
        cb.record_success()
        assert cb.state == ProviderState.CLOSED

    def test_half_open_to_open_on_failure(self):
        t = 0.0
        cb = CircuitBreaker("test", failure_threshold=2, open_duration_s=5, clock=lambda: t)
        cb.record_failure(ErrorCategory.TRANSIENT)
        cb.record_failure(ErrorCategory.TRANSIENT)
        t = 6.0
        assert cb.state == ProviderState.HALF_OPEN
        cb.record_failure(ErrorCategory.TRANSIENT)
        assert cb.state == ProviderState.OPEN

    def test_permanent_errors_dont_trip(self):
        cb = CircuitBreaker("test", failure_threshold=2)
        cb.record_failure(ErrorCategory.PERMANENT)
        cb.record_failure(ErrorCategory.PERMANENT)
        assert cb.state == ProviderState.CLOSED

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.record_failure(ErrorCategory.TRANSIENT)
        cb.record_failure(ErrorCategory.TRANSIENT)
        cb.record_success()
        cb.record_failure(ErrorCategory.TRANSIENT)
        assert cb.state == ProviderState.CLOSED


class TestCircuitBreakerRegistry:
    def test_get_creates_breaker(self):
        from genesis.routing.types import ProviderConfig

        providers = {
            "p1": ProviderConfig(
                name="p1", provider_type="test", model_id="m",
                is_free=True, rpm_limit=None, open_duration_s=60,
            ),
        }
        reg = CircuitBreakerRegistry(providers)
        cb = reg.get("p1")
        assert isinstance(cb, CircuitBreaker)
        assert cb is reg.get("p1")  # Same instance

    def test_degradation_all_up(self):
        reg = CircuitBreakerRegistry({})
        assert reg.compute_degradation_level() == DegradationLevel.NORMAL

    def test_degradation_one_cloud_down(self):
        from genesis.routing.types import ProviderConfig

        providers = {
            "cloud1": ProviderConfig(
                name="cloud1", provider_type="anthropic", model_id="m",
                is_free=False, rpm_limit=None, open_duration_s=120,
            ),
            "cloud2": ProviderConfig(
                name="cloud2", provider_type="openai", model_id="m",
                is_free=False, rpm_limit=None, open_duration_s=120,
            ),
        }
        reg = CircuitBreakerRegistry(providers)
        cb = reg.get("cloud1")
        for _ in range(3):
            cb.record_failure(ErrorCategory.TRANSIENT)
        assert reg.compute_degradation_level() == DegradationLevel.FALLBACK

    def test_degradation_ollama_down_is_l5(self):
        from genesis.routing.types import ProviderConfig

        providers = {
            "ollama-3b": ProviderConfig(
                name="ollama-3b", provider_type="ollama", model_id="m",
                is_free=True, rpm_limit=None, open_duration_s=60,
            ),
        }
        reg = CircuitBreakerRegistry(providers)
        cb = reg.get("ollama-3b")
        for _ in range(3):
            cb.record_failure(ErrorCategory.TRANSIENT)
        assert reg.compute_degradation_level() == DegradationLevel.LOCAL_COMPUTE_DOWN
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_routing/test_circuit_breaker.py -v`
Expected: FAIL — ImportError

**Step 3: Write implementation**

`src/genesis/routing/circuit_breaker.py`:
```python
"""Per-provider circuit breaker and registry."""

from __future__ import annotations

import time

from genesis.routing.types import (
    DegradationLevel,
    ErrorCategory,
    ProviderConfig,
    ProviderState,
)


class CircuitBreaker:
    """Tracks provider health. CLOSED → OPEN → HALF_OPEN → CLOSED."""

    def __init__(
        self,
        provider: str,
        failure_threshold: int = 3,
        open_duration_s: int = 120,
        success_threshold: int = 2,
        clock=None,
    ):
        self.provider = provider
        self.failure_threshold = failure_threshold
        self.open_duration_s = open_duration_s
        self.success_threshold = success_threshold
        self._clock = clock or time.monotonic
        self._state = ProviderState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> ProviderState:
        if self._state == ProviderState.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self.open_duration_s:
                self._state = ProviderState.HALF_OPEN
                self._success_count = 0
        return self._state

    def is_available(self) -> bool:
        return self.state != ProviderState.OPEN

    def record_success(self) -> None:
        if self.state == ProviderState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._state = ProviderState.CLOSED
                self._failure_count = 0
        else:
            self._failure_count = 0

    def record_failure(self, category: ErrorCategory) -> None:
        if category == ErrorCategory.PERMANENT:
            return
        self._failure_count += 1
        if self.state == ProviderState.HALF_OPEN:
            self._state = ProviderState.OPEN
            self._opened_at = self._clock()
        elif self._failure_count >= self.failure_threshold:
            self._state = ProviderState.OPEN
            self._opened_at = self._clock()


class CircuitBreakerRegistry:
    """Manages all provider circuit breakers."""

    def __init__(self, providers: dict[str, ProviderConfig], clock=None):
        self._providers = providers
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, provider: str) -> CircuitBreaker:
        if provider not in self._breakers:
            cfg = self._providers.get(provider)
            open_dur = cfg.open_duration_s if cfg else 120
            self._breakers[provider] = CircuitBreaker(
                provider, open_duration_s=open_dur, clock=self._clock,
            )
        return self._breakers[provider]

    def compute_degradation_level(self) -> DegradationLevel:
        """Compute current degradation from breaker states."""
        if not self._breakers:
            return DegradationLevel.NORMAL

        open_breakers = [
            name for name, cb in self._breakers.items()
            if cb.state == ProviderState.OPEN
        ]
        if not open_breakers:
            return DegradationLevel.NORMAL

        # Check independent axes first
        ollama_names = [
            n for n, p in self._providers.items() if p.provider_type == "ollama"
        ]
        if any(n in open_breakers for n in ollama_names):
            all_ollama_down = all(n in open_breakers for n in ollama_names)
            if all_ollama_down and ollama_names:
                return DegradationLevel.LOCAL_COMPUTE_DOWN

        # Count open cloud (non-ollama) breakers
        cloud_names = [
            n for n, p in self._providers.items() if p.provider_type != "ollama"
        ]
        cloud_open = [n for n in cloud_names if n in open_breakers]

        if len(cloud_open) == len(cloud_names) and cloud_names:
            return DegradationLevel.ESSENTIAL
        if len(cloud_open) >= 2:
            return DegradationLevel.REDUCED
        if len(cloud_open) >= 1:
            return DegradationLevel.FALLBACK

        return DegradationLevel.NORMAL
```

**Step 4: Run tests**

Run: `pytest tests/test_routing/test_circuit_breaker.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/genesis/routing/circuit_breaker.py tests/test_routing/test_circuit_breaker.py
git commit -m "feat(routing): add circuit breaker state machine and registry"
```

---

### Task 7: Retry logic

**Files:**
- Create: `src/genesis/routing/retry.py`
- Create: `tests/test_routing/test_retry.py`

**Step 1: Write the failing test**

`tests/test_routing/test_retry.py`:
```python
"""Tests for retry logic — error classification and backoff computation."""

import pytest

from genesis.routing.retry import classify_error, compute_delay
from genesis.routing.types import ErrorCategory, RetryPolicy


class TestClassifyError:
    def test_429_is_transient(self):
        assert classify_error(429, "") == ErrorCategory.TRANSIENT

    def test_503_is_transient(self):
        assert classify_error(503, "") == ErrorCategory.TRANSIENT

    def test_500_is_transient(self):
        assert classify_error(500, "") == ErrorCategory.TRANSIENT

    def test_401_is_permanent(self):
        assert classify_error(401, "") == ErrorCategory.PERMANENT

    def test_403_is_permanent(self):
        assert classify_error(403, "") == ErrorCategory.PERMANENT

    def test_404_is_permanent(self):
        assert classify_error(404, "") == ErrorCategory.PERMANENT

    def test_timeout_in_message_is_transient(self):
        assert classify_error(None, "connection timeout") == ErrorCategory.TRANSIENT

    def test_malformed_is_degraded(self):
        assert classify_error(200, "malformed JSON response") == ErrorCategory.DEGRADED

    def test_unknown_defaults_to_transient(self):
        assert classify_error(None, "something weird") == ErrorCategory.TRANSIENT


class TestComputeDelay:
    def test_first_attempt_uses_base(self):
        policy = RetryPolicy(base_delay_ms=1000, jitter_pct=0.0)
        delay = compute_delay(policy, attempt=0)
        assert delay == pytest.approx(1.0)

    def test_exponential_growth(self):
        policy = RetryPolicy(base_delay_ms=1000, backoff_multiplier=2.0, jitter_pct=0.0)
        d0 = compute_delay(policy, attempt=0)
        d1 = compute_delay(policy, attempt=1)
        d2 = compute_delay(policy, attempt=2)
        assert d0 == pytest.approx(1.0)
        assert d1 == pytest.approx(2.0)
        assert d2 == pytest.approx(4.0)

    def test_capped_at_max(self):
        policy = RetryPolicy(
            base_delay_ms=1000, max_delay_ms=5000,
            backoff_multiplier=10.0, jitter_pct=0.0,
        )
        delay = compute_delay(policy, attempt=3)
        assert delay == pytest.approx(5.0)

    def test_jitter_within_bounds(self):
        policy = RetryPolicy(base_delay_ms=1000, jitter_pct=0.25)
        delays = [compute_delay(policy, attempt=0) for _ in range(100)]
        assert all(0.75 <= d <= 1.25 for d in delays)

    def test_never_negative(self):
        policy = RetryPolicy(base_delay_ms=100, jitter_pct=0.5)
        delays = [compute_delay(policy, attempt=0) for _ in range(200)]
        assert all(d >= 0 for d in delays)
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_routing/test_retry.py -v`
Expected: FAIL — ImportError

**Step 3: Write implementation**

`src/genesis/routing/retry.py`:
```python
"""Error classification and exponential backoff with jitter."""

from __future__ import annotations

import random

from genesis.routing.types import ErrorCategory, RetryPolicy

_TRANSIENT_CODES = {429, 500, 502, 503, 504}
_PERMANENT_CODES = {401, 403, 404}


def classify_error(status_code: int | None, error_msg: str) -> ErrorCategory:
    """Classify an error for retry/fallback decisions."""
    if status_code in _PERMANENT_CODES:
        return ErrorCategory.PERMANENT
    if status_code in _TRANSIENT_CODES:
        return ErrorCategory.TRANSIENT
    msg = error_msg.lower()
    if "timeout" in msg or "connection" in msg:
        return ErrorCategory.TRANSIENT
    if "malformed" in msg or "partial" in msg or "truncated" in msg:
        return ErrorCategory.DEGRADED
    return ErrorCategory.TRANSIENT


def compute_delay(policy: RetryPolicy, attempt: int) -> float:
    """Compute retry delay in seconds with exponential backoff and jitter."""
    base = policy.base_delay_ms * (policy.backoff_multiplier ** attempt)
    capped = min(base, policy.max_delay_ms)
    jitter = capped * policy.jitter_pct * random.uniform(-1, 1)
    return max(0, (capped + jitter) / 1000.0)
```

**Step 4: Run tests**

Run: `pytest tests/test_routing/test_retry.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/genesis/routing/retry.py tests/test_routing/test_retry.py
git commit -m "feat(routing): add error classification and backoff computation"
```

---

### Task 8: Cost tracker

**Files:**
- Create: `src/genesis/routing/cost_tracker.py`
- Create: `tests/test_routing/test_cost_tracker.py`

**Step 1: Write the failing test**

`tests/test_routing/test_cost_tracker.py`:
```python
"""Tests for cost tracking and budget enforcement."""

from datetime import UTC, datetime, timedelta

import pytest

from genesis.routing.cost_tracker import CostTracker
from genesis.routing.types import BudgetStatus, CallResult


async def test_record_cost_event(db):
    tracker = CostTracker(db)
    result = CallResult(
        success=True, content="ok", input_tokens=100,
        output_tokens=50, cost_usd=0.01,
    )
    await tracker.record("3_micro_reflection", "mistral-free", result)
    from genesis.db.crud import cost_events
    rows = await cost_events.query(db, limit=10)
    assert len(rows) == 1
    assert rows[0]["provider"] == "mistral-free"
    assert rows[0]["cost_usd"] == pytest.approx(0.01)


async def test_check_budget_under_limit(db):
    tracker = CostTracker(db)
    status = await tracker.check_budget()
    assert status == BudgetStatus.UNDER_LIMIT


async def test_check_budget_warning(db):
    now = datetime.now(UTC)
    tracker = CostTracker(db, clock=lambda: now)
    # Daily budget is $2.00, warning at 80% = $1.60
    result = CallResult(success=True, cost_usd=1.70)
    for i in range(1):
        await tracker.record(f"site_{i}", "paid", result)
    status = await tracker.check_budget()
    assert status == BudgetStatus.WARNING


async def test_check_budget_exceeded(db):
    now = datetime.now(UTC)
    tracker = CostTracker(db, clock=lambda: now)
    # Daily budget is $2.00, spend $2.50
    result = CallResult(success=True, cost_usd=2.50)
    await tracker.record("site_0", "paid", result)
    status = await tracker.check_budget()
    assert status == BudgetStatus.EXCEEDED


async def test_get_period_cost(db):
    now = datetime.now(UTC)
    tracker = CostTracker(db, clock=lambda: now)
    result = CallResult(success=True, cost_usd=0.50)
    await tracker.record("site_a", "paid", result)
    await tracker.record("site_b", "paid", result)
    cost = await tracker.get_period_cost("today")
    assert cost == pytest.approx(1.00)
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_routing/test_cost_tracker.py -v`
Expected: FAIL — ImportError

**Step 3: Write implementation**

`src/genesis/routing/cost_tracker.py`:
```python
"""Cost tracking and budget enforcement wrapping Phase 0 CRUD."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.crud import budgets as budgets_crud
from genesis.db.crud import cost_events as cost_events_crud
from genesis.routing.types import BudgetStatus, CallResult


class CostTracker:
    def __init__(self, db: aiosqlite.Connection, *, clock=None):
        self.db = db
        self._clock = clock or (lambda: datetime.now(UTC))

    async def record(
        self, call_site_id: str, provider: str, result: CallResult,
    ) -> None:
        """Record a cost event from a completed LLM call."""
        now = self._clock()
        await cost_events_crud.create(
            self.db,
            id=str(uuid.uuid4()),
            event_type="llm_call",
            model=provider,
            provider=provider,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            created_at=now.isoformat(),
            metadata={"call_site": call_site_id},
        )

    async def check_budget(self, *, task_id: str | None = None) -> BudgetStatus:
        """Check all active budgets. Returns the worst status found."""
        worst = BudgetStatus.UNDER_LIMIT
        for budget_type in ("daily", "weekly", "monthly"):
            status = await self._check_period(budget_type)
            if status == BudgetStatus.EXCEEDED:
                return BudgetStatus.EXCEEDED
            if status == BudgetStatus.WARNING:
                worst = BudgetStatus.WARNING
        return worst

    async def get_period_cost(self, period: str) -> float:
        """Get total cost for a period. period: 'today', 'this_week', 'this_month'."""
        since = self._period_start(period)
        return await cost_events_crud.sum_cost(self.db, since=since)

    async def _check_period(self, budget_type: str) -> BudgetStatus:
        active = await budgets_crud.list_active(self.db, budget_type=budget_type)
        if not active:
            return BudgetStatus.UNDER_LIMIT
        budget = active[0]
        period_map = {"daily": "today", "weekly": "this_week", "monthly": "this_month"}
        since = self._period_start(period_map[budget_type])
        spent = await cost_events_crud.sum_cost(self.db, since=since)
        limit = budget["limit_usd"]
        warning_pct = budget["warning_pct"]
        if spent >= limit:
            return BudgetStatus.EXCEEDED
        if spent >= limit * warning_pct:
            return BudgetStatus.WARNING
        return BudgetStatus.UNDER_LIMIT

    def _period_start(self, period: str) -> str:
        now = self._clock()
        if period == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "this_week":
            start = now - timedelta(days=now.weekday())
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "this_month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            start = now
        return start.isoformat()
```

**Step 4: Run tests**

Run: `pytest tests/test_routing/test_cost_tracker.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/genesis/routing/cost_tracker.py tests/test_routing/test_cost_tracker.py
git commit -m "feat(routing): add cost tracker with budget enforcement"
```

---

### Task 9: Dead-letter queue

**Files:**
- Create: `src/genesis/routing/dead_letter.py`
- Create: `tests/test_routing/test_dead_letter_queue.py`

**Step 1: Write the failing test**

`tests/test_routing/test_dead_letter_queue.py`:
```python
"""Tests for dead-letter queue (high-level wrapper around CRUD)."""

from datetime import UTC, datetime, timedelta

import pytest

from genesis.routing.dead_letter import DeadLetterQueue


async def test_enqueue(db):
    now = datetime.now(UTC)
    dlq = DeadLetterQueue(db, clock=lambda: now)
    dl_id = await dlq.enqueue(
        operation_type="memory_write",
        payload={"key": "value"},
        target_provider="qdrant",
        failure_reason="connection refused",
    )
    assert dl_id is not None
    from genesis.db.crud import dead_letter
    row = await dead_letter.get_by_id(db, dl_id)
    assert row["status"] == "pending"
    assert row["operation_type"] == "memory_write"


async def test_get_pending_count(db):
    now = datetime.now(UTC)
    dlq = DeadLetterQueue(db, clock=lambda: now)
    await dlq.enqueue("a", {}, "qdrant", "err")
    await dlq.enqueue("b", {}, "ollama", "err")
    assert await dlq.get_pending_count() == 2
    assert await dlq.get_pending_count(target_provider="qdrant") == 1


async def test_replay_marks_replayed(db):
    now = datetime.now(UTC)
    dlq = DeadLetterQueue(db, clock=lambda: now)
    dl_id = await dlq.enqueue("a", {}, "qdrant", "err")
    count = await dlq.replay_pending("qdrant")
    assert count == 1
    from genesis.db.crud import dead_letter
    row = await dead_letter.get_by_id(db, dl_id)
    assert row["status"] == "replayed"


async def test_expire_old(db):
    old_time = datetime.now(UTC) - timedelta(hours=100)
    dlq = DeadLetterQueue(db, clock=lambda: datetime.now(UTC))
    # Create an entry with an old timestamp
    from genesis.db.crud import dead_letter
    await dead_letter.create(
        db, id="old-1", operation_type="a", payload="{}",
        target_provider="x", failure_reason="err",
        created_at=old_time.isoformat(),
    )
    expired = await dlq.expire_old(max_age_hours=72)
    assert expired == 1
    row = await dead_letter.get_by_id(db, "old-1")
    assert row["status"] == "expired"
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_routing/test_dead_letter_queue.py -v`
Expected: FAIL — ImportError

**Step 3: Write implementation**

`src/genesis/routing/dead_letter.py`:
```python
"""Dead-letter queue — staging for failed operations awaiting replay."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.crud import dead_letter as dl_crud


class DeadLetterQueue:
    def __init__(self, db: aiosqlite.Connection, *, clock=None):
        self.db = db
        self._clock = clock or (lambda: datetime.now(UTC))

    async def enqueue(
        self,
        operation_type: str,
        payload: dict | str,
        target_provider: str,
        failure_reason: str,
    ) -> str:
        """Stage a failed operation for later replay."""
        dl_id = str(uuid.uuid4())
        payload_str = json.dumps(payload) if isinstance(payload, dict) else payload
        await dl_crud.create(
            self.db,
            id=dl_id,
            operation_type=operation_type,
            payload=payload_str,
            target_provider=target_provider,
            failure_reason=failure_reason,
            created_at=self._clock().isoformat(),
        )
        return dl_id

    async def replay_pending(self, target_provider: str) -> int:
        """Mark all pending items for a provider as replayed. Returns count."""
        pending = await dl_crud.query_pending(
            self.db, target_provider=target_provider,
        )
        for item in pending:
            await dl_crud.update_status(self.db, item["id"], status="replayed")
        return len(pending)

    async def expire_old(self, max_age_hours: int = 72) -> int:
        """Mark pending items older than max_age_hours as expired. Returns count."""
        cutoff = self._clock() - timedelta(hours=max_age_hours)
        pending = await dl_crud.query_pending(self.db, limit=1000)
        count = 0
        for item in pending:
            if item["created_at"] < cutoff.isoformat():
                await dl_crud.update_status(self.db, item["id"], status="expired")
                count += 1
        return count

    async def get_pending_count(
        self, *, target_provider: str | None = None,
    ) -> int:
        return await dl_crud.count_pending(
            self.db, target_provider=target_provider,
        )
```

**Step 4: Run tests**

Run: `pytest tests/test_routing/test_dead_letter_queue.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/genesis/routing/dead_letter.py tests/test_routing/test_dead_letter_queue.py
git commit -m "feat(routing): add dead-letter queue with enqueue, replay, and expiry"
```

---

### Task 10: Degradation tracker

**Files:**
- Create: `src/genesis/routing/degradation.py`
- Create: `tests/test_routing/test_degradation.py`

**Step 1: Write the failing test**

`tests/test_routing/test_degradation.py`:
```python
"""Tests for graceful degradation tracking."""

import pytest

from genesis.routing.degradation import DegradationTracker, should_skip_call_site
from genesis.routing.types import DegradationLevel


class TestShouldSkip:
    def test_l0_skips_nothing(self):
        assert should_skip_call_site("12_surplus_brainstorm", DegradationLevel.NORMAL) is False

    def test_l2_skips_surplus(self):
        assert should_skip_call_site("12_surplus_brainstorm", DegradationLevel.REDUCED) is True

    def test_l2_skips_outreach_draft(self):
        assert should_skip_call_site("19_outreach_draft", DegradationLevel.REDUCED) is True

    def test_l2_allows_micro_reflection(self):
        assert should_skip_call_site("3_micro_reflection", DegradationLevel.REDUCED) is False

    def test_l3_skips_deep_reflection(self):
        assert should_skip_call_site("5_deep_reflection", DegradationLevel.ESSENTIAL) is True

    def test_l3_allows_triage(self):
        assert should_skip_call_site("2_triage", DegradationLevel.ESSENTIAL) is False

    def test_l3_allows_micro_reflection(self):
        assert should_skip_call_site("3_micro_reflection", DegradationLevel.ESSENTIAL) is False


class TestDegradationTracker:
    def test_tracks_level(self):
        tracker = DegradationTracker()
        assert tracker.current_level == DegradationLevel.NORMAL
        tracker.update(DegradationLevel.FALLBACK)
        assert tracker.current_level == DegradationLevel.FALLBACK

    def test_should_skip(self):
        tracker = DegradationTracker()
        tracker.update(DegradationLevel.REDUCED)
        assert tracker.should_skip("12_surplus_brainstorm") is True
        assert tracker.should_skip("2_triage") is False
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_routing/test_degradation.py -v`
Expected: FAIL — ImportError

**Step 3: Write implementation**

`src/genesis/routing/degradation.py`:
```python
"""Graceful degradation tracking and sacrifice ordering."""

from __future__ import annotations

from genesis.routing.types import DegradationLevel

# Sacrifice ordering: first items are sacrificed first at lower degradation levels.
# L2 (Reduced): surplus, outreach drafts, morning report
# L3 (Essential): everything except triage, micro reflection, health monitoring
_L2_SKIP = {
    "12_surplus_brainstorm", "19_outreach_draft", "13_morning_report",
}

_L3_KEEP = {
    "2_triage", "3_micro_reflection", "21_embeddings", "22_tagging",
}


def should_skip_call_site(call_site_id: str, level: DegradationLevel) -> bool:
    """Whether a call site should be skipped at the given degradation level."""
    if level in (DegradationLevel.NORMAL, DegradationLevel.FALLBACK):
        return False
    if level == DegradationLevel.REDUCED:
        return call_site_id in _L2_SKIP
    if level == DegradationLevel.ESSENTIAL:
        return call_site_id not in _L3_KEEP
    # L4/L5 are independent axes — handled by circuit breaker, not call site skip
    return False


class DegradationTracker:
    """Tracks current degradation level and provides skip decisions."""

    def __init__(self):
        self._level = DegradationLevel.NORMAL

    @property
    def current_level(self) -> DegradationLevel:
        return self._level

    def update(self, level: DegradationLevel) -> None:
        self._level = level

    def should_skip(self, call_site_id: str) -> bool:
        return should_skip_call_site(call_site_id, self._level)
```

**Step 4: Run tests**

Run: `pytest tests/test_routing/test_degradation.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/genesis/routing/degradation.py tests/test_routing/test_degradation.py
git commit -m "feat(routing): add degradation tracker with sacrifice ordering"
```

---

### Task 11: Router

**Files:**
- Create: `src/genesis/routing/router.py`
- Create: `tests/test_routing/conftest.py`
- Create: `tests/test_routing/test_router.py`

**Step 1: Write the failing test**

`tests/test_routing/conftest.py`:
```python
"""Test fixtures for routing tests."""

import pytest

from genesis.routing.types import (
    CallResult,
    CallSiteConfig,
    ProviderConfig,
    RetryPolicy,
    RoutingConfig,
)


class MockDelegate:
    """Configurable mock for CallDelegate protocol."""

    def __init__(self, responses=None):
        self.responses: dict[str, CallResult] = responses or {}
        self.calls: list[dict] = []

    async def call(self, provider, model_id, messages, **kwargs):
        self.calls.append({
            "provider": provider, "model_id": model_id, "messages": messages,
        })
        if provider in self.responses:
            return self.responses[provider]
        return CallResult(success=True, content="mock response", cost_usd=0.01)


@pytest.fixture
def sample_providers():
    return {
        "free-1": ProviderConfig(
            name="free-1", provider_type="mistral", model_id="mistral-large",
            is_free=True, rpm_limit=2, open_duration_s=120,
        ),
        "free-2": ProviderConfig(
            name="free-2", provider_type="groq", model_id="llama-70b",
            is_free=True, rpm_limit=30, open_duration_s=120,
        ),
        "paid-1": ProviderConfig(
            name="paid-1", provider_type="anthropic", model_id="claude-haiku",
            is_free=False, rpm_limit=None, open_duration_s=120,
        ),
        "paid-2": ProviderConfig(
            name="paid-2", provider_type="openai", model_id="gpt-5-nano",
            is_free=False, rpm_limit=None, open_duration_s=120,
        ),
    }


@pytest.fixture
def sample_config(sample_providers):
    return RoutingConfig(
        providers=sample_providers,
        call_sites={
            "test_mixed": CallSiteConfig(
                id="test_mixed", chain=["free-1", "free-2", "paid-1"],
            ),
            "test_paid": CallSiteConfig(
                id="test_paid", chain=["paid-1", "paid-2"], default_paid=True,
            ),
            "test_never_pays": CallSiteConfig(
                id="test_never_pays", chain=["free-1", "free-2"], never_pays=True,
            ),
        },
        retry_profiles={
            "default": RetryPolicy(max_retries=1, base_delay_ms=10, jitter_pct=0.0),
        },
    )
```

`tests/test_routing/test_router.py`:
```python
"""Tests for the main router — chain walking, filtering, budget enforcement."""

import pytest

from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.cost_tracker import CostTracker
from genesis.routing.degradation import DegradationTracker
from genesis.routing.router import Router
from genesis.routing.types import (
    BudgetStatus,
    CallResult,
    DegradationLevel,
    ErrorCategory,
)

from .conftest import MockDelegate


class TestRouterSuccess:
    async def test_first_provider_succeeds(self, db, sample_config, sample_providers):
        delegate = MockDelegate()
        breakers = CircuitBreakerRegistry(sample_providers)
        tracker = CostTracker(db)
        degradation = DegradationTracker()
        router = Router(sample_config, breakers, tracker, degradation, delegate)

        result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
        assert result.success is True
        assert result.provider_used == "free-1"
        assert result.fallback_used is False
        assert len(delegate.calls) == 1

    async def test_fallback_on_failure(self, db, sample_config, sample_providers):
        delegate = MockDelegate(responses={
            "free-1": CallResult(success=False, error="timeout", status_code=503),
        })
        breakers = CircuitBreakerRegistry(sample_providers)
        tracker = CostTracker(db)
        degradation = DegradationTracker()
        router = Router(sample_config, breakers, tracker, degradation, delegate)

        result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
        assert result.success is True
        assert result.provider_used == "free-2"
        assert result.fallback_used is True


class TestRouterFiltering:
    async def test_never_pays_skips_paid(self, db, sample_config, sample_providers):
        delegate = MockDelegate(responses={
            "free-1": CallResult(success=False, error="down", status_code=503),
            "free-2": CallResult(success=False, error="down", status_code=503),
        })
        breakers = CircuitBreakerRegistry(sample_providers)
        tracker = CostTracker(db)
        degradation = DegradationTracker()
        router = Router(sample_config, breakers, tracker, degradation, delegate)

        result = await router.route_call("test_never_pays", [])
        assert result.success is False
        providers_called = [c["provider"] for c in delegate.calls]
        assert "paid-1" not in providers_called
        assert "paid-2" not in providers_called


class TestRouterBudget:
    async def test_budget_exceeded_skips_paid(self, db, sample_config, sample_providers):
        from datetime import UTC, datetime
        now = datetime.now(UTC)
        tracker = CostTracker(db, clock=lambda: now)
        # Spend over daily budget ($2)
        big_result = CallResult(success=True, cost_usd=3.00)
        await tracker.record("setup", "paid", big_result)

        delegate = MockDelegate()
        breakers = CircuitBreakerRegistry(sample_providers)
        degradation = DegradationTracker()
        router = Router(sample_config, breakers, tracker, degradation, delegate)

        result = await router.route_call("test_mixed", [])
        assert result.success is True
        assert result.provider_used == "free-1"

    async def test_budget_override_allows_paid(self, db, sample_config, sample_providers):
        from datetime import UTC, datetime
        now = datetime.now(UTC)
        tracker = CostTracker(db, clock=lambda: now)
        big_result = CallResult(success=True, cost_usd=3.00)
        await tracker.record("setup", "paid", big_result)

        delegate = MockDelegate()
        breakers = CircuitBreakerRegistry(sample_providers)
        degradation = DegradationTracker()
        router = Router(sample_config, breakers, tracker, degradation, delegate)

        result = await router.route_call("test_paid", [], budget_override=True)
        assert result.success is True
        assert result.provider_used == "paid-1"


class TestRouterCircuitBreaker:
    async def test_skips_open_breaker(self, db, sample_config, sample_providers):
        delegate = MockDelegate()
        breakers = CircuitBreakerRegistry(sample_providers)
        # Trip free-1's breaker
        cb = breakers.get("free-1")
        for _ in range(3):
            cb.record_failure(ErrorCategory.TRANSIENT)

        tracker = CostTracker(db)
        degradation = DegradationTracker()
        router = Router(sample_config, breakers, tracker, degradation, delegate)

        result = await router.route_call("test_mixed", [])
        assert result.success is True
        assert result.provider_used == "free-2"


class TestRouterDegradation:
    async def test_degradation_skips_call_site(self, db, sample_config, sample_providers):
        delegate = MockDelegate()
        breakers = CircuitBreakerRegistry(sample_providers)
        tracker = CostTracker(db)
        degradation = DegradationTracker()
        degradation.update(DegradationLevel.REDUCED)

        # Add a surplus call site to the config for this test
        from genesis.routing.types import CallSiteConfig, RoutingConfig
        config = RoutingConfig(
            providers=sample_config.providers,
            call_sites={
                **sample_config.call_sites,
                "12_surplus_brainstorm": CallSiteConfig(
                    id="12_surplus_brainstorm", chain=["free-1"], never_pays=True,
                ),
            },
            retry_profiles=sample_config.retry_profiles,
        )
        router = Router(config, breakers, tracker, degradation, delegate)

        result = await router.route_call("12_surplus_brainstorm", [])
        assert result.success is False
        assert "degradation" in (result.error or "").lower()


class TestRouterAllFailed:
    async def test_all_providers_exhausted(self, db, sample_config, sample_providers):
        delegate = MockDelegate(responses={
            "paid-1": CallResult(success=False, error="down", status_code=503),
            "paid-2": CallResult(success=False, error="down", status_code=503),
        })
        breakers = CircuitBreakerRegistry(sample_providers)
        tracker = CostTracker(db)
        degradation = DegradationTracker()
        router = Router(sample_config, breakers, tracker, degradation, delegate)

        result = await router.route_call("test_paid", [])
        assert result.success is False
        assert result.attempts > 0
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_routing/test_router.py -v`
Expected: FAIL — ImportError

**Step 3: Write implementation**

`src/genesis/routing/router.py`:
```python
"""Main router — walks fallback chain, delegates LLM calls."""

from __future__ import annotations

import asyncio

from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.cost_tracker import CostTracker
from genesis.routing.degradation import DegradationTracker
from genesis.routing.retry import classify_error, compute_delay
from genesis.routing.types import (
    BudgetStatus,
    CallDelegate,
    CallResult,
    RoutingConfig,
    RoutingResult,
)


class Router:
    def __init__(
        self,
        config: RoutingConfig,
        breakers: CircuitBreakerRegistry,
        cost_tracker: CostTracker,
        degradation: DegradationTracker,
        delegate: CallDelegate,
    ):
        self.config = config
        self.breakers = breakers
        self.cost_tracker = cost_tracker
        self.degradation = degradation
        self.delegate = delegate

    async def route_call(
        self,
        call_site_id: str,
        messages: list[dict],
        *,
        budget_override: bool = False,
        **kwargs,
    ) -> RoutingResult:
        """Route an LLM call through the fallback chain for a call site."""
        if call_site_id not in self.config.call_sites:
            return RoutingResult(
                success=False, call_site_id=call_site_id,
                error=f"Unknown call site: {call_site_id}",
            )

        if self.degradation.should_skip(call_site_id):
            return RoutingResult(
                success=False, call_site_id=call_site_id,
                error=f"Skipped due to degradation level {self.degradation.current_level}",
            )

        site = self.config.call_sites[call_site_id]
        policy = self.config.retry_profiles.get(
            site.retry_profile, self.config.retry_profiles["default"],
        )
        chain = self._filter_chain(site)

        attempts = 0
        for i, provider_name in enumerate(chain):
            provider = self.config.providers[provider_name]
            breaker = self.breakers.get(provider_name)

            if not breaker.is_available():
                continue

            if not provider.is_free and not budget_override:
                budget_status = await self.cost_tracker.check_budget()
                if budget_status == BudgetStatus.EXCEEDED:
                    continue

            result = await self._try_with_retry(
                provider_name, provider.model_id, messages, policy, **kwargs,
            )
            attempts += 1

            if result.success:
                breaker.record_success()
                if result.cost_usd > 0:
                    await self.cost_tracker.record(
                        call_site_id, provider_name, result,
                    )
                return RoutingResult(
                    success=True,
                    call_site_id=call_site_id,
                    provider_used=provider_name,
                    model_id=provider.model_id,
                    content=result.content,
                    attempts=attempts,
                    fallback_used=(i > 0),
                )
            else:
                category = classify_error(result.status_code, result.error or "")
                breaker.record_failure(category)

        return RoutingResult(
            success=False,
            call_site_id=call_site_id,
            attempts=attempts,
            error="All providers in chain exhausted",
        )

    def _filter_chain(self, site) -> list[str]:
        """Filter chain based on never_pays flag."""
        if site.never_pays:
            return [
                p for p in site.chain
                if self.config.providers[p].is_free
            ]
        return list(site.chain)

    async def _try_with_retry(
        self, provider: str, model_id: str, messages: list[dict],
        policy, **kwargs,
    ) -> CallResult:
        """Try a provider with retry policy. Returns last result."""
        last_result = CallResult(success=False, error="no attempts made")
        for attempt in range(policy.max_retries + 1):
            result = await self.delegate.call(
                provider, model_id, messages, **kwargs,
            )
            if result.success:
                return result
            last_result = result
            category = classify_error(result.status_code, result.error or "")
            if category.value == "permanent":
                return result
            if attempt < policy.max_retries:
                delay = compute_delay(policy, attempt)
                if result.retry_after_s:
                    delay = max(delay, result.retry_after_s)
                await asyncio.sleep(delay)
        return last_result
```

**Step 4: Run tests**

Run: `pytest tests/test_routing/test_router.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/genesis/routing/router.py tests/test_routing/conftest.py tests/test_routing/test_router.py
git commit -m "feat(routing): add main router with chain walking, budget, and degradation"
```

---

### Task 12: Package exports + integration test + final verification

**Files:**
- Modify: `src/genesis/routing/__init__.py`
- Create: `tests/test_routing/test_integration.py`

**Step 1: Update package exports**

`src/genesis/routing/__init__.py`:
```python
"""Compute routing — fallback chains, circuit breakers, cost tracking."""

from genesis.routing.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry
from genesis.routing.config import load_config, load_config_from_string
from genesis.routing.cost_tracker import CostTracker
from genesis.routing.dead_letter import DeadLetterQueue
from genesis.routing.degradation import DegradationTracker, should_skip_call_site
from genesis.routing.retry import classify_error, compute_delay
from genesis.routing.router import Router
from genesis.routing.types import (
    BudgetStatus,
    CallDelegate,
    CallResult,
    CallSiteConfig,
    DegradationLevel,
    ErrorCategory,
    ProviderConfig,
    ProviderState,
    RetryPolicy,
    RoutingConfig,
    RoutingResult,
)

__all__ = [
    "BudgetStatus",
    "CallDelegate",
    "CallResult",
    "CallSiteConfig",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CostTracker",
    "DeadLetterQueue",
    "DegradationLevel",
    "DegradationTracker",
    "ErrorCategory",
    "ProviderConfig",
    "ProviderState",
    "RetryPolicy",
    "Router",
    "RoutingConfig",
    "RoutingResult",
    "classify_error",
    "compute_delay",
    "load_config",
    "load_config_from_string",
    "should_skip_call_site",
]
```

**Step 2: Write integration test**

`tests/test_routing/test_integration.py`:
```python
"""Integration test — full routing stack with real config."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from genesis.routing import (
    CircuitBreakerRegistry,
    CostTracker,
    DeadLetterQueue,
    DegradationTracker,
    Router,
    load_config,
)
from genesis.routing.types import CallResult, ErrorCategory

from .conftest import MockDelegate


YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "model_routing.yaml"


@pytest.fixture
def full_config():
    if not YAML_PATH.exists():
        pytest.skip("Full YAML not available")
    return load_config(YAML_PATH)


async def test_full_stack_success(db, full_config):
    """Route a micro_reflection call through the full stack."""
    delegate = MockDelegate()
    breakers = CircuitBreakerRegistry(full_config.providers)
    tracker = CostTracker(db)
    degradation = DegradationTracker()
    router = Router(full_config, breakers, tracker, degradation, delegate)

    result = await router.route_call(
        "3_micro_reflection", [{"role": "user", "content": "reflect"}],
    )
    assert result.success is True
    assert result.provider_used == "groq-free"


async def test_full_stack_fallback_chain(db, full_config):
    """First provider fails, should fallback to second."""
    delegate = MockDelegate(responses={
        "groq-free": CallResult(success=False, error="rate limited", status_code=429),
    })
    breakers = CircuitBreakerRegistry(full_config.providers)
    tracker = CostTracker(db)
    degradation = DegradationTracker()
    router = Router(full_config, breakers, tracker, degradation, delegate)

    result = await router.route_call("3_micro_reflection", [])
    assert result.success is True
    assert result.provider_used == "mistral-free"
    assert result.fallback_used is True


async def test_surplus_never_pays(db, full_config):
    """Surplus brainstorm should never use paid providers."""
    all_fail = {
        name: CallResult(success=False, error="down", status_code=503)
        for name in full_config.providers
    }
    delegate = MockDelegate(responses=all_fail)
    breakers = CircuitBreakerRegistry(full_config.providers)
    tracker = CostTracker(db)
    degradation = DegradationTracker()
    router = Router(full_config, breakers, tracker, degradation, delegate)

    result = await router.route_call("12_surplus_brainstorm", [])
    assert result.success is False
    providers_called = {c["provider"] for c in delegate.calls}
    # Should only have called free providers
    for p in providers_called:
        assert full_config.providers[p].is_free is True


async def test_dead_letter_queue_lifecycle(db):
    """Enqueue → count → replay → verify replayed."""
    now = datetime.now(UTC)
    dlq = DeadLetterQueue(db, clock=lambda: now)
    await dlq.enqueue("embedding", {"vec": [0.1]}, "ollama", "timeout")
    await dlq.enqueue("memory_write", {"key": "v"}, "qdrant", "refused")
    assert await dlq.get_pending_count() == 2
    replayed = await dlq.replay_pending("qdrant")
    assert replayed == 1
    assert await dlq.get_pending_count() == 1


async def test_circuit_breaker_affects_routing(db, full_config):
    """A tripped breaker should cause the router to skip that provider."""
    delegate = MockDelegate()
    breakers = CircuitBreakerRegistry(full_config.providers)
    tracker = CostTracker(db)
    degradation = DegradationTracker()

    # Trip groq-free breaker
    cb = breakers.get("groq-free")
    for _ in range(3):
        cb.record_failure(ErrorCategory.TRANSIENT)

    router = Router(full_config, breakers, tracker, degradation, delegate)
    result = await router.route_call("3_micro_reflection", [])
    assert result.success is True
    assert result.provider_used == "mistral-free"
```

**Step 3: Run full test suite**

Run: `cd ~/genesis && ruff check . && pytest -v`
Expected: ALL tests pass (existing + new routing tests).

**Step 4: Verify test counts**

Run: `pytest tests/test_routing/ -v --tb=short`
Expected: All routing tests pass. Aim for 50+ routing tests total.

**Step 5: Commit**

```bash
git add src/genesis/routing/__init__.py tests/test_routing/test_integration.py
git commit -m "feat(routing): add package exports and integration tests — Phase 2 complete"
```

---

## Verification Checklist

After all 12 tasks:

1. `ruff check .` — no lint errors
2. `pytest -v` — all tests pass (existing Phase 0/1 + new Phase 2)
3. `config/model_routing.yaml` loads with all 22 call sites
4. Dead-letter table exists with correct schema
5. Budget seed data (daily $2, weekly $10, monthly $30) present
6. Circuit breaker state transitions work correctly
7. Router walks fallback chain, respects budget, skips tripped breakers
8. Surplus calls never use paid providers
9. Degradation tracker skips appropriate call sites at each level

## Module Dependency Graph

```
types.py (no deps)
  ↑
config.py (types, yaml)
retry.py (types)
  ↑
circuit_breaker.py (types)
cost_tracker.py (types, db/crud/cost_events, db/crud/budgets)
dead_letter.py (db/crud/dead_letter)
degradation.py (types)
  ↑
router.py (all of the above)
```
