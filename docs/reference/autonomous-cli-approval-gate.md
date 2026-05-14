# Autonomous CLI Approval Gate — UX and Gating Semantics

The autonomous CLI fallback approval gate guards any background
subsystem that wants to spawn a real Claude Code session when its
primary API path fails (or when it has no API path at all, like inbox).
Approvals are delivered to Telegram with inline buttons and gated
per-call-site so the system **pauses** a call site while an approval
is in flight instead of queuing duplicate prompts.

## Delivery destination

Approvals are posted to the `"Approvals"` topic in the supergroup
configured via `TELEGRAM_FORUM_CHAT_ID`. The topic is auto-created on
first use by `TopicManager.get_or_create_persistent("approvals")`.

- Source: `src/genesis/channels/telegram/topics.py` — `DEFAULT_CATEGORIES`
  + `resolve_outreach_category("approval") → "approvals"`
- Routing: `config/outreach.yaml` — `delivery_routing.approval: supergroup`
- Category: `OutreachCategory.APPROVAL` (`src/genesis/outreach/types.py`)

**Approvals never deliver to the general DM.** Prior to this change,
`AutonomousCliApprovalGate._send_request` bypassed the outreach pipeline
and called `adapter.send_message` directly, which skipped all category
routing. The refactor moves delivery to `pipeline.submit_raw` with
`OutreachRequest(category=APPROVAL)`, picking up topic routing for free.

## Inline button UX

Every approval message carries an inline keyboard built by
`AutonomousCliApprovalGate._send_request`:

1. **Row 1:** A single `✅ Approve` button whose `callback_data` is
   `cli_approve:{request_id}`. Tapping resolves only that request.
2. **Row 2 (conditional):** A `✅✅ Approve all N pending` button whose
   `callback_data` is `cli_approve_all:{request_id}`. Appears only when
   at least two gated approvals are pending at send time (the triggering
   one plus at least one other). Tapping resolves the triggering request
   first, then calls `approve_all_pending` to clear every other pending
   approval regardless of action type or subsystem.

There is **no reject button.** Rejection is handled by not clicking —
the approval stays parked until the 24h re-ask fires a reminder, and
the subsystem stays blocked. Explicit rejection still works via
`resolve_from_reply` (typing "reject" as a quote-reply) or the
dashboard approvals API.

## Resolution paths

The handler recognizes three ways to resolve an approval, in priority
order:

1. **Inline button press** — `cli_approve:{id}` / `cli_approve_all:{id}`
   via `handle_callback_query` in
   `src/genesis/channels/telegram/_handler_messages.py`. Bypasses
   `ReplyWaiter` and calls the gate directly.
2. **Bare text in the Approvals topic** — typing a single bare word
   (`approve`, `ok`, `yes`, `reject`, `no`, etc.) in the Approvals
   topic. The text handler looks up the most recent pending
   `autonomous_cli_fallback` row via
   `AutonomousCliApprovalGate.resolve_most_recent_pending` and resolves
   it. Only matches exact single-token words with optional trailing
   punctuation so general conversation never triggers.

   ⚠️ **IMPORTANT**: bare text resolves **only the most recent pending
   request**, not the one you happen to be looking at. If you have
   three pending approvals and you type `reject`, only the newest of
   the three is rejected. The ack reply always shows which request_id
   was resolved so you can verify. If you want to target a specific
   message, tap the inline button on that message instead, or
   quote-reply to it directly.
3. **Quote-reply to a specific message** — the legacy
   `resolve_from_reply(delivery_id, reply_text)` path. Still works for
   users who formally quote-reply. **Single-reply no longer
   auto-batches** (removed from the April 10 UX redesign) — if you
   reply "approve" to one message, only that message resolves.

## Call-site gating

The gate implements *gating*, not deduplication: when an approval is
pending for a given `(subsystem, policy_id)` tuple, that call site
**stops scheduling new work** until the approval resolves. Each call
site performs a pre-check before building an expensive invocation:

```python
pending = await self._autonomous_dispatcher.approval_gate.find_site_pending(
    subsystem="reflection",
    policy_id="reflection_deep",
)
if pending is not None:
    return  # skip this cycle entirely — no new row, no new approval
```

Gated call sites and their `(subsystem, policy_id)` keys:

| Call site | subsystem | policy_id |
|---|---|---|
| Inbox monitor | `inbox` | `inbox_evaluation` |
| Ego cycle | `ego` | `ego_cycle` |
| Reflection (per depth) | `reflection` | `reflection_{light,deep,strategic}` |
| Task executor (per step type) | `task_executor` | `executor_{code,research,analysis,synthesis,verification}` |

Depths and step types are independently gated — deep reflection can
be blocked on approval while light reflection continues running.

### Race safety

If two concurrent schedulers both see "not blocked" and both create
approval requests, `AutonomousCliApprovalGate._find_existing` has a
secondary lookup that matches pending rows by `(subsystem, policy_id)`
when the primary content-stable `approval_key` misses. The second
caller finds the first caller's pending row and reuses it.

## Inbox resume pass — state-transition semantics

When an inbox evaluation is parked awaiting approval, the row stays in
`status='processing'` with an `awaiting_approval:<request_id>` marker
in `error_message`. On every scan, the resume pass:

1. Hash-validates each awaiting row's file (vanished → invalidate;
   changed → invalidate with `approval_invalidated:content changed`).
2. Looks up the referenced approval via
   `approval_manager.get_by_id(request_id)`.
3. Routes by approval status:
   - **approved** → load file content and queue for dispatch (this
     time, `ensure_approval` sees the approved row and returns
     `cli_approved` → CC runs).
   - **rejected** → mark the inbox row failed AND bump `retry_count`
     to `max_retries` so the scanner won't re-detect and re-prompt.
   - **pending** → leave the row alone. No dispatch, no Telegram, no
     DB churn. Next scan re-checks.
   - **expired / cancelled / row missing** → mark the inbox row failed
     with the `approval_invalidated:` prefix; the next scan re-detects
     the file as new.

This replaces the earlier "re-dispatch every scan and rely on the
approval gate's stable key to dedup" approach. Dispatch now only fires
on `pending → approved` state transitions.

## Database

Adding `OutreachCategory.APPROVAL` required a schema migration because
`outreach_history.category` has a `CHECK` constraint that only allowed
the pre-existing values. See `_migrate_add_columns` in
`src/genesis/db/schema/_migrations.py` for the table-rebuild migration
(SQLite cannot `ALTER` a `CHECK` constraint). The migration is
idempotent — it checks the stored DDL for `'approval'` before
rebuilding.

## Key files

| Concern | File |
|---|---|
| Approval gate class | `src/genesis/autonomy/autonomous_dispatch.py` |
| Outreach category enum | `src/genesis/outreach/types.py` |
| Outreach routing config | `config/outreach.yaml` |
| Topic category + mapping | `src/genesis/channels/telegram/topics.py` |
| Callback handler | `src/genesis/channels/telegram/_handler_messages.py` |
| Handler context wiring | `src/genesis/channels/telegram/_handler_context.py` |
| Telegram startup — standalone (live) | `src/genesis/hosting/standalone.py::StandaloneAdapter._start_telegram` |
| Telegram startup — legacy bridge | `src/genesis/channels/bridge.py::main` |
| DB schema + migration | `src/genesis/db/schema/_tables.py`, `_migrations.py` |
| Runtime accessor | `src/genesis/runtime/_core.py::autonomous_cli_approval_gate` |
| Inbox resume pass | `src/genesis/inbox/monitor.py::_check_once_inner` |
| Pre-check call sites | `src/genesis/ego/session.py`, `src/genesis/cc/reflection_bridge/_bridge.py`, `src/genesis/autonomy/executor/engine.py`, `src/genesis/inbox/monitor.py` |

## Prerequisites

- `TELEGRAM_FORUM_CHAT_ID` must be set in `secrets.env`. Without it,
  `supergroup` routing degrades to DM and the topic UX is lost.
- The bot must have permission to create topics in the supergroup.
  `TopicManager.get_or_create_persistent` logs and returns `None` on
  permission errors.

## Manual verification

1. Drop a file in `~/inbox/`.
2. Verify an approval message appears in the **Approvals** supergroup
   topic (not the DM) with one `[✅ Approve]` button.
3. On subsequent scan cycles, verify the inbox monitor logs
   `Inbox new/modified file detection skipped — call site blocked on
   approval <id>`.
4. Tap ✅ — verify the message edits to `✅ Approved`, a real CC
   session fires, and the inbox row transitions to `completed` with a
   real response file.
5. Drop a second file while the first is still pending — verify the
   second message shows the `[✅✅ Approve all 2 pending]` button.
6. In the Approvals topic, type a bare `approve` (no quote-reply) —
   verify it resolves the most recent pending request.
