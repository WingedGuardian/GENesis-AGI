# CC Go-Live Design

> Phased activation of Claude Code as Genesis's intelligence layer.
> This document covers GL-1 (Reflection Activation) in detail.
> GL-2 (Terminal Conversation) and GL-3 (Full Relay) are outlined for context.

## Context

The CC integration infrastructure is complete (646 tests):
- `genesis.cc` package: invoker, session manager, checkpoint, reflection bridge
- DB tables: message_queue, cc_sessions
- AZ extension _40: wires everything at agent init
- AwarenessLoop: dispatches Deep/Strategic to CCReflectionBridge

None of this is live. The bridge has hardcoded system prompts, `_build_args()`
is missing the `--effort` flag, and `_parse_output()` hasn't been validated
against real CLI output. This plan activates it.

## Phased Rollout

### GL-1: Reflection Activation (this plan)

Deep/Strategic reflections fire via CC background sessions when the awareness
loop triggers them. No user-facing CC. Telegram stays on AZ's chat_model.

### GL-2: Terminal Conversation (future plan)

Talk to Genesis-via-CC from terminal. Full identity system prompt, session
persistence via `--resume`, morning reset. Manual testing ground before Telegram.

### GL-3: Telegram Relay — RESOLVED

Telegram messages route through CC via ConversationLoop. Minimal-first: foreground
conversation only. Background capabilities (reflections, triage, tasks) wire in
naturally as Phase 7+ delivers them.

**Implementation:** Rewired `handlers.py`, `adapter.py`, `bridge.py` to use
ConversationLoop instead of AZClient. Bridge runs standalone (no AZ dependency
for conversation). `triage_pipeline=None` for now.

**Usage:** `python -m genesis.channels.bridge` (outside CC session)

**Deferred to GL-4 (after Phase 7):**
1. **CCOutput enrichment** — `tool_calls` field when CC CLI structured output supports it.
2. **Triage pipeline wiring** — Phase 7 `session_config.py` enables this.
3. **Stream-json validation** — empirical shape verification.
4. **Streaming invoker** — live progress for terminal + Telegram.

See `docs/plans/2026-03-09-gl4-streaming-and-live-feedback.md` for full GL-4 plan.

**Deferred to Phase 8 (NOT GL-4):**
- **Web UI CC chat widget** — CC-backed conversation in the Genesis dashboard panel.
  AZ's web UI chat stays on its own engine (deeply coupled to `AgentContext`).
  Phase 8 dashboard adds a CC chat widget via new Flask endpoint calling
  `ConversationLoop.handle_message()`.

**Web UI convergence path:**
- **Now (GL-3):** Telegram → CC. AZ web UI → AZ engine. Two separate paths.
- **Phase 8:** Genesis dashboard adds CC chat widget. AZ chat becomes debug/legacy.
- **Post-V3:** AZ chat tab hidden or removed. Genesis dashboard is the primary UI.

---

## GL-1 Design: Reflection Activation

### 1. System Prompt Files

Two markdown files replace hardcoded strings in `reflection_bridge.py`.
Static content — dynamic context goes in the user prompt.

**`src/genesis/identity/REFLECTION_DEEP.md`**
- Abbreviated SOUL.md (drives, weaknesses, hard constraints)
- Deep reflection instructions (analyze patterns, identify anomalies)
- Output schema: `{observations: [], patterns: [], recommendations: []}`

**`src/genesis/identity/REFLECTION_STRATEGIC.md`**
- Same abbreviated SOUL.md
- Strategic scope (long-term patterns, system evolution, goal alignment)
- Drive-alignment assessment
- Output schema: same structure, broader scope

**CAPS naming convention:** All user-editable markdown files use UPPERCASE
filenames. This makes them visually distinct as files the user can audit and
modify — not hidden implementation details.

**Why abbreviated SOUL.md?** Token cost. Full SOUL.md is ~120 lines. Deep
reflections run every 48h+, Strategic weekly — not high frequency but worth
keeping lean. Full SOUL.md is for user conversation (GL-2).

### 2. CCInvoker Updates

**Add `--effort` flag to `_build_args()`.**
Confirmed: `claude --help` shows `--effort <level>` (low, medium, high).
Currently missing from the args builder.

**Validate `_parse_output()` against real CLI output.**
A test script (`scripts/test_cc_cli.sh`) runs `claude -p` outside this
session with env vars stripped and captures the actual JSON shape. If the
format differs from what we mocked (`{"type": "result", ...}`), we update
the parser.

### 3. ReflectionBridge Updates

**Load system prompts from files.** Replace `_system_prompt_for_depth()`
with file loading. Paths injected at construction (testable — tests can
pass temp files).

**Enrich user prompt with cognitive state.** Currently only tick signals
and scores. Add cognitive state from DB via existing `cognitive_state.render()`.
This is the dynamic context:
- Tick ID, timestamp, trigger reason
- Signal readings
- Depth scores
- Cognitive state (active context + pending actions)

### 4. Verification

**Empirical test script** — `scripts/test_cc_cli.sh`:
- Strips CLAUDECODE + CLAUDE_CODE_ENTRYPOINT
- Tests `--output-format json` and `stream-json`
- Tests `--effort high` and `--system-prompt`
- Saves raw output for inspection
- Not run by pytest (requires real CC + Max subscription)

**Integration test** — `tests/test_cc/test_integration.py`:
- Skips if `claude` not on PATH or inside CC session
- Real `CCInvoker.run()` with trivial prompt
- Verifies `_parse_output()` produces valid CCOutput

**Unit tests:**
- System prompt files load without error
- `_system_prompt_for_depth()` returns file content
- `_build_reflection_prompt()` includes cognitive state
- `_build_args()` includes `--effort` flag
- All existing tests pass

---

## Post-GL-1 Discussion Items (Resolved 2026-03-08)

### 1. Skills vs MCP Architecture — RESOLVED

**Decision:** Genesis already follows the right pattern (MCP for live data,
markdown for static knowledge). Formalized as the CAPS markdown convention:
all user-editable files that shape LLM behavior use UPPERCASE filenames
(SOUL.md, USER.md, REFLECTION_DEEP.md, etc.).

**Why CAPS:** Makes files visually distinct as user-auditable, not hidden
implementation details. Transparency breeds trust; opacity breeds suspicion.

**Why externalize to markdown:** Code should handle structure (timeouts,
validation, event wiring); judgment-shaping instructions belong in editable
files. Hardcoded Python strings hide behavior that only developers can audit.

**Actions taken:**
- Renamed `user.md` → `USER.md`, `reflection_deep.md` → `REFLECTION_DEEP.md`,
  `reflection_strategic.md` → `REFLECTION_STRATEGIC.md`
- Added CAPS convention to CLAUDE.md design principles
- Identified perception templates (6 hardcoded strings in `PromptBuilder`)
  as near-term externalization target
- Added identity file audit UI to Phase 8 dashboard spec

**Future:** Phase 8 dashboard will include a page for viewing/editing all
CAPS markdown files. Perception templates will be externalized as a cleanup
task before Phase 6.

### 2. Web Scraping / 403 Errors — RESOLVED

**Decision:** Not an either/or choice between approaches. All methods (direct
fetch, search cache, archive.org, reader APIs, headless browser, third-party
MCPs) are tools in a fallback chain ordered by path of least resistance.

**Why a learning system, not a static chain:** The web landscape changes daily.
Sites block automated requests, new workaround tools emerge, existing tools
stop working. A static fallback chain becomes stale. Genesis needs to maintain
a dynamic registry of methods through its learning system (Phase 6 procedures),
recording what works for which sites and updating rankings as effectiveness
changes.

**Why this applies broadly:** The same "exhaust creative workarounds before
reporting failure" principle applies to ALL obstacles — API rate limits, model
unavailability, tool failures, permission errors. The vision doc's "failure is
not an option" philosophy means the resolution method registry is a
general-purpose capability, not a web-specific one.

**Actions taken:**
- Added "Adaptive Obstacle Resolution" section to Phase 6 in build phases
- Added "Adaptive Web Resilience & Tool Registry" to deferred integrations (V4)
- V3 scope: static fallback chain, structured as procedure-like data so V4
  can take over ranking
- V4 scope: learned procedures with effectiveness scores, proactive tool
  research via surplus compute

### 3. GL-2 Design — RESOLVED

Terminal conversation with full Genesis identity system prompt, CC session
persistence via `--resume`, morning reset, intent parsing.

Implementation: `docs/plans/2026-03-08-gl2-terminal-conversation.md`

Key components:
- `cc_session_id` column on `cc_sessions` for CLI resume tracking
- `SystemPromptAssembler` — SOUL.md + USER.md + CONVERSATION.md + cognitive state
- `ConversationLoop` — channel-agnostic orchestrator (terminal + Telegram)
- Terminal entry point: `python -m genesis.cc.terminal`
