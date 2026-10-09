# Private peer coordinator

`PeerCoordinator` joins durable peer lifecycle state to the existing
`DirectSessionRunner`, private broker and individual approval manager. This
slice supplies orchestration, not runtime installation: task readiness remains
dark until owned result delivery, recovery and shutdown are installed.

Dispatch reserves the original remaining allowance before starting a contained
session. Conversation ASK creates and associates a durable approval hold before
notifying the owner; it starts no model invocation. A separate resource ASK
fences leases and cancels the old session. Scope and broker drain must be
confirmed before an approved operation can resume. Consent covers the exact
unchanged request across attempts; operation arguments and immutable resource
versions retain separate exact digests. Neither an approved status alone nor a
peer credential can authorize continuation.

The coordinator delivers approval requests through the existing CLI approval
notification helper and OutreachPipeline. Notification failure is persisted for
retry, with no independent generic outreach retry. Normal individual dashboard
or authorized Telegram buttons resolve requests. Rejection, revocation,
cancellation and original exchange expiry prevent resumed work.

Typed provider interruptions use the existing park table with a peer-only
lineage payload. The original task owns a stable park identifier and attempt
count; holds neither reset the work budget nor add daily admissions. Park claim,
continuation insertion and generation change occur in one private transaction.
The legacy resumer delegates peer parks to the installed peer controller and
refuses to reconstruct them as owner requests. Missing controllers fail closed.
Terminal task transitions retire open parked, resuming and needs-user rows
atomically through versioned compare-and-set. Provider master-off prevents
parking, and only live resume mode resumes provider holds.

Malformed legacy payloads retain the existing backoff and owner-alert recovery.
Live resume selects separate bounded owner and peer batches using the same Python
lineage classifier; `max_due_per_tick` applies to each class. Pending peer holds
also cannot crowd owner alerts out of the needs-user batch. Default unfiltered
CRUD selection and propose-only mixed selection remain available. The classifier
registers once per connection, with query execution outside its registration lock.

Every broker handler runs inside a durable prepared/executing/completed/unknown
operation wrapper. An interrupted immutable read can retry only after drain and
fresh authorization. Executing or unknown consequential operations block both
approval and provider continuation, as well as successful result handoff, until
reconciliation. Exact completed receipts still require current grants and
consent; broker checks after handler execution remain in place.

A timely completion candidate is separate from success and cleanup. Successful
handoff requires its exact task/generation/segment/session proof, confirmed
drain, current disclosure authority, no outstanding hold and known consequential
outcomes. An exact-budget timely result can be handed off after cleanup; it
cannot start another execution segment. The required trusted result publisher is
an internal seam supplied by `PeerArtifacts`, not an exposed callback. Its final
transaction rechecks authority and receipts before atomically publishing the
artifact, completing the task and retiring its provider park. See
[owned results](peer-results.md).

The early integration fixture uses actual SQLite, runner, Unix broker, stdio
facade, notification pipeline and authenticated dashboard resolution, with fake
model, scope calls, delivery adapter and result publisher. It does not prove
production tailnet access, full artifact retrieval or restart recovery.

Shutdown stops new dispatch before waiting for the existing dispatch lock. An
in-flight spawn must finish registering or settling before shutdown fences its
bindings and snapshots sessions. Pending sessions retain the runner's cleanup
ownership; shutdown joins cleanup before closing the broker and refuses closure
while cleanup requires reconciliation. Lifecycle cleanup joins occur outside the
dispatch lock. A cancelled shutdown must be retried; it is not successful closure.
Unconfirmed cleanup before session registration retains a global runner hold and
its reserved allowance; it cannot permit a neighboring launch or broker closure.
