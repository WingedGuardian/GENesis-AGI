# Internal shared MCP HTTP composition

`genesis.mcp.shared_http.build_http_app` is an explicitly constructed transport.
No launcher, installer or rollout selector calls it yet. It receives an already
initialized FastMCP server, a matching `RequestDatabase` and tracker, immutable
`SharedHTTPConfig`, a loaded P3 `CallerResolver`, known loaded commit, aware
startup timestamp and live enabled callback. Resource construction, owner drainage
and provider closure belong to the enclosing bootstrap. This builder neither
creates credentials nor deploys or starts a listener.

The shared-only metadata fence admits FastMCP4.1.4/MCP2.3.0/Uvicorn0.54.0 exactly,
before importing the version-specific assembly. The configuration requires a
literal loopback HTTP URL with explicit port and `/mcp` path, positive finite
limits and tool concurrency no greater than the facade's `foreground_limit`.
The later bootstrap must run this preflight before constructing resources and
capture identity before importing service tools; this injected-resource API is
not proof that a launcher does so.

The composition retains the original low-level FastMCP server and SDK stateful
session manager. Its manager owns the single FastMCP lifespan entry. The SDK
receives explicit session-count, body-size and idle-time limits; response mode
prefers SSE; modern fast calls without notifications can return JSON. Event
storage is absent. Shutdown terminates transports before manager
cancellation. It does not guarantee all HTTP handlers finish before resource
shutdown; the resource-owner contract is a separate bootstrap obligation.

An app-wide SDK-backed exact Host/Origin guard precedes bearer authentication.
P3 authentication covers `/status` GET/HEAD and `/mcp` GET/POST/DELETE. Body limits
and raw-envelope admission follow authentication. Duplicate structural JSON
fields, nonfinite constants, batches and IDs colliding under the qualified SDK's
actual coercion helper are refused; admitted bytes are replayed unchanged.
Task-augmented calls are explicitly refused: disabling the task extension alone
does not prevent ordinary body execution on this stack.

The entire validated caller participates in the SDK session principal. Every
message binds caller, session and profile context from authenticated HTTP
metadata. Shared mode clears process identity only after assembly succeeds.
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

Targeted tests: `test_shared_http_admission.py`, `test_shared_http_policy.py` and
`test_shared_http_runtime.py` under `tests/test_mcp/`, plus
`tests/test_observability/test_activity_persistence_scope.py`. Runtime fixtures
exercise actual SDK dispatch and SQLite with synthetic qualified tool modules;
they do not prove four real service factories or native-client compatibility.
