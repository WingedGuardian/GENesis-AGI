# Peer roadmap and replacement boundaries

The immediate outcome is independent agents conversing through one authenticated
front door. Same-owner native A2A conversations, poll/full-result and the pull
outbox remain the small usable path. Explicitly shared cross-owner reasoning is
the next bounded expansion, with public context and no tools. Operational
federation and knowledge exchange are separate design work, not prerequisites
for the same-owner conversation client.

## Current native work

| Concern | Delivery / gate handoff |
|---|---|
| Effective quota-event state | #3572; preserve usable warning/overage responses and real failure/no-replay semantics. |
| Scheduler saved versus registered state | #3573; retain definitions for registration retry, report actual registration. No new peer scheduling authority. |
| Published task-state clock and immutable scoped cursor | #3574; apply before final combined client acceptance, and reconcile its shared conversation reader changes. |
| Contained native execution | #3575, replacement for #3241; native helpers/scopes, whole-lifetime isolation, no new peer grant by itself. |
| Explicit shared conversations | #3570 consumer; depends on #3575 and must preserve #3574's final snapshot/cursor contract. Same-owner compatibility and foreign owner-notice/new-workflow boundaries need combined acceptance. |
| Database lock failures | #3457 remains diagnosis/design work. Scratch evidence distinguishes stale-WAL and open-schema-lock behaviors; no generic reconnect/transaction rewrite is bundled here. |

These are build handoffs, not merge readiness. Closing owns exact-head reviews,
CI, findings, applicable owner authorization, dependency reconciliation and
post-review functional checks. Deploy uses approved scripts after merge.
Production acceptance additionally proves the actual peer's network/ACL path,
credential handling, configured provider readiness and owner channel. A local
synthetic fixture is not remote peer or Telegram acceptance.

For independent owners, only current active epoch plus conversation allow AND
conversation_shared allow can select public contained turns. Missing grants
remain closed. Owners authorize actions in a **new ordinary workflow**. No
foreign turn resumes with owner context/tools, and private results are not
shared back automatically. Existing scheduler tools handle owner-approved
recurrence proposals; there is no new peer schedule endpoint or task/day budget.

## Parked branches are not a merge checklist

The older stack uses a different execution/ownership model and must not be
merged mechanically after these native changes. Its titles describe broader
operational work; conversation-only coverage does not dispose of that scope.

| Older PR | Disposition for this roadmap |
|---|---|
| #3241 constrained invocation | #3575 replaces the containment boundary. Leave the old PR open for the closing session's posted file-by-file coverage check, per its owner amendment. |
| #3104 contained background-session ownership | Parked; current native receipts serve conversation ownership. Any additional operational ownership requirements need a fresh design/reconciliation with #3575, not its older base. |
| #3105 private MCP capability leases | Deferred operational authority. Shared reasoning deliberately has no leased tools or facade implementation. |
| #3106 individual owner consent for peer operations | Deferred operational authority. An informational notice is not this consent mechanism. |
| #3116 request consent across attempts | Deferred operational lifecycle. No generic retry or authority-preserving operational continuation is introduced. |
| #3117 holds/approvals/continuations | Deferred operational coordination. Existing native same-owner approval flows remain; foreign turns never promote. |
| #3118 owned result disclosure | Native committed artifacts/scoped reads serve conversation results. Any additional old-stack result/lease obligations remain separately reviewable, not implicitly closed. |
| #3119 recovered standalone runtime | Current conversation binding uses the native runtime and orphan no-replay behavior. A separate operational runtime is not an MVP prerequisite. |
| #3121 consented research/vetted HTTPS | Deferred operational research, including its tested HTTPX-adapter choice. Shared text reasoning gains no fetch capability. |
| #3094 peer knowledge identity/deletion retries | Deferred knowledge design. Preserve owner deletion blocking until explicit restoration, and the prior informed snapshot-restoration authority decision. No foreign automatic knowledge ingestion. |

Only #3241 has a complete containment replacement coverage map here. Other draft
closures require their own scope checks; no wholesale supersession is claimed.
#3395–#3397 retain their posted evidence/scope reconciliation rather than being
silently treated as closed by conversation fallback. See the relevant issue
threads for current review status, not this document as a live PR dashboard.

## Acceptance and remaining design

After the native work lands together, exercise same-owner submit/poll/full
result, both-direction outbox/ACK/reply and the normal approval notify-to-resume
cycle. Exercise explicitly shared submit/poll/result/continuation with private
builder canaries, refusal of every foreign proactive outbox sink and approval
resolver, atomic notice/retry behavior, reclassification/revocation and an owner
starting a separate trusted workflow. Include cancellation, deadline, uncertain
cleanup, lost response/commit acknowledgement, missing artifacts and restart
without replay. Qualify configured backup-provider behavior separately from a
synthetic refusal fixture. The client and server must agree on wrapper shapes,
version headers and cursor semantics after #3574.

Then decide whether there is a concrete need for operational delegation,
proactive foreign delivery, knowledge offers or a separate research surface.
Prefer existing native authority/lifecycle chokepoints; do not import the old
stack just because it already has draft PRs. Restored/removed knowledge and
saved peer authority keep their explicit owner-restoration rules. A new design
must name what remains unserved by the conversation front door before adding
protocols, persistent ownership stores or recovery machinery.
