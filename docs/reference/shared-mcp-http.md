# Shared MCP HTTP composition and owned bootstrap

`genesis.mcp.shared_http.build_http_app` is an explicitly constructed transport.
The explicit shared launcher described below calls it. It receives an already
initialized FastMCP server, a matching `RequestDatabase` and tracker, immutable
`SharedHTTPConfig`, a loaded P3 `CallerResolver`, known loaded commit, aware
startup timestamp and live enabled callback. Resource construction, owner drainage
and provider closure belong to the enclosing bootstrap. This builder neither
creates credentials nor deploys or starts a listener.

The shared-only metadata fence admits FastMCP4.1.4/MCP2.3.0/Uvicorn0.54.0 exactly,
before importing the version-specific assembly. The configuration requires a
literal loopback HTTP URL with explicit port and `/mcp` path, positive finite
limits and tool concurrency no greater than the facade's `foreground_limit`.
The shared bootstrap runs this preflight before constructing resources and
captures identity from its actual source checkout before importing service tools.
The launcher and imported implementation checkout must match before secrets or
rollout configuration are read; an editable install pointing at another checkout
is refused rather than serving code under the wrong commit label.

The composition retains the original low-level FastMCP server and SDK stateful
session manager. Its manager owns the single FastMCP lifespan entry. The SDK
receives explicit session-count, body-size and idle-time limits; response mode
prefers SSE; modern fast calls without notifications can return JSON. Event
storage is absent. Shutdown terminates transports before manager
cancellation. It does not guarantee all HTTP handlers finish before resource
shutdown; the resource-owner contract is a separate bootstrap obligation.

An app-wide SDK-backed exact Host/Origin guard precedes bearer authentication.
P3 authentication covers `/status` GET/HEAD and both `/mcp` and `/mcp/v1`
GET/POST/DELETE. The versioned route binds the forwarder's caller-file contract;
it points at the same transport and manager, with the same session cap and
authenticated principal. Body limits
and raw-envelope admission follow authentication. Duplicate structural JSON
fields, nonfinite constants, batches and IDs colliding under the qualified SDK's
actual coercion helper are refused; admitted bytes are replayed unchanged.
Task-augmented calls are explicitly refused: disabling the task extension alone
does not prevent ordinary body execution on this stack.

Both exact MCP routes pass through raw-envelope admission. Shared tool
middleware binds the actual request's path interpretation and public FastMCP
request metadata for the tool continuation, then resets that scope. Ingestion
identity hints cannot retarget a nested call's arguments. Missing shared scope
refuses at the ingestion/resume wrapper before constructing its provider-facing
consumer; ordinary wrappers retain their defaults. See
[caller-file semantics](mcp-shared-forwarder.md#caller-files-and-backend-compatibility).

The entire validated caller participates in the SDK session principal. Every
message binds caller, session and profile context from authenticated HTTP
metadata. The builder selects shared mode after validation; the explicit launcher
also clears inherited identity before bootstrap and restores the untrusted
process fallback after standalone routing loads its environment.
Health validator messages additionally suppress automatic activity-row admission
for their whole middleware chain, including post-unit instrumentation and copied
child contexts. Counters remain in memory; ordinary health and validator-memory
persistence retain their defaults. The tracker construction flag remains a hard
opt-out. Flush/reap behavior and existing batch durability limits are unchanged;
ordinary rows may flush concurrently with a validator request.

One frozen catalog classifies actual defining-function and transformed-parent
ancestry, enforcing both location and all parent capability names. Browser,
module and payment ancestry is local; unknown modules, sync handlers and new
resource/prompt/task surfaces require qualification before startup. Listing,
direct calls and status revision use this same policy. A tool token is acquired
without waiting before instrumentation and released after its real unit finishes.
Overflow returns `Shared MCP service busy`; discovery and control messages do
not borrow tool capacity. Runtime disablement empties discovery and refuses tool
work while authenticated status and protocol control routes remain available.

`/status` contains service, loaded commit, startup timestamp, transport schema
version1, caller-visible catalog revision, enabled flag, tri-state freshness,
aggregate active tools and busy refusals. It contains no caller identifiers,
credentials or private paths. Freshness joins the tool's unit or opens a status
unit, reusing `commit_identity.is_stale` without latching across rollback.
Unavailable freshness is JSON null; guarded tools retain the existing live
off/warn/block policy and positive-confirmation semantics.

Targeted tests: pure `test_shared_http_settings.py`; qualified
`test_shared_http_admission.py`, `test_shared_http_policy.py` and
`test_shared_http_runtime.py` under `tests/test_mcp/`, plus
`tests/test_observability/test_activity_persistence_scope.py`. Runtime fixtures
exercise actual SDK dispatch and SQLite with synthetic qualified tool modules;
they do not prove four real service factories, native-client compatibility or
deployed service readiness.

The separate shared-MCP qualification workflow runs the real protocol and shared CLI suites
under its pinned SDK constraints and requires zero skips. Ordinary floating CI
keeps pure configuration/refusal coverage and explicitly skips the
version-specific modules when unqualified; the dedicated job fails instead.
See [testing environments](testing.md#shared-mcp-qualification-environments).

`tests/test_mcp/test_shared_cli_startup_contract.py` additionally starts each real
service through the explicit CLI with disposable SQLite, local Qdrant and dummy
credentials. It checks the shared catalog against the existing fixture (excluding
the health browser/module local edge), authentication, one safe read per service,
and SDK/owner/provider closure before SIGTERM replay. The two complete
`recon_config` output-schema `anyOf` alternatives may appear in either order;
all other advertised contract fields remain exact. This does not qualify native
clients, real providers, the full local/shared union or deployed readiness.

## Explicit owned startup

`scripts/genesis_mcp_server.py --shared-service health --transport streamable-http`
selects the owned path; `memory`, `outreach` and `recon` are also supported. The
required selection group keeps ordinary `--server` launch intact. Shared mode
rejects legacy bind, auth and external-profile flags, including empty overrides.
Its configured URL is the sole bind authority and P3 credentials authenticate
each message. A legacy HTTP token cannot substitute for the P3 store.

`genesis.mcp.shared_settings.load_shared_settings` executes the complete current
trusted `scripts/lib/mcp_rollout.py` program in its own temporary module namespace
and calls its genuine reader. It consumes the existing repository config plus
install overlay, retaining the loader's merge semantics. It reads source bytes,
so a same-size replacement with an old timestamp cannot select a stale bytecode
cache. Missing or invalid source/configuration refuses this explicit path with a
sanitized field error. Every returned limit is a generation snapshot.

All these service fields must be explicitly configured:

| Fields | Requirement |
| --- | --- |
| `unit`, `url` | Safe service unit and literal loopback HTTP `/mcp` URL with explicit port |
| `db_units`, `db_background_units`, `db_background_jobs` | Positive integer connection/job budgets |
| `tool_concurrency`, `max_sessions`, `max_body_bytes` | Positive integers; concurrency cannot exceed foreground units |
| `db_checkout_timeout_s`, `session_idle_timeout_s` | Finite positive seconds |
| `shutdown_grace_s` | Finite nonnegative seconds; zero requests immediate cancellation |

The shipped rollout remains off and carries no machine-specific resource limits.
The explicit launcher requires the enabled flag, usable existing P3 credentials,
known loaded commit and aware startup timestamp. It never provisions credentials
or persists its identity against an inherited caller slot.

`initialize_shared_service` owns one bounded `RequestDatabase` and tracker. It
requires an existing database containing the canonical table inventory, warms
the tracker in a bootstrap unit and calls the actual request-service initializer
after that unit exits. The table check is a startup floor, not a migration or a
full column/version validator. A bootstrapped runtime cannot be borrowed.

Health uses the standalone health data service with the same facade and ordinary
action wiring. Memory retains distinct storage and priority-tier recall chains,
their vector-space checks, the reranker and standalone dependencies; it uses no
read pool and leaves session bookmark files untouched. Outreach and recon retain
their standalone dependency modes. The existing standalone routing setup is reused.

Every completed provider/cache construction is registered immediately. Builtin
embedding clients close only when owned; injected clients remain borrowed. Each
provider's cache closes on its constructing loop thread, separately from its
backends. Partial construction unwinds the completed safe prefix. After confirmed
registry drainage, an ordinary closer failure does not skip other safe closers;
failures are aggregated. Unconfirmed registered work withholds dependent closers.

The outer owner spans the HTTP lifecycle. Shutdown closes database admission
synchronously, starts its configured grace concurrently with Uvicorn shutdown,
then settles registered work before provider closure. A shielded shutdown task
allows a cancelled waiter to rejoin cleanup. Provider retirement finishes before
Uvicorn replays a captured termination signal. Exceptional startup also settles
an already-started ASGI lifespan before owner retirement, including cancellation
before the listener-ready flag. HTTP grace and resource grace are separate waits
using the configured value; expiry permits cancellation, never premature resource
closure. A nonterminating physical worker can still require operator intervention.

## Readiness and rendered service units

The four `scripts/systemd/genesis-mcp-*.service.template` files use `Type=notify`
and `NotifyAccess=main`. Their fixed shell wrapper executes the configured Python
and explicit shared launcher as the same main process, preserving literal path
arguments. Both ordinary renderers substitute and stamp them through their
existing managed-unit paths. Rendering does not enable or start these services;
existing edited/masked units retain the normal keep/take policy. Bootstrap's
explicit named take can replace a mask after backup; blanket take cannot.

`shared_notify.notify_ready` sends the fixed `READY=1` datagram only after the
qualified Uvicorn startup completes its actual bind, and skips it if shutdown
was requested during startup. It accepts an absolute local `NOTIFY_SOCKET` path
or the `@` abstract form. Unset/empty means manual CLI operation. A malformed
address or failed/short send fails startup; after a completed bind, the existing
runner stops the listener and settles SDK/registered work before resource
retirement. Successful queueing alone does not prove a manager accepted the
message: the actual manager/P7 state remains the readiness consumer.

The templates load no environment file before launcher preflight. Existing
credentials, all explicit budgets, the enabled flag and the qualified SDK set
are still required. They set no startup/stop timeouts, so manager defaults apply.
Before enabling a unit, qualify and select measured start/stop settings against
the configured grace and actual physical settlement behavior; no portable
hard-stop budget is established here. Do not enable services merely because a
template exists. The explicit [forwarder transport](mcp-shared-forwarder.md)
does not change registrations. Shared routing remains off while caller-path
integration, the local capability/bookmark union, stage routing and matched
measurements are completed.
