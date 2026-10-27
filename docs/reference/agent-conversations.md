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

## Connect a same-owner agent (generic client)

`scripts/genesis_peer_mcp.py` is a single-file stdio MCP server that speaks this
contract. Any MCP-capable agent you own (Claude Code, Codex, OpenCode) can use
it to hand Genesis a task and read the answer. It needs `uv` and Python 3.10+
on the agent's machine and nothing from a Genesis checkout. Tools:
`genesis_ask`, `genesis_check`, `genesis_tasks`, `genesis_cancel`,
`genesis_inbox` (messages Genesis started; see the [pull outbox](agent-outbox.md))
and `genesis_health`.

On the Genesis host, once per agent: add a distinct credential to `secrets.env`
as `GENESIS_PEER_<NAME>_TOKEN`, restart through `scripts/deploy_code_only.sh
restart`, then:

```bash
python -m genesis peers register <name> --same-owner --token-name GENESIS_PEER_<NAME>_TOKEN
python -m genesis peers grant <name> conversation allow
```

On the agent's machine, copy the script and register it with each harness:

```bash
claude mcp add --scope user genesis-peer -e GENESIS_PEER_URL=https://YOUR-HOST/v1/agent/a2a -e GENESIS_PEER_TOKEN=<value> -- uv run --no-managed-python --script /path/to/genesis_peer_mcp.py
codex mcp add genesis-peer --env GENESIS_PEER_URL=https://YOUR-HOST/v1/agent/a2a --env GENESIS_PEER_TOKEN=<value> -- uv run --no-managed-python --script /path/to/genesis_peer_mcp.py
```

`genesis_ask` and `genesis_check` wait up to `wait_s` (default 300, capped at
480) seconds, plus a few send retries, so give the harness a tool timeout of
about 10 minutes; Codex defaults lower, so set `tool_timeout_sec` on its
`genesis-peer` entry. Each `genesis_ask` result carries its `message_id`;
passing it back retries that exact send without duplicating work. The client
keeps no local state: `genesis_inbox` never acknowledges on read, and only the
ids the agent passes back as `ack` are marked collected, so a message the
agent has not dealt with is shown again rather than lost. `--no-managed-python` uses the machine's own Python: on
Windows over SSH, a uv-managed interpreter can sit behind a junction the session
refuses to traverse. The agent's machine must resolve the configured host name
(on a tailnet, enable the tailnet's DNS on that machine).

Genesis starts each turn with no knowledge of the calling session, so the
caller writes a self-contained brief. A completed task whose reply looked like
a credential comes back with `replyError: disclosure_refused` and no text; the
client says so rather than returning an empty answer.

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

Managed turns now perform provider readiness selection before executing the
task: a home-provider rate/quota refusal can select a configured roster backup.
Preflight remains inside the existing task deadline and concurrency reservation;
it does not execute the peer's prompt. The chosen provider retains the same
background profile, authority ceiling and owner approval gates. See
[native provider selection](native-managed-sessions.md#provider-selection-before-work).

After task execution starts, a later limit fails the task without another
provider attempt. Failed turns are not waiting for a quota reset. An exact
message-ID retry returns the original receipt, including a terminal failure;
an intentional new turn needs a new message ID. **Fallback** on the neural
monitor reports the last actual fallback outcome, and the dashboard's last
successful fallback indicator is historical rather than a claim that all
concurrent tasks use that model.

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

A send that is still being admitted when the route's 15-second bound expires
returns `503 not_ready`, but the admission is not abandoned: it keeps running,
and either commits its receipt or is refused (403, 404, 409 or 429) after the
response has gone. Retry a 503 send with the **same** `messageId` and the same
request, never a new one: an exact retry returns the original receipt
(idempotent), or the refusal, while a new message ID is a second turn and
duplicates the work. A same-ID retry that arrives while the original admission
has not yet committed waits on that same work rather than starting its own, so
it can itself return 503 with the same `taskId`; retry again later. When a send
fails with 503 after computing its receipt (a timeout, or a failure after the
receipt committed), the body carries the receipt ID the send computed as a
top-level `taskId` (beside `code`), so a client can poll `GET /tasks/<taskId>`
instead of resending. The send may still be refused, so that poll can return
404 until admission commits the receipt and stays 404 if it was refused: treat
a 404 there as "not yet known" and fall back to the same-`messageId` retry.

Each fresh native turn gets at most eight previous prompt/reply heads, each at
most 4 KiB UTF-8, with an overall 64 KiB prior-context cap and visible omissions.
This is a bounded recap, not CC transcript resume or lossless shared memory.
Full previous artifacts remain separately retrievable while retained.

Lists default to 20 tasks, maximum 100; use `pageSize`, `pageToken` and optional
`contextId`. Tasks sort by recorded public-status timestamp descending, then
task ID descending. The next-page token is an opaque issued position scoped to
the peer, relationship epoch and context filter. Pass it unchanged; changing or
deleting the anchor task does not move its boundary. Old task-ID tokens return
400 `validation_error`; restart listing without a token. This is a live list,
not a frozen snapshot: deduplicate task IDs and poll page one again for updates
that moved ahead of an earlier page boundary. Every page rechecks current grants.

The public-status clock changes only when the effective A2A state changes,
including first/last pending owner approval. Heartbeats, repeated native state,
and additional overlapping approvals do not bump it. Get, List, Send and Cancel
project the recorded state/time with pending-owner flags in one authorized
snapshot. Missing or malformed projection returns503 `not_ready`, including an
incomplete owned row outside the current page. Existing rows backfill an
approximate normalized completion/activity/start timestamp; old transition
history cannot be reconstructed. New clocks record UTC milliseconds; ties use
task ID and wall-clock rollback is not made monotonic.

`GET /approvals`
pages the same way (`pageSize`, `pageToken`, oldest first; the token is the last
approval ID), and each task carries `pendingOwnerActions` from its snapshot. Task history can be
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
