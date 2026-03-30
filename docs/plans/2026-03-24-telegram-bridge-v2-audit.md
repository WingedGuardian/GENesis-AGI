# Telegram Bridge V2 — Independent Bug Audit

**Date:** 2026-03-24
**Scope:** `genesis/channels/telegram/` (V2 primary, V1 secondary)
**Reviewer:** Independent audit (code walkthrough, no runtime)
**Confidence framework:** Explicit % with reasoning; separate root-cause from fix confidence

---

## CRITICAL — Must Fix

### 1. Early return in `_handle_voice_inner` skips interrupt cleanup (P0)

**File:** `handlers_v2.py:686-688`

```python
if not text:
    await msg.reply_text("(couldn't transcribe audio)")
    return   # ← Bypasses finally block
```

**Problem:** When STT returns empty, the function returns early after sending a user-facing reply. The `finally` block at lines 777-779 (`await typing_ka.stop()` + `_active_interrupts.pop()`) is never reached.

**Impact:** Stale `interrupt_event` entries accumulate in `_active_interrupts` keyed by `(user.id, msg.chat.id)`. A subsequent `/stop` in the same session could retrieve and set a stale event from a previously failed voice session, causing premature interruption of an unrelated request.

**Fix confidence:** 95% (path is unambiguous — early return always skips finally)

**Fix approach:** Replace `return` with structured flow — move the error reply into a `try/except` or use a flag to let the function fall through to the `finally` block.

---

### 2. Outbound message IDs not captured — breaks outreach correlation (P0)

**Files:** `handlers_v2.py:568-574` (text), `handlers_v2.py:730-735` (voice)

**Problem:** `_reply_formatted(msg, response)` calls `msg.reply_text()` which returns the sent `Message` object. This return value is discarded. `_persist_tg_message` is then called with the **user's incoming** `msg.message_id`, not the actual sent response's `message_id`:

```python
# Line 577-580 — outbound stored with user's message_id
await _persist_tg_message(
    msg.chat.id, msg.message_id, "genesis", response,  # ← msg.message_id is user's
    thread_id=tid, direction="outbound",
)
```

**Impact:** `telegram_messages` table stores incorrect `message_id` for outbound messages. Any code that correlates outbound message IDs (e.g., outreach `reply_waiter` that relies on quote-reply matching to `message_id`) will use wrong IDs. The `direction='outbound'` column partially mitigates this by allowing same ID twice (one per direction), but the actual sent message's ID is lost.

**Fix confidence:** 90% (the design intent is clear from the audit doc mentioning the negative-ID fix, but the capture of the actual sent message_id was missed)

**Fix approach:** Change `_reply_formatted` to return the sent `Message` (or return it as a named result), then capture `msg.message_id` from the returned object for persistence.

---

## SIGNIFICANT

### 3. Stale `_active_interrupts` entries from interrupted sessions

**File:** `handlers_v2.py:508-510, 618`

**Problem:** `interrupt_event` is registered into `_active_interrupts` before `handle_message_streaming` runs. The `finally` block pops it. But the flow is:

```
if interrupt_event.is_set():   # → pops (line 618 via finally)
    ...
else:                         # → also pops (line 618 via finally)
    ...
```

This is actually correct — both branches reach `finally`. The real risk is: if an exception occurs after `interrupt_event` is set but before `finally`, the entry stays. However `finally` always runs.

**Confidence:** 70% — mostly handled, minor residual risk from very unusual exception types

---

### 4. V1 adapter missing `documents` in capabilities

**File:** `adapter.py:147-154` (V1), `adapter_v2.py:244-252` (V2)

```python
# V1
def get_capabilities(self) -> dict:
    return {
        "markdown": True,
        "buttons": True,
        "reactions": False,
        "voice": True,
        "max_length": 4096,    # ← no "documents"
    }

# V2 adds:
        "documents": True,
```

**Impact:** If outreach code checks `get_capabilities()["documents"]` on V1, it gets `KeyError` or false. The `send_document` method exists on both adapters, so this is a capability reporting bug only.

**Confidence:** 80%

---

### 5. V1 handler doesn't pass `reply_to_message_id` for outbound persistence

**File:** `handlers.py:517-520`

```python
await _persist_tg_message(
    msg.chat.id, -msg.message_id, "genesis", response,
    thread_id=tid,
    # ← reply_to_message_id not passed
)
```

**Impact:** Outbound messages in V1 aren't threaded to their request message in the DB. Less severe than #2 since V1 uses negative IDs which are being phased out anyway.

**Confidence:** 85%

---

### 6. Error handler creates empty exception instead of passing real error

**Files:** `handlers_v2.py:613`, `handlers_v2.py:774`

```python
except Exception:
    log.exception("CC request failed for user %s", user.id)
    try:
        await msg.reply_text(_format_error(Exception()))  # ← fresh Exception(), not `e`
    except Exception:
        log.error("Failed to send error reply to user %s", user.id, exc_info=True)
```

**Impact:** Logging is accurate (`log.exception` uses the real `e`), but `_format_error(Exception())` produces `"Sorry, something went wrong."` (the fallback) even when the actual error was more specific. The user gets the correct generic message; the format_error is just suboptimal.

**Confidence:** 95%

---

## MINOR

### 7. No `/help` command

`/start` lists available commands, but `/help` is a standard Telegram convention. Users typing `/help` get no response.

**Confidence:** 90%

---

### 8. Redundant mkdir on every `write_offset`

**File:** `offset_store.py:45`

`write_offset` always calls `ensure_telegram_dir()` (which does `mkdir -p`) even after the first write. The directory already exists. Debounced writes mitigate the I/O cost.

**Confidence:** 95%

---

### 9. `handle_photo` is a stub

**File:** `handlers_v2.py:781-788`

Returns a placeholder text message. Known gap — photo analysis via vision is planned for later.

**Confidence:** 100%

---

### 10. No rate limit on `/model` and `/effort` commands

**Files:** `handlers_v2.py:293-329`, `handlers_v2.py:331-368`

A user could hammer these endpoints. Not exploited in practice.

**Confidence:** 80%

---

### 11. Spurious `exc_info=True` in warning log

**File:** `handlers_v2.py:83`

```python
except BadRequest:
    log.warning("HTML send failed, falling back to plain text", exc_info=True)
```

`exc_info=True` adds a stack trace when the actual `BadRequest` is caught and handled as an expected case. Minor log noise.

**Confidence:** 95%

---

### 12. V1 logs user message content at INFO level

**File:** `handlers.py:464`

```python
log.info("Text from %s: %s", user.id, msg.text[:80])
```

Already fixed in V2 (`len(msg.text)` at line 456).

**Confidence:** 100%

---

## SECOND-ORDER ISSUES

### 13. `reply_waiter.resolve()` silently does nothing without a waiter

**File:** `handlers_v2.py:465-469`

```python
if msg.reply_to_message and reply_waiter:
    reply_to_id = str(msg.reply_to_message.message_id)
    if reply_waiter.resolve(reply_to_id, msg.text):  # returns bool
        log.info("Outreach reply detected for delivery %s", reply_to_id)
        return
```

If no waiter is registered, `resolve()` returns `False` silently. The user's reply-to is discarded without any indication. Not a crash, but could confuse debugging when outreach reply detection doesn't work.

**Confidence:** 80%

---

### 14. `_reply_formatted` doesn't return the sent message

**File:** `handlers_v2.py:87-96`

Unlike `_send_formatted` (which returns the `Message`), `_reply_formatted` returns `None`. This forced the capture problem in issue #2.

**Confidence:** 90%

---

## SUMMARY TABLE

| # | Severity | File | Issue | Confidence | Resolution (2026-03-24) |
|---|----------|------|-------|------------|------------------------|
| 1 | CRITICAL | handlers_v2.py:686 | Early return skips interrupt cleanup | 95% | **FALSE POSITIVE** — Python `finally` always runs on `return`. Not a bug. |
| 2 | CRITICAL | handlers_v2.py:577,739 | Outbound message_id not captured/stored | 90% | **FIXED** — `_reply_formatted` now returns Message, handlers capture sent ID. |
| 3 | SIGNIFICANT | handlers_v2.py:508-618 | Stale `_active_interrupts` entries | 70% | **NOT A BUG** — `finally` block always runs. Residual risk minimal. |
| 4 | SIGNIFICANT | adapter.py:147 | V1 missing `documents` capability | 80% | **INCORRECT** — V1 correctly omits `documents` because it lacks `send_document()`. |
| 5 | SIGNIFICANT | handlers.py:517 | V1 outbound msg not threaded in DB | 85% | **DEFERRED** — V1 deprecated; direction column partially mitigates. |
| 6 | SIGNIFICANT | handlers_v2.py:613,774 | Empty Exception() in error handler | 95% | **FIXED** — All 4 instances (V1+V2) now pass caught `e` to `_format_error`. |
| 7 | MINOR | handlers_v2.py | No `/help` command | 90% | **FIXED** — `/help` aliased to `/start`, registered in adapter. |
| 8 | MINOR | offset_store.py:45 | Redundant mkdir on every write | 95% | **DEFERRED** — Low impact, debounced writes mitigate I/O cost. |
| 9 | MINOR | handlers_v2.py:781 | `handle_photo` stub | 100% | **ACCEPTED** — Known gap, photo analysis planned for later. |
| 10 | MINOR | handlers_v2.py:293-368 | No rate limit on /model /effort | 80% | **DEFERRED** — Single-user bot, not exploitable in practice. |
| 11 | MINOR | handlers_v2.py:83 | Spurious exc_info in warning log | 95% | **FIXED** — Removed from both `_send_formatted` and `_reply_formatted`. |
| 12 | MINOR | handlers.py:464 | User content in V1 logs | 100% | **ALREADY FIXED** — V2 logs `len(msg.text)` not content. V1 deprecated. |
| 13 | SECOND-ORDER | handlers_v2.py:467 | reply_waiter silently ignores no-waiter case | 80% | **BY DESIGN** — `resolve()` returns bool, caller checks. Now tested. |
| 14 | SECOND-ORDER | handlers_v2.py:87 | `_reply_formatted` doesn't return sent message | 90% | **FIXED** — Now returns `Message | None`. |

---

## RESOLUTION SUMMARY (2026-03-24)

- **4 FIXED**: #2 (outbound message ID), #6 (Exception handler), #7 (/help), #11 (exc_info), #14 (_reply_formatted return)
- **2 FALSE POSITIVE**: #1 (finally always runs), #4 (V1 correctly omits documents)
- **1 BY DESIGN**: #13 (reply_waiter silent-no-waiter)
- **1 ALREADY FIXED**: #12 (V2 doesn't log content)
- **1 ACCEPTED**: #9 (photo stub, planned feature)
- **3 DEFERRED**: #5 (V1 threading), #8 (mkdir), #10 (rate limit)
- **1 LOW RISK**: #3 (stale interrupts — finally handles it)

**Test coverage added**: 20 new V2 tests (10 → 30). Covers voice handler, outreach reply detection, error paths, /help, photo handler, DraftStreamer interaction.

## RECOMMENDED FIX ORDER (ORIGINAL — retained for reference)

1. ~~**#1** — Fix the early return in voice transcription (interrupt leak)~~ FALSE POSITIVE
2. ~~**#2** — Capture and store actual outbound message_id (outreach correlation)~~ FIXED
3. ~~**#6** — Pass real exception to `_format_error` (correct user messaging)~~ FIXED
4. ~~**#4** — Add `documents` to V1 capabilities (correct capability reporting)~~ INCORRECT
5. **#5** — Thread V1 outbound messages in DB (data completeness) — DEFERRED (V1 deprecated)
6. ~~**#7** — Add `/help` command (UX parity with `/start`)~~ FIXED
7. ~~**#11** — Remove `exc_info=True` from expected BadRequest warning (log noise)~~ FIXED
