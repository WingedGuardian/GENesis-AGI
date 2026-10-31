# Offline ego runtime comparison: canonical proposal baseline

This opt-in evaluation slice exercises the real Genesis proposal operations in
disposable SQLite databases. It supplies a baseline for a later comparison of
Genesis and Headlong runtime shapes. It does not run an ego, provider or worker.

## Run

`python -m genesis.eval.ego_runtime --temp-root <existing-scratch-directory>`
creates a fresh temporary child, runs every fixed case, checks committed state
through independent read-only connections, verifies claim-gated fixture artifacts,
closes its database handles and removes its temporary child. The parent directory
is not deleted. No caller-selected existing database is opened.

The versioned JSON report goes to stdout. Exit 0 means all matrix cells passed;
1 means an actual result, committed state or fixture artifact contradicted its
fixed expectation; 2 means an operating error prevented a complete report.
Cancellation does not emit a passing report. The CLI does not accept arbitrary
trace files or evaluate a second implementation of approval policy.

## Fixed matrix

Every case starts with a synthetic pending proposal in a fresh database, using
the canonical `ego_proposals` and `ego_proposal_revisions` table definitions.
The real CRUD helpers create, resolve, revoke, revise and claim the proposal.
Approval and rejection calls include an explicit expected revision; claims pass
`allow_develop=False` so scope constraints are exercised rather than bypassed.

| Case | Expected terminal state | Claim-gated fixture effects |
|---|---|---|
| Approve, revoke, delayed approval, claim | rejected, revision 1 | 0 |
| Approve, claim, revoke, delayed approval, duplicate claim | executed, revision 1 | 1 |
| Reject, delayed approval, claim | rejected, revision 1 | 0 |
| Revise, stale approval, premature claim, current approval, claim | executed, revision 2 | 1 |
| Approve, refused revision, claim | executed, revision 1 | 1 |
| Approve twice, claim twice | executed, revision 1 | 1 |
| Approved develop-scoped proposal, claim | approved, revision 1 | 0 |
| Approved operate-scoped proposal, claim | executed, revision 1 | 1 |
| Approved legacy/null-scoped proposal, claim | executed, revision 1 | 1 |

Nine cases cross two connection modes: continuous writer and clean writer reopen
before every operation. Every operation, including a refused one, is followed by
an independent read-only connection observing committed status, revision, scope
and ego source. Results are compared with fixed case expectations, not inferred
from observed outcomes. The report records the full 18-cell denominator, actual
observations, expectation and source hashes, artifact digests and cleanup status.

Only a successful real claim invokes the tiny fixture writer. Artifact count and
actual bytes are checked independently of the proposal's `executed` status.
These files establish fixture wiring, not useful work or production-worker success.

## Evidence boundaries and remaining work

The two sequential orders force both claim-versus-revocation outcomes. They do
not measure concurrent scheduling. Clean connection reopen does not test process
death, power loss or crash durability. The two-table fixture is not a full
production database or migration/index-parity check. Source hashes identify
inspected file bytes and table definitions; they do not authenticate loaded
bytecode or arbitrary monkeypatches.

No-op, false-success, removed-claim-guard and corrupted-artifact controls test
that the instrument can disprove incorrect behavior. Runtime adapters, native
execution, feedback learning, generation fencing, partial/uncertain execution,
full ego-output parity and the G/H/B/C comparison remain separate work. No model
quality, authentication, adoption or production activation is established here.

Later runtime experiments must preserve the same Genesis authorities. The ego
identifies worthwhile work and proposes it; owner approval, denial and steering
govern the resulting work. Runtime scheduling and history cannot create another
approval authority. Headlong shell execution still requires disposable containment
without production mounts or credentials and without network after setup.
