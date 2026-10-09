# Bounded peer research

The standalone peer runtime registers `research_search` and `research_fetch`
on its private broker. A peer's admitted and current `research` grants must
permit the operation. ASK uses the normal owner approval path and an exact
operation-and-arguments digest; peers cannot approve their own requests.
The authenticated card advertises research only while the installed runtime
can execute. The facade's tool names alone confer no authority.

Search uses the existing owner-configured SearXNG/Brave backend order. Peers
cannot select a backend, credentials or headers. Queries are limited to4096
UTF-8 bytes and results to1–10. The private search path streams at most1MiB
before JSON parsing and rejects compressed responses. Its diagnostics omit
queries and upstream exception details; task-local filters suppress HTTPX and
httpcore diagnostics without suppressing concurrent owner calls.

Fetch accepts HTTPS on port443, with no userinfo, fragments, controls or IPv6
zones. Every DNS candidate must be public unicast; mixed public/private answers
are refused. Each hop pins one vetted numeric address while preserving the
canonical ASCII Host and TLS SNI. Environment proxies and TLS environment
overrides are disabled. At most three redirects are followed, each revalidated;
cookies are not carried between hops. There is no browser or fallback fetch.

Responses must use identity encoding and a supported text/HTML/JSON media type.
Streaming refuses bodies larger than1MiB rather than truncating them or trusting
Content-Length. Network work has a20-second ceiling and the remaining segment
deadline. Existing synchronous HTML extraction works only on that bounded body;
the deadline is checked afterwards, without claiming CPU preemption.

Returned strings are scanned and wrapped as untrusted external content.
Completed receipts bind strict arguments and a closed result schema. Retrying
an exact completed operation returns the stored snapshot, even if the remote
page later changes. An uncertain read can retry only after the previous scope
and broker operations have drained. Changed arguments require new exact consent;
retries retain the original task allowance and expiry.

Publication and download revalidate every completed receipt, the currently
registered research operation and current authority. Withdrawal withholds
dependent output. Unknown schemas, malformed receipts and failed searches do
not become approved provenance. Existing2MiB receipt storage remains capped.

The runtime owns its search client and closes it after coordinator drain. A
pending scope shutdown retains the client until users have drained. These
changes add no public listeners, browser access or approval resolver.
