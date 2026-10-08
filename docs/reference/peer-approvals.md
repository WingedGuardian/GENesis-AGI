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
fields. Telegram callbacks still require an authorized owner account.

Manager resolution and the named gate refuse peer-operation approval/rejection
from batch, voice, bare/quoted text, generic user, system or autonomous origins.
Only exact dashboard or named Telegram button provenance is accepted. The batch
sweep also excludes this action type, including the batch button's triggering
row. Expiration and cancellation never approve work. The later coordinator must
check this individual provenance again when consuming approval atomically with
the continuation queue insertion; classification as human alone is insufficient.
The private [lifecycle state foundation](peer-lifecycle.md) now implements that
transaction and request-lifetime consent. Runtime scheduling remains a later slice.

Both existing dashboard approval mutation routes refuse a configured peer or SAM
backend bearer even alongside an owner cookie. Disabled/revoked credential families
remain refused. Any other provided Authorization header must prove the existing
internal owner credential before proceeding. Credential comparison uses the
existing single reader and emits no values. The peer API has only an owned,
read-only `/v1/agent/a2a/approvals` extension, with safe description, creation time,
remaining integer timeout and notification failure status.

Missing or failed notification remains visible in the durable association and can
be retried without creating a new approval. Retries and owned summaries recheck
relationship, generation, cancellation, budget, expiry and admission/current
grant intersection. Current task and grants come from one SQLite snapshot.
The coordinator is responsible for periodic retry scheduling and reconciliation
of creation intents after restart; this slice alone does not install runtime work.
Peer notifications disable generic outreach recovery: the peer service owns the
durable retry so every attempt retains its binding, current checks and buttons.
Existing non-peer approval notifications retain their previous recovery path.

The complete hold/notification/named-resolution/contained-resume/result integration
belongs to the coordinator slice. API readiness remains dark until that path is
installed and tested. No foreground session, automatic self-approval or live
Telegram consent is inferred from isolated notification tests.
