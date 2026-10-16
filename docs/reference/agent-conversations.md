# Same-owner agent conversations

Genesis-originated messages use the [pull outbox](agent-outbox.md). A reply
supplies the outgoing message's contextId and metadata.inReplyTo. The first
distinct reply is reserved atomically with its native task; an exact retry
reuses that task, and later conversation turns omit inReplyTo.

This is an asynchronous A2A 1.0 HTTP+JSON interface, using SDK 1.2.2 models on
the existing Flask/runtime loop. Genesis reasons independently with its identity,
current cognitive state, available prior-experience retrieval and permitted native
tools. It may answer, clarify or refuse. External messages are input, never owner
instructions or approval. The API changes no listener. Access starts disabled.

For the MVP, enroll only agents owned by the operator. Registry entries for
other owners are refused by conversation admission. The registry authenticates
explicit fallback scoped credentials or an explicitly configured SAM backend;
there is no automatic downgrade or acceptance of a general MCP/desk token.
SAM is optional, not needed for the direct fallback client. See
[peer registry](peer-registry.md) and [bounded input](peer-request-input.md).

## Activate after review, merge and deploy

Use the approved deploy scripts. Verify the running revision and scope the peer
proxy to `/v1/agent/a2a`; do not expose owner mutation/tool routes to peers.
Presented peer/backend tokens are refused by owner mutation authentication even
when passwordless, disabled, or accompanied by an owner cookie. This does not
protect an otherwise passwordless trusted-network route reached **without** that
token. Connector-only ingress is an activation prerequisite, not an inferred
property of the default listener or Tailscale configuration.

Generate distinct credentials locally, store them in `secrets.env`, restart
through the approved deployment path, and transfer through the Secure Vault.
Never publish a value in chat, logs or test output. The operator then runs:

```bash
python -m genesis peers configure fallback --service-url https://YOUR-HOST/v1/agent/a2a
python -m genesis peers register muse --same-owner --token-name GENESIS_PEER_MUSE_TOKEN
python -m genesis peers grant muse conversation allow
```

An `ask` or `deny` grant refuses admission; the machine interface cannot approve
it. Enrollment authorizes the conversation relationship, not new tool authority.
No mandatory peer tasks/day quota applies. Current native concurrency and
background autonomy remain in force. A changed/revoked relationship takes effect
on the next request; no new admission or disclosure is allowed. Accepted work is
Genesis-owned and can be canceled explicitly; revocation does not replay it or
introduce new per-tool leases. See restore/epoch cautions in the registry guide.

## Wire contract

Use `Authorization: Bearer <vault-supplied value>` and `A2A-Version: 1.0`.
The authenticated card lives at `/v1/agent/a2a/.well-known/agent-card.json`.
Its interface URL is the configured HTTPS URL, never the supplied Host header.
Streaming, push notifications and extended cards are unavailable.

| Operation | Path under `/v1/agent/a2a` |
| --- | --- |
| Send asynchronous message | `POST /message:send` |
| Get task | `GET /tasks/<task_id>` |
| List tasks | `GET /tasks` |
| Cancel task | `POST /tasks/<task_id>:cancel` |
| Full saved result, text/plain | `GET /tasks/<task_id>/result` |
| Read pending owner actions | `GET /approvals` |
| Authenticated runtime/native readiness | `GET /health` |

Example request (no credential value):

```json
{
  "message": {
    "messageId": "client-durable-message-id",
    "role": "ROLE_USER",
    "parts": [{"text": "Please assess this proposal independently."}]
  },
  "configuration": {"returnImmediately": true},
  "metadata": {"profile": "observe", "timeout_s": 3600}
}
```

Blocking sends are refused before creating work. Text and structured JSON parts
are supported (at most 32); files, raw content, tenant overrides, extensions,
push settings, message metadata and task references are unavailable. Request
bodies have a 256 KiB refuse-not-truncate cap and a host-owned five-second input
deadline. A mount without the trusted bounded-input capability cannot send or
cancel; it returns `503 not_ready` rather than reading an unbounded stream.

Server-supported options are `profile` (`observe`/`interact`), a native `CCModel`
tier, `effort` (`low`/`medium`/`high`) and integral `timeout_s` (default 3600,
range 1–7200). No tool exceptions, system prompt, provider selection or autonomy
override. The current native background effective level chooses the default
profile; explicit interact requires at least L3, observe at least L1. The runner
rechecks its existing breaker and ceiling before hooks or execution. Native
profile/roster model selection still applies; interact currently pins Opus before
normal roster handling, so a requested tier is not a guaranteed provider/model.

Send returns the A2A `SendMessageResponse` task. Persist its task/context IDs.
Reuse `messageId` and identical parsed request semantics for retry: one receipt,
one adoption. Changing that message's content/options conflicts. Context IDs are
server-issued UUIDs bound to peer/epoch; continue with that `contextId` and a new
message ID. A malformed or unowned `contextId` is refused (400/404) before any
identity or memory work. Overlapping turns in one context conflict. At most two
outstanding agent turns are admitted globally and per peer; excess returns 429
without work. Capacity stays reserved through cancellation and uncertain cleanup.
Once the runtime starts shutting down, new sends return 503 and in-flight
admissions are drained or cancelled before sessions stop; an admission cut off
there is recorded as outcome unknown after restart, never replayed.

Each fresh native turn gets at most eight previous prompt/reply heads, each at
most 4 KiB UTF-8, with an overall 64 KiB prior-context cap and visible omissions.
This is a bounded recap, not CC transcript resume or lossless shared memory.
Full previous artifacts remain separately retrievable while retained.

Lists default to 20 tasks, maximum 100; use `pageSize`, `pageToken` and optional
`contextId`. A next-page token is the last owned receipt ID. `GET /approvals`
pages the same way (`pageSize`, `pageToken`, oldest first; the token is the last
approval ID), and each task carries `pendingOwnerActions` from a bounded check. Task history can be
requested with `historyLength`; this binding returns no history (a permitted
maximum of zero), with the reply head in `status.message`. Unsupported list
filters are explicitly refused; no silently ignored filter. Terminal reply
messages are always summaries of native metadata, never labeled full results.

Full result fetch checks the derived UUID artifact, file type, symlinks, UTF-8,
a 2 MiB disclosure limit and the SHA-256 committed at finalization.
Missing/partial/uncommitted/oversized or rewritten artifacts are explicitly
unavailable; a truncated metadata head is never returned as full.
Whole result and summary disclosure use bounded credential/path scanning,
including decoded and embedded JSON. Malformed plausible JSON and compound JSON
mapping keys may be refused. This is the existing deterministic pattern policy,
not a claim to recognize every conceivable sensitive fact or encoding.

Pending native state maps to SUBMITTED; running/canceling to WORKING; native
success/failure/cancellation to their corresponding terminal A2A states. Cleanup
uncertainty is visible and retains the native reservation. A live turn with a
pending attributed owner request reports AUTH_REQUIRED. `pendingOwnerActions`
can also be true on completed cognition: the action remains held by its existing
watcher. Approval details are read-only, ownership-filtered and scanned; timeout
is seconds remaining, or null for an indefinite owner wait. No peer resolver.

Owner notification/resolution stays in the normal Telegram/dashboard flow.
Waiting tools can resume after their existing wait resolves; held actions use
their existing watchers. There is no generic model continuation/replay after an
approval. Cancel waits briefly for confirmed native cleanup; uncertain/slow
cleanup returns 503 `cancellation_pending` and remains pollable. Successful
cancellation retries reuse the canceled receipt; other terminal tasks return
the standard A2A TaskNotCancelable error (HTTP 400).

Runtime restart marks orphaned receipts failed/outcome_unknown and never
automatically reruns them. Result retention may remove full artifacts while
retry anchors remain. Deliberate data purge/snapshot rollback lies outside the
retained-state exactly-once guarantee. Local component tests do not establish
Muse adapter readiness, production approval delivery or tailnet acceptance.
