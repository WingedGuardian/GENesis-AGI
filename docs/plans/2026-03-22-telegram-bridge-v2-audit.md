# Telegram Bridge V2 — Audit Report & Fix Status

**Date:** 2026-03-22
**Scope:** `genesis/channels/telegram/adapter_v2.py`, `handlers_v2.py`, and all `transport/` modules
**Reviews:** 3 independent reviews (manual + 2 coding agents), cross-referenced

---

## CRITICAL — Fixed

### 1. Disappearing messages — draft skip logic (P0)
**Files:** `handlers_v2.py` (text + voice handlers)
**Problem:** `send_message_draft()` creates ephemeral previews (returns `bool`,
not `Message`). Handler skipped final real message when draft text matched
response. Draft vanishes, user sees nothing.
**Fix:** Removed `streamer_delivered`/`streamer_ok` skip logic. Always send
final message via `_reply_formatted()`. Drafts remain as UX preview.
**Status:** FIXED

### 2. `typing_ka.stop()` not in `finally` — task leak
**Status:** FIXED (prior commit 356dc74)

### 3. Voice handler `enabled` vs `any_draft_sent` — duplicate messages
**Status:** FIXED (prior commit 356dc74), then superseded by fix #1 above

---

## SIGNIFICANT — Fixed

### 4. Exception chain traversal misses `__context__`
**File:** `send_safety.py`
**Problem:** Only followed explicit `raise X from Y` chains, not implicit PTB wrapping.
**Fix:** Added `_unwrap()` helper checking both `__cause__` and `__context__`.
**Status:** FIXED

### 5. Stall recovery partial failure
**File:** `adapter_v2.py`
**Problem:** If `updater.stop()` fails, `start_polling()` still attempted.
**Fix:** Return early if stop fails — don't start_polling on half-stopped updater.
**Status:** FIXED

### 6. Polling watchdog false-positive stall loop
**File:** `transport/polling.py`
**Problem:** Watchdog fired every 90s when no user activity — can't distinguish
"quiet chat" from "broken polling." Bridge logs showed constant restart spam.
**Fix:** Added consecutive-stall backoff (doubles threshold each time, caps at
15 minutes). `record_activity()` resets backoff.
**Status:** FIXED

### 7. Pending settings bleed across sessions on DB error
**File:** `handlers_v2.py`
**Problem:** If `update_model_effort()` raised, settings were never popped.
**Fix:** Pop before DB call (always pop). Extracted `_apply_pending_settings()` helper.
**Status:** FIXED

### 8. `_active_interrupts` keyed by user.id — cross-chat confusion
**File:** `handlers_v2.py`
**Problem:** `/stop` in one chat could interrupt request in another chat.
**Fix:** Key by `(user.id, chat_id)` tuple.
**Status:** FIXED

### 9. `send_voice` has no retry logic
**File:** `adapter_v2.py`
**Problem:** Called `bot.send_voice()` directly without safe_send wrapper.
**Fix:** Created `safe_send_voice()` in `transport/send.py`, wired into adapter.
**Status:** FIXED

### 10. `safe_send_document` missing thread fallback
**File:** `transport/send.py`
**Problem:** Unlike `safe_send_message`, didn't retry without thread on "thread not found".
**Fix:** Added same `BadRequest` thread-fallback logic.
**Status:** FIXED

---

## MINOR — Fixed

### 11. User message content in INFO logs
**File:** `handlers_v2.py:419`
**Fix:** `msg.text[:80]` → `len(msg.text)`.
**Status:** FIXED

### 12. DraftStreamer uses `time.time()` instead of `time.monotonic()`
**File:** `transport/streaming.py`
**Status:** FIXED

### 13. DraftStreamer permanently disables on transient errors
**File:** `transport/streaming.py`
**Fix:** 3 consecutive failures before disable, resets on success.
**Status:** FIXED

### 14. Bridge V1 log says "(default)" incorrectly
**File:** `bridge.py:138`
**Fix:** Changed to "(fallback via TG_ADAPTER=v1)".
**Status:** FIXED

### 15. Dead code: `is_retryable_telegram_error`
**File:** `transport/network_config.py`
**Status:** DELETED

### 16. Duplicated pending settings logic
**File:** `handlers_v2.py`
**Fix:** Extracted `_apply_pending_settings()` helper.
**Status:** FIXED

---

## OPEN — Not Fixed This Cycle

### Negative `message_id` for genesis responses
**File:** `handlers_v2.py:547,710`
**Problem:** `-msg.message_id` as synthetic ID is fragile.
**Status:** FIXED — added `direction` column to `telegram_messages` table.
Uses real message_id + direction='outbound' instead of negative IDs.
Schema migration auto-converts existing negative IDs on bootstrap.

### Ephemeral lock when `adapter` is `None`
**File:** `handlers_v2.py:441-444`
**Status:** DEFERRED — adapter is always non-None in production.

### Duplicate `_thread_id` / `_thread_id_from_msg`
**Status:** DEFERRED — take different input types (Update vs message).

### Offset persistence unwired
**File:** `transport/offset_store.py`
**Status:** FIXED — offset read on start(), written on each update and on stop().
Uses PTB's internal `_last_update_id` to persist across restarts.

---

## Gap Analysis

| Gap | Priority | Status |
|-----|----------|--------|
| Health check endpoint | HIGH | FIXED — `/api/genesis/health` returns 200/503 with bridge data |
| Session timeout/cleanup | MEDIUM | FIXED — session marked failed on CC timeout; reaper tightened to 7 days |
| Task observation in streaming | BUG | FIXED — `handle_message_streaming()` now emits `task_detected` observations |
| Message edit for corrections | HIGH | OPEN |
| Per-user rate limiting | MEDIUM | OPEN |
| Message edit detection | MEDIUM | OPEN |
| Document size pre-check | LOW | OPEN |
| `/help` command | LOW | OPEN |
| Photo caption processing | LOW | OPEN |
| Callback query handler | LOW | OPEN |
| Webhook mode | LOW | OPEN |
| Metrics/counters | LOW | OPEN |

---

## Origin Classification

| Category | Count |
|----------|-------|
| Original V2 design flaw | ~15 |
| Introduced/worsened by bug fix | 3 |
| Inherited from V1 | 1 |
