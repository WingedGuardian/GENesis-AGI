# CC GL-1: Reflection Activation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Activate Deep/Strategic reflections via CC background sessions with proper system prompts and the `--effort` flag.

**Architecture:** Two markdown system prompt files (deep, strategic) replace hardcoded strings. CCInvoker gains `--effort` flag. ReflectionBridge loads prompts from files and enriches user prompts with cognitive state. An empirical test script validates real CLI output format. An integration test proves end-to-end CC invocation.

**Tech Stack:** Python 3.12, asyncio subprocess, pathlib, existing Genesis DB/CRUD

---

## Task 1: Add `--effort` flag to CCInvoker

**Files:**
- Modify: `src/genesis/cc/invoker.py:23-34`
- Modify: `tests/test_cc/test_invoker.py`

**Step 1: Write the failing test**

Add to `tests/test_cc/test_invoker.py`:

```python
def test_build_args_with_effort(invoker):
    inv = CCInvocation(prompt="hello", effort=EffortLevel.HIGH)
    args = invoker._build_args(inv)
    assert "--effort" in args
    assert "high" in args


def test_build_args_default_effort(invoker):
    inv = CCInvocation(prompt="hello")
    args = invoker._build_args(inv)
    assert "--effort" in args
    assert "medium" in args
```

This requires importing `EffortLevel` at the top:
```python
from genesis.cc.types import CCInvocation, EffortLevel
```

**Step 2: Run test to verify it fails**

Run: `cd ~/genesis && source ~/agent-zero/.venv/bin/activate && python -m pytest tests/test_cc/test_invoker.py::test_build_args_with_effort -v`
Expected: FAIL (--effort not in args)

**Step 3: Implement**

In `src/genesis/cc/invoker.py`, add after the `--output-format` line (line 26) and before the `system_prompt` check (line 27):

```python
        args += ["--effort", str(inv.effort)]
```

**Step 4: Run tests to verify they pass**

Run: `cd ~/genesis && python -m pytest tests/test_cc/test_invoker.py -v`
Expected: All 11 tests PASS (9 existing + 2 new)

**Step 5: Commit**

```bash
git add src/genesis/cc/invoker.py tests/test_cc/test_invoker.py
git commit -m "feat: add --effort flag to CCInvoker._build_args()"
```

---

## Task 2: Create reflection system prompt files

**Files:**
- Create: `src/genesis/identity/reflection_deep.md`
- Create: `src/genesis/identity/reflection_strategic.md`

**Step 1: Create `src/genesis/identity/reflection_deep.md`**

```markdown
# Genesis — Deep Reflection

You are Genesis performing a Deep reflection. You are a cognitive partner that
remembers, learns, anticipates, and evolves.

## Your Drives

- **Preservation** — Protect what works. System health, user data, earned trust.
- **Curiosity** — Seek new information. Notice patterns, explore unknowns.
- **Cooperation** — Create value for the user. Deliver results, anticipate needs.
- **Competence** — Get better at getting better. Improve processes, refine judgment.

## Your Weaknesses

You confabulate — label speculation as speculation.
You lose the forest for the trees — step back and look at the big picture.
You are overconfident — default to the null hypothesis.
You are sycophantic — challenge your own conclusions with evidence.

## Hard Constraints

- Never act outside granted autonomy permissions
- Never claim certainty you don't have
- Never spend above budget thresholds without user approval

## Task

Analyze the signals, scores, and cognitive state provided in the user message.
Identify meaningful patterns, anomalies, and actionable observations.

Focus on:
- What has changed since the last reflection?
- What patterns are emerging across signals?
- What should Genesis pay attention to next?
- Are any observations contradicting prior assumptions?

## Output Format

Respond with valid JSON:

```json
{
  "observations": ["observation 1", "observation 2"],
  "patterns": ["pattern 1", "pattern 2"],
  "recommendations": ["recommendation 1", "recommendation 2"],
  "confidence": 0.7,
  "focus_next": "what to monitor until next reflection"
}
```
```

**Step 2: Create `src/genesis/identity/reflection_strategic.md`**

```markdown
# Genesis — Strategic Reflection

You are Genesis performing a Strategic reflection. You are a cognitive partner
that thinks broadly about long-term patterns, goals, and system evolution.

## Your Drives

- **Preservation** — Protect what works. System health, user data, earned trust.
- **Curiosity** — Seek new information. Notice patterns, explore unknowns.
- **Cooperation** — Create value for the user. Deliver results, anticipate needs.
- **Competence** — Get better at getting better. Improve processes, refine judgment.

## Your Weaknesses

You confabulate — label speculation as speculation.
You lose the forest for the trees — step back and look at the big picture.
You are overconfident — default to the null hypothesis.
You are sycophantic — challenge your own conclusions with evidence.

## Hard Constraints

- Never act outside granted autonomy permissions
- Never claim certainty you don't have
- Never spend above budget thresholds without user approval

## Task

Perform a strategic-level analysis. This runs weekly. Think broadly about:

- **System evolution** — How is Genesis developing as a system? What capabilities
  are maturing? What's still fragile?
- **Goal alignment** — Are current activities aligned with the user's long-term
  goals? Is anything drifting?
- **Drive balance** — Are the four drives in healthy tension, or is one
  dominating? (Preservation→paralysis, Curiosity→distraction,
  Cooperation→sycophancy, Competence→navel-gazing)
- **Emerging opportunities** — What patterns suggest new capabilities or
  approaches worth exploring?
- **Risk assessment** — What could go wrong in the next week? What's the
  biggest blind spot?

## Output Format

Respond with valid JSON:

```json
{
  "observations": ["observation 1", "observation 2"],
  "patterns": ["pattern 1", "pattern 2"],
  "recommendations": ["recommendation 1", "recommendation 2"],
  "drive_assessment": {
    "preservation": "healthy|dominant|suppressed",
    "curiosity": "healthy|dominant|suppressed",
    "cooperation": "healthy|dominant|suppressed",
    "competence": "healthy|dominant|suppressed"
  },
  "confidence": 0.7,
  "focus_next_week": "strategic priority for coming week"
}
```
```

**Step 3: Verify files exist and are readable**

Run: `cd ~/genesis && python -c "from pathlib import Path; p1 = Path('src/genesis/identity/reflection_deep.md'); p2 = Path('src/genesis/identity/reflection_strategic.md'); assert p1.exists() and p2.exists() and len(p1.read_text()) > 100 and len(p2.read_text()) > 100; print('OK')"`

**Step 4: Commit**

```bash
git add src/genesis/identity/reflection_deep.md src/genesis/identity/reflection_strategic.md
git commit -m "feat: add reflection system prompt markdown files (deep + strategic)"
```

---

## Task 3: Update ReflectionBridge to load prompts from files + add cognitive state

**Files:**
- Modify: `src/genesis/cc/reflection_bridge.py:19-27,52,82-113`
- Modify: `tests/test_cc/test_reflection_bridge.py`

**Step 1: Write the failing tests**

Add these tests to `tests/test_cc/test_reflection_bridge.py`:

```python
def test_system_prompt_loads_from_file(tmp_path, db, mock_invoker, mock_session_mgr):
    deep_file = tmp_path / "deep.md"
    deep_file.write_text("Deep prompt content here")
    strategic_file = tmp_path / "strategic.md"
    strategic_file.write_text("Strategic prompt content here")

    b = CCReflectionBridge(
        session_manager=mock_session_mgr, invoker=mock_invoker, db=db,
        prompt_dir=tmp_path,
    )
    assert "Deep prompt content" in b._system_prompt_for_depth(Depth.DEEP)
    assert "Strategic prompt content" in b._system_prompt_for_depth(Depth.STRATEGIC)


def test_system_prompt_falls_back_when_file_missing(db, mock_invoker, mock_session_mgr):
    b = CCReflectionBridge(
        session_manager=mock_session_mgr, invoker=mock_invoker, db=db,
        prompt_dir=Path("/nonexistent"),
    )
    # Should fall back to hardcoded prompt, not crash
    prompt = b._system_prompt_for_depth(Depth.DEEP)
    assert "Genesis" in prompt


async def test_reflection_prompt_includes_cognitive_state(db, bridge, tick):
    from genesis.db.crud import cognitive_state
    import uuid
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    await cognitive_state.create(
        db, id=str(uuid.uuid4()), content="Currently researching vehicles",
        section="active_context", generated_by="test", created_at=now,
    )
    prompt = await bridge._build_reflection_prompt(depth=Depth.DEEP, tick=tick, db=db)
    assert "vehicles" in prompt.lower()
```

This requires adding `from pathlib import Path` to the imports.

**Step 2: Run tests to verify they fail**

Run: `cd ~/genesis && python -m pytest tests/test_cc/test_reflection_bridge.py::test_system_prompt_loads_from_file -v`
Expected: FAIL (prompt_dir not accepted)

**Step 3: Implement**

Update `src/genesis/cc/reflection_bridge.py`:

1. Add `from pathlib import Path` to imports.

2. Update `__init__` to accept `prompt_dir`:
```python
_DEFAULT_PROMPT_DIR = Path(__file__).resolve().parent.parent / "identity"

class CCReflectionBridge:
    def __init__(self, *, session_manager, invoker, db, event_bus=None,
                 prompt_dir: Path | None = None):
        self._session_manager = session_manager
        self._invoker = invoker
        self._db = db
        self._event_bus = event_bus
        self._prompt_dir = prompt_dir or _DEFAULT_PROMPT_DIR
```

3. Replace `_system_prompt_for_depth`:
```python
    _PROMPT_FILES = {
        Depth.DEEP: "reflection_deep.md",
        Depth.STRATEGIC: "reflection_strategic.md",
    }

    _FALLBACK_PROMPTS = {
        Depth.DEEP: (
            "You are Genesis performing a Deep reflection. "
            "Analyze recent signals and observations for meaningful patterns. "
            "Output structured JSON with 'observations', 'patterns', 'recommendations'."
        ),
        Depth.STRATEGIC: (
            "You are Genesis performing a Strategic reflection. "
            "Think broadly about long-term patterns, goals, and system evolution. "
            "Output structured JSON with 'observations', 'patterns', 'recommendations'."
        ),
    }

    def _system_prompt_for_depth(self, depth: Depth) -> str:
        filename = self._PROMPT_FILES.get(depth)
        if filename:
            path = self._prompt_dir / filename
            if path.exists():
                return path.read_text()
        return self._FALLBACK_PROMPTS.get(depth, self._FALLBACK_PROMPTS[Depth.DEEP])
```

4. Make `_build_reflection_prompt` async and add cognitive state:
```python
    async def _build_reflection_prompt(self, depth: Depth, tick: TickResult, *, db) -> str:
        from genesis.db.crud import cognitive_state

        signals_summary = ", ".join(
            f"{s.name}={s.value}" for s in tick.signals[:10]
        ) if tick.signals else "none"

        scores_summary = ", ".join(
            f"{s.depth.value}={s.final_score:.2f}" for s in tick.scores
        ) if tick.scores else "none"

        cog_state = await cognitive_state.render(db)

        return (
            f"Perform a {depth.value} reflection.\n\n"
            f"Tick ID: {tick.tick_id}\n"
            f"Timestamp: {tick.timestamp}\n"
            f"Trigger: {tick.trigger_reason or 'scheduled'}\n"
            f"Signals: {signals_summary}\n"
            f"Depth scores: {scores_summary}\n\n"
            f"## Current Cognitive State\n\n{cog_state}\n\n"
            f"Analyze the current state, identify patterns and observations, "
            f"and provide actionable insights."
        )
```

5. Update `reflect()` to await the now-async prompt builder (line 52):
```python
        # 2. Build prompt
        prompt = await self._build_reflection_prompt(depth, tick, db=db)
```

**Step 4: Update existing tests that construct bridge without prompt_dir**

The existing `bridge` fixture passes no `prompt_dir`, so it will use the default
(`src/genesis/identity/`). Since we just created the files, existing tests should
still pass. But update the fixture to be explicit:

No change needed — the default `_DEFAULT_PROMPT_DIR` points to the real identity
directory which now has the files.

**Step 5: Run all tests**

Run: `cd ~/genesis && python -m pytest tests/test_cc/test_reflection_bridge.py -v`
Expected: All 11 tests PASS (8 existing + 3 new)

**Step 6: Commit**

```bash
git add src/genesis/cc/reflection_bridge.py tests/test_cc/test_reflection_bridge.py
git commit -m "feat: load reflection prompts from markdown files, enrich with cognitive state"
```

---

## Task 4: Empirical CLI test script

**Files:**
- Create: `scripts/test_cc_cli.sh`

**Step 1: Create the script**

```bash
#!/usr/bin/env bash
# Test claude -p CLI output format for Genesis CC integration.
# Run OUTSIDE a Claude Code session (or it will fail with nesting error).
#
# Usage: bash scripts/test_cc_cli.sh
# Output: scripts/cc_cli_output/ directory with captured responses

set -euo pipefail

OUTDIR="$(dirname "$0")/cc_cli_output"
mkdir -p "$OUTDIR"

# Strip env vars that block nesting
export CLAUDECODE=
export CLAUDE_CODE_ENTRYPOINT=

echo "=== Test 1: --output-format json ==="
claude -p "Respond with exactly: hello world" --output-format json \
    > "$OUTDIR/test_json.txt" 2>&1 || true
echo "Saved to $OUTDIR/test_json.txt"
cat "$OUTDIR/test_json.txt" | head -20

echo ""
echo "=== Test 2: --output-format stream-json ==="
claude -p "Respond with exactly: hello world" --output-format stream-json \
    > "$OUTDIR/test_stream_json.txt" 2>&1 || true
echo "Saved to $OUTDIR/test_stream_json.txt"
cat "$OUTDIR/test_stream_json.txt" | head -30

echo ""
echo "=== Test 3: --effort high ==="
claude -p "Respond with exactly: hello world" --output-format json --effort high \
    > "$OUTDIR/test_effort.txt" 2>&1 || true
echo "Saved to $OUTDIR/test_effort.txt"
cat "$OUTDIR/test_effort.txt" | head -20

echo ""
echo "=== Test 4: --system-prompt ==="
claude -p "What are you?" --output-format json \
    --system-prompt "You are Genesis. Respond in one sentence." \
    > "$OUTDIR/test_system_prompt.txt" 2>&1 || true
echo "Saved to $OUTDIR/test_system_prompt.txt"
cat "$OUTDIR/test_system_prompt.txt" | head -20

echo ""
echo "=== Done. Inspect $OUTDIR/ for raw output. ==="
echo "Check if 'type: result' JSON shape matches CCInvoker._parse_output()"
```

**Step 2: Make executable and add output dir to gitignore**

```bash
chmod +x scripts/test_cc_cli.sh
echo "scripts/cc_cli_output/" >> .gitignore
```

**Step 3: Commit**

```bash
git add scripts/test_cc_cli.sh .gitignore
git commit -m "feat: add empirical CLI test script for CC output format validation"
```

**NOTE:** Do NOT run this script inside the current CC session. It must be run
manually in a plain terminal after implementation is complete.

---

## Task 5: Integration test (skippable)

**Files:**
- Create: `tests/test_cc/test_integration.py`

**Step 1: Write the test**

```python
"""Integration test — real CC CLI invocation.

Skipped when:
- claude CLI is not on PATH
- Running inside a Claude Code session (CLAUDECODE env var set)
"""

import os
import shutil

import pytest

from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation, EffortLevel

_skip_no_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not available",
)
_skip_inside_cc = pytest.mark.skipif(
    os.environ.get("CLAUDECODE") == "1",
    reason="Cannot nest CC sessions",
)


@_skip_no_claude
@_skip_inside_cc
async def test_real_cc_invocation():
    """Invoke claude -p with a trivial prompt and verify output parsing."""
    invoker = CCInvoker()
    output = await invoker.run(
        CCInvocation(
            prompt="Respond with exactly: hello",
            output_format="json",
            timeout_s=60,
        ),
    )
    assert not output.is_error, f"CC invocation failed: {output.error_message}"
    assert output.exit_code == 0
    assert len(output.text) > 0
    # session_id may or may not be populated depending on output format
    # but text must be non-empty for a successful call


@_skip_no_claude
@_skip_inside_cc
async def test_real_cc_with_system_prompt():
    """Invoke claude -p with a system prompt."""
    invoker = CCInvoker()
    output = await invoker.run(
        CCInvocation(
            prompt="What are you?",
            system_prompt="You are Genesis. Respond in one sentence.",
            output_format="json",
            timeout_s=60,
        ),
    )
    assert not output.is_error, f"CC invocation failed: {output.error_message}"
    assert len(output.text) > 0
```

**Step 2: Run test to verify it's skipped (we're inside CC)**

Run: `cd ~/genesis && python -m pytest tests/test_cc/test_integration.py -v`
Expected: 2 tests SKIPPED with reason "Cannot nest CC sessions"

**Step 3: Commit**

```bash
git add tests/test_cc/test_integration.py
git commit -m "feat: add CC integration tests (skipped when inside CC session)"
```

---

## Task 6: Full verification + docs update

**Step 1: Run full test suite**

```bash
cd ~/genesis && ruff check . && pytest -v
```

Expected: All tests pass (~650+). Zero ruff errors.

**Step 2: Check for untracked files**

```bash
cd ~/genesis && git status --short
```

Stage any `??` files under `src/` or `tests/`.

**Step 3: Update build-phase-current.md**

Update `.claude/docs/build-phase-current.md` status line from "COMPLETE (infrastructure only)" to note GL-1 is complete:

```markdown
# Current Build Phase: CC Go-Live GL-1 — Reflection Activation

**Status:** COMPLETE
**Dependencies:** CC Integration Workstream (COMPLETE), Phase 0-4 (COMPLETE)

## What GL-1 Delivers

- Reflection system prompt markdown files (deep + strategic)
- CCInvoker --effort flag support
- ReflectionBridge loads prompts from files (with fallback)
- Enriched reflection user prompts with cognitive state
- Empirical CLI test script (scripts/test_cc_cli.sh)
- Integration test (skipped inside CC, runs in plain terminal)
- ~5 new tests

## What Comes Next

- **GL-1 manual activation**: Run scripts/test_cc_cli.sh in a plain terminal.
  If output format matches _parse_output(), reflections are ready to fire.
  If not, update _parse_output() to match real format.
- **Post-GL-1 discussion**: Skills vs MCP architecture, web scraping/403 handling
- **GL-2**: Terminal conversation (system prompt, session persistence, morning reset)
- **GL-3**: Full Telegram relay
- **Phase 5**: Memory Operations (parallel track)
```

**Step 4: Commit**

```bash
git add -A
git commit -m "docs: update build-phase-current.md for GL-1 reflection activation"
```

**Step 5: Push**

```bash
git push origin main
```

---

## Dependency Graph

```
Task 1 (--effort flag) ---+
Task 2 (prompt files) ----+--> Task 3 (bridge update) --> Task 6 (verify+docs)
                           |
                           +--> Task 4 (CLI test script)
                           +--> Task 5 (integration test)
```

Tasks 1, 2, 4, 5 are independent. Task 3 depends on 1+2. Task 6 depends on all.

---

## Verification

After all tasks complete:
```bash
cd ~/genesis && ruff check .                              # Zero errors
cd ~/genesis && pytest -v                                 # All tests pass
cd ~/genesis && pytest tests/test_cc/ -v                  # CC-specific tests pass
cd ~/genesis && python -c "
from genesis.cc.reflection_bridge import CCReflectionBridge
from genesis.awareness.types import Depth
from pathlib import Path
b = CCReflectionBridge.__new__(CCReflectionBridge)
b._prompt_dir = Path('src/genesis/identity')
p = b._system_prompt_for_depth(Depth.DEEP)
assert 'Genesis' in p and 'Deep' in p and 'JSON' in p
print('System prompt loading OK')
"
```

## Post-Implementation: Manual CLI Test

After all code is committed, in a **plain terminal** (not inside CC):

```bash
cd ~/genesis && bash scripts/test_cc_cli.sh
```

Inspect `scripts/cc_cli_output/test_json.txt`. If the JSON shape has
`{"type": "result", "result": "...", "session_id": "..."}`, the parser
is correct. If it differs, open a follow-up task to update `_parse_output()`.
