# Troubleshooting Guide

This document tracks known issues, their root causes, and solutions for the Nanobot Executive Copilot project.

---

## WhatsApp Channel: Duplicate LLM Responses

**Issue ID**: WHATSAPP-DUPLICATE-001
**First Occurrence**: Unknown (prior to 2026-02-14)
**Second Occurrence**: 2026-02-14
**Status**: Fixed (2026-02-14)

### Symptoms

- User sends a single WhatsApp message
- Copilot generates **two different responses** (not identical duplicates) to the same incoming message
- Each response is a unique LLM generation, not a simple duplicate send
- Typically occurs during WhatsApp bridge reconnection events

### Root Cause

The WhatsApp bridge (using Baileys library) can send the same message twice during reconnection events. Without message ID deduplication in the WhatsApp channel handler, each message was processed independently through the full agent loop, resulting in two separate LLM invocations and two different responses.

### Technical Details

**File**: `nanobot/channels/whatsapp.py`

The `_handle_bridge_message` method was processing all incoming messages without checking if they had already been handled. During WebSocket reconnections or network instability, the Baileys library would resend recent messages, causing duplicate processing.

### Solution Applied (2026-02-14)

Added message ID deduplication with cache management to `nanobot/channels/whatsapp.py`:

```python
# In __init__ method:
self._processed_message_ids: set[str] = set()  # Track processed message IDs
self._max_message_id_cache = 1000  # Prevent unbounded growth

# In _handle_bridge_message method:
# Deduplicate messages by ID (prevents double-processing during reconnects)
message_id = data.get("id")
if message_id:
    if message_id in self._processed_message_ids:
        logger.debug(f"Skipping duplicate message {message_id}")
        return
    self._processed_message_ids.add(message_id)

    # Prevent unbounded cache growth
    if len(self._processed_message_ids) > self._max_message_id_cache:
        # Remove oldest half of cached IDs
        to_remove = len(self._processed_message_ids) // 2
        for _ in range(to_remove):
            self._processed_message_ids.pop()
```

### Verification

After fix deployment:
- User sent test message "Testing" - received single response ✓
- User sent test message "Test2" - received single response ✓
- User sent additional test message - confirmed single response ✓

### Prevention

This issue should not recur with the current fix in place. However, if it does occur again:

1. Check if `_processed_message_ids` set is being properly maintained
2. Verify WhatsApp bridge is sending valid message IDs
3. Check for any race conditions in async message processing
4. Review logs for duplicate message ID patterns

### Related Issues

- Verbose LiteLLM logging (fixed concurrently in `litellm_provider.py`)
- LM Studio offline causing embedding failures (operational issue, not code bug)

---

## Agent Loop: Approval System Deadlock

**Issue ID**: AGENT-DEADLOCK-001
**First Occurrence**: 2026-02-14
**Status**: Fixed (2026-02-14)

### Symptoms

- User sends a message, system shows "..." (composing) indefinitely
- Approval request is sent to WhatsApp but user's reply never gets processed
- Agent loop appears completely frozen
- Composing indicator never clears
- Triggered when LLM calls a tool requiring approval (e.g. `exec` with non-read-only command)

### Root Cause

**Classic deadlock.** The agent loop is single-threaded:

1. User message arrives → agent loop calls `_process_message()`
2. LLM responds with tool call (e.g. `exec`) → approval interceptor triggers
3. `interceptor.check()` sends approval request to WhatsApp
4. `interceptor.check()` blocks with `await event.wait()` (up to 300s)
5. User replies "approve" → message enters the bus queue
6. **But** the agent loop can't call `bus.consume_inbound()` because it's blocked at step 4
7. **Deadlock**: loop waits for approval → approval response waits for loop

### Solution Applied (2026-02-14)

**4 changes across 4 files:**

1. **`nanobot/copilot/approval/interceptor.py`**: Added concurrent bus drainer during approval wait. While waiting for approval event, a background task consumes messages from the bus, forwarding approval responses and re-queuing other messages.

2. **`nanobot/copilot/approval/patterns.py`**: Added `set_preference` to `AUTO_APPROVE` set (it only changes internal config, no approval needed).

3. **`nanobot/agent/loop.py`**: Added `/status` as direct slash command — bypasses LLM entirely, calls status tool directly. Previously `/status` was routed through Opus just to call a tool.

4. **`nanobot/channels/whatsapp.py`**: Added 5-minute max duration to composing loop. If system hangs, "..." auto-clears after 5 minutes and sends "paused" presence.

### Prevention

- Any new tools that only modify internal state should be added to `AUTO_APPROVE`
- The composing timeout acts as a safety net for any future hangs
- The concurrent bus drainer ensures approval responses always get processed

---

## Agent Loop: Approval System Removed

**Issue ID**: AGENT-APPROVAL-REMOVED-001
**Date**: 2026-02-15
**Status**: Permanent fix (supersedes AGENT-DEADLOCK-001)

### Context

The approval interceptor was the root cause of AGENT-DEADLOCK-001 and blocked normal tool usage (write_file, exec, etc.) during onboarding. The concurrent bus drainer fix (AGENT-DEADLOCK-001) was a band-aid — the fundamental issue was that approval added no safety value beyond what already existed in hard code guards.

### Solution

Removed the approval system entirely (commits dbbfeb9, faf8071):
- Deleted `nanobot/copilot/approval/` module (interceptor, patterns, queue, parser)
- Removed all `_approval_interceptor` references from `agent/loop.py` and `cli/commands.py`
- Replaced with `data/copilot/policy.md` — soft instructions injected into system prompt
- Hard safety guards remain: filesystem deny-list (`filesystem.py`), shell command filtering (`shell.py`), secrets isolation (`secrets.py`)

### Current Safety Architecture

1. **Hard guards (code-enforced):** deny_patterns in shell.py, protected paths in filesystem.py — cannot be bypassed by LLM
2. **Soft policy (prompt-based):** policy.md injected into system prompt — LLM self-polices confirmable actions
3. **No approval flow** — no mechanism for the LLM to ask user for permission and wait for response

---

## WhatsApp: Composing Indicator Stuck Forever

**Issue ID**: WHATSAPP-COMPOSING-001
**First Occurrence**: 2026-02-14
**Status**: Fixed (2026-02-14)

### Symptoms

- WhatsApp shows "..." (typing indicator) permanently
- No response ever arrives
- Related to AGENT-DEADLOCK-001 but can occur in any hang scenario

### Root Cause

The composing loop (`_composing_loop`) ran `while True` with no max duration. If the agent loop hung for any reason, the composing indicator would persist indefinitely.

### Solution Applied (2026-02-14)

Added 5-minute max duration to `_composing_loop` in `nanobot/channels/whatsapp.py`. After 300 seconds, it sends "paused" presence and logs a warning.

---

## Routing: /use Cascade Failure and Context Loss

**Issue ID**: ROUTING-CASCADE-001
**First Occurrence**: 2026-02-15
**Status**: Fixed (2026-02-15) [RESOLVED 2026-02-20 — Router V2 eliminates heuristic classification and clears agents.defaults.model]

### Symptoms

- `/use openrouter haiku` causes all LLM requests to fail
- System locked in loop: "All providers failed. Last error: None"
- After recovery (5min circuit breaker cooldown), LLM has no conversation context
- Error messages ("I'm having trouble connecting...") replace real history

### Root Cause

Three compounding issues:

1. **No model name validation**: `/use openrouter haiku` stored bare "haiku" as model name. litellm requires fully-qualified names like `anthropic/claude-haiku-4.5`. Every request failed.

2. **Circuit breaker deadlock**: 3 rapid failures opened the circuit on openrouter (the only cloud provider). With all circuits open, `try_providers()` skipped every provider, `last_error` stayed `None`, and the system was locked out for 5 minutes.

3. **Error response pollution**: Each failed request saved "I'm having trouble connecting..." to the session. After recovery, `get_history(50)` returned a window dominated by error messages, pushing real conversation out of the LLM's view.

### Solution Applied (2026-02-15)

**3 files changed:**

1. **`nanobot/agent/loop.py`**: Added `MODEL_ALIASES` dict mapping short names (haiku, opus, sonnet, gpt4, etc.) to full litellm IDs. `/use` handler resolves aliases and rejects unknown bare names with helpful error. Error responses tagged with `is_error=True` metadata.

2. **`nanobot/copilot/routing/failover.py`**: When all circuits are open (`last_error is None`), forces a probe through the provider whose circuit opened earliest. Prevents silent deadlock and produces meaningful error messages.

3. **`nanobot/session/manager.py`**: `get_history()` filters out error assistant messages (by `is_error` flag or content prefix match) before applying the max_messages window. Raw JSONL retains everything for audit.

### Verification

- `/use openrouter haiku` → resolves to `anthropic/claude-haiku-4.5`
- `/use openrouter badmodel` → helpful error with valid short names
- All circuits open → forced probe instead of silent "Last error: None"
- After error cascade → `get_history()` returns real conversation, not error noise

### Prevention

- Model name validation prevents the root cause
- Circuit breaker probe prevents total lockout
- Error filtering ensures context continuity even if other failure modes occur

---

## Context: Write-Only Episodic Memory

**Issue ID**: CONTEXT-MEMORY-001
**First Occurrence**: 2026-02-15 (discovered during audit)
**Status**: Fixed (2026-02-15)

### Symptoms

- After model switch, context compaction, or fresh start, LLM has no memory of prior conversation
- Episodic memory (Qdrant + FTS5) stores exchanges but never auto-retrieves them
- `proactive_recall()` fully implemented but never called (dead code)
- `_memory_manager` set on ExtendedContextBuilder but never used in `build_messages()`

### Root Cause

The V1 architecture designed a three-tier memory system (working → episodic → structured) but only wired the write path. Exchanges and extractions were stored in Qdrant/FTS5 via `remember_exchange()` and `remember_extractions()`, but nothing in the context pipeline read them back. The `memory` tool existed for explicit LLM search, but the LLM didn't know to use it on fresh starts.

> **Note (2026-02-19)**: The three-tier design was subsequently redesigned — Redis/working tier removed entirely, memory is now two-tier (Qdrant + SQLite). See CHANGELOG.md "Memory Architecture Redesign" entry.

### Solution Applied (2026-02-15)

Three complementary mechanisms, each with a clear role:

1. **`recall_messages` tool** (`nanobot/copilot/tools/recall.py`): New tool that lets the LLM "scroll up" in conversation history on demand. Reads session JSONL directly — no Qdrant dependency. Filters error noise, truncates long messages, shows timestamps. Zero tokens wasted when context is already sufficient.

2. **Proactive episodic injection** (`nanobot/agent/loop.py`): Before `build_messages()`, calls `proactive_recall()` to fetch 2-3 relevant cross-session memories. Injected into system prompt with ~200 token budget. Gracefully degrades if Qdrant is down.

3. **Orientation hint** (`nanobot/copilot/context/extended.py`): When history has fewer than 3 exchanges, appends one line to system prompt: "You may be continuing a prior conversation. Use recall_messages to review recent exchanges if needed."

Also tracks `last_model_used` in session metadata for future model-switch detection.

### Verification

- Conversation → model switch → follow-up → LLM should use recall_messages or reference proactive memory
- Fresh session → recall_messages returns "No prior messages"
- Qdrant down → proactive injection silently skipped, recall_messages still works (reads JSONL)
- After 10 error responses → recall_messages returns real messages only

### Prevention

- The scroll-up tool provides on-demand context without wasting tokens
- Proactive injection provides cross-session memory automatically
- The orientation hint ensures the LLM knows to look for context when it's thin

---

## Context: Proactive Recall Performance & UX Fixes

**Issue ID**: CONTEXT-MEMORY-002
**First Occurrence**: 2026-02-16 (discovered during code review)
**Status**: Fixed (2026-02-16)

### Symptoms

- Memory increase after context continuity changes deployed
- Potential for message processing to hang if Qdrant or embedder is slow/down
- Orientation hint appearing on brand-new conversations with no prior history
- Excessive token usage from 800-token proactive injection every turn

### Root Cause

Three bugs introduced during CONTEXT-MEMORY-001 implementation:

1. **No timeout on proactive recall**: `proactive_recall()` calls embedding service + Qdrant search with no timeout. If either hangs, every message blocks indefinitely. The `try/except` catches errors but not timeouts.

2. **Orientation hint fires on empty history**: Condition `if len(real_history) < 6` fires when history=0 (brand-new conversation). First 3 exchanges of every conversation got "You may be continuing a prior conversation" even for fresh starts.

3. **Proactive recall budget too large**: 800-token injection every turn wasteful when recall_messages tool handles detailed on-demand retrieval. Should be a "nudge" not a dump.

### Solution Applied (2026-02-16)

**3 one-line fixes:**

1. **`nanobot/agent/loop.py:533-538`**: Wrapped proactive recall in `asyncio.wait_for(..., timeout=2.0)`. If Qdrant/embedder doesn't respond in 2s, silently skip — recall_messages tool provides backup.

2. **`nanobot/copilot/context/extended.py:134`**: Changed orientation hint condition to `if 0 < len(real_history) < 6` — only fires when there IS history but it's thin, not on fresh conversations.

3. **`nanobot/copilot/memory/manager.py:148`**: Reduced proactive recall budget from 800 to 200 tokens (`budget_tokens=200`). Cross-session recall is now a lightweight nudge.

### Verification

- Brand-new conversation → no orientation hint on first message
- Conversation with 2 exchanges → orientation hint appears
- Kill Qdrant → send message → completes within 2s (proactive recall skipped), no hang
- Proactive recall output length → under 200 tokens

### Prevention

- Performance-critical async calls should have timeouts
- Orientation hints should check for actual prior context before firing
- Token budgets should match usage pattern (nudge vs full retrieval)

---

## Extraction: SLM Queue Accumulating

**Issue ID**: EXTRACTION-QUEUE-001
**First Occurrence**: 2026-02-16
**Status**: By Design (queue drains automatically)

### Symptoms

- Low-quality extractions (heuristic-only, no SLM refinement)
- `/status` shows growing SLM Queue pending count
- Extraction fields (entities, decisions) are sparse or missing

### Root Cause

LM Studio is offline or unreachable. The SLM work queue buffers extraction and embedding jobs (SQLite-backed, 500-item cap, 30 items/min drain rate). While queued, extraction falls back to heuristic-only mode — functional but lower quality.

### Solution

1. Restart LM Studio on the local inference host (`http://${LM_STUDIO_HOST:-localhost:1234}`)
2. Monitor drain progress via `/status` — pending count should decrease steadily
3. Queue drains automatically at 30 items/min once LM Studio is reachable

### Verification

- `/status` shows SLM Queue pending count decreasing
- New messages get full SLM extraction (check `extraction_source` in logs)
- Queue stabilizes at 0 pending after drain completes

### Prevention

- LM Studio auto-start is configured via systemd on the local inference host
- Queue caps at 500 items to prevent unbounded growth
- No cloud fallback by design — extraction stays local

---

## Gateway: Duplicate Responses from Multiple Instances

**Issue ID**: GATEWAY-DUPLICATE-002
**First Occurrence**: 2026-02-17
**Status**: Fixed (2026-02-17)

### Symptoms

- User sends one WhatsApp message, receives two different responses
- Distinct from WHATSAPP-DUPLICATE-001 (Baileys reconnect dedup) — this is two separate LLM invocations
- Caused by two gateway processes running simultaneously

### Root Cause

The PID file singleton mechanism (`fcntl.flock` on `/tmp/nanobot-gateway.pid`) was defeated by deleting the PID file before starting a new instance. When the file is deleted and recreated, the new process flocks a different inode than the old process holds. Both processes think they have exclusive access.

Sequence:
1. Gateway A runs, holds flock on `/tmp/nanobot-gateway.pid` (inode X)
2. External restart does `rm -f /tmp/nanobot-gateway.pid && nanobot gateway`
3. Gateway B creates new `/tmp/nanobot-gateway.pid` (inode Y), flocks it successfully
4. Gateway A still runs with flock on deleted inode X — both process WhatsApp messages

### Solution Applied (2026-02-17)

Replaced passive flock-only singleton with active takeover approach in `nanobot/cli/commands.py`:

1. **Port-based detection**: On startup, `lsof -ti tcp:{port}` finds any process on the gateway port. Sends SIGTERM to each.
2. **PID file backup**: Reads old PID file and kills that process too (catches pre-bind startups).
3. **Grace period**: Waits 2 seconds after kills for clean shutdown.
4. **Flock retained**: Belt-and-suspenders — flock still acquired after port kill.
5. **No file deletion on exit**: PID file persists across restarts (atexit no longer deletes it).

### Verification

- Start gateway → start second gateway → first is auto-killed, second runs alone
- `ps aux | grep 'nanobot gateway'` shows exactly one process
- Send WhatsApp message → exactly one response

### Prevention

- Never manually `rm -f` the PID file before restart — just run `nanobot gateway` and it handles the old instance
- The port-based kill is immune to PID file manipulation

---

## Gateway: Duplicate Systemd Services SIGTERM Loop

**Issue ID**: GATEWAY-SYSTEMD-003
**First Occurrence**: 2026-02-18
**Status**: Fixed (2026-02-18)

### Symptoms

- Gateway starts, initializes fully (WhatsApp connects), then shuts down ~3 seconds later
- `asyncio.wait(FIRST_COMPLETED)` completes with `shutdown` task (shutdown_event set by SIGTERM)
- `agent` and `channels` tasks still pending — no code error, external signal
- systemd service restart counter climbs rapidly (13+ restarts observed)
- Gateway process sometimes hangs after shutdown (zombie-like, holds lock but doesn't serve)

### Root Cause

Two systemd user services managing the same gateway process:
1. `nanobot-gateway.service` — the original, pre-existing service (`Restart=always`)
2. `nanobot.service` — a duplicate created during a troubleshooting session

Both had `WantedBy=default.target` and were enabled. On boot (or restart), both started simultaneously:
- Service A starts, acquires port + lock
- Service B starts, runs `_kill_existing_gateway()` → kills Service A's process
- systemd detects Service A died → restarts it → it kills Service B
- Loop continues, each instance living only ~3 seconds before the other kills it

The SIGTERM appeared to come from "nowhere" because it was the *other* service's startup killing the current instance.

### Solution Applied (2026-02-18)

Removed the duplicate `nanobot.service`:
```bash
systemctl --user stop nanobot
systemctl --user disable nanobot
rm ~/.config/systemd/user/nanobot.service
systemctl --user daemon-reload
```

Kept the original `nanobot-gateway.service` which has proper dependencies (`Wants=whatsapp-bridge.service`).

### Verification

- `systemctl --user list-units --type=service | grep nano` → single service
- Gateway runs stable for 50+ seconds after restart
- WhatsApp connects successfully
- No SIGTERM in logs

### Prevention

- Before creating new systemd services, check for existing ones: `systemctl --user list-units --type=service --all | grep nano`
- Only one service should manage the gateway process
- The `_kill_existing_gateway()` port-based detection is correct behavior — the problem was having two managers, not the kill logic

---

## Memory: Qdrant API Migration (search → query_points)

**Issue ID**: MEMORY-QDRANT-001
**First Occurrence**: 2026-02-18
**Status**: Fixed (2026-02-18, commit fc812a5)

### Symptoms

- `/status` shows: `'AsyncQdrantClient' object has no attribute 'search'`
- Episodic memory recall fails silently (proactive recall skipped due to 2s timeout)
- Memory tool searches return errors

### Root Cause

`qdrant-client` v1.7+ removed the deprecated `search()` method in favor of `query_points()`. The installed version (1.16.2) no longer has `search()`. Code in `episodic.py` was using the old API.

### Solution Applied (2026-02-18)

Changed `nanobot/copilot/memory/episodic.py`:
```python
# Old:
results = await self._client.search(collection_name=..., query_vector=vector, ...)

# New:
response = await self._client.query_points(collection_name=..., query=vector, ...)
results = response.points
```

Return type changed from `list[ScoredPoint]` to `QueryResponse` — extracting `.points` preserves the same iteration interface.

### Verification

- Send message → no `search` attribute errors in logs
- `/status` → Qdrant alerts cleared

### Prevention

- When upgrading qdrant-client, check for API breaking changes
- The 2s timeout on proactive recall means Qdrant issues degrade gracefully rather than blocking

---

## Routing: `/use <provider>` Sends Wrong Model

**Issue ID**: ROUTING-MODEL-001
**First Occurrence**: 2026-02-18
**Status**: Fixed [RESOLVED 2026-02-20 — Router V2 eliminates heuristic classification and clears agents.defaults.model]

### Symptoms
- `/use minimax` sent `anthropic/claude-opus-4.6` to MiniMax's API → failed
- Every non-gateway provider had this problem — model always came from RouterProvider's `_big_model`
- `/use` (bare, no args) fell through to LLM as a regular message instead of showing help

### Root Cause
RouterProvider's force_provider logic used `_big_model` (an Anthropic model) when no explicit model was given. Non-gateway providers (MiniMax, OpenAI, DeepSeek, etc.) can't serve Anthropic models. There was no per-provider default model concept.

Additionally, `/use` without a trailing space didn't match `startswith("/use ")`.

### Solution Applied
1. Added `default_model` field to `ProviderConfig` in `config/schema.py`
2. RouterProvider accepts `provider_models` dict — checks before falling back to `_big_model`
3. `/use` handler validates provider exists, shows actual model being used
4. Bare `/use` shows available providers with their default models
5. Config updated: `~/.nanobot/config.json` providers section now has `defaultModel` per provider

### Verification
`/use minimax` → shows "Routing to minimax (MiniMax-M2.5)". `/use` (bare) → shows provider list.

### Prevention
- When adding a new provider, set its `defaultModel` in config
- Gateways (OpenRouter, AiHubMix) don't need a default model — they can route any model

---

## Status: Wrong Context Window Triggers Overflow Escalation

**Issue ID**: STATUS-CONTEXT-001
**First Occurrence**: 2026-02-18
**Status**: Fixed

### Symptoms
- MiniMax-M2.5 session showed "6,228 / 8,192 tokens (76%)" — wrong context window
- Router triggered "overflow" escalation → sent one call through openrouter/opus at $0.32
- User didn't request opus — the router auto-escalated because it thought context was full
- Cost tracking showed `$0.00` for MiniMax calls (pricing data missing)

### Root Cause
`copilot/context/budget.py` only had Anthropic models in `_MODEL_WINDOWS`. Unknown models defaulted to 8,192 tokens. MiniMax-M2.5 actually has 200K context. When the session hit 76% of the (incorrect) 8K limit, the router overflow logic kicked in.

**IMPORTANT**: The default context window is now **128K** (was 8K). This is a safer modern assumption — most current models support at least 128K. But any model with a smaller window (old local models) should be explicitly listed in `_MODEL_WINDOWS` to avoid overestimating their capacity. Overestimating context is less dangerous than underestimating (overflow escalation costs real money and confuses users), but it can still cause issues if messages are too long for the actual model.

### Solution Applied
1. Added context windows: OpenAI (128K), DeepSeek (128K), Gemini (1M), MiniMax (200K)
2. Changed `_DEFAULT_WINDOW` from 8,192 to 128,000
3. Added pricing for all non-Anthropic providers in `cost/logger.py`
4. Added prefix stripping for `minimax/`, `deepseek/`, `gemini/` in cost calculator

### Verification
`/status` shows correct context window for MiniMax-M2.5 (200K). Cost tracking shows non-zero for MiniMax calls.

### Prevention
- When adding a new provider/model, add its context window to `_MODEL_WINDOWS` in `budget.py`
- When adding a new provider/model, add its pricing to `_PRICING` in `cost/logger.py`
- The 128K default is deliberately generous — false overflow escalation ($$$) is worse than slightly overestimating context

---

## Status: Heartbeat Showing Copilot Health Check Instead of Nanobot Heartbeat

**Issue ID**: STATUS-HEARTBEAT-001
**First Occurrence**: 2026-02-18
**Status**: Fixed

### Symptoms
- `/status` showed "Heartbeat: 6m ago" when nanobot heartbeat (2h interval) hadn't fired yet
- Was actually displaying copilot health check (30min interval) from `heartbeat_log` table
- Route_log DB fallback queried non-existent `route_log` table (actual: `routing_log`)

### Root Cause
The status aggregator had a fallback: if `HeartbeatService.last_tick_at` was None (e.g. after restart, before first tick), it queried `heartbeat_log` — which is written by the copilot health check, not the nanobot heartbeat. This showed misleading data.

### Solution Applied
1. Removed `heartbeat_log` fallback — shows "not yet" until nanobot heartbeat actually runs
2. Fixed `route_log` → `routing_log` table name

### Prevention
- Be explicit about which heartbeat is being tracked in `/status`
- Always verify table names against actual schema (`sqlite3 ... ".tables"`)

---

## Extraction: Silently Failing, Structured Items Always 0

**Issue ID**: EXTRACT-FALLBACK-001
**First Occurrence**: 2026-02-18
**Status**: Fixed

### Symptoms
- `Structured items: 0` in `/status` despite active conversations
- SLM queue showed only embedding jobs (15 pending, 5 completed) — zero extraction jobs
- Repeated log spam: `Extraction failed for whatsapp:...: '\n  "facts"'`
- All extraction results were regex heuristics (near-zero useful data)

### Root Cause
Three compounding failures:
1. **Unescaped braces in prompt template.** `_EXTRACTION_PROMPT` contained a JSON schema example with `{` and `}` braces. Python's `.format()` interpreted `{"facts": ...}` as a format variable, throwing `KeyError: '\n  "facts"'`. This meant extraction NEVER worked — not local, not cloud, nothing. The `.format()` call at line 118 crashed before any LLM was ever called.
2. **No cloud fallback for extraction.** The code comment said "Cloud fallback (Haiku) is intentionally skipped." Even after fixing the brace issue, when LM Studio went down, extraction had nowhere to go except the regex heuristic.
3. **Queue code was unreachable.** The exception in `_run()` meant `extract()` never returned normally, so the SLM queue enqueue code was never reached for extraction jobs.

**Fix:** Double the braces in the JSON schema portion of `_EXTRACTION_PROMPT` (`{` → `{{`, `}` → `}}`). Python's `.format()` treats `{{` as a literal `{`.

### Solution Applied
1. Added cloud fallback to `extract()`: local SLM → cloud (Haiku ~$0.001/call) → queue → heuristic
2. Added dedicated extraction API key in secrets.json (`cloudExtractionApiKey`) using OpenRouter
3. Added config fields: `cloud_extraction_api_key`, `cloud_extraction_api_base`, `cloud_extraction_model`
4. Better error logging (includes exception type name)
5. Falls back to main cloud provider if no dedicated key configured

### Verification
After a WhatsApp message, check logs for `Cloud extraction succeeded` or `Extraction done for`. Check `Structured items` in `/status` — should increase over time.

### Prevention
- Every background service that depends on LM Studio MUST have a cloud fallback
- Pattern: local (free) → cloud (cheap) → queue (deferred) → heuristic (last resort)
- Both embeddings and extractions now have dedicated API keys in secrets.json

---

## Status: Added Background Processing Diagnostics

**Issue ID**: STATUS-DIAGNOSTICS-001
**First Occurrence**: 2026-02-18
**Status**: Enhancement (not a bug fix)

### Context
After fixing extraction fallback (EXTRACT-FALLBACK-001), there was no way to verify via `/status` that extraction and embedding were actually working. The SLM queue section showed job count but not the breakdown by work type.

### Changes Applied
1. **Background Processing section** added to `/status` output:
   - Extraction source: `local`, `cloud`, or `heuristic` (tracks `BackgroundExtractor._last_source`)
   - Extractions today: count from session metadata
   - Embedding source: `local`, `cloud`, or `down`
2. **SLM Queue breakdown** now shows count by work_type (embedding vs extraction)
3. **`breakdown()` method** added to `SlmWorkQueue` — groups pending items by work_type
4. **Extractor wired into status aggregator** via `status_aggregator._extractor = extractor`

### Files Modified
- `nanobot/copilot/status/aggregator.py` — new `extraction_stats` and `queue_breakdown` fields in `DashboardReport`, `_get_extraction_stats()` method, updated `to_text()`
- `nanobot/copilot/slm_queue/manager.py` — added `breakdown()` method
- `nanobot/copilot/extraction/background.py` — added `_last_source` tracking
- `nanobot/cli/commands.py` — wired `status_aggregator._extractor = extractor`

### Backfill Command
Added `nanobot backfill-extractions <session.jsonl> [--dry-run]` CLI command to run cloud extraction on missed chat history from session JSONL files. Reads user/assistant pairs, runs cloud extraction (Haiku), stores results in session metadata.

---

## Extraction: Structured Items Still 0 After Cloud Fallback Fix

**Issue ID**: EXTRACT-STRUCTURED-002
**First Occurrence**: 2026-02-19
**Status**: Fixed (2026-02-19)

### Symptoms

- `Structured items: 0` in `/status` despite 115 episodes and working extraction pipeline
- 36 items actually exist in `memory_items` table — all at confidence 0.5 (invisible to display)
- 5 queued cloud extractions all failed: "Cloud extraction returned unparseable JSON"
- 39 completed embeddings in queue, zero completed extractions

### Root Cause

Two compounding issues after EXTRACT-FALLBACK-001 was fixed:

1. **Display threshold too high**: `get_high_confidence_items(min_confidence=0.6)` filtered out all 36 items. Items start at 0.5 and only boost to 0.6 on duplicate key match (`UNIQUE(category, key[:100])`). With only 19 extraction-sourced items and varied wording, no two items had the same key — so none crossed 0.6.

2. **Cloud extraction JSON parsing too strict**: `_parse_json()` stripped markdown fences and attempted `json.loads()` on the full response. When Haiku 4.5 wrapped its JSON in preamble text ("Here is the extraction:\n{...}"), the parse failed. The `{` was never at position 0, so `json.loads()` threw JSONDecodeError.

### Solution Applied (2026-02-19)

1. **`nanobot/copilot/memory/manager.py`**: Lowered `min_confidence` from 0.6 to 0.4. Items are now visible on first appearance. 36 previously hidden items immediately visible.

2. **`nanobot/copilot/extraction/background.py`**: `_parse_json()` now has a fallback: if full-text parse fails, finds first `{` to last `}` and attempts to parse that substring. This handles LLM preamble/postamble wrapping.

### Verification

- `SELECT COUNT(*) FROM memory_items` → 36 (was showing 0 in status)
- Future cloud extractions should parse successfully even with LLM preamble
- Queue extractions that previously failed will be retried on next drain cycle

### Prevention

- JSON parsing from LLMs should always handle wrapping text — LLMs rarely return bare JSON
- Display thresholds should be validated against actual data distribution
- This is the same symptom as EXTRACT-FALLBACK-001 but different root causes — always re-investigate when a "fixed" metric stays at 0

---

## Template for New Issues

When adding new issues to this document, use the following template:

```markdown
## [Component]: [Brief Description]

**Issue ID**: [COMPONENT-KEYWORD-NNN]
**First Occurrence**: [Date]
**Status**: [Open/Fixed/Workaround]

### Symptoms
- Bullet point list of observed behavior

### Root Cause
Technical explanation of why the issue occurs

### Solution Applied
Code changes, configuration updates, or workarounds

### Verification
How to confirm the fix works

### Prevention
Steps to prevent recurrence
```
