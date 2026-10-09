# Private peer capabilities and published resources

The contained CLI's sole MCP entry point is `genesis.peers.facade`. Its lease
file is private (0600), contains the Unix socket path and a short opaque lease,
and is never a command-line credential. The facade offers `task_context`,
`resources_list`, `resource_read`, `research_search` and `research_fetch`; the broker additionally checks each name
against the segment's exact allowed tools. It has no shell, configuration,
secret, raw recall or approval-resolution operation.

The broker uses aiohttp UnixSite, not a new TCP listener. Its directory is 0700
and socket 0600; existing socket paths require reconciliation. HTTPX connects to
that socket with environment proxies disabled. Lease lookup precedes body
parsing, and request bodies are bounded to 256 KiB by an accumulated cap+1 read.
Malformed/duplicate JSON, unknown operations, extra fields and encoded bodies
are refused with constant errors.
Parser and transport failures also use constant responses and metadata-only
logging through a local aiohttp protocol adapter; malformed headers cannot
fall through to aiohttp error messages containing the private lease. Real wire
tests guard this adapter, and an unsupported handler configuration fails closed.

Every operation checks active relationship epoch, task generation, cancellation,
expiry, segment deadline and the admission-snapshot intersection with current
grants. Executing operations require a working task with remaining allowance.
An explicitly installed coordinator authorizer must confirm the exact operation
digest; an `ask` is not an automatic approval. Resource-read digests bind the
immutable content digest. Current authorization is checked again before returning
results. Audit records contain time, lease credential category, task/segment,
operation and outcome, without lease values or arguments.

Tracked broker operations remain owned when a caller disconnects. Draining
invalidates leases before cancellation, settles registered effects under repeated
cancellation and prevents reissuing that segment. Scope cleanup remains a separate
runner obligation. Mutating extension handlers must retain their own effect
ownership and durable outcome records; broker completion alone does not establish
that an interrupted external effect never happened.

Publish only an explicit approved snapshot through the local operator CLI:

```bash
python -m genesis peers resource-publish --title 'Approved document' --file document.txt
python -m genesis peers grant <peer> resource:<resource-id> allow
python -m genesis peers resource-retire <resource-id>
```

Publication returns only the generated resource ID and SHA256. Content is immutable,
scanned, limited to 256 KiB and never loaded through raw memory recall. A later grant
cannot expand an already accepted task's snapshot. Listing discloses only snapshots
permitted for that task; retirement and digest corruption prevent subsequent reads.
The operator CLI is the publication surface; no peer API or facade may publish.

The [runtime owner](peer-runtime.md) installs this broker after startup recovery
with the coordinator and normal approval gate. This module alone does not
activate cross-owner work, add an approval resolver or establish live tailnet
acceptance. Stricter cross-owner disclosure and signed evidence are separate
prerequisites in the approved series.

The standalone owner also registers [bounded research](peer-research.md).
Trusted registration classifies retry-safe reads; peer arguments cannot change
that classification. Unknown consequential outcomes still require reconciliation.
