# Peer session runner foundation

`DirectSessionRunner.spawn()` branches on a trusted immutable `PeerSessionBinding`
before legacy skills, profile overlays or owner context assembly. Peer callers
cannot submit this binding through A2A. The source tag `peer_api` is reserved:
requests without a binding or with owner execution overrides are refused.

The runner requires durable session storage, background autonomy state, and an
installed lifecycle coordinator with authorization, start, completion timing, provider parking, drain and
finalization callbacks. Missing readiness fails closed before creating a session. The current
background level is preserved, capped at L3. Session metadata carries task,
segment, generation and ceiling before start hooks execute. A constrained
invocation receives only explicit identity, the authorized request, private
facade configuration and its immutable execution policy.

The first authoritative streaming result records server wall time and elapsed
execution time through the mandatory `completed` lifecycle callback. This is a
timing candidate, not proof of a successful result or stopped work. Invocation
return occurs after process reaping and scope cleanup, so using return time alone
would charge cleanup against an answer that finished before its allowance ended.
The no-result fallback uses return time conservatively; refused completion proof
withholds output. Provider-reported duration does not establish completion time.

After timely proof commits, a separate 7,200-second completion tail bounds the
invoker's post-result callbacks and cleanup. This uses the project default because
no smaller legitimate callback bound has been established; a hanging downgrade
callback was reproduced in a scratch probe. It extends neither model execution,
the systemd scope deadline nor broker leases. Tail timeout, cancellation, failed
cleanup or classified invocation error cannot become successful output merely
because a timing candidate exists. Capacity remains owned until cleanup is proven.

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

The private coordinator is described in [peer coordinator](peer-coordinator.md).
Runtime installation and task readiness remain dark in this slice. Dependent
slices supply leased broker operations, human approvals and generation-checked
continuation. [Owned results](peer-results.md) now supply the trusted publisher,
safe SDK projections and authenticated full-result endpoint; runtime installation
and recovery still gate activation. `GENESIS_PEER_TASKS` remains unset,
and no task skills are advertised until those prerequisites are ready. Local
functionality uses isolated databases and a fake provider; it does not establish
live tailnet acceptance or production approval/resumption.
