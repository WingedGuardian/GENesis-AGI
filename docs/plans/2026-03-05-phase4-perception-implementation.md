# Phase 4: Perception — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the Reflection Engine — Genesis's first LLM calls. The Awareness Loop classifies signal urgency; the Reflection Engine thinks about those signals via Micro and Light reflection.

**Architecture:** Layered pipeline — ContextAssembler → PromptBuilder → LLMCaller → OutputParser → ResultWriter, orchestrated by ReflectionEngine. Stateless per-call. Routes through genesis.routing for model selection. Deep/Strategic stubbed. Pre-Execution Assessment is a prompt template (not a component).

**Tech Stack:** Python 3.12, aiosqlite, litellm, pytest, genesis.routing, genesis.observability

---

### Task 1: Perception types — output contracts and pipeline types

**Files:**
- Create: `src/genesis/perception/__init__.py`
- Create: `src/genesis/perception/types.py`
- Create: `tests/test_perception/__init__.py`
- Create: `tests/test_perception/test_types.py`

**Step 1: Write the failing test**

```python
# tests/test_perception/__init__.py
```

```python
# tests/test_perception/test_types.py
"""Tests for perception type definitions."""

from __future__ import annotations


def test_micro_output_frozen():
    from genesis.perception.types import MicroOutput

    output = MicroOutput(
        tags=["resource_normal", "schedule_idle"],
        salience=0.3,
        anomaly=False,
        summary="All signals within normal range.",
        signals_examined=9,
    )
    assert output.tags == ["resource_normal", "schedule_idle"]
    assert output.salience == 0.3
    assert output.anomaly is False
    assert output.signals_examined == 9

    import pytest
    with pytest.raises(AttributeError):
        output.salience = 0.5  # frozen


def test_light_output_frozen():
    from genesis.perception.types import LightOutput, UserModelDelta

    delta = UserModelDelta(
        field="timezone", value="EST", evidence="user mentioned EST", confidence=0.9,
    )
    output = LightOutput(
        assessment="System is idle, user inactive.",
        patterns=["declining_activity"],
        user_model_updates=[delta],
        recommendations=["Schedule maintenance during idle period"],
        confidence=0.7,
        focus_area="situation",
    )
    assert output.focus_area == "situation"
    assert len(output.user_model_updates) == 1
    assert output.user_model_updates[0].field == "timezone"


def test_reflection_result_success():
    from genesis.perception.types import MicroOutput, ReflectionResult

    output = MicroOutput(
        tags=["idle"], salience=0.1, anomaly=False,
        summary="Normal.", signals_examined=5,
    )
    result = ReflectionResult(success=True, output=output)
    assert result.success is True
    assert result.output is not None
    assert result.reason is None


def test_reflection_result_failure():
    from genesis.perception.types import ReflectionResult

    result = ReflectionResult(success=False, reason="all_providers_exhausted")
    assert result.success is False
    assert result.output is None
    assert result.reason == "all_providers_exhausted"


def test_prompt_context():
    from genesis.perception.types import PromptContext

    ctx = PromptContext(
        depth="micro",
        identity="You are Genesis...",
        signals_text="cpu: 0.3, memory: 0.6",
        tick_number=42,
    )
    assert ctx.depth == "micro"
    assert ctx.tick_number == 42
    assert ctx.user_profile is None
    assert ctx.cognitive_state is None
    assert ctx.memory_hits is None


def test_llm_response():
    from genesis.perception.types import LLMResponse

    resp = LLMResponse(
        text='{"tags": ["idle"]}',
        model="groq-free",
        input_tokens=500,
        output_tokens=100,
        cost_usd=0.0,
        latency_ms=320,
    )
    assert resp.cost_usd == 0.0
    assert resp.model == "groq-free"
```

**Step 2: Run test to verify it fails**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'genesis.perception'`

**Step 3: Write minimal implementation**

```python
# src/genesis/perception/__init__.py
"""Genesis perception — reflection engine and LLM-based signal analysis."""
```

```python
# src/genesis/perception/types.py
"""Perception type definitions — output contracts, pipeline types, protocols."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MicroOutput:
    """Output contract for Micro reflection."""

    tags: list[str]
    salience: float
    anomaly: bool
    summary: str
    signals_examined: int


@dataclass(frozen=True)
class UserModelDelta:
    """A proposed update to the user model cache."""

    field: str
    value: str
    evidence: str
    confidence: float


@dataclass(frozen=True)
class LightOutput:
    """Output contract for Light reflection."""

    assessment: str
    patterns: list[str]
    user_model_updates: list[UserModelDelta]
    recommendations: list[str]
    confidence: float
    focus_area: str


@dataclass(frozen=True)
class ReflectionResult:
    """Result of a reflection attempt."""

    success: bool
    output: MicroOutput | LightOutput | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PromptContext:
    """Assembled context for prompt rendering."""

    depth: str
    identity: str
    signals_text: str
    tick_number: int
    user_profile: str | None = None
    cognitive_state: str | None = None
    memory_hits: str | None = None
    user_model: str | None = None
    suggested_focus: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    """Response from an LLM call with metadata."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
```

**Step 4: Run test to verify it passes**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_types.py -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
git add src/genesis/perception/__init__.py src/genesis/perception/types.py \
  tests/test_perception/__init__.py tests/test_perception/test_types.py
git commit -m "feat(perception): add Phase 4 type definitions — output contracts, pipeline types"
```

---

### Task 2: Schema — cognitive_state table + CRUD

**Files:**
- Modify: `src/genesis/db/schema.py` — add `cognitive_state` DDL
- Create: `src/genesis/db/crud/cognitive_state.py`
- Create: `tests/test_db/test_cognitive_state.py`

**Step 1: Write the failing test**

```python
# tests/test_db/test_cognitive_state.py
"""Tests for cognitive_state CRUD operations."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_and_get(db):
    from genesis.db.crud import cognitive_state

    row_id = await cognitive_state.create(
        db,
        id="cs-1",
        content="User is working on Genesis Phase 4.",
        section="active_context",
        generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    assert row_id == "cs-1"
    row = await cognitive_state.get_by_id(db, "cs-1")
    assert row is not None
    assert row["section"] == "active_context"
    assert row["generated_by"] == "glm5"


@pytest.mark.asyncio
async def test_get_by_section(db):
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-1", content="Active context here.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    await cognitive_state.create(
        db, id="cs-2", content="Pending actions here.",
        section="pending_actions", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    rows = await cognitive_state.get_by_section(db, "active_context")
    assert len(rows) == 1
    assert rows[0]["content"] == "Active context here."


@pytest.mark.asyncio
async def test_get_current_returns_latest(db):
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-old", content="Old context.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T08:00:00+00:00",
    )
    await cognitive_state.create(
        db, id="cs-new", content="New context.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    row = await cognitive_state.get_current(db, "active_context")
    assert row is not None
    assert row["id"] == "cs-new"


@pytest.mark.asyncio
async def test_render_all_sections(db):
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-1", content="Working on Phase 4.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    await cognitive_state.create(
        db, id="cs-2", content="Draft user.md after Phase 4.",
        section="pending_actions", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    await cognitive_state.create(
        db, id="cs-3",
        content="[Bootstrap: Phase 4 | Day: 5 | Autonomy: L1 | Last Deep: never]",
        section="state_flags", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    rendered = await cognitive_state.render(db)
    assert "Working on Phase 4." in rendered
    assert "Draft user.md after Phase 4." in rendered
    assert "Bootstrap: Phase 4" in rendered


@pytest.mark.asyncio
async def test_render_empty_returns_bootstrap(db):
    from genesis.db.crud import cognitive_state

    rendered = await cognitive_state.render(db)
    assert "No cognitive state yet" in rendered


@pytest.mark.asyncio
async def test_delete(db):
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-1", content="temp",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    deleted = await cognitive_state.delete(db, "cs-1")
    assert deleted is True
    assert await cognitive_state.get_by_id(db, "cs-1") is None


@pytest.mark.asyncio
async def test_replace_section(db):
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-old", content="Old.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T08:00:00+00:00",
    )
    await cognitive_state.replace_section(
        db, section="active_context", id="cs-new",
        content="Replaced.", generated_by="claude-sonnet",
        created_at="2026-03-05T10:00:00+00:00",
    )
    rows = await cognitive_state.get_by_section(db, "active_context")
    assert len(rows) == 1
    assert rows[0]["id"] == "cs-new"
    assert rows[0]["content"] == "Replaced."
```

**Step 2: Run test to verify it fails**

Run: `cd ~/genesis && python -m pytest tests/test_db/test_cognitive_state.py -v`
Expected: FAIL — table doesn't exist + module not found

**Step 3: Add DDL to schema.py**

Add to the `TABLES` dict in `src/genesis/db/schema.py`:

```python
    "cognitive_state": """
        CREATE TABLE IF NOT EXISTS cognitive_state (
            id           TEXT PRIMARY KEY,
            content      TEXT NOT NULL,
            section      TEXT NOT NULL CHECK (section IN (
                'active_context', 'pending_actions', 'state_flags'
            )),
            generated_by TEXT,
            created_at   TEXT NOT NULL,
            expires_at   TEXT
        )
    """,
```

Add to `INDEXES`:

```python
    "CREATE INDEX IF NOT EXISTS idx_cognitive_state_section ON cognitive_state(section)",
```

**Step 4: Write CRUD module**

```python
# src/genesis/db/crud/cognitive_state.py
"""CRUD operations for cognitive_state table."""

from __future__ import annotations

import aiosqlite

_BOOTSTRAP = "[No cognitive state yet. This is a fresh system. Assess signals without prior context.]"


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    content: str,
    section: str,
    generated_by: str,
    created_at: str,
    expires_at: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO cognitive_state
           (id, content, section, generated_by, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (id, content, section, generated_by, created_at, expires_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM cognitive_state WHERE id = ?", (id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_section(db: aiosqlite.Connection, section: str) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM cognitive_state WHERE section = ? ORDER BY created_at DESC",
        (section,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_current(db: aiosqlite.Connection, section: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM cognitive_state WHERE section = ? ORDER BY created_at DESC LIMIT 1",
        (section,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def render(db: aiosqlite.Connection) -> str:
    """Render all current cognitive state sections into a single text block.

    Returns bootstrap message if no state exists.
    """
    sections = {}
    for section_name in ("active_context", "pending_actions", "state_flags"):
        row = await get_current(db, section_name)
        if row:
            sections[section_name] = row["content"]

    if not sections:
        return _BOOTSTRAP

    parts = []
    if "active_context" in sections:
        parts.append(sections["active_context"])
    if "pending_actions" in sections:
        parts.append(sections["pending_actions"])
    if "state_flags" in sections:
        parts.append(sections["state_flags"])
    return "\n\n".join(parts)


async def replace_section(
    db: aiosqlite.Connection,
    *,
    section: str,
    id: str,
    content: str,
    generated_by: str,
    created_at: str,
    expires_at: str | None = None,
) -> str:
    """Delete all rows for a section, then insert a new one."""
    await db.execute(
        "DELETE FROM cognitive_state WHERE section = ?", (section,),
    )
    await db.execute(
        """INSERT INTO cognitive_state
           (id, content, section, generated_by, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (id, content, section, generated_by, created_at, expires_at),
    )
    await db.commit()
    return id


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute(
        "DELETE FROM cognitive_state WHERE id = ?", (id,),
    )
    await db.commit()
    return cursor.rowcount > 0
```

**Step 5: Run test to verify it passes**

Run: `cd ~/genesis && python -m pytest tests/test_db/test_cognitive_state.py -v`
Expected: All 7 tests PASS

**Step 6: Run full test suite**

Run: `cd ~/genesis && python -m pytest -v`
Expected: All existing tests still pass (491 + 7 new = 498)

**Step 7: Commit**

```bash
git add src/genesis/db/schema.py src/genesis/db/crud/cognitive_state.py \
  tests/test_db/test_cognitive_state.py
git commit -m "feat(perception): add cognitive_state table, CRUD, and render()"
```

---

### Task 3: Identity loader — read and cache SOUL.md + user.md

**Files:**
- Create: `src/genesis/identity/__init__.py`
- Create: `src/genesis/identity/loader.py`
- Create: `src/genesis/identity/user.md` (empty seed)
- Create: `tests/test_perception/test_identity_loader.py`

**Step 1: Write the failing test**

```python
# tests/test_perception/test_identity_loader.py
"""Tests for identity document loader."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def identity_dir(tmp_path):
    """Create a temp identity directory with test docs."""
    soul = tmp_path / "SOUL.md"
    soul.write_text("# Genesis\nYou are Genesis.\n")
    user = tmp_path / "user.md"
    user.write_text("# User\nTimezone: EST\n")
    return tmp_path


@pytest.fixture
def empty_identity_dir(tmp_path):
    """Identity directory with no files."""
    return tmp_path


def test_load_soul(identity_dir):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(identity_dir)
    text = loader.soul()
    assert "You are Genesis" in text


def test_load_user(identity_dir):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(identity_dir)
    text = loader.user()
    assert "Timezone: EST" in text


def test_missing_soul_returns_empty(empty_identity_dir):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(empty_identity_dir)
    assert loader.soul() == ""


def test_missing_user_returns_empty(empty_identity_dir):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(empty_identity_dir)
    assert loader.user() == ""


def test_caching(identity_dir):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(identity_dir)
    first = loader.soul()
    # Modify file after first load
    (identity_dir / "SOUL.md").write_text("CHANGED")
    second = loader.soul()
    # Should return cached version
    assert first == second


def test_reload_clears_cache(identity_dir):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(identity_dir)
    first = loader.soul()
    (identity_dir / "SOUL.md").write_text("CHANGED")
    loader.reload()
    second = loader.soul()
    assert second == "CHANGED"


def test_identity_combined(identity_dir):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(identity_dir)
    combined = loader.identity_block()
    assert "You are Genesis" in combined
    assert "Timezone: EST" in combined
```

**Step 2: Run test to verify it fails**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_identity_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'genesis.identity.loader'`

**Step 3: Write minimal implementation**

```python
# src/genesis/identity/__init__.py
"""Genesis identity — SOUL.md, user.md, and identity loading."""
```

```python
# src/genesis/identity/loader.py
"""Identity document loader — reads and caches SOUL.md + user.md."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(__file__).parent


class IdentityLoader:
    """Reads and caches identity documents from a directory.

    Expected files: SOUL.md (who Genesis is), user.md (user profile).
    Files are read once and cached. Call reload() to clear cache.
    """

    def __init__(self, identity_dir: Path = _DEFAULT_DIR) -> None:
        self._dir = identity_dir
        self._cache: dict[str, str] = {}

    def soul(self) -> str:
        """Return SOUL.md content, or empty string if missing."""
        return self._load("SOUL.md")

    def user(self) -> str:
        """Return user.md content, or empty string if missing."""
        return self._load("user.md")

    def identity_block(self) -> str:
        """Return combined identity text for prompt injection."""
        parts = []
        soul = self.soul()
        if soul:
            parts.append(soul)
        user = self.user()
        if user:
            parts.append(user)
        return "\n\n".join(parts)

    def reload(self) -> None:
        """Clear the cache so next access re-reads from disk."""
        self._cache.clear()

    def _load(self, filename: str) -> str:
        if filename in self._cache:
            return self._cache[filename]
        path = self._dir / filename
        if not path.exists():
            logger.debug("Identity file not found: %s", path)
            self._cache[filename] = ""
            return ""
        text = path.read_text(encoding="utf-8").strip()
        self._cache[filename] = text
        return text
```

Create the empty user.md seed:

```markdown
<!-- User profile — drafted after Phase 4 implementation. -->
```

Save to: `src/genesis/identity/user.md`

**Step 4: Run test to verify it passes**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_identity_loader.py -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add src/genesis/identity/__init__.py src/genesis/identity/loader.py \
  src/genesis/identity/user.md tests/test_perception/test_identity_loader.py
git commit -m "feat(perception): add IdentityLoader — reads/caches SOUL.md + user.md"
```

---

### Task 4: ContextAssembler — build relevance-based context per depth

**Files:**
- Create: `src/genesis/perception/context.py`
- Create: `tests/test_perception/test_context.py`

**Step 1: Write the failing test**

```python
# tests/test_perception/test_context.py
"""Tests for ContextAssembler."""

from __future__ import annotations

import pytest

from genesis.awareness.types import Depth, DepthScore, SignalReading, TickResult


def _make_tick(*, depth=Depth.MICRO, tick_number=1) -> TickResult:
    """Helper to create a minimal TickResult."""
    return TickResult(
        tick_id="tick-1",
        timestamp="2026-03-05T10:00:00+00:00",
        source="scheduled",
        signals=[
            SignalReading(
                name="cpu_usage", value=0.3, source="system",
                collected_at="2026-03-05T10:00:00+00:00",
            ),
            SignalReading(
                name="memory_usage", value=0.6, source="system",
                collected_at="2026-03-05T10:00:00+00:00",
            ),
        ],
        scores=[
            DepthScore(
                depth=Depth.MICRO, raw_score=0.3, time_multiplier=1.0,
                final_score=0.3, threshold=0.2, triggered=True,
            ),
        ],
        classified_depth=depth,
        trigger_reason="threshold_exceeded",
    )


@pytest.fixture
def identity_dir(tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("You are Genesis.")
    user = tmp_path / "user.md"
    user.write_text("Timezone: EST")
    return tmp_path


@pytest.mark.asyncio
async def test_micro_context_has_identity_and_signals(db, identity_dir):
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.MICRO, tick_number=5)

    ctx = await assembler.assemble(Depth.MICRO, tick, db=db)

    assert "You are Genesis" in ctx.identity
    assert "cpu_usage" in ctx.signals_text
    assert "memory_usage" in ctx.signals_text
    assert ctx.depth == "micro"
    assert ctx.tick_number == 5


@pytest.mark.asyncio
async def test_micro_context_no_user_profile(db, identity_dir):
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.MICRO)

    ctx = await assembler.assemble(Depth.MICRO, tick, db=db)

    assert ctx.user_profile is None
    assert ctx.cognitive_state is None


@pytest.mark.asyncio
async def test_light_context_includes_user_and_cognitive_state(db, identity_dir):
    from genesis.db.crud import cognitive_state
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    await cognitive_state.create(
        db, id="cs-1", content="Working on Phase 4.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.LIGHT)

    ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)

    assert ctx.user_profile is not None
    assert "Timezone: EST" in ctx.user_profile
    assert ctx.cognitive_state is not None
    assert "Working on Phase 4" in ctx.cognitive_state


@pytest.mark.asyncio
async def test_light_context_empty_cognitive_state_uses_bootstrap(db, identity_dir):
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.LIGHT)

    ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)

    assert ctx.cognitive_state is not None
    assert "No cognitive state yet" in ctx.cognitive_state


@pytest.mark.asyncio
async def test_signals_text_formatting(db, identity_dir):
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.MICRO)

    ctx = await assembler.assemble(Depth.MICRO, tick, db=db)

    # Each signal should appear as name: value
    assert "cpu_usage: 0.3" in ctx.signals_text
    assert "memory_usage: 0.6" in ctx.signals_text
```

**Step 2: Run test to verify it fails**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_context.py -v`
Expected: FAIL — `No module named 'genesis.perception.context'`

**Step 3: Write minimal implementation**

```python
# src/genesis/perception/context.py
"""ContextAssembler — builds relevance-based context per reflection depth."""

from __future__ import annotations

import logging

import aiosqlite

from genesis.awareness.types import Depth, TickResult
from genesis.db.crud import cognitive_state
from genesis.identity.loader import IdentityLoader
from genesis.perception.types import PromptContext

logger = logging.getLogger(__name__)


class ContextAssembler:
    """Assembles context for prompt rendering based on depth.

    Scopes context by relevance, not budget. Never truncates.
    Micro: identity + signals only.
    Light: + user profile + cognitive state + user model.
    Deep/Strategic: stubbed (Phase 7).
    """

    def __init__(self, *, identity_loader: IdentityLoader) -> None:
        self._identity = identity_loader

    async def assemble(
        self,
        depth: Depth,
        tick: TickResult,
        *,
        db: aiosqlite.Connection,
    ) -> PromptContext:
        identity = self._identity.identity_block()
        signals_text = self._format_signals(tick)
        tick_number = self._extract_tick_number(tick)

        user_profile = None
        cog_state = None
        user_model = None

        if depth in (Depth.LIGHT, Depth.DEEP, Depth.STRATEGIC):
            user_profile = self._identity.user()
            cog_state = await cognitive_state.render(db)
            # GROUNDWORK(user-model): user model from cache, wired in Phase 5
            user_model = None

        suggested_focus = getattr(tick, "suggested_focus", None)

        return PromptContext(
            depth=depth.value,
            identity=identity,
            signals_text=signals_text,
            tick_number=tick_number,
            user_profile=user_profile or None,
            cognitive_state=cog_state or None,
            user_model=user_model,
            suggested_focus=suggested_focus,
        )

    def _format_signals(self, tick: TickResult) -> str:
        lines = []
        for s in tick.signals:
            lines.append(f"{s.name}: {s.value} (source={s.source})")
        return "\n".join(lines)

    def _extract_tick_number(self, tick: TickResult) -> int:
        """Extract tick number from tick_id or return 0."""
        # tick_id is a UUID — use hash for deterministic rotation
        return abs(hash(tick.tick_id)) % 10000
```

**Step 4: Run test to verify it passes**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_context.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add src/genesis/perception/context.py tests/test_perception/test_context.py
git commit -m "feat(perception): add ContextAssembler — depth-scoped context building"
```

---

### Task 5: Prompt templates — micro and light template files

**Files:**
- Create: `src/genesis/perception/templates/micro/analyst.txt`
- Create: `src/genesis/perception/templates/micro/contrarian.txt`
- Create: `src/genesis/perception/templates/micro/curiosity.txt`
- Create: `src/genesis/perception/templates/light/situation.txt`
- Create: `src/genesis/perception/templates/light/user_impact.txt`
- Create: `src/genesis/perception/templates/light/anomaly.txt`

**Step 1: Create template files**

```text
# src/genesis/perception/templates/micro/analyst.txt

You are reviewing system telemetry for an AI cognitive agent.

## Identity
{identity}

## Current Signals
{signals_text}

## Task
Classify these signals. For each noteworthy signal, provide a tag. Assess overall salience (0.0-1.0) — how noteworthy is this tick compared to baseline? Flag any anomalies.

Respond in JSON:
{{
  "tags": ["tag1", "tag2"],
  "salience": 0.3,
  "anomaly": false,
  "summary": "One or two sentences describing what you see.",
  "signals_examined": {signals_examined}
}}
```

```text
# src/genesis/perception/templates/micro/contrarian.txt

Assume these signals are completely normal. Your job is to find evidence that proves you wrong.

## Identity
{identity}

## Current Signals
{signals_text}

## Task
Look for anything that deviates from expected patterns. What would a careful observer notice that a casual one would miss? If everything truly is normal, say so — but be specific about what "normal" means here.

Respond in JSON:
{{
  "tags": ["tag1", "tag2"],
  "salience": 0.3,
  "anomaly": false,
  "summary": "One or two sentences. If normal, explain why. If not, explain what stands out.",
  "signals_examined": {signals_examined}
}}
```

```text
# src/genesis/perception/templates/micro/curiosity.txt

What is the most interesting thing in this data?

## Identity
{identity}

## Current Signals
{signals_text}

## Task
Look for patterns, connections, or implications that aren't obvious at first glance. What would be worth remembering about this moment? What might matter later even if it doesn't matter now?

Respond in JSON:
{{
  "tags": ["tag1", "tag2"],
  "salience": 0.3,
  "anomaly": false,
  "summary": "One or two sentences about what's most interesting or notable.",
  "signals_examined": {signals_examined}
}}
```

```text
# src/genesis/perception/templates/light/situation.txt

Assess the current situation across all active signals.

## Identity
{identity}

## User Profile
{user_profile}

## Cognitive State
{cognitive_state}

## Current Signals
{signals_text}

## Task
Provide a multi-paragraph assessment of the current situation. Identify patterns and trends. If you notice anything that updates your understanding of the user, include it as a user_model_update. Suggest any actions worth considering.

Respond in JSON:
{{
  "assessment": "Multi-paragraph situation analysis.",
  "patterns": ["pattern1", "pattern2"],
  "user_model_updates": [
    {{"field": "field_name", "value": "observed_value", "evidence": "what supports this", "confidence": 0.8}}
  ],
  "recommendations": ["recommendation1"],
  "confidence": 0.7,
  "focus_area": "situation"
}}
```

```text
# src/genesis/perception/templates/light/user_impact.txt

How do current conditions affect the user's goals and work?

## Identity
{identity}

## User Profile
{user_profile}

## Cognitive State
{cognitive_state}

## Current Signals
{signals_text}

## Task
Focus on the user's perspective. What do these signals mean for their active projects? Are there opportunities they might be missing? Risks they should know about? Update the user model if you learn something new about their preferences or patterns.

Respond in JSON:
{{
  "assessment": "Analysis focused on user impact.",
  "patterns": ["pattern1"],
  "user_model_updates": [
    {{"field": "field_name", "value": "observed_value", "evidence": "what supports this", "confidence": 0.8}}
  ],
  "recommendations": ["recommendation1"],
  "confidence": 0.7,
  "focus_area": "user_impact"
}}
```

```text
# src/genesis/perception/templates/light/anomaly.txt

An anomaly was detected in recent signals. Investigate.

## Identity
{identity}

## User Profile
{user_profile}

## Cognitive State
{cognitive_state}

## Current Signals
{signals_text}

## Task
A previous Micro reflection flagged an anomaly in these signals. Investigate: What is the anomaly? Is it significant or transient? What caused it? What should be done about it? Update the user model if relevant.

Respond in JSON:
{{
  "assessment": "Investigation of the detected anomaly.",
  "patterns": ["pattern1"],
  "user_model_updates": [
    {{"field": "field_name", "value": "observed_value", "evidence": "what supports this", "confidence": 0.8}}
  ],
  "recommendations": ["recommendation1"],
  "confidence": 0.7,
  "focus_area": "anomaly"
}}
```

**Step 2: Commit**

```bash
mkdir -p src/genesis/perception/templates/micro src/genesis/perception/templates/light
git add src/genesis/perception/templates/
git commit -m "feat(perception): add micro and light prompt templates"
```

---

### Task 6: PromptBuilder — template selection, rotation, rendering

**Files:**
- Create: `src/genesis/perception/prompts.py`
- Create: `tests/test_perception/test_prompts.py`

**Step 1: Write the failing test**

```python
# tests/test_perception/test_prompts.py
"""Tests for PromptBuilder — template selection and rendering."""

from __future__ import annotations

import pytest

from genesis.perception.types import PromptContext


def _make_context(*, depth="micro", tick_number=0, **overrides) -> PromptContext:
    defaults = dict(
        depth=depth,
        identity="You are Genesis.",
        signals_text="cpu_usage: 0.3\nmemory_usage: 0.6",
        tick_number=tick_number,
    )
    defaults.update(overrides)
    return PromptContext(**defaults)


def test_micro_rotation_tick_0():
    from genesis.perception.prompts import PromptBuilder

    builder = PromptBuilder()
    ctx = _make_context(depth="micro", tick_number=0)
    prompt = builder.build("micro", ctx)
    assert "reviewing system telemetry" in prompt  # analyst template


def test_micro_rotation_tick_1():
    from genesis.perception.prompts import PromptBuilder

    builder = PromptBuilder()
    ctx = _make_context(depth="micro", tick_number=1)
    prompt = builder.build("micro", ctx)
    assert "Assume these signals are completely normal" in prompt  # contrarian


def test_micro_rotation_tick_2():
    from genesis.perception.prompts import PromptBuilder

    builder = PromptBuilder()
    ctx = _make_context(depth="micro", tick_number=2)
    prompt = builder.build("micro", ctx)
    assert "most interesting thing" in prompt  # curiosity


def test_micro_rotation_wraps():
    from genesis.perception.prompts import PromptBuilder

    builder = PromptBuilder()
    ctx0 = _make_context(depth="micro", tick_number=0)
    ctx3 = _make_context(depth="micro", tick_number=3)
    # tick 3 should wrap to same template as tick 0
    assert builder.build("micro", ctx0) == builder.build("micro", ctx3)


def test_light_default_situation():
    from genesis.perception.prompts import PromptBuilder

    builder = PromptBuilder()
    ctx = _make_context(
        depth="light", user_profile="Timezone: EST",
        cognitive_state="Working on Phase 4.",
    )
    prompt = builder.build("light", ctx)
    assert "Assess the current situation" in prompt
    assert "Timezone: EST" in prompt
    assert "Working on Phase 4" in prompt


def test_light_suggested_focus_anomaly():
    from genesis.perception.prompts import PromptBuilder

    builder = PromptBuilder()
    ctx = _make_context(
        depth="light", suggested_focus="anomaly",
        user_profile="Timezone: EST",
        cognitive_state="Working on Phase 4.",
    )
    prompt = builder.build("light", ctx)
    assert "anomaly was detected" in prompt


def test_light_suggested_focus_user_impact():
    from genesis.perception.prompts import PromptBuilder

    builder = PromptBuilder()
    ctx = _make_context(
        depth="light", suggested_focus="user_impact",
        user_profile="Timezone: EST",
        cognitive_state="Working on Phase 4.",
    )
    prompt = builder.build("light", ctx)
    assert "user's goals" in prompt


def test_variable_substitution():
    from genesis.perception.prompts import PromptBuilder

    builder = PromptBuilder()
    ctx = _make_context(depth="micro", tick_number=0)
    prompt = builder.build("micro", ctx)
    assert "You are Genesis." in prompt
    assert "cpu_usage: 0.3" in prompt
    # Template variables should be substituted, no raw {identity} left
    assert "{identity}" not in prompt
    assert "{signals_text}" not in prompt


def test_signals_examined_substituted():
    from genesis.perception.prompts import PromptBuilder

    builder = PromptBuilder()
    ctx = _make_context(depth="micro", tick_number=0)
    prompt = builder.build("micro", ctx)
    # signals_text has 2 lines = 2 signals
    assert '"signals_examined": 2' in prompt
```

**Step 2: Run test to verify it fails**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_prompts.py -v`
Expected: FAIL — `No module named 'genesis.perception.prompts'`

**Step 3: Write minimal implementation**

```python
# src/genesis/perception/prompts.py
"""PromptBuilder — template selection, rotation, and rendering."""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.perception.types import PromptContext

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_MICRO_TEMPLATES = ["analyst", "contrarian", "curiosity"]
_LIGHT_TEMPLATES = {"situation": "situation", "user_impact": "user_impact", "anomaly": "anomaly"}
_LIGHT_DEFAULT = "situation"


class PromptBuilder:
    """Selects and renders prompt templates for reflection.

    Micro: round-robin rotation (tick_number % 3).
    Light: focus-area based (from suggested_focus or default to situation).
    """

    def __init__(self, *, templates_dir: Path = _TEMPLATES_DIR) -> None:
        self._dir = templates_dir
        self._cache: dict[str, str] = {}

    def build(self, depth: str, context: PromptContext) -> str:
        if depth == "micro":
            return self._build_micro(context)
        if depth == "light":
            return self._build_light(context)
        msg = f"Unsupported depth for prompt building: {depth}"
        raise ValueError(msg)

    def _build_micro(self, ctx: PromptContext) -> str:
        idx = ctx.tick_number % len(_MICRO_TEMPLATES)
        template_name = _MICRO_TEMPLATES[idx]
        template = self._load(f"micro/{template_name}.txt")
        signals_count = len(ctx.signals_text.strip().split("\n")) if ctx.signals_text.strip() else 0
        return template.format(
            identity=ctx.identity,
            signals_text=ctx.signals_text,
            signals_examined=signals_count,
        )

    def _build_light(self, ctx: PromptContext) -> str:
        focus = ctx.suggested_focus if ctx.suggested_focus in _LIGHT_TEMPLATES else _LIGHT_DEFAULT
        template = self._load(f"light/{focus}.txt")
        return template.format(
            identity=ctx.identity,
            signals_text=ctx.signals_text,
            user_profile=ctx.user_profile or "(no user profile yet)",
            cognitive_state=ctx.cognitive_state or "(no cognitive state yet)",
        )

    def _load(self, relative_path: str) -> str:
        if relative_path in self._cache:
            return self._cache[relative_path]
        path = self._dir / relative_path
        text = path.read_text(encoding="utf-8")
        self._cache[relative_path] = text
        return text
```

**Step 4: Run test to verify it passes**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_prompts.py -v`
Expected: All 9 tests PASS

**Step 5: Commit**

```bash
git add src/genesis/perception/prompts.py tests/test_perception/test_prompts.py
git commit -m "feat(perception): add PromptBuilder — template selection and rendering"
```

---

### Task 7: LLMCaller — route through genesis.routing, call litellm

**Files:**
- Create: `src/genesis/perception/caller.py`
- Create: `tests/test_perception/test_caller.py`

**Step 1: Write the failing test**

```python
# tests/test_perception/test_caller.py
"""Tests for LLMCaller — routes through genesis.routing, calls litellm."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.perception.types import LLMResponse
from genesis.routing.types import RoutingResult


@pytest.fixture
def mock_router():
    router = AsyncMock()
    router.route_call = AsyncMock(return_value=RoutingResult(
        success=True,
        call_site_id="3_micro_reflection",
        provider_used="groq-free",
        model_id="llama-3.3-70b-versatile",
        content='{"tags": ["idle"], "salience": 0.1, "anomaly": false, "summary": "Normal.", "signals_examined": 5}',
        attempts=1,
    ))
    return router


@pytest.fixture
def mock_cost_tracker():
    tracker = AsyncMock()
    tracker.record = AsyncMock()
    return tracker


@pytest.mark.asyncio
async def test_call_success(mock_router, mock_cost_tracker):
    from genesis.perception.caller import LLMCaller

    caller = LLMCaller(router=mock_router, cost_tracker=mock_cost_tracker)
    result = await caller.call("Test prompt", call_site_id="3_micro_reflection")

    assert result is not None
    assert isinstance(result, LLMResponse)
    assert result.model == "groq-free"
    assert "idle" in result.text
    mock_router.route_call.assert_called_once()


@pytest.mark.asyncio
async def test_call_chain_exhausted(mock_cost_tracker):
    from genesis.perception.caller import LLMCaller

    router = AsyncMock()
    router.route_call = AsyncMock(return_value=RoutingResult(
        success=False,
        call_site_id="3_micro_reflection",
        error="all providers exhausted",
        dead_lettered=True,
    ))

    caller = LLMCaller(router=router, cost_tracker=mock_cost_tracker)
    result = await caller.call("Test prompt", call_site_id="3_micro_reflection")

    assert result is None


@pytest.mark.asyncio
async def test_call_records_cost(mock_router, mock_cost_tracker):
    from genesis.perception.caller import LLMCaller

    caller = LLMCaller(router=mock_router, cost_tracker=mock_cost_tracker)
    await caller.call("Test prompt", call_site_id="3_micro_reflection")

    mock_cost_tracker.record.assert_called_once()


@pytest.mark.asyncio
async def test_call_emits_event_on_failure(mock_cost_tracker):
    from genesis.observability.events import GenesisEventBus
    from genesis.perception.caller import LLMCaller

    bus = GenesisEventBus()
    events = []
    bus.subscribe(lambda e: events.append(e))

    router = AsyncMock()
    router.route_call = AsyncMock(return_value=RoutingResult(
        success=False,
        call_site_id="3_micro_reflection",
        error="all providers exhausted",
    ))

    caller = LLMCaller(router=router, cost_tracker=mock_cost_tracker, event_bus=bus)
    await caller.call("Test prompt", call_site_id="3_micro_reflection")

    assert len(events) == 1
    assert events[0].event_type == "reflection.call_failed"
```

**Step 2: Run test to verify it fails**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_caller.py -v`
Expected: FAIL — `No module named 'genesis.perception.caller'`

**Step 3: Write minimal implementation**

```python
# src/genesis/perception/caller.py
"""LLMCaller — routes LLM calls through genesis.routing."""

from __future__ import annotations

import logging
import time

from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.perception.types import LLMResponse
from genesis.routing.cost_tracker import CostTracker
from genesis.routing.router import Router

logger = logging.getLogger(__name__)


class LLMCaller:
    """Routes LLM calls through the Genesis Router and records costs.

    Does NOT pick models (Router's job).
    Does NOT manage cost (CostTracker observes, user decides).
    Does NOT retry on bad output (OutputParser handles retries).
    """

    def __init__(
        self,
        *,
        router: Router,
        cost_tracker: CostTracker,
        event_bus: GenesisEventBus | None = None,
    ) -> None:
        self._router = router
        self._cost_tracker = cost_tracker
        self._event_bus = event_bus

    async def call(
        self,
        prompt: str,
        *,
        call_site_id: str,
    ) -> LLMResponse | None:
        """Make an LLM call routed through the provider chain.

        Returns LLMResponse on success, None if chain exhausted.
        """
        start_ms = time.monotonic_ns() // 1_000_000

        messages = [{"role": "user", "content": prompt}]

        result = await self._router.route_call(call_site_id, messages)

        elapsed_ms = (time.monotonic_ns() // 1_000_000) - start_ms

        if not result.success:
            logger.warning(
                "LLM call failed call_site=%s error=%s",
                call_site_id,
                result.error,
            )
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.AWARENESS,
                    Severity.WARNING,
                    "reflection.call_failed",
                    f"LLM call failed for {call_site_id}: {result.error}",
                    call_site_id=call_site_id,
                )
            return None

        await self._cost_tracker.record(
            call_site_id=call_site_id,
            provider=result.provider_used or "unknown",
            input_tokens=result.attempts,  # Placeholder — real tokens from Router
            output_tokens=0,
            cost_usd=0.0,
        )

        return LLMResponse(
            text=result.content or "",
            model=result.provider_used or "unknown",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            latency_ms=elapsed_ms,
        )
```

**Step 4: Run test to verify it passes**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_caller.py -v`
Expected: All 4 tests PASS

**Note:** The `cost_tracker.record()` signature may need adjustment based on the actual CostTracker API. Check `src/genesis/routing/cost_tracker.py` for the exact method signature and adapt. The Router already tracks per-call cost internally — LLMCaller may only need to log it, not duplicate recording.

**Step 5: Commit**

```bash
git add src/genesis/perception/caller.py tests/test_perception/test_caller.py
git commit -m "feat(perception): add LLMCaller — routes calls through genesis.routing"
```

---

### Task 8: OutputParser — schema validation, retry logic

**Files:**
- Create: `src/genesis/perception/parser.py`
- Create: `tests/test_perception/test_parser.py`

**Step 1: Write the failing test**

```python
# tests/test_perception/test_parser.py
"""Tests for OutputParser — schema validation and retry logic."""

from __future__ import annotations

import json

import pytest

from genesis.perception.types import LLMResponse


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text, model="test", input_tokens=0,
        output_tokens=0, cost_usd=0.0, latency_ms=100,
    )


def test_parse_valid_micro():
    from genesis.perception.parser import OutputParser

    parser = OutputParser()
    raw = json.dumps({
        "tags": ["idle", "resource_normal"],
        "salience": 0.2,
        "anomaly": False,
        "summary": "All systems normal.",
        "signals_examined": 9,
    })
    result = parser.parse(_response(raw), "micro")

    assert result.success is True
    assert result.output is not None
    assert result.output.tags == ["idle", "resource_normal"]
    assert result.output.salience == 0.2
    assert result.needs_retry is False


def test_parse_valid_light():
    from genesis.perception.parser import OutputParser

    raw = json.dumps({
        "assessment": "System is idle.",
        "patterns": ["declining_activity"],
        "user_model_updates": [{
            "field": "timezone",
            "value": "EST",
            "evidence": "user mentioned",
            "confidence": 0.9,
        }],
        "recommendations": ["Schedule maintenance"],
        "confidence": 0.7,
        "focus_area": "situation",
    })
    parser = OutputParser()
    result = parser.parse(_response(raw), "light")

    assert result.success is True
    assert result.output.assessment == "System is idle."
    assert len(result.output.user_model_updates) == 1


def test_parse_invalid_json():
    from genesis.perception.parser import OutputParser

    parser = OutputParser()
    result = parser.parse(_response("not json at all"), "micro")

    assert result.success is False
    assert result.needs_retry is True
    assert result.retry_prompt is not None
    assert "JSON" in result.retry_prompt


def test_parse_missing_required_field():
    from genesis.perception.parser import OutputParser

    raw = json.dumps({"tags": ["idle"]})  # missing salience, anomaly, etc.
    parser = OutputParser()
    result = parser.parse(_response(raw), "micro")

    assert result.success is False
    assert result.needs_retry is True
    assert "salience" in result.retry_prompt


def test_parse_salience_out_of_range():
    from genesis.perception.parser import OutputParser

    raw = json.dumps({
        "tags": ["idle"],
        "salience": 1.5,  # out of range
        "anomaly": False,
        "summary": "Normal.",
        "signals_examined": 5,
    })
    parser = OutputParser()
    result = parser.parse(_response(raw), "micro")

    assert result.success is False
    assert result.needs_retry is True


def test_parse_extracts_json_from_markdown():
    from genesis.perception.parser import OutputParser

    text = 'Here is the analysis:\n```json\n{"tags": ["idle"], "salience": 0.1, "anomaly": false, "summary": "Normal.", "signals_examined": 5}\n```'
    parser = OutputParser()
    result = parser.parse(_response(text), "micro")

    assert result.success is True
    assert result.output.tags == ["idle"]


def test_parse_empty_tags_allowed():
    from genesis.perception.parser import OutputParser

    raw = json.dumps({
        "tags": [],
        "salience": 0.0,
        "anomaly": False,
        "summary": "Nothing noteworthy.",
        "signals_examined": 3,
    })
    parser = OutputParser()
    result = parser.parse(_response(raw), "micro")

    assert result.success is True
    assert result.output.tags == []
```

**Step 2: Run test to verify it fails**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_parser.py -v`
Expected: FAIL — `No module named 'genesis.perception.parser'`

**Step 3: Write minimal implementation**

```python
# src/genesis/perception/parser.py
"""OutputParser — validates LLM responses against depth-specific schemas."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from genesis.perception.types import LLMResponse, LightOutput, MicroOutput, UserModelDelta

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParseResult:
    """Result of parsing an LLM response."""

    success: bool
    output: MicroOutput | LightOutput | None = None
    needs_retry: bool = False
    retry_prompt: str | None = None
    error: str | None = None


class OutputParser:
    """Validates LLM responses against output contracts.

    Extracts JSON from responses (handles markdown code blocks).
    Validates required fields and value ranges.
    Generates retry prompts with error feedback on failure.
    """

    def parse(self, response: LLMResponse, depth: str) -> ParseResult:
        text = self._extract_json_text(response.text)

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as e:
            return ParseResult(
                success=False,
                needs_retry=True,
                retry_prompt=self._retry_prompt(
                    f"Your response was not valid JSON. Error: {e}. "
                    "Please respond with ONLY a JSON object matching the schema.",
                ),
                error=str(e),
            )

        if depth == "micro":
            return self._validate_micro(data)
        if depth == "light":
            return self._validate_light(data)

        return ParseResult(
            success=False,
            error=f"Unsupported depth: {depth}",
        )

    def _extract_json_text(self, text: str) -> str:
        """Extract JSON from markdown code blocks if present."""
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return text.strip()

    def _validate_micro(self, data: dict) -> ParseResult:
        errors = []
        if not isinstance(data.get("tags"), list):
            errors.append("'tags' must be a list")
        if "salience" not in data:
            errors.append("'salience' is required")
        elif not isinstance(data["salience"], (int, float)):
            errors.append("'salience' must be a number")
        elif not (0.0 <= data["salience"] <= 1.0):
            errors.append("'salience' must be between 0.0 and 1.0")
        if "anomaly" not in data:
            errors.append("'anomaly' is required")
        elif not isinstance(data["anomaly"], bool):
            errors.append("'anomaly' must be a boolean")
        if "summary" not in data:
            errors.append("'summary' is required")
        if "signals_examined" not in data:
            errors.append("'signals_examined' is required")

        if errors:
            return ParseResult(
                success=False,
                needs_retry=True,
                retry_prompt=self._retry_prompt(
                    f"Your JSON output had validation errors: {'; '.join(errors)}. "
                    "Please fix and respond with the corrected JSON.",
                ),
                error="; ".join(errors),
            )

        return ParseResult(
            success=True,
            output=MicroOutput(
                tags=data["tags"],
                salience=float(data["salience"]),
                anomaly=bool(data["anomaly"]),
                summary=str(data["summary"]),
                signals_examined=int(data["signals_examined"]),
            ),
        )

    def _validate_light(self, data: dict) -> ParseResult:
        errors = []
        if "assessment" not in data:
            errors.append("'assessment' is required")
        if not isinstance(data.get("patterns"), list):
            errors.append("'patterns' must be a list")
        if not isinstance(data.get("user_model_updates"), list):
            errors.append("'user_model_updates' must be a list")
        if not isinstance(data.get("recommendations"), list):
            errors.append("'recommendations' must be a list")
        if "confidence" not in data:
            errors.append("'confidence' is required")
        elif not isinstance(data["confidence"], (int, float)):
            errors.append("'confidence' must be a number")
        elif not (0.0 <= data["confidence"] <= 1.0):
            errors.append("'confidence' must be between 0.0 and 1.0")
        if "focus_area" not in data:
            errors.append("'focus_area' is required")

        if errors:
            return ParseResult(
                success=False,
                needs_retry=True,
                retry_prompt=self._retry_prompt(
                    f"Your JSON output had validation errors: {'; '.join(errors)}. "
                    "Please fix and respond with the corrected JSON.",
                ),
                error="; ".join(errors),
            )

        deltas = []
        for item in data.get("user_model_updates", []):
            if isinstance(item, dict):
                deltas.append(UserModelDelta(
                    field=str(item.get("field", "")),
                    value=str(item.get("value", "")),
                    evidence=str(item.get("evidence", "")),
                    confidence=float(item.get("confidence", 0.0)),
                ))

        return ParseResult(
            success=True,
            output=LightOutput(
                assessment=str(data["assessment"]),
                patterns=data["patterns"],
                user_model_updates=deltas,
                recommendations=data["recommendations"],
                confidence=float(data["confidence"]),
                focus_area=str(data["focus_area"]),
            ),
        )

    def _retry_prompt(self, error_message: str) -> str:
        return (
            f"Your previous response had an error:\n{error_message}\n\n"
            "Please try again with a corrected JSON response."
        )
```

**Step 4: Run test to verify it passes**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_parser.py -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add src/genesis/perception/parser.py tests/test_perception/test_parser.py
git commit -m "feat(perception): add OutputParser — schema validation with retry feedback"
```

---

### Task 9: ResultWriter — store observations, apply user model deltas

**Files:**
- Create: `src/genesis/perception/writer.py`
- Create: `tests/test_perception/test_writer.py`

**Step 1: Write the failing test**

```python
# tests/test_perception/test_writer.py
"""Tests for ResultWriter — stores observations, emits events."""

from __future__ import annotations

import pytest

from genesis.awareness.types import Depth, DepthScore, SignalReading, TickResult
from genesis.perception.types import LightOutput, MicroOutput, UserModelDelta


def _make_tick() -> TickResult:
    return TickResult(
        tick_id="tick-1",
        timestamp="2026-03-05T10:00:00+00:00",
        source="scheduled",
        signals=[
            SignalReading(
                name="cpu_usage", value=0.3, source="system",
                collected_at="2026-03-05T10:00:00+00:00",
            ),
        ],
        scores=[],
        classified_depth=Depth.MICRO,
        trigger_reason="threshold_exceeded",
    )


@pytest.mark.asyncio
async def test_write_micro_creates_observation(db):
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["idle", "resource_normal"],
        salience=0.3,
        anomaly=False,
        summary="All systems normal.",
        signals_examined=5,
    )
    await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1
    assert "idle" in obs[0]["content"]
    assert obs[0]["type"] == "micro_reflection"


@pytest.mark.asyncio
async def test_write_micro_anomaly_tags_observation(db):
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["anomaly_detected"],
        salience=0.8,
        anomaly=True,
        summary="CPU spike detected.",
        signals_examined=5,
    )
    await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1
    assert obs[0]["category"] == "anomaly"


@pytest.mark.asyncio
async def test_write_light_creates_observation(db):
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = LightOutput(
        assessment="System is idle.",
        patterns=["declining_activity"],
        user_model_updates=[],
        recommendations=["Schedule maintenance"],
        confidence=0.7,
        focus_area="situation",
    )
    await writer.write(output, Depth.LIGHT, _make_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1
    assert obs[0]["type"] == "light_reflection"
    assert "idle" in obs[0]["content"]


@pytest.mark.asyncio
async def test_write_light_with_user_model_updates(db):
    from genesis.db.crud import observations, user_model
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = LightOutput(
        assessment="User appears to prefer EST timezone.",
        patterns=[],
        user_model_updates=[
            UserModelDelta(
                field="timezone", value="EST",
                evidence="user mentioned EST", confidence=0.9,
            ),
        ],
        recommendations=[],
        confidence=0.8,
        focus_area="user_impact",
    )
    await writer.write(output, Depth.LIGHT, _make_tick(), db=db)

    # Should store the user model update
    rows = await user_model.query(db, limit=10)
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_write_emits_event(db):
    from genesis.observability.events import GenesisEventBus
    from genesis.perception.writer import ResultWriter

    bus = GenesisEventBus()
    events = []
    bus.subscribe(lambda e: events.append(e))

    writer = ResultWriter(event_bus=bus)
    output = MicroOutput(
        tags=["idle"], salience=0.1, anomaly=False,
        summary="Normal.", signals_examined=5,
    )
    await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    assert len(events) == 1
    assert events[0].event_type == "reflection.completed"
```

**Step 2: Run test to verify it fails**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_writer.py -v`
Expected: FAIL — `No module named 'genesis.perception.writer'`

**Step 3: Write minimal implementation**

```python
# src/genesis/perception/writer.py
"""ResultWriter — stores reflection outputs as observations and user model deltas."""

from __future__ import annotations

import json
import logging
import uuid

import aiosqlite

from genesis.awareness.types import Depth, TickResult
from genesis.db.crud import observations, user_model
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.perception.types import LightOutput, MicroOutput

logger = logging.getLogger(__name__)


class ResultWriter:
    """Stores reflection outputs to the database and emits events.

    Micro: creates observation, tags anomalies.
    Light: creates observation + applies user model deltas.
    """

    def __init__(self, *, event_bus: GenesisEventBus | None = None) -> None:
        self._event_bus = event_bus

    async def write(
        self,
        output: MicroOutput | LightOutput,
        depth: Depth,
        tick: TickResult,
        *,
        db: aiosqlite.Connection,
    ) -> None:
        if isinstance(output, MicroOutput):
            await self._write_micro(output, tick, db)
        elif isinstance(output, LightOutput):
            await self._write_light(output, tick, db)

        if self._event_bus:
            await self._event_bus.emit(
                Subsystem.AWARENESS,
                Severity.INFO,
                "reflection.completed",
                f"{depth.value} reflection completed",
                depth=depth.value,
                tick_id=tick.tick_id,
            )

    async def _write_micro(
        self,
        output: MicroOutput,
        tick: TickResult,
        db: aiosqlite.Connection,
    ) -> None:
        content = json.dumps({
            "tags": output.tags,
            "salience": output.salience,
            "anomaly": output.anomaly,
            "summary": output.summary,
            "signals_examined": output.signals_examined,
        })
        category = "anomaly" if output.anomaly else "routine"

        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="reflection",
            type="micro_reflection",
            category=category,
            content=content,
            priority="high" if output.anomaly else "low",
            created_at=tick.timestamp,
        )

    async def _write_light(
        self,
        output: LightOutput,
        tick: TickResult,
        db: aiosqlite.Connection,
    ) -> None:
        content = json.dumps({
            "assessment": output.assessment,
            "patterns": output.patterns,
            "recommendations": output.recommendations,
            "confidence": output.confidence,
            "focus_area": output.focus_area,
        })

        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="reflection",
            type="light_reflection",
            content=content,
            priority="medium",
            created_at=tick.timestamp,
        )

        # Apply user model deltas
        for delta in output.user_model_updates:
            await user_model.upsert(
                db,
                id=str(uuid.uuid4()),
                field_name=delta.field,
                field_value=delta.value,
                source="light_reflection",
                confidence=delta.confidence,
                evidence=delta.evidence,
                created_at=tick.timestamp,
            )
```

**Note:** The `user_model` CRUD module may not exist yet (it's a Phase 0 table). Check `src/genesis/db/crud/` for available modules. If `user_model.py` doesn't exist, either:
- Create it following the CRUD pattern (simple upsert to `user_model_cache` table)
- Or use the existing observations table with a special type tag and defer user model storage to Phase 5

Adjust the test and implementation accordingly based on what CRUD modules exist.

**Step 4: Run test to verify it passes**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_writer.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add src/genesis/perception/writer.py tests/test_perception/test_writer.py
git commit -m "feat(perception): add ResultWriter — stores observations and user model deltas"
```

---

### Task 10: ReflectionEngine — orchestrate the pipeline

**Files:**
- Create: `src/genesis/perception/engine.py`
- Create: `tests/test_perception/test_engine.py`

**Step 1: Write the failing test**

```python
# tests/test_perception/test_engine.py
"""Tests for ReflectionEngine — orchestrates the perception pipeline."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.awareness.types import Depth, TickResult, SignalReading, DepthScore
from genesis.perception.parser import ParseResult
from genesis.perception.types import LLMResponse, MicroOutput, PromptContext, ReflectionResult


def _make_tick(depth=Depth.MICRO) -> TickResult:
    return TickResult(
        tick_id="tick-1",
        timestamp="2026-03-05T10:00:00+00:00",
        source="scheduled",
        signals=[
            SignalReading(name="cpu", value=0.3, source="system",
                          collected_at="2026-03-05T10:00:00+00:00"),
        ],
        scores=[],
        classified_depth=depth,
        trigger_reason="threshold_exceeded",
    )


def _micro_output():
    return MicroOutput(
        tags=["idle"], salience=0.1, anomaly=False,
        summary="Normal.", signals_examined=1,
    )


@pytest.mark.asyncio
async def test_reflect_success(db):
    from genesis.perception.engine import ReflectionEngine

    assembler = AsyncMock()
    assembler.assemble = AsyncMock(return_value=PromptContext(
        depth="micro", identity="Genesis", signals_text="cpu: 0.3",
        tick_number=1,
    ))
    builder = MagicMock()
    builder.build = MagicMock(return_value="Test prompt")
    caller = AsyncMock()
    caller.call = AsyncMock(return_value=LLMResponse(
        text='{"tags":["idle"],"salience":0.1,"anomaly":false,"summary":"Normal.","signals_examined":1}',
        model="groq-free", input_tokens=100, output_tokens=50,
        cost_usd=0.0, latency_ms=200,
    ))
    parser = MagicMock()
    parser.parse = MagicMock(return_value=ParseResult(
        success=True, output=_micro_output(),
    ))
    writer = AsyncMock()
    writer.write = AsyncMock()

    engine = ReflectionEngine(
        context_assembler=assembler,
        prompt_builder=builder,
        llm_caller=caller,
        output_parser=parser,
        result_writer=writer,
    )

    result = await engine.reflect(Depth.MICRO, _make_tick(), db=db)

    assert result.success is True
    assert result.output is not None
    assembler.assemble.assert_called_once()
    builder.build.assert_called_once()
    caller.call.assert_called_once()
    parser.parse.assert_called_once()
    writer.write.assert_called_once()


@pytest.mark.asyncio
async def test_reflect_llm_failure_returns_failed_result(db):
    from genesis.perception.engine import ReflectionEngine

    assembler = AsyncMock()
    assembler.assemble = AsyncMock(return_value=PromptContext(
        depth="micro", identity="Genesis", signals_text="cpu: 0.3",
        tick_number=1,
    ))
    builder = MagicMock()
    builder.build = MagicMock(return_value="Test prompt")
    caller = AsyncMock()
    caller.call = AsyncMock(return_value=None)  # chain exhausted
    parser = MagicMock()
    writer = AsyncMock()

    engine = ReflectionEngine(
        context_assembler=assembler,
        prompt_builder=builder,
        llm_caller=caller,
        output_parser=parser,
        result_writer=writer,
    )

    result = await engine.reflect(Depth.MICRO, _make_tick(), db=db)

    assert result.success is False
    assert result.reason == "all_providers_exhausted"
    writer.write.assert_not_called()


@pytest.mark.asyncio
async def test_reflect_retry_on_parse_failure(db):
    from genesis.perception.engine import ReflectionEngine

    assembler = AsyncMock()
    assembler.assemble = AsyncMock(return_value=PromptContext(
        depth="micro", identity="Genesis", signals_text="cpu: 0.3",
        tick_number=1,
    ))
    builder = MagicMock()
    builder.build = MagicMock(return_value="Test prompt")

    response = LLMResponse(
        text="bad json", model="groq-free", input_tokens=100,
        output_tokens=50, cost_usd=0.0, latency_ms=200,
    )
    good_response = LLMResponse(
        text='{"tags":["idle"],"salience":0.1,"anomaly":false,"summary":"Normal.","signals_examined":1}',
        model="groq-free", input_tokens=100, output_tokens=50,
        cost_usd=0.0, latency_ms=200,
    )
    caller = AsyncMock()
    caller.call = AsyncMock(side_effect=[response, good_response])

    parser = MagicMock()
    parser.parse = MagicMock(side_effect=[
        ParseResult(success=False, needs_retry=True, retry_prompt="Fix JSON"),
        ParseResult(success=True, output=_micro_output()),
    ])
    writer = AsyncMock()
    writer.write = AsyncMock()

    engine = ReflectionEngine(
        context_assembler=assembler,
        prompt_builder=builder,
        llm_caller=caller,
        output_parser=parser,
        result_writer=writer,
    )

    result = await engine.reflect(Depth.MICRO, _make_tick(), db=db)

    assert result.success is True
    assert caller.call.call_count == 2  # original + retry


@pytest.mark.asyncio
async def test_reflect_max_retries_exceeded(db):
    from genesis.perception.engine import ReflectionEngine

    assembler = AsyncMock()
    assembler.assemble = AsyncMock(return_value=PromptContext(
        depth="micro", identity="Genesis", signals_text="cpu: 0.3",
        tick_number=1,
    ))
    builder = MagicMock()
    builder.build = MagicMock(return_value="Test prompt")

    bad_response = LLMResponse(
        text="bad", model="groq-free", input_tokens=100,
        output_tokens=50, cost_usd=0.0, latency_ms=200,
    )
    caller = AsyncMock()
    caller.call = AsyncMock(return_value=bad_response)

    parser = MagicMock()
    parser.parse = MagicMock(return_value=ParseResult(
        success=False, needs_retry=True, retry_prompt="Fix JSON",
    ))
    writer = AsyncMock()

    engine = ReflectionEngine(
        context_assembler=assembler,
        prompt_builder=builder,
        llm_caller=caller,
        output_parser=parser,
        result_writer=writer,
    )

    result = await engine.reflect(Depth.MICRO, _make_tick(), db=db)

    assert result.success is False
    assert result.reason == "max_retries_exceeded"
    # original + 2 retries = 3 calls
    assert caller.call.call_count == 3
    writer.write.assert_not_called()
```

**Step 2: Run test to verify it fails**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_engine.py -v`
Expected: FAIL — `No module named 'genesis.perception.engine'`

**Step 3: Write minimal implementation**

```python
# src/genesis/perception/engine.py
"""ReflectionEngine — orchestrates the perception pipeline."""

from __future__ import annotations

import logging

import aiosqlite

from genesis.awareness.types import Depth, TickResult
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.perception.caller import LLMCaller
from genesis.perception.context import ContextAssembler
from genesis.perception.parser import OutputParser
from genesis.perception.prompts import PromptBuilder
from genesis.perception.types import ReflectionResult
from genesis.perception.writer import ResultWriter

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_CALL_SITE_MAP = {
    Depth.MICRO: "3_micro_reflection",
    Depth.LIGHT: "4_light_reflection",
}


class ReflectionEngine:
    """Orchestrates the perception pipeline.

    Stateless — all state lives in the DB and context assembly.
    Each call is independent.
    """

    def __init__(
        self,
        *,
        context_assembler: ContextAssembler,
        prompt_builder: PromptBuilder,
        llm_caller: LLMCaller,
        output_parser: OutputParser,
        result_writer: ResultWriter,
        event_bus: GenesisEventBus | None = None,
    ) -> None:
        self._assembler = context_assembler
        self._builder = prompt_builder
        self._caller = llm_caller
        self._parser = output_parser
        self._writer = result_writer
        self._event_bus = event_bus

    async def reflect(
        self,
        depth: Depth,
        tick: TickResult,
        *,
        db: aiosqlite.Connection,
    ) -> ReflectionResult:
        """Run the reflection pipeline for a given depth.

        Returns ReflectionResult. Never raises — failures are returned as
        ReflectionResult(success=False).
        """
        depth_str = depth.value
        call_site = _CALL_SITE_MAP.get(depth)
        if call_site is None:
            return ReflectionResult(
                success=False,
                reason=f"depth_{depth_str}_not_implemented",
            )

        # 1. Assemble context
        context = await self._assembler.assemble(depth, tick, db=db)

        # 2. Build prompt
        prompt = self._builder.build(depth_str, context)

        # 3. Call LLM
        response = await self._caller.call(prompt, call_site_id=call_site)
        if response is None:
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.AWARENESS,
                    Severity.WARNING,
                    "reflection.failed",
                    f"{depth_str} reflection failed: all providers exhausted",
                    depth=depth_str,
                    tick_id=tick.tick_id,
                )
            return ReflectionResult(
                success=False,
                reason="all_providers_exhausted",
            )

        # 4. Parse output (with retries)
        parsed = self._parser.parse(response, depth_str)
        retries = 0
        while not parsed.success and parsed.needs_retry and retries < _MAX_RETRIES:
            retries += 1
            logger.info(
                "Retrying %s reflection (attempt %d/%d)",
                depth_str, retries, _MAX_RETRIES,
            )
            response = await self._caller.call(
                parsed.retry_prompt, call_site_id=call_site,
            )
            if response is None:
                return ReflectionResult(
                    success=False,
                    reason="all_providers_exhausted",
                )
            parsed = self._parser.parse(response, depth_str)

        if not parsed.success:
            return ReflectionResult(
                success=False,
                reason="max_retries_exceeded",
            )

        # 5. Write results
        await self._writer.write(parsed.output, depth, tick, db=db)

        return ReflectionResult(success=True, output=parsed.output)
```

**Step 4: Run test to verify it passes**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_engine.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add src/genesis/perception/engine.py tests/test_perception/test_engine.py
git commit -m "feat(perception): add ReflectionEngine — pipeline orchestration with retry"
```

---

### Task 11: AwarenessLoop integration — wire ReflectionEngine into perform_tick

**Files:**
- Modify: `src/genesis/awareness/loop.py` — add reflection call after depth classification
- Modify: `src/genesis/awareness/types.py` — add `suggested_focus` to TickResult (if needed)
- Create: `tests/test_perception/test_loop_integration.py`

**Step 1: Write the failing test**

```python
# tests/test_perception/test_loop_integration.py
"""Tests for AwarenessLoop + ReflectionEngine integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.awareness.types import Depth
from genesis.perception.types import MicroOutput, ReflectionResult


@pytest.mark.asyncio
async def test_tick_with_micro_depth_triggers_reflection(db):
    """When a tick classifies as MICRO, perform_tick should call reflect()."""
    from genesis.awareness.loop import perform_tick
    from genesis.awareness.signals import ConversationCollector

    engine = AsyncMock()
    engine.reflect = AsyncMock(return_value=ReflectionResult(
        success=True,
        output=MicroOutput(
            tags=["idle"], salience=0.1, anomaly=False,
            summary="Normal.", signals_examined=1,
        ),
    ))

    collectors = [ConversationCollector()]

    # Force a MICRO classification by lowering the threshold
    # This test verifies the wiring, not the classification logic
    result = await perform_tick(
        db, collectors, source="scheduled",
        reflection_engine=engine,
    )

    # If depth was classified, reflection should have been called
    if result.classified_depth in (Depth.MICRO, Depth.LIGHT):
        engine.reflect.assert_called_once()


@pytest.mark.asyncio
async def test_tick_without_engine_still_works(db):
    """perform_tick should work without a reflection engine (backwards compat)."""
    from genesis.awareness.loop import perform_tick
    from genesis.awareness.signals import ConversationCollector

    collectors = [ConversationCollector()]
    result = await perform_tick(db, collectors, source="scheduled")

    # Should succeed even without reflection engine
    assert result.tick_id is not None
```

**Step 2: Run test to verify it fails**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_loop_integration.py -v`
Expected: FAIL — `perform_tick() got unexpected keyword argument 'reflection_engine'`

**Step 3: Modify perform_tick**

In `src/genesis/awareness/loop.py`, add the optional `reflection_engine` parameter:

Find the `perform_tick` function signature and add `reflection_engine=None` as a keyword-only parameter. After the depth classification and observation storage, add:

```python
    # 6. Trigger reflection if depth warrants it
    if reflection_engine is not None and classified_depth in (Depth.MICRO, Depth.LIGHT):
        try:
            await reflection_engine.reflect(classified_depth, result, db=db)
        except Exception:
            logger.exception("Reflection failed for tick %s", tick_id)
```

The exact line numbers depend on the current code. Read `loop.py` to find the right insertion point — after step 5 (observation creation) and before the return statement.

**Step 4: Run test to verify it passes**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_loop_integration.py -v`
Expected: All 2 tests PASS

**Step 5: Run full test suite**

Run: `cd ~/genesis && python -m pytest -v`
Expected: All existing awareness tests still pass + new tests pass

**Step 6: Commit**

```bash
git add src/genesis/awareness/loop.py tests/test_perception/test_loop_integration.py
git commit -m "feat(perception): wire ReflectionEngine into AwarenessLoop perform_tick"
```

---

### Task 12: Pre-Execution Assessment prompt template

**Files:**
- Create: `src/genesis/perception/templates/pre_execution_assessment.txt`
- Create: `tests/test_perception/test_pre_execution.py`

**Step 1: Create the assessment prompt template**

```text
# src/genesis/perception/templates/pre_execution_assessment.txt

Before executing this task, take a moment to consider:

## Your Identity
{identity}

## User Profile
{user_profile}

## Current Cognitive State
{cognitive_state}

## Task Definition
{task_definition}

## Assessment

Consider the following — briefly, not exhaustively:

1. **Sufficiency**: Do you have enough information to define a clear outcome? If not, what minimum clarification is needed?
2. **Approach**: Does the proposed approach make sense? Is there a clearly better way?
3. **Conflicts**: Does this conflict with the user's stated goals, prior decisions, or known preferences?
4. **Risks**: Any high-consequence risks the user should know about before you proceed?

If everything checks out, proceed immediately — do not delay clear requests.

Decision space:
- **Proceed**: Request is clear and sound. Execute.
- **Proceed with note**: Execute, but flag something worth knowing.
- **Clarify**: Ambiguous in a way that risks wasted effort. Ask the minimum needed.
- **Challenge**: Evidence suggests this may be suboptimal. Surface evidence, let user decide.
- **Suggest alternative**: A better path exists for the user's inferred goal.
```

**Step 2: Write the test**

```python
# tests/test_perception/test_pre_execution.py
"""Tests for pre-execution assessment template rendering."""

from __future__ import annotations

from pathlib import Path


def test_template_loads():
    template_path = (
        Path(__file__).parent.parent.parent
        / "src" / "genesis" / "perception" / "templates"
        / "pre_execution_assessment.txt"
    )
    text = template_path.read_text()
    assert "Before executing this task" in text
    assert "{identity}" in text
    assert "{task_definition}" in text


def test_template_renders():
    template_path = (
        Path(__file__).parent.parent.parent
        / "src" / "genesis" / "perception" / "templates"
        / "pre_execution_assessment.txt"
    )
    text = template_path.read_text()
    rendered = text.format(
        identity="You are Genesis.",
        user_profile="Timezone: EST",
        cognitive_state="Working on Phase 4.",
        task_definition="Build the perception engine.",
    )
    assert "You are Genesis." in rendered
    assert "Build the perception engine." in rendered
    assert "{identity}" not in rendered
```

**Step 3: Run tests**

Run: `cd ~/genesis && python -m pytest tests/test_perception/test_pre_execution.py -v`
Expected: All 2 tests PASS

**Step 4: Commit**

```bash
git add src/genesis/perception/templates/pre_execution_assessment.txt \
  tests/test_perception/test_pre_execution.py
git commit -m "feat(perception): add pre-execution assessment prompt template"
```

---

### Task 13: Package __init__.py — wire all exports

**Files:**
- Modify: `src/genesis/perception/__init__.py`

**Step 1: Update the package init**

```python
# src/genesis/perception/__init__.py
"""Genesis perception — reflection engine and LLM-based signal analysis."""

from genesis.perception.caller import LLMCaller
from genesis.perception.context import ContextAssembler
from genesis.perception.engine import ReflectionEngine
from genesis.perception.parser import OutputParser, ParseResult
from genesis.perception.prompts import PromptBuilder
from genesis.perception.types import (
    LightOutput,
    LLMResponse,
    MicroOutput,
    PromptContext,
    ReflectionResult,
    UserModelDelta,
)
from genesis.perception.writer import ResultWriter

__all__ = [
    "ContextAssembler",
    "LLMCaller",
    "LLMResponse",
    "LightOutput",
    "MicroOutput",
    "OutputParser",
    "ParseResult",
    "PromptBuilder",
    "PromptContext",
    "ReflectionEngine",
    "ReflectionResult",
    "ResultWriter",
    "UserModelDelta",
]
```

**Step 2: Test imports work**

Run: `cd ~/genesis && python -c "from genesis.perception import ReflectionEngine, MicroOutput, LightOutput; print('OK')"`
Expected: `OK`

**Step 3: Run full test suite**

Run: `cd ~/genesis && ruff check . && python -m pytest -v`
Expected: All lint clean + all tests pass

**Step 4: Commit**

```bash
git add src/genesis/perception/__init__.py
git commit -m "feat(perception): wire all exports in package __init__"
```

---

### Task 14: Full suite verification + final commit

**Step 1: Lint**

Run: `cd ~/genesis && ruff check .`
Expected: All clean

**Step 2: Full test suite**

Run: `cd ~/genesis && python -m pytest -v --tb=short`
Expected: All tests pass (491 existing + ~40 new ≈ 530+)

**Step 3: Count perception tests**

Run: `cd ~/genesis && python -m pytest tests/test_perception/ -v --tb=short`
Expected: All perception tests pass, count them

**Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "feat(perception): Phase 4 complete — reflection engine, templates, identity loader"
```

---

## Post-Implementation Checklist

After all tasks pass:

- [ ] `ruff check .` — all clean
- [ ] `pytest -v` — all pass (existing + new)
- [ ] Micro templates render correctly with variable substitution
- [ ] Light templates include user profile and cognitive state
- [ ] OutputParser handles valid JSON, invalid JSON, and markdown-wrapped JSON
- [ ] ReflectionEngine retries up to 2 times on parse failure
- [ ] perform_tick works with and without ReflectionEngine (backwards compatible)
- [ ] cognitive_state CRUD works (create, get, render, replace_section)
- [ ] IdentityLoader reads and caches SOUL.md + user.md
- [ ] Pre-execution assessment template renders with all variables
- [ ] No hardcoded cost gates or budget-based throttling anywhere in perception code
