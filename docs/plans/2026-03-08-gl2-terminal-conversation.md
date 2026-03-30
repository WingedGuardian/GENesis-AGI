# GL-2: Terminal Conversation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable Genesis conversation via terminal — full identity system prompt, CC session persistence via `--resume`, morning reset, intent parsing.

**Architecture:** A `ConversationLoop` class orchestrates the flow: user text → IntentParser → SessionManager → CCInvoker → ResponseFormatter → output. The terminal entry point (`python -m genesis.cc.terminal`) wraps this with stdin/stdout. Session persistence uses the CC CLI's `session_id` (stored in a new `cc_session_id` column) for `--resume`. System prompt assembled at runtime from SOUL.md + USER.md + CONVERSATION.md + cognitive state.

**Tech Stack:** Python 3.12, asyncio, aiosqlite, existing `genesis.cc` package, `genesis.identity` package

**Design Doc:** `docs/plans/2026-03-07-agentic-runtime-design.md` (sections 4.1, 6.1-6.3)

---

## Task 1: Schema + CRUD — add cc_session_id to cc_sessions

**Files:**
- Modify: `src/genesis/db/schema.py:360-380`
- Modify: `src/genesis/db/crud/cc_sessions.py`
- Modify: `tests/test_db/test_cc_sessions.py`

**Step 1: Write the failing tests**

Add these tests to the end of `tests/test_db/test_cc_sessions.py`:

```python
async def test_update_cc_session_id(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    ok = await cc_sessions.update_cc_session_id(
        db, "sess-1", cc_session_id="cc-cli-uuid-123")
    assert ok
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["cc_session_id"] == "cc-cli-uuid-123"


async def test_cc_session_id_null_by_default(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["cc_session_id"] is None
```

**Step 2: Run tests to verify they fail**

Run: `cd ~/genesis && python -m pytest tests/test_db/test_cc_sessions.py -v`
Expected: `OperationalError` or `KeyError` for `cc_session_id`

**Step 3: Implement**

Add `cc_session_id TEXT` column to `src/genesis/db/schema.py` in the `cc_sessions` table definition, after the `metadata` line:

```python
"cc_sessions": """
    CREATE TABLE IF NOT EXISTS cc_sessions (
        id               TEXT PRIMARY KEY,
        session_type     TEXT NOT NULL CHECK (session_type IN (
            'foreground', 'background_reflection', 'background_task'
        )),
        user_id          TEXT,
        channel          TEXT,
        model            TEXT NOT NULL,
        effort           TEXT NOT NULL DEFAULT 'medium',
        status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
            'active', 'checkpointed', 'completed', 'failed', 'expired'
        )),
        pid              INTEGER,
        started_at       TEXT NOT NULL,
        last_activity_at TEXT NOT NULL,
        checkpointed_at  TEXT,
        completed_at     TEXT,
        source_tag       TEXT NOT NULL DEFAULT 'foreground',
        metadata         TEXT,
        cc_session_id    TEXT
    )
""",
```

Add this function to `src/genesis/db/crud/cc_sessions.py`:

```python
async def update_cc_session_id(
    db: aiosqlite.Connection,
    id: str,
    *,
    cc_session_id: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE cc_sessions SET cc_session_id = ? WHERE id = ?",
        (cc_session_id, id),
    )
    await db.commit()
    return cursor.rowcount > 0
```

**Step 4: Run tests to verify they pass**

Run: `cd ~/genesis && python -m pytest tests/test_db/test_cc_sessions.py -v`
Expected: All tests PASS (existing + 2 new)

**Step 5: Commit**

```bash
git add src/genesis/db/schema.py src/genesis/db/crud/cc_sessions.py tests/test_db/test_cc_sessions.py
git commit -m "feat: add cc_session_id column to cc_sessions for CLI resume tracking"
```

---

## Task 2: CONVERSATION.md + SystemPromptAssembler

**Files:**
- Create: `src/genesis/identity/CONVERSATION.md`
- Create: `src/genesis/cc/system_prompt.py`
- Create: `tests/test_cc/test_system_prompt.py`

**Step 1: Write the failing tests**

```python
# tests/test_cc/test_system_prompt.py
"""Tests for SystemPromptAssembler."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from genesis.cc.system_prompt import SystemPromptAssembler


@pytest.fixture
def assembler(tmp_path):
    """Assembler with identity files in a temp dir."""
    (tmp_path / "SOUL.md").write_text("You are Genesis.")
    (tmp_path / "USER.md").write_text("User prefers concise answers.")
    (tmp_path / "CONVERSATION.md").write_text("Respond naturally.")
    return SystemPromptAssembler(identity_dir=tmp_path)


@pytest.fixture
def assembler_no_user(tmp_path):
    """Assembler with no USER.md."""
    (tmp_path / "SOUL.md").write_text("You are Genesis.")
    (tmp_path / "CONVERSATION.md").write_text("Respond naturally.")
    return SystemPromptAssembler(identity_dir=tmp_path)


def test_assemble_sync_parts(assembler):
    """Static parts (no DB) are included."""
    result = assembler.assemble_static()
    assert "You are Genesis." in result
    assert "User prefers concise answers." in result
    assert "Respond naturally." in result


def test_assemble_without_user_profile(assembler_no_user):
    """Missing USER.md is handled gracefully."""
    result = assembler_no_user.assemble_static()
    assert "You are Genesis." in result
    assert "Respond naturally." in result


@pytest.mark.asyncio
async def test_assemble_includes_cognitive_state(assembler, db):
    """Full assembly includes cognitive state from DB."""
    from genesis.db.crud import cognitive_state

    await cognitive_state.replace_section(
        db, section="active_context", id="cs-1",
        content="Working on vehicle registration.",
        generated_by="test", created_at="2026-03-08T12:00:00",
    )
    result = await assembler.assemble(db=db)
    assert "You are Genesis." in result
    assert "Working on vehicle registration." in result


@pytest.mark.asyncio
async def test_assemble_with_empty_cognitive_state(assembler, db):
    """Assembly works when cognitive state is empty (fresh system)."""
    result = await assembler.assemble(db=db)
    assert "You are Genesis." in result
    # Should include bootstrap text or graceful empty state
    assert len(result) > 50


def test_assemble_includes_date(assembler):
    """Static parts include current date."""
    result = assembler.assemble_static()
    assert "Date:" in result


def test_sections_are_separated(assembler):
    """Sections are cleanly separated."""
    result = assembler.assemble_static()
    assert "---" in result
```

**Step 2: Run tests to verify they fail**

Run: `cd ~/genesis && python -m pytest tests/test_cc/test_system_prompt.py -v`
Expected: `ModuleNotFoundError: No module named 'genesis.cc.system_prompt'`

**Step 3: Implement**

Create `src/genesis/identity/CONVERSATION.md`:

```markdown
# Conversation Mode

You are in a direct conversation with your user. This is foreground interaction —
real-time, back-and-forth dialogue.

## How to Respond

- Be concise by default. Elaborate when the topic warrants it or the user asks.
- Respond naturally — don't preface with "As Genesis" or "I think."
- Use markdown formatting where it helps readability.
- Challenge weak reasoning, but don't nitpick trivial things.
- If you notice something that updates your understanding of the user, remember it.
- If a request would benefit from structured task execution, suggest it.

## What You Have Access To

Your identity, drives, and constraints are defined above. Your current understanding
of the user and recent cognitive state are provided for context. Use them to inform
your responses — don't recite them.

## Decision Making

Most requests are straightforward — just do them well. But when something doesn't
add up, or when a request has high-consequence implications, pause and surface it.
The user decides; you provide the information and judgment to decide well.
```

Create `src/genesis/cc/system_prompt.py`:

```python
"""SystemPromptAssembler — builds the full CC foreground system prompt."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from genesis.identity.loader import IdentityLoader

_DEFAULT_IDENTITY_DIR = Path(__file__).resolve().parent.parent / "identity"


class SystemPromptAssembler:
    """Assembles system prompt from identity files + dynamic DB state.

    Static parts: SOUL.md, USER.md, CONVERSATION.md, current date.
    Dynamic parts: cognitive state from DB (requires async).
    """

    def __init__(self, *, identity_dir: Path = _DEFAULT_IDENTITY_DIR) -> None:
        self._loader = IdentityLoader(identity_dir)
        self._conversation_path = identity_dir / "CONVERSATION.md"

    def _load_conversation_instructions(self) -> str:
        if self._conversation_path.exists():
            return self._conversation_path.read_text(encoding="utf-8").strip()
        return ""

    def assemble_static(self) -> str:
        """Assemble the static portions (no DB access needed)."""
        parts = []

        soul = self._loader.soul()
        if soul:
            parts.append(soul)

        parts.append("---")

        user = self._loader.user()
        if user:
            parts.append(f"## User Profile\n\n{user}")
            parts.append("---")

        now = datetime.now(UTC)
        parts.append(f"Date: {now.strftime('%Y-%m-%d %H:%M UTC')}")

        parts.append("---")

        conversation = self._load_conversation_instructions()
        if conversation:
            parts.append(conversation)

        return "\n\n".join(parts)

    async def assemble(self, *, db) -> str:
        """Assemble full system prompt including dynamic cognitive state."""
        from genesis.db.crud import cognitive_state

        static = self.assemble_static()

        cog_state = await cognitive_state.render(db)
        if cog_state:
            return f"{static}\n\n---\n\n## Current Cognitive State\n\n{cog_state}"

        return static
```

**Step 4: Run tests to verify they pass**

Run: `cd ~/genesis && python -m pytest tests/test_cc/test_system_prompt.py -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add src/genesis/identity/CONVERSATION.md src/genesis/cc/system_prompt.py tests/test_cc/test_system_prompt.py
git commit -m "feat: add CONVERSATION.md + SystemPromptAssembler for foreground CC"
```

---

## Task 3: ConversationLoop

**Files:**
- Create: `src/genesis/cc/conversation.py`
- Create: `tests/test_cc/test_conversation.py`

**Step 1: Write the failing tests**

```python
# tests/test_cc/test_conversation.py
"""Tests for ConversationLoop."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from genesis.cc.conversation import ConversationLoop
from genesis.cc.formatter import ResponseFormatter
from genesis.cc.intent import IntentParser
from genesis.cc.session_manager import SessionManager
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.cc.types import CCModel, CCOutput, ChannelType, EffortLevel
from genesis.db.crud import cc_sessions


@pytest.fixture
def mock_invoker():
    invoker = AsyncMock()
    invoker.run = AsyncMock(return_value=CCOutput(
        session_id="cc-cli-session-1",
        text="Hello! I'm Genesis.",
        model_used="claude-sonnet-4-6",
        cost_usd=0.05,
        input_tokens=500,
        output_tokens=50,
        duration_ms=2000,
        exit_code=0,
    ))
    return invoker


@pytest.fixture
def assembler(tmp_path):
    (tmp_path / "SOUL.md").write_text("You are Genesis.")
    (tmp_path / "CONVERSATION.md").write_text("Be concise.")
    return SystemPromptAssembler(identity_dir=tmp_path)


@pytest.fixture
async def loop(db, mock_invoker, assembler):
    session_mgr = SessionManager(db=db, invoker=mock_invoker)
    return ConversationLoop(
        db=db,
        invoker=mock_invoker,
        session_manager=session_mgr,
        prompt_assembler=assembler,
    )


@pytest.mark.asyncio
async def test_first_message_creates_session(db, loop, mock_invoker):
    """First message creates a new session and includes system prompt."""
    response = await loop.handle_message(
        "Hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert response == "Hello! I'm Genesis."

    # Session was created in DB
    row = await cc_sessions.get_active_foreground(
        db, user_id="u1", channel="terminal")
    assert row is not None

    # CCInvoker was called with system prompt (first message)
    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.system_prompt is not None
    assert "Genesis" in call_args.system_prompt
    assert call_args.resume_session_id is None


@pytest.mark.asyncio
async def test_second_message_resumes(db, loop, mock_invoker):
    """Second message uses --resume with stored cc_session_id."""
    await loop.handle_message(
        "Hello", user_id="u1", channel=ChannelType.TERMINAL)
    await loop.handle_message(
        "Follow up", user_id="u1", channel=ChannelType.TERMINAL)

    # Second call should use resume
    second_call = mock_invoker.run.call_args[0][0]
    assert second_call.resume_session_id == "cc-cli-session-1"
    assert second_call.system_prompt is None


@pytest.mark.asyncio
async def test_cc_session_id_stored(db, loop, mock_invoker):
    """CC CLI session_id is stored in the DB after first response."""
    await loop.handle_message(
        "Hello", user_id="u1", channel=ChannelType.TERMINAL)

    row = await cc_sessions.get_active_foreground(
        db, user_id="u1", channel="terminal")
    assert row["cc_session_id"] == "cc-cli-session-1"


@pytest.mark.asyncio
async def test_model_override_via_intent(db, loop, mock_invoker):
    """/model opus changes the model for the invocation."""
    await loop.handle_message(
        "/model opus explain quantum computing",
        user_id="u1", channel=ChannelType.TERMINAL)

    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.model == CCModel.OPUS


@pytest.mark.asyncio
async def test_effort_override_via_intent(db, loop, mock_invoker):
    """/effort high changes the effort level."""
    await loop.handle_message(
        "/effort high analyze this deeply",
        user_id="u1", channel=ChannelType.TERMINAL)

    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.effort == EffortLevel.HIGH


@pytest.mark.asyncio
async def test_morning_reset_creates_fresh_session(db, loop, mock_invoker):
    """After morning reset, a new session is created without resume."""
    # Create an old session from yesterday
    await cc_sessions.create(
        db, id="old-sess", session_type="foreground",
        model="sonnet", effort="medium", status="active",
        user_id="u1", channel="terminal",
        started_at="2026-03-07T08:00:00+00:00",
        last_activity_at="2026-03-07T23:00:00+00:00",
        cc_session_id="old-cc-id",
    )
    response = await loop.handle_message(
        "Good morning", user_id="u1", channel=ChannelType.TERMINAL)

    # Old session should be completed
    old = await cc_sessions.get_by_id(db, "old-sess")
    assert old["status"] == "completed"

    # New invocation should NOT resume
    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.resume_session_id is None
    assert call_args.system_prompt is not None


@pytest.mark.asyncio
async def test_cc_error_returns_error_message(db, loop, mock_invoker):
    """CC invocation error returns a user-friendly message."""
    mock_invoker.run = AsyncMock(return_value=CCOutput(
        session_id="", text="", model_used="sonnet",
        cost_usd=0, input_tokens=0, output_tokens=0,
        duration_ms=100, exit_code=1,
        is_error=True, error_message="CLI crashed",
    ))
    response = await loop.handle_message(
        "Hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert "error" in response.lower() or "CLI crashed" in response


@pytest.mark.asyncio
async def test_empty_response_handled(db, loop, mock_invoker):
    """Empty CC response is handled gracefully."""
    mock_invoker.run = AsyncMock(return_value=CCOutput(
        session_id="cc-1", text="", model_used="sonnet",
        cost_usd=0, input_tokens=10, output_tokens=0,
        duration_ms=100, exit_code=0,
    ))
    response = await loop.handle_message(
        "Hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert isinstance(response, str)
```

**Step 2: Run tests to verify they fail**

Run: `cd ~/genesis && python -m pytest tests/test_cc/test_conversation.py -v`
Expected: `ModuleNotFoundError: No module named 'genesis.cc.conversation'`

**Step 3: Implement**

Create `src/genesis/cc/conversation.py`:

```python
"""ConversationLoop — orchestrates user ↔ CC message flow."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from genesis.cc.formatter import ResponseFormatter
from genesis.cc.intent import IntentParser
from genesis.cc.session_manager import SessionManager
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.cc.types import CCInvocation, CCModel, ChannelType, EffortLevel
from genesis.db.crud import cc_sessions

logger = logging.getLogger(__name__)


class ConversationLoop:
    """Handles the message flow: parse → session → invoke → format.

    Not responsible for I/O — the caller (terminal, Telegram relay, etc.)
    handles reading input and displaying output.
    """

    def __init__(
        self,
        *,
        db,
        invoker,
        session_manager: SessionManager,
        prompt_assembler: SystemPromptAssembler,
        intent_parser: IntentParser | None = None,
        formatter: ResponseFormatter | None = None,
        day_boundary_hour: int = 0,
    ) -> None:
        self._db = db
        self._invoker = invoker
        self._session_manager = session_manager
        self._prompt_assembler = prompt_assembler
        self._intent_parser = intent_parser or IntentParser()
        self._formatter = formatter or ResponseFormatter()
        self._day_boundary_hour = day_boundary_hour

    async def handle_message(
        self,
        text: str,
        *,
        user_id: str,
        channel: ChannelType | str,
    ) -> str:
        """Process a user message and return the response text.

        Handles intent parsing, session management, CC invocation,
        morning reset, and response formatting.
        """
        channel = ChannelType(channel) if isinstance(channel, str) else channel

        # 1. Parse intent
        intent = self._intent_parser.parse(text)
        model = intent.model_override or CCModel.SONNET
        effort = intent.effort_override or EffortLevel.MEDIUM
        prompt = intent.cleaned_text or intent.raw_text

        # 2. Check for morning reset on existing session
        existing = await cc_sessions.get_active_foreground(
            self._db, user_id=user_id, channel=str(channel),
        )
        if existing and self._should_reset(existing):
            logger.info(
                "Morning reset: completing session %s", existing["id"])
            await self._session_manager.complete(existing["id"])
            existing = None

        # 3. Get or create session
        session = await self._session_manager.get_or_create_foreground(
            user_id=user_id, channel=channel, model=model, effort=effort,
        )

        # 4. Build invocation
        cc_session_id = session.get("cc_session_id")
        system_prompt = None
        if not cc_session_id:
            # First message in session — include full system prompt
            system_prompt = await self._prompt_assembler.assemble(db=self._db)

        invocation = CCInvocation(
            prompt=prompt,
            model=model,
            effort=effort,
            system_prompt=system_prompt,
            resume_session_id=cc_session_id,
        )

        # 5. Invoke CC
        output = await self._invoker.run(invocation)

        # 6. Handle errors
        if output.is_error:
            logger.error("CC invocation failed: %s", output.error_message)
            return f"[Genesis error: {output.error_message}]"

        # 7. Store CC session ID (first response only)
        if output.session_id and not cc_session_id:
            await cc_sessions.update_cc_session_id(
                self._db, session["id"],
                cc_session_id=output.session_id,
            )

        # 8. Update activity timestamp
        await self._session_manager.update_activity(session["id"])

        # 9. Format and return
        if not output.text:
            return "(no response)"
        chunks = self._formatter.format(output.text, channel=channel)
        return "\n".join(chunks)

    def _should_reset(self, session: dict) -> bool:
        """Check if a session should be reset (day boundary crossed)."""
        started_str = session.get("started_at", "")
        if not started_str:
            return False
        try:
            started = datetime.fromisoformat(started_str)
            # Ensure timezone-aware comparison
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        except ValueError:
            return False

        now = datetime.now(UTC)
        boundary = now.replace(
            hour=self._day_boundary_hour, minute=0, second=0, microsecond=0,
            tzinfo=UTC,
        )
        if now < boundary:
            boundary -= timedelta(days=1)

        return started < boundary
```

**Step 4: Run tests to verify they pass**

Run: `cd ~/genesis && python -m pytest tests/test_cc/test_conversation.py -v`
Expected: All 9 tests PASS

**Step 5: Commit**

```bash
git add src/genesis/cc/conversation.py tests/test_cc/test_conversation.py
git commit -m "feat: add ConversationLoop — orchestrates user ↔ CC message flow"
```

---

## Task 4: Terminal entry point

**Files:**
- Create: `src/genesis/cc/terminal.py`

**Step 1: Implement**

This is a thin I/O wrapper. No TDD needed — it delegates to the tested `ConversationLoop`.

Create `src/genesis/cc/terminal.py`:

```python
"""Terminal conversation entry point.

Usage: python -m genesis.cc.terminal [--user USER_ID] [--boundary-hour HOUR]

Runs an interactive Genesis conversation in the terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from genesis.cc.conversation import ConversationLoop
from genesis.cc.invoker import CCInvoker
from genesis.cc.session_manager import SessionManager
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.cc.types import ChannelType
from genesis.db.connection import init_db

logger = logging.getLogger(__name__)


async def run_terminal(*, user_id: str = "default", day_boundary_hour: int = 0):
    """Run the terminal conversation loop."""
    # Initialize infrastructure
    db = await init_db()
    invoker = CCInvoker()
    session_mgr = SessionManager(
        db=db, invoker=invoker, day_boundary_hour=day_boundary_hour)
    assembler = SystemPromptAssembler()

    loop = ConversationLoop(
        db=db,
        invoker=invoker,
        session_manager=session_mgr,
        prompt_assembler=assembler,
        day_boundary_hour=day_boundary_hour,
    )

    print("Genesis Terminal (type 'exit' or Ctrl+D to quit)")
    print("Commands: /model [sonnet|opus|haiku], /effort [low|medium|high]")
    print("-" * 60)

    while True:
        try:
            text = input("\nyou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not text:
            continue
        if text.lower() in ("exit", "quit", "/exit", "/quit"):
            print("Goodbye.")
            break

        try:
            response = await loop.handle_message(
                text, user_id=user_id, channel=ChannelType.TERMINAL)
            print(f"\ngenesis: {response}")
        except Exception:
            logger.exception("Error processing message")
            print("\n[Genesis error: unexpected failure. Check logs.]")

    await db.close()


def main():
    parser = argparse.ArgumentParser(description="Genesis Terminal Conversation")
    parser.add_argument(
        "--user", default="default", help="User ID (default: 'default')")
    parser.add_argument(
        "--boundary-hour", type=int, default=0,
        help="Day boundary hour in UTC for morning reset (default: 0)")
    parser.add_argument(
        "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level, format="%(name)s %(levelname)s %(message)s")

    asyncio.run(run_terminal(
        user_id=args.user, day_boundary_hour=args.boundary_hour))


if __name__ == "__main__":
    main()
```

Also create `src/genesis/cc/__main__.py` so `python -m genesis.cc.terminal` works:

Wait — `python -m genesis.cc.terminal` already runs `terminal.py` since it has `if __name__ == "__main__"`. For `python -m genesis.cc` to work, we'd need `__main__.py` in the `cc` package. Let's not add that — `python -m genesis.cc.terminal` is clearer.

**Step 2: Verify it imports cleanly**

Run: `cd ~/genesis && python -c "from genesis.cc.terminal import main; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add src/genesis/cc/terminal.py
git commit -m "feat: add terminal entry point for Genesis conversation"
```

---

## Task 5: Full verification + docs update

**Step 1: Run ruff + full test suite**

Run: `cd ~/genesis && ruff check . && pytest -v`
Expected: Zero lint errors, all tests pass (651 existing + ~13 new ≈ 664)

**Step 2: Check for untracked files**

Run: `cd ~/genesis && git status --short`
If any `??` files under `src/` or `tests/`, stage them.

**Step 3: Update go-live design doc**

Update `docs/plans/2026-03-07-cc-go-live-design.md` section "GL-2 Design — PENDING" to mark it resolved:

Replace the "### 3. GL-2 Design — PENDING" section with:

```markdown
### 3. GL-2 Design — RESOLVED

**Decision:** Terminal conversation implemented as `ConversationLoop` class with
a thin terminal I/O wrapper. System prompt assembled at runtime from SOUL.md +
USER.md + CONVERSATION.md + cognitive state. Session persistence via CC CLI's
`session_id` stored in new `cc_session_id` column. Morning reset detects day
boundary from session `started_at` timestamp.

**Implementation:** `docs/plans/2026-03-08-gl2-terminal-conversation.md`

**Usage:** `python -m genesis.cc.terminal` (outside CC session)

**Key design decisions:**
- System prompt on first message only; subsequent messages use `--resume`
- Morning reset completes old session, creates fresh with updated cognitive state
- Intent parser runs before CC (model/effort overrides applied per-message)
- MCP config deferred to GL-3 (CC built-in tools sufficient for terminal testing)
- ConversationLoop is channel-agnostic — same class serves terminal and future Telegram
```

**Step 4: Update build-phase-current.md**

Already updated at start of this session. Verify it reflects GL-2 status.

**Step 5: Commit**

```bash
git add docs/plans/2026-03-07-cc-go-live-design.md docs/plans/2026-03-08-gl2-terminal-conversation.md .claude/docs/build-phase-current.md
git commit -m "docs: GL-2 terminal conversation plan + design doc updates"
```

---

## Dependency Graph

```
Task 1 (schema + CRUD) ──────────┐
                                   ├──> Task 3 (ConversationLoop) ──> Task 4 (terminal entry point)
Task 2 (CONVERSATION.md + asm.) ──┘                                           │
                                                                               v
                                                                    Task 5 (verify + docs)
```

Tasks 1 and 2 are parallel. Task 3 depends on both. Task 4 depends on 3. Task 5 is final.

---

## Critical Implementation Notes

1. **cc_session_id vs id:** The `id` column in cc_sessions is a Genesis-internal UUID
   (created before calling CC). The `cc_session_id` is the CC CLI's own session
   identifier (returned in the JSON response). Only `cc_session_id` works with
   `--resume`. They are different values.

2. **System prompt on first message only:** `--system-prompt` is passed on the first
   CC invocation for a session. On `--resume`, CC already has the system prompt context
   from the original session. Don't re-pass it.

3. **Morning reset is started_at-based:** The `_should_reset()` method compares the
   session's `started_at` against the day boundary. This is simpler and more correct
   than `check_morning_reset()` which checks all stale sessions globally.

4. **No MCP config in GL-2:** CC's built-in tools (file I/O, bash, web) are sufficient
   for terminal testing. Genesis MCP servers (memory, health, outreach, recon) aren't
   running as servers yet. MCP config is a GL-3 concern.

5. **Terminal must run outside CC:** The terminal entry point invokes `claude -p`,
   which requires not being inside a CC session. `CCInvoker._build_env()` strips
   `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` env vars to enable this.

6. **ConversationLoop is channel-agnostic:** The same class will serve both terminal
   (GL-2) and Telegram relay (GL-3). The caller provides `channel` and handles I/O.
   ResponseFormatter adapts output per channel.

## Verification

After all tasks complete:
```bash
cd ~/genesis && ruff check .                         # Zero errors
cd ~/genesis && pytest -v                            # All tests pass
cd ~/genesis && pytest tests/test_cc/ -v             # CC-specific tests pass
cd ~/genesis && pytest tests/test_db/test_cc_sessions.py -v  # Schema change tests pass
```

Manual terminal test (outside CC session):
```bash
cd ~/genesis && source ~/agent-zero/.venv/bin/activate
python -m genesis.cc.terminal --verbose
```
Type "Hello" → should see Genesis respond via CC CLI.

---

## What Comes After GL-2

- **GL-3 (Full Telegram Relay):** AZ relay handler pipes Telegram messages through
  ConversationLoop. Checkpoint-and-resume for async questions. Message queue polling.
  MCP config for Genesis tools.
- **Phase 5 (Memory Operations):** Parallel track. Memory activation, retrieval,
  consolidation.
