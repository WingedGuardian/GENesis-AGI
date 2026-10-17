# Codex interactive Genesis MCP access

The standalone server offers three closed external roles. The launcher defaults
to `external`; `--profile external`, `--profile validator` and
`--profile interactive` select a role explicitly. Invalid or duplicate selections
are rejected. `external` and `validator` retain five health and eight recall
tools. `interactive` admits 83 health and 34 memory tools; additions require
explicit review in `src/genesis/mcp/external_profiles.py`.

This change makes the interactive role available. The committed project
`.codex/config.toml` still selects the narrow default. Project activation and
GitNexus wiring are a separate change, dependent on the native harness and
memory-correction identity fixes. It does not activate the validator launcher.

The interactive role reuses ordinary health router, direct-queue and campaign
initialization. It starts no Genesis worker/executor and never makes Codex a
Genesis session. Session-bound tools are for an existing Genesis session
explicitly identified by the user; never supply Codex identifiers, register a
transcript, or create a synthetic charter. Pending Claude plan bookmarks remain
unprocessed in every external role.
External charter updates and ledger additions require exact target membership
in the existing charter, recorded-transcript or heartbeat identity stores after
prefix resolution; a valid UUID alone cannot create an external charter.

Directed interactive follow-ups record `source=external_interactive`, remain
eligible for the sanctioned hot lane, and match the `mine` source filter.
Their default `source_session` is null. Explicit external references are resolved
against the same three identity stores: known full IDs or unique prefixes are
retained; unknown or ambiguous follow-up provenance becomes null with a diagnostic
while the follow-up itself is saved. External `bookmark_shelve` and structured
`knowledge_ingest` / `reference_store` session provenance refuse unknown targets
before transcript reads or persistence. A filesystem-only legacy session does
not qualify externally; ordinary clients retain filesystem bookmark resolution
and their existing provenance behavior. Knowledge normalization copies the
provenance dictionary. General free-text source labels remain caller metadata;
these checks do not inspect arbitrary payloads for identifiers. The marker is request-local; it creates
no Genesis session or lifecycle record. Existing dispatched-session cold routing
and promotion restrictions remain unchanged and take precedence over the marker.

The launcher scrubs inherited session context and resolves live runtime state
through the main checkout even when invoked in a linked worktree. The native
entry point sets a process-local policy before bootstrap. At the router's shared
secret-loading chokepoint, external roles exclude the eight launcher session
markers and GENESIS_REPO_ROOT, and preserve all existing environment values.
Native startup also clears those eight inherited markers for both transports,
while preserving the selected runtime root. File loading admits only
`routing.standalone.EXTERNAL_SECRET_KEYS`: the original MCP infrastructure,
embedding and timeout settings plus explicit supported routing, web, document
and Cloudflare credentials and local-provider settings. Proposal kill switches,
the custom privacy fingerprint path, MCP staleness mode and relocated procedure
trigger cache are retained as safety/compatibility inputs. The policy also
applies after failed bootstrap on lazy retries. Ordinary clients keep their
existing dotenv override behavior.
This positive file policy does not remove unrelated credentials already
inherited from the operator, restrict explicit module environment configuration,
or prevent trusted code from reading files. Custom integrations requiring other
variables must supply them explicitly; arbitrary credential names are not loaded
from the file automatically. No live provider execution is established here.
Experiment, evolution and skill-replay router construction also uses this
policy for its shared secret loader, including provider-key remapping. In
external mode it honors the selected secrets path and never falls back to the
legacy home checkout when that file is missing; ordinary eval loading is unchanged.

Deferred tools are `document_delete` and these health names:
`health_errors`, `health_alerts`, `task_control`, `campaign_trigger`,
`user_job_create`, `user_job_list`, `user_job_control`, `user_job_history`,
`ego_proposal_resolve`, `session_config`. The last tool writes a singleton
model/effort cache read by unrelated new sessions; it needs a target-aware
cache contract before admission ([#3384](https://github.com/WingedGuardian/GENesis-AGI/issues/3384)). Health capability gaps are tracked in
[diagnostics #3152](https://github.com/WingedGuardian/GENesis-AGI/issues/3152),
[task control #3153](https://github.com/WingedGuardian/GENesis-AGI/issues/3153),
[campaign trigger #3154](https://github.com/WingedGuardian/GENesis-AGI/issues/3154),
[user jobs #3155](https://github.com/WingedGuardian/GENesis-AGI/issues/3155) and
[proposal actuation #3156](https://github.com/WingedGuardian/GENesis-AGI/issues/3156).
These are exclusions, not repairs in this change.

Existing Genesis gates and the user's instructions still govern every action.
Admission is not permission to publish, dispatch, pay, or change settings.
This is a capability boundary, not authentication, universal consent enforcement,
or isolation under full host access. Reference lookup auditing and ordinary
recall semantics are retained; no credential-only recall guarantee is added.

`memory_store` returns an ID from existing storage. Embedding/vector failure can
leave durable SQLite/FTS content with pending indexing, so an ID alone is not
proof of vector success. Check a fresh reader against the claimed effect.
Duplicate saves preserve the original identity. Inspect durable state after
failure/cancellation and report uncertainty for ambiguous remote effects;
do not blindly retry a non-idempotent action.

Qualification uses explicit test files and isolated child homes, databases,
secrets and provider targets. `test_interactive_profiles.py` checks the complete
registered census and synthetic dispatch/refusal for every admitted/excluded
name; it does not prove every production tool workflow. Real stdio tests exercise
both wrapper and native stdio entry points, bootstrap, catalogs, refusal, health snapshot and memory
save/duplicate/restart/keyword recall without embedding providers, plus imported
source hashes and scrubbed child-environment receipts. The fixture supplies
an isolated checkout linked to the test interpreter's dependency environment,
so it also runs when CI installs dependencies outside a checkout-local venv.
Full native
Codex CLI/app-server and tool-family qualification belongs to activation.

Rollback selects the narrow profile through normal reviewed configuration and
deployment. It does not delete saved memories or undo previously requested work.
