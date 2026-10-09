# Peer session runner foundation

`DirectSessionRunner.spawn()` branches on a trusted immutable `PeerSessionBinding`
before legacy skills, profile overlays or owner context assembly. Peer callers
cannot submit this binding through A2A. The source tag `peer_api` is reserved:
requests without a binding or with owner execution overrides are refused.

The runner requires durable session storage, background autonomy state, and an
installed lifecycle coordinator with authorization, start, drain and completion
callbacks. Missing readiness fails closed before creating a session. The current
background level is preserved, capped at L3. Session metadata carries task,
segment, generation and ceiling before start hooks execute. A constrained
invocation receives only explicit identity, the authorized request, private
facade configuration and its immutable execution policy.

The existing runner's two-slot semaphore is shared with ordinary background work.
Its public `cancel(session_id)` handles an individual session waiting for a slot
or executing, without canceling neighbors. A peer canceled before its coroutine
starts still owns cleanup and terminal recording. Repeated cancellation waits
for scope/broker cleanup and in-flight result writes. Unknown cleanup retains
acquired capacity and blocks new peer launches until the coordinator reconciles
it. The coordinator must persist aggregate cancellation and invalidate leases
before calling runner cancellation; public task states are not derived solely
from the CC session row.

Full results are scanned before a private atomic artifact write in the segment's
working directory. A successful session is recorded only after that write;
empty successful output also has an artifact. Session previews retain the
existing 20,000-character limit. Tool telemetry includes approved facade names
and counts, never arguments. Peer records have no owner transcript path,
proposal delivery or automatic memory extraction. The internal artifact path is
not a peer-visible URL or path.

This foundation does not install a coordinator or enable task routes. Dependent
slices provide leased broker operations, human approvals, generation-checked
continuation and owned artifact disclosure. `GENESIS_PEER_TASKS` remains unset,
and no task skills are advertised until those prerequisites are ready. Local
functionality uses isolated databases and a fake provider; it does not establish
live tailnet acceptance or production approval/resumption.
