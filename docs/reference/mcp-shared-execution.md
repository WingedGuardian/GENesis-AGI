# Shared MCP execution compatibility

`genesis.mcp.shared_execution.SharedExecution` is an internal adapter for the
opt-in shared HTTP manager. It is not selected by a launcher yet. Ordinary
FastMCP servers keep their existing object, class, handlers and `run` method.

The adapter supports FastMCP 2.14.6 or 2.14.7 paired with MCP 1.30.0. It refuses
other versions, unavailable package metadata, subclasses and execution overrides
before serving. The broader project dependency floors are not qualification of
every release they permit. Shared startup must select a qualified pair through
the normal install/update procedure before activation; it must not bypass this
check or fall back to an unauthenticated or heavyweight server. The 2.14.7 wheel
has the same 156 package files as 2.14.6 and adds a fakeredis dependency bound;
it was tested as an actual extracted dependency, not accepted by version naming.

FastMCP's low-level `run` override omits the SDK's handler cancellation-finally.
The adapter retains its MiddlewareServerSession, initialization interception,
lifespan and subscription wiring, and cancels/joins the handler task group when
the incoming stream ends. It delegates initialization options to the original
server. There is no second request registry or runtime class patch. The copied
boundary is version-coupled; remove it after a reviewed upstream replacement
passes the same disposal, protocol and database qualification.

Session disposal and request disconnect differ. An abandoned SSE POST permits
idle expiry once all HTTP requests end; an open GET still protects its session.
JSON's result-wait loop keeps its HTTP request active after client abandonment,
so it needs explicit cancellation or DELETE. The shared-service design selects
SSE responses. This adapter adds no tool deadline, replay or durable job queue.
Service-owned detached database jobs retain their own lifetime until service
shutdown, rather than dying with a parent protocol session.

`tests/test_mcp/test_shared_execution.py` covers dependency/override refusal and
real TCP DELETE, expiry/protection, explicit cancellation, peer isolation and
shutdown. Each HTTP case uses a fresh process. The dedicated shared-execution
CI job pins each qualified pair and rejects skipped cases; the broad dependency
CI may skip these cases on an unqualified pair. The HTTP fixture uses the real
launcher worker-suppression helper and a cancellation-protected finalizer barrier.
Actual request-database settlement is additionally checked against the frozen D
integration source before publication; full service bootstrap remains a later
shared-core concern.

The run-loop structure is adapted from FastMCP 2.14.6's `server/low_level.py`.
Its Apache-2.0 license is retained beside the adapter as `fastmcp-LICENSE.txt`
and included as package data. The adapter documents its delegation/finalizer
modifications.
