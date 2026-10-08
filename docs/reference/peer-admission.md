# Peer task admission

This dependent foundation defines durable admission and owned A2A1.0 task
operations. The standalone runtime leaves
`GENESIS_PEER_TASKS` unset until the constrained executor, leased broker,
human approval and recovery coordinator are ready; task calls then return 503
`not_ready`. This is an internal Flask configuration object, not an environment
switch. Discovery advertises conversation only while the installed service is
ready. See [runtime installation and recovery](peer-runtime.md).

After activation, the existing scoped peer authentication and 256 KiB body cap
apply before parsing every route. Use the pinned A2A SDK 1.2.2 HTTP+JSON transport
and `A2A-Version: 1.0`:

- `POST /v1/agent/a2a/message:send`: user-role text or inline structured data,
  printable ASCII message ID, optional owned context ID. Profiles, model choices,
  tool exceptions, URL/raw file parts, streaming and push notifications are not
  accepted. Request configuration `returnImmediately: true` returns the durable
  submitted task. Otherwise the route waits up to thirty seconds; 504 includes
  its task ID and leaves accepted work durable.
- `GET /v1/agent/a2a/tasks/<id>`: owned task state and timestamps; authorized
  completed publication can also supply a safe status preview and artifact URL.
- `GET /v1/agent/a2a/tasks`: `pageSize` defaults to twenty, maximum one hundred;
  `pageToken` is bound to the peer and relationship epoch. Unsupported filters
  are refused. Other peers' task IDs and cursors return 404.
- `POST /v1/agent/a2a/tasks/<id>:cancel`: accepts the SDK cancellation body;
  its optional ID must match the URL. Pending work is canceled atomically and
  its reserved slot released. Claimed work records cancellation and retains its
  slot until the coordinator proves both process scope and broker effects drained.
  Repeating cancellation is idempotent; other terminal states return the SDK's
  `TaskNotCancelableError` (HTTP400).

Admission uses an existing-file private `BEGIN IMMEDIATE` connection. It commits
task, receipt, grant snapshot, UTC-day charge and prepared queue row together.
No model/network work occurs in the transaction. A retry with the same peer,
epoch, message ID and exact validated intent reuses its task without another
charge, including at quota or slot capacity. Changed intent returns 409.
Conversation permission must still be `allow` or `ask`; admission never resolves
an `ask` decision. Grant revocation removes subsequent task disclosure.

Limits: two reserved slots per peer and globally, twenty nonterminal tasks per
peer, the owner's explicit daily allowance, default cumulative work allowance
3600 seconds with ceiling7200, and an absolute twenty-four-hour task expiry.
This slice stores work/expiry budgets; the coordinator enforces elapsed work and
expiry. Paused/held segments retain durable state; their slot policy belongs to
that coordinator. Legacy direct-session claiming and stale-claim recovery exclude
peer queue rows, so they cannot fall through to an unconstrained session.

Status views disclose no input history, tool arguments or internal paths. The
dependent [owned result components](peer-results.md) project safe publication
snapshots and serve authenticated full artifacts. LIST omits artifact references
but still checks preview authority. Approval holds and failures use constant
status explanations. Complete runtime installation and recovery remain required
before public activation, with separate functionality/E2E evidence.
