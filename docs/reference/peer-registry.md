# Trusted peer registry

The registry supports scoped health, local relationship management and the
native A2A conversation binding. Access starts disabled. A ready
native runner/database is required before advertising the Agent Card. No new
listener, automatic deployment or enrollment. See
[agent conversations](agent-conversations.md) and the existing ingress runbook.

The database must already exist with migrated peer tables. Operator commands
use private guarded SQLite connections; a missing/quarantined database is
refused. No peer endpoint changes configuration, identities, grants or secrets.

```bash
python -m genesis peers configure fallback --service-url https://YOUR-HOST/v1/agent/a2a
python -m genesis peers register muse --same-owner --token-name GENESIS_PEER_MUSE_TOKEN
python -m genesis peers grant muse conversation allow
python -m genesis peers list
python -m genesis peers revoke muse
python -m genesis peers configure disabled
```

Registration creates a relationship epoch and grants nothing. Conversation
admission requires current active identity/epoch and `conversation allow`.
Cross-owner relationships additionally require explicit `conversation_shared allow`
and are restricted to public tools-free reasoning/proposals. `ask`, `deny` or a
missing grant refuses access; no migration seeds shared authority. No per-day task
allowance is enforced. Deprecated `--daily-allowance` preserves the positive
legacy field for database compatibility only. Native concurrency, wall deadline,
background autonomy and permitted tool gates still apply.

Revocation retains the identity, epoch and history and denies subsequent
admission/disclosure without restart. Grant changes increment revision.
Resource keys (`resource:<32 lowercase hex ID>`) and other capabilities may be
recorded, but the conversation binding does not implement knowledge/recurrence
or leased federation. Already accepted work remains Genesis-owned; revocation
does not automatically replay or cancel it.

Optional SAM mode additionally needs `--sam-realm`, pinned peer `--sam-node`
and optionally exact delegated `--principal`. Cross-owner identities can be
recorded with `--cross-owner`; their conversations stay refused until both grants
are explicitly allowed. The owner-only default authorization still refuses all
foreign proactive outbox operations and approval-description reads. Node
and scoped credential names are unique. SAM accepts only the dedicated backend
credential and verified identity headers; fallback accepts only active registered
peer credentials and ignores those headers. No automatic downgrade.

Backend-only SAM health/card probes return no owned task data. Active peer
credentials determine fallback readiness; inactive credentials still participate
in scope collision checks. Values must be ASCII and independently generated,
stored/transferred through protected operator storage and the Secure Vault.
Never reuse MCP, desk, another peer, internal API, dashboard password or active/
fallback Flask signing authority. Equality refuses access and gives a boot
warning with no values. Request checks use cached/environment credentials;
no request opens `secrets.env` or a signing/internal token file. CLI/DB record
credential names, never values.

Clear a value and restart through the approved deploy path for file-based
revocation. With no selected configured credentials the API fails closed 503;
an invalid/cleared credential against an otherwise configured surface receives
401. Development does not generate or enroll production credentials.

Readiness: authenticated `GET /v1/agent/a2a/health`. The card is authenticated at
`/v1/agent/a2a/.well-known/agent-card.json`; its HTTPS URL is operator-configured,
never derived from Host. A running runtime loop is mandatory. A2A SDK is pinned
to 1.2.2; streaming, push and extended-card capabilities are unavailable.
Send/cancel consume bounded host input; alternate mounts without the trusted
capability cannot accept work. Logs contain timestamp, credential name, verified
peer, endpoint, task ID when known and outcome, never bearer values or bodies.

Scope ingress to the connector before activation. Presented peer/backend
bearers are refused on owner mutations before passwordless/off/cookie shortcuts;
this does not protect an otherwise passwordless owner route reached **without**
that bearer from a trusted network. Do not expose owner tool/resolution routes
through a root peer proxy.

Registry revisions/grants share private `BEGIN IMMEDIATE` transactions, separate
from unrelated runtime coroutines. Conversation receipts bind peer/epoch and are
retained for retries. Existing restore handling resets saved peer authority on
normal data restore; Guardian snapshot rollback uses the owner's informed
snapshot-specific approval. Do not claim retained-state exactly-once behavior
across deliberate purge or rollback; re-enrollment resets client epoch/context.


For an intentionally shared foreign relationship after deployment:

```bash
python -m genesis peers register other-agent --cross-owner --token-name GENESIS_PEER_OTHER_TOKEN
python -m genesis peers grant other-agent conversation allow
python -m genesis peers grant other-agent conversation_shared allow
```

Generate/store/transfer the credential through the existing protected procedure;
these commands store its name, never its value. Receipt trust mode is immutable:
reclassification never exposes old private or shared receipts across modes.
See [conversation boundaries](agent-conversations.md#explicit-shared-conversations)
and the [roadmap coverage](peer-roadmap.md) before reviving older peer branches.
