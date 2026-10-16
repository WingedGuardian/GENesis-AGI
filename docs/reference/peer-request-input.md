# Peer request input prerequisite

Standalone hosting installs `PeerRequestHandler` through Werkzeug's supported
request-handler option. The configured listener, port and proxy topology are
preserved. This prerequisite does not register peer routes, enroll a peer or
activate task execution.

Only POST `/v1/agent/a2a/message:send` and POST paths under
`/v1/agent/a2a/tasks/` ending in `:cancel` receive a trusted
`PeerRequestInput` capability in their WSGI environment. Dependent admission
routes must authenticate and check readiness before consuming it. Hosts that
do not install this capability must refuse body ingestion rather than fall back
to an unbounded stream read.

The reader enforces a five-second monotonic deadline beginning when the body is
consumed, not during authentication or model execution. It reads at most the
256 KiB decoded body limit plus one byte and refuses oversize input. A separate
4 MiB additional socket-input limit bounds chunk framing overhead, apart from
bytes already prefetched into the existing 8 KiB buffer while reading headers;
Werkzeug still owns HTTP
framing and chunk decoding. At most one bounded read is permitted per request.
Timeout raises HTTP408, malformed/disconnected input raises HTTP400, and
oversize input raises HTTP413. The API consumer must map these exceptions to
its safe JSON error contract.

After response headers, unread-body cleanup on these endpoints is limited to
250 ms and 64 KiB of additional socket input. This covers authentication,
readiness and size refusals without letting a slow client retain the worker.
Some input may already be in Werkzeug's existing buffered reader. Connections
retain Werkzeug's existing close-after-response behavior.

Ordinary dashboard routes and WebSockets retain their existing policies. This
is a body-ingestion boundary, not a general header timeout, connection-count
limit, production-server migration or all-route hardening policy. Agent Zero
does not inherit the standalone capability automatically.

Before activation, qualify send/cancel through the installed host, including
Content-Length and chunked bodies, both size boundaries, trickle/unterminated
input, refusals and disconnects. Repeat ordinary-route and WebSocket checks.
Disposable socket tests establish the backend behavior; they do not establish
tailnet reachability, provider execution or approval/resume acceptance.
