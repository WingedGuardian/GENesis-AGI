# Individual peer operation consent

Peer operation approvals use the existing ApprovalManager and approval_requests
store. A separate immutable association binds the approval ID to peer/relationship
epoch, task, segment, generation, capability and exact operation digest. The
association is committed before creating the approval with its fixed UUID, so a
creation retry recovers that ID rather than minting a different request. Neither
creation nor notification grants execution authority.

The normal approval notification helper routes through OutreachPipeline, using
an operation-specific card with individual Approve and Reject buttons. It does
not offer batch approval or bare-text instructions. The owner dashboard retains
its per-item consent surface and displays peer/task/digest instead of CLI fallback
fields. Generic communications and outreach-modal approval cards exclude peer
operations; the dedicated approval feed retains them. Telegram callbacks still
require an authorized owner account.

Both generic communication and outreach history views hide `cli_approval`
notification duplicates, including ordinary CLI fallback notifications. The
dedicated approval cards remain the consent surface; non-CLI history is retained.

Manager resolution and the named gate refuse peer-operation approval/rejection
from batch, voice, bare/quoted text, generic user, system or autonomous origins.
Only exact dashboard or named Telegram button provenance is accepted. The batch
sweep also excludes this action type, including the batch button's triggering
row. Expiration and cancellation never approve work. The later coordinator must
check this individual provenance again when consuming approval atomically with
the continuation queue insertion; classification as human alone is insufficient.

Both existing dashboard approval mutation routes refuse a configured peer or SAM
backend bearer even alongside an owner cookie. Disabled/revoked credential families
remain refused. Any other provided Authorization header must prove the existing
internal owner credential before proceeding. Credential comparison uses the
existing single reader and emits no values. The peer API has only an owned,
read-only `/v1/agent/a2a/approvals` extension, with safe description, creation time,
remaining integer timeout and notification failure status.

Individual dashboard approval and rejection of a stored peer-operation row also
require positive owner proof: the existing internal owner bearer, or a verified
password-backed owner session with the existing same-origin check. This check
holds when general dashboard API authentication is disabled. Passwordless
dashboard access and stale cookies on a passwordless install cannot resolve peer
operations; configure owner dashboard authentication or use the authorized
Telegram buttons. Ordinary passwordless approvals retain their existing behavior.

Missing or failed notification remains visible in the durable association and can
be retried without creating a new approval. A matched nonempty delivery receipt
already committed in manager context reconciles missing association bookkeeping
without sending again. A send interrupted before that receipt commits may still
be retried; this is not an exactly-once transport guarantee. Retries and owned summaries recheck
relationship, generation, cancellation, budget, expiry and admission/current
grant intersection. Current task and grants come from one SQLite snapshot.
The coordinator is responsible for periodic retry scheduling and reconciliation
of creation intents after restart; this slice alone does not install runtime work.
Peer notifications disable generic outreach recovery: the peer service owns the
durable retry so every attempt retains its binding, current checks and buttons.
Existing non-peer approval notifications retain their previous recovery path.

Concurrent notification attempts for the same approval share a process-local
lock while holders or waiters remain. Unused locks are collected; durable
association and receipt checks retain retry identity after collection. This does
not provide cross-process serialization or exactly-once transport.

The complete hold/notification/named-resolution/contained-resume/result integration
belongs to the coordinator slice. API readiness remains dark until that path is
installed and tested. No foreground session, automatic self-approval or live
Telegram consent is inferred from isolated notification tests.
