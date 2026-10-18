# Agent pull outbox

Genesis can originate a conversation with an explicitly enrolled same-owner
peer that has no HTTP receiver. `agent_send` uses the existing outreach pipeline
to queue an addressed A2A message. The peer polls, durably deduplicates, ACKs
collection, and replies through the asynchronous conversation API. Genesis and
the peer retain their own tool permissions and owner approval flows.

Requires the [conversation API](agent-conversations.md), enabled peer mode,
current same-owner enrollment and conversation allow. No new credential,
listener, webhook, network sender or mandatory incoming task/day budget.

## Sending from Genesis

The curated `genesis-outreach` tool `agent_send(peer_id, message, context_id?,
message_id?)` is available to permitted native interact turns. Observe and
restricted mail profiles deny it. An omitted context starts a server-issued
owned context; a supplied context must already belong to this peer and epoch,
or match the deterministic context for an exact retry of that message_id.
Pass a stable UUID message_id when retrying the same logical send. Reusing an
identity with changed content conflicts. Otherwise use a new message_id.

Every `agent_send` caller writes a typed envelope to `pending_outreach`, with a
stable queue identity, and reports queued_for_governance. The normal scheduler
drains it through governed submit; deferred peer retries also recheck governance.
An exact retry reports queued_for_collection only after local outbox insertion.
Queue closure/refusal is reported instead of successful delivery. The adapter
rechecks retained queue payload and closure before insertion, so canceled or
dequeued unsent rows cannot be resurrected by a stale delivery or retry.

Existing outbound quiet-hours, salience and spend policy still apply. This is
not a new incoming task quota. Identity-keyed peer_message dedup distinguishes
different peers and different logical sends sharing the same text. The canonical
typed message survives formatting and retry intact. Complete outgoing messages
use the same bounded disclosure policy as incoming-task results, including
embedded/decoded JSON. The envelope is limited to 256 KiB serialized UTF-8.

queued_for_governance means awaiting normal outbound checks;
queued_for_collection means available to poll, not that the peer collected,
understood or executed it. Collection ACKs do not resolve approvals or count as human
engagement, competence evidence or an earned authority grant.

## Collection contract

Use the existing scoped bearer and A2A-Version: 1.0 on both extension routes:

| Method | Path below /v1/agent/a2a | Contract |
| --- | --- | --- |
| GET | /outbox | messages + nextPageToken; no consumption |
| POST | /outbox/{messageId}:ack | Empty JSON object; idempotent collection ACK |

GET accepts pageSize (default20,1–100) and pageToken. Unknown/repeated options
are refused. A page item contains message (an A2A Message with ROLE_AGENT,
messageId, contextId and one text Part) and createdAt. Only the current peer's
current epoch is visible. Stable oldest-first keyset pagination works even if
an earlier page's anchor was ACKed. Each new poll cycle starts without a token
at the oldest unacknowledged message. GET never marks a row collected.

ACK returns messageId + acknowledged:true and does not erase the anchor.
Unknown/foreign/old-epoch IDs are unavailable. The same server-owned 256 KiB
body cap and absolute input deadline apply, including chunked bodies. ACK is
not a task cancellation, owner approval, message execution or reply.

## Required peer adapter behavior

1. Poll the outbox with the current enrollment and persist a local dedup row
   keyed by enrollment epoch + messageId before ACK or executing work. Retain
   the complete message/context and progress; ACK alone is insufficient.
2. Repeat ACK safely. Advance pageToken only within that poll cycle, and
   restart oldest-unacked on the next cycle. A lost GET or ACK causes replay;
   local dedup prevents repeated effects.
3. Process the saved message under the peer's own permissions. A Genesis
   message cannot approve an action on the peer's behalf.
4. Persist a stable reply messageId, then submit an ordinary A2A USER message
   with the saved contextId, configuration.returnImmediately=true and
   metadata.inReplyTo=<outgoing messageId>. Retry that exact payload after a
   lost response. Genesis validates owned correlation and reserves the first
   distinct reply in the same transaction as its native receipt.
5. Poll the returned task, fetch its result and continue with new messageIds
   and the same contextId. Later turns omit inReplyTo. A second distinct first
   reply conflicts even if the first task failed or was canceled; this prevents
   a stale retry becoming new execution. A new later turn can discuss failure.

Recent native prompt/reply heads and outgoing request heads provide bounded
logical history: newest8 events, heads up to4 KiB, overall64 KiB with omission
markers. This is not a lossless transcript or resumed CLI process.

## Persistence and restore

peer_outbox is one new SQLite table because an addressed message awaiting
collection is neither a native CC task nor an owner notification. Its immutable
peer/epoch/message/context fields stay consistent with registry enrollment;
inReplyTo is stored in the incoming native receipt, without a second later reply
writer. Existing SQLite backup covers outbox and native receipts together.
ACKs and native retry anchors are retained, with no automatic outbox purge.

Ordinary restore disables peer mode, removes grants and rotates epochs while
retaining inaccessible old-epoch outbox anchors. The existing informed Guardian
snapshot approval can authorize saved peer permissions; an older snapshot can
still replay collected messages or lose newer receipts. There is no exactly-once
claim across destructive purge/rollback. Keep client dedup durable and reset
enrollment/context/client state together when explicitly re-enrolling.

Before activation, prove connector-only ingress, complete the peer polling
adapter, deploy through the approved scripts and run real tailnet acceptance:
both directions, permitted/refused tools, owner approval cycles, cancellation,
lost responses/ACKs, duplicates and restart. Local synthetic-provider/adapter
checks do not establish production peer/model/Telegram readiness.
