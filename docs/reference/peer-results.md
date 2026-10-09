# Owned peer results

`PeerArtifacts` publishes full results from a contained peer session after its
trusted completion candidate is timely and all task segments have drained.
Publication rechecks the current relationship epoch, original expiry,
cancellation, cumulative work allowance, conversation consent and completed
operation receipts. Uncertain consequential outcomes prevent success and retain
a reconciliation hold. The [runtime owner](peer-runtime.md) installs the result
publisher only after startup recovery and execution readiness checks.

Full UTF-8 output stays in the private segment's `.peer-results` directory. Each
directory must be owned by the service user with mode0700 and the regular result
file with mode0600; anchored no-follow opens refuse symlinks, nonregular files
and mismatched segment paths. The complete output is scanned before publication
and download. There is no separate artifact size cap or silent truncation.

The `peer_artifacts` table stores an opaque owned artifact identifier, byte
length, SHA256, a UTF-8-safe preview of at most4096 bytes and validated facade
tool counts. Artifact insertion, completed task status and associated provider
park retirement commit together. A private file left by a failed transaction
is not a published result; the trusted publisher can retry the same completed
segment without inserting a second artifact.

Every task response projects authorized publication snapshots through the same
gate, including GET, paginated LIST and duplicate message submissions. The
pinned A2A1.0 SDK carries the preview in the status message and the full result
as a URL part. LIST omits artifact references but still gates its preview.
Failed and held work exposes constant status explanations, never raw provider
errors or owner execution context.

`GET /v1/agent/a2a/tasks/<task_id>/artifacts/<artifact_id>` uses the existing peer
bearer boundary and task ownership checks. Downloads revalidate full bytes,
hash and size, then recheck authority after the file read before returning them.
Responses use `text/plain`, a fixed `result.md` attachment name, `no-store` and
`nosniff`. Missing or unsafe files return409 `result_not_ready`; they can leave
the authorized historical preview and completed status visible. An artifact URL
identifies the publication and does not promise continuing file availability.

Disclosure conservatively depends on every completed task receipt across all
attempts, including reads whose bytes the broker subsequently withheld. Known
builtin receipts use their canonical operation digests and exact schemas;
unclassified capability receipts withhold results until a trusted extension
policy exists. Published resource bytes and versions must remain current and
authorized. Resource-list metadata requires ALLOW in both admitted and current
grants; lowering either dependency to ASK or DENY withholds the result. A
conversation approval does not authorize resource metadata. Restoring current
ALLOW can permit the original result only within its admitted grants, epoch,
expiry and all other checks.

Installed research has a closed receipt policy that revalidates strict argument
digests, bounded result schemas, outbound safety and current broker registration.
Historical research snapshots need not be refetched, but their current research
grant and exact consent remain prerequisites for disclosure.

Publication snapshots avoid rereading large backing files for each task
preview. Each response refreshes task ownership and receipt authority in a final
single serialized transaction after awaited settings access; production
authority is not cached. The final projection and download checks use
`BEGIN IMMEDIATE`, so a concurrent authority change cannot commit midway
through an older WAL snapshot. File reads stay outside that transaction. An
authority change committed before the final check withholds disclosure; one
waiting behind it applies to subsequent requests. A final synchronous expiry
check follows awaited proofs, including every earlier row in a preview batch.
Publication expiry detected after park retirement rolls the whole transaction
back. Full download has its own file and final-authority checks.
