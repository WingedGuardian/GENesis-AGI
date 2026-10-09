# Trusted peer registry

This foundation enables authenticated discovery and readiness only. Task skills
remain unadvertised and task service readiness is false until the coordinator
lands. It changes no listener or live deployment. Follow the ingress runbook
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
downgrade. Valid backend-only card/health probes return no owned peer state.

Provide independent ASCII values through protected operator storage. Never reuse
the MCP, desk, another peer, or internal API credential. Equality refuses
activation and generates a boot warning without exposing values. The CLI lists
credential names only; the database stores no token values. Clear a value and
use the approved restart path for file-based revocation; relationship revocation
applies on the next request without restart. No production token is generated
by development tests.

Discovery: `GET /v1/agent/a2a/.well-known/agent-card.json`. Readiness:
`GET /v1/agent/a2a/health`. Both require scoped bearer authentication, including
OPTIONS and otherwise unmatched paths. A running runtime loop is required; the
test-only fallback loop in the shared dashboard decorator is not accepted here.
The card uses the owner-configured HTTPS URL, never the request Host. The A2A
SDK is pinned to 1.2.2 and serializes its 1.0 protobuf models. Streaming, push and
extended-card capabilities are not advertised.

Bodies are read after authentication on the Flask worker, capped at 256 KiB by
reading cap plus one and refusing overflow, including absent Content-Length.
Logs record timestamp, credential name, verified peer or unverified marker,
endpoint, task-ID placeholder and status; never bearer values, request bodies
or tool arguments. Discovery does not read provider credentials or token files.
