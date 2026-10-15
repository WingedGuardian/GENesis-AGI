# Trusted peer registry

This foundation enables authenticated health checks and local peer management.
The Agent Card returns `503 not_ready`; it cannot advertise an A2A transport
before the message and task routes are installed. Task service readiness is
false until the coordinator lands. It changes no listener or live deployment. Follow the ingress runbook
before operator activation; a peer credential is never dashboard authority.

The database must already exist and contain the migrated peer tables. Local
operator commands use private guarded connections; missing/quarantined databases
are refused. No HTTP endpoint changes configuration, identities or grants.

```bash
python -m genesis peers configure fallback --service-url https://YOUR-TAILNET-HOST/v1/agent/a2a
python -m genesis peers register muse --same-owner --token-name GENESIS_PEER_MUSE_TOKEN
python -m genesis peers grant muse conversation ask
python -m genesis peers list
python -m genesis peers revoke muse
python -m genesis peers configure disabled
```

Each registration creates a new relationship epoch and grants nothing. Genesis
decides permitted requests under its own authority; enrollment does not assign
a daily task budget. The optional deprecated `--daily-allowance` argument only
preserves the positive legacy database field for backup compatibility; it is
not an execution limit in the corrected admission slice. Revocation
retains identity, epoch and history. Grant
changes increment the relationship revision. Resource decisions use
`resource:<32 lowercase hex ID>` and capability decisions use `allow`, `ask` or
`deny`. Actual admission and capability enforcement are dependent slices.

SAM selection additionally requires `--sam-realm`; register each peer with
`--sam-realm`, `--sam-node` and optional exact `--principal`. Select `--cross-owner`
instead of `--same-owner` when applicable. Node and fallback credential names
are unique. SAM uses only `GENESIS_PEER_BACKEND_TOKEN`; fallback uses only active
registered scoped names and ignores SAM identity headers. There is no automatic
downgrade. Valid backend-only health probes return no owned peer state; the card
remains unavailable in this foundation. Revoked peer credentials never make
fallback readiness appear usable, but still participate in collision checks.

Provide independent ASCII values through protected operator storage. Never reuse
the MCP, desk, another peer, internal API credential, dashboard password, or a
Flask session-signing key. Collision checks use the app's loaded active and
accepted fallback signing keys, including during boot diagnostics; editing the
key file does not change the live app's signing authority. Equality refuses
activation and generates a boot warning without exposing values. The CLI lists
credential names only; the database stores no token values. Clear a value and
use the approved restart path for file-based revocation; relationship revocation
applies on the next request without restart. No production token is generated
by development tests.

Readiness: `GET /v1/agent/a2a/health`. The reserved discovery path,
`GET /v1/agent/a2a/.well-known/agent-card.json`, returns `503 not_ready`
after valid authentication until the task transport is installed. Both paths
require scoped bearer authentication, including
OPTIONS and otherwise unmatched paths. A running runtime loop is required; the
test-only fallback loop in the shared dashboard decorator is not accepted here.
The owner-configured HTTPS URL is reserved for the later A2A transport, never
derived from the request Host. The A2A SDK is pinned to 1.2.2 for that dependent
transport. Streaming, push and extended-card capabilities are not advertised.

Request bodies are never read by these routes: no current route consumes one.
(The development server still discards any unread body after responding, so
this alone does not bound how long a slow client can hold a connection.) After authentication,
a declared Content-Length over 256 KiB is refused `413 body_too_large` from the
header alone. The task transport must add its own bounded, deadlined body read.
Logs record timestamp, credential name, verified peer or unverified marker,
endpoint, task-ID placeholder and status; never bearer values, request bodies
or tool arguments. These routes do not read provider credentials or token files.

Registry SQL stays inside the private guarded transaction boundary so relationship
revisions and grant changes commit together without sharing an active transaction
with unrelated runtime coroutines. It neither creates a missing database nor
opens one quarantined by the restore guard.
