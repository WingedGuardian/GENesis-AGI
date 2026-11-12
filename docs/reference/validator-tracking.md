# Validator routing and checkpoints

`validating-merges` owns measurement and routing together. `pr_verifications`
records evidence; an actionable concern additionally needs a real task. Follow
CLAUDE.md's public-by-default disclosure policy, including install-local and
security-related work. Keep supporting identifiers/evidence private when useful
public wording is safe. A whole private task needs a specific unavoidable harm
and why redaction/public-task/private-evidence cannot avoid it.

## Entry prompt for a validator batch

Use this contract in batch prompts, including bare CLI clients:

> Read CLAUDE.md and the validating-merges skill. Keep this client's existing
> authorization boundaries. Preflight actual public/private tools, select and log
> the explicit PR set, then measure one row at a time. Persist each valid
> measurement before filing; unresolved routing is pending, not a reason to lose
> evidence. Classify each concern, search/reuse or file its public task, verify
> live readback and meaning, and record structured tracking. Whole private tasks
> require concrete unavoidable disclosure harm. Checkpoint each row and batch.
> On user pause stop selecting work immediately, preserve evidence and report a
> pause checkpoint. Claim handled only when tracking for the selected set is
> complete. Never silently enable a writer/profile or create a Genesis session
> for an external client. Validation does not authorize building, deploying,
> restarting, pushing, merging or messaging others.

The ordinary external Codex profile has no private writer. The separately
qualified validator profile exposes its own closed tool set and can force
verification preview. Discover the tools actually exposed by the connection;
the profile's existence is not authorization to activate it. See
`codex-validator-writers.md` and `.agents/skills/genesis-external-client/SKILL.md`.

## Preflight

From the serving checkout, using its venv (a worktree needs the interpreter and
`--db-path` spelled explicitly):

```bash
.venv/bin/python scripts/validator_checkpoint.py preflight --repo OWNER/REPO
```

This read-only CLI probe reports GitHub public/write availability and database
existence. It reports that the CLI has no private task writer; it never enables
MCP or proves a session has authorization. A missing database still needs fixing
before measurements can be recorded. A private writer is needed only if a useful
task truly cannot be public. Missing private tools do not block safe public filing.
For MCP, discover the actual verification and follow-up create/list/update tools
before selecting work; respect server-forced preview and native guard denials.

## Persist, route, verify

Write valid evidence immediately through the authorized CLI or MCP writer.
Until routing is complete the measurement stays OPEN with its actual verdict.
CLI exit 4 means the measurement was recorded (or previewed) but routing is
pending; read the row before retrying. MCP reports `recorded`, `tracking_complete`,
`tracking` and `would_close` separately. A preview records nothing.

The strict evidence document adds `tracking`, one entry per target:

```json
{
  "tracking": [
    {
      "kind": "public",
      "target": "findings/0",
      "url": "https://github.com/OWNER/REPO/issues/123",
      "quote": "Exact excerpt from the live task body",
      "rationale": "How that task addresses this measured concern"
    },
    {
      "kind": "accepted_limit",
      "target": "gaps/0",
      "rationale": "Why this method limit requires no further work"
    }
  ]
}
```

Targets are `findings/N`, `claims/N`, `gaps/N`, `deploy`, and `controls`, with
zero-based indexes. Claims require classification when failed, unverified, or
passing only at INFERRED. Missing deployment and negative controls also require
classification. Accepted limits apply to method gaps, inferred passing claims,
genuinely unverifiable/no-work claims and missing controls. They cannot discharge
findings, failures, deployment or reachable declared gaps (`owner_only`,
`event_gated`, `not_yet_done`). A reachable skipped check must be declared as
`not_yet_done`, never hidden in an accepted hypothetical limit.

A public receipt names an open issue in the evidence repository, optionally its
exact `#issuecomment-ID`. A comment's parent is checked. A PR, closed issue,
unavailable read or absent excerpt cannot complete routing. Search existing issues
before creating another; the same issue may cover multiple targets with individual
coverage rationales. The exact excerpt proves readback; the validator must still
judge relevance. File/scan comments with `file_tracker_issue.py` too.

A private receipt uses `kind: "private"`, `target`, full follow-up `id`, `quote`,
`rationale`, `disclosure_harm`, and `redaction_insufficient`. It must resolve to an
eligible active private follow-up. The reader does not activate a private writer.
Keep detailed supporting evidence local; do not paste it into a public task merely
to make the excerpt easy to match.

## Checkpoint and pause

```bash
.venv/bin/python scripts/validator_checkpoint.py checkpoint --repo OWNER/REPO --pr 123 --pr 456
.venv/bin/python scripts/validator_checkpoint.py checkpoint --repo OWNER/REPO --pr 123 --pr 456 --pause
```

Save the explicit selected set and checkpoint output in the run log. Exit 0
normally means complete tracking; 4 means pending. `--pause` skips public network
reads and always exits 0 for
pending/unavailable audits while the JSON retains `complete: false`. It honors a
pause, never certifies completion. Neither command is a session lifecycle hook or
a Stop-hook trap. The shared writers enforce completion; the workflow requires
the checkpoint before claiming a batch handled. A client that never submits its
measurement is not covered by a writer gate.

## Historical audit

```bash
.venv/bin/python scripts/validator_checkpoint.py audit --repo OWNER/REPO --offline
.venv/bin/python scripts/validator_checkpoint.py audit --repo OWNER/REPO --pr 123
```

The audit reads OPEN and CLOSED records through the admitted read-only database
connection. It lists missing/malformed/mismatched legacy evidence as requiring
manual classification; it never rewrites or reopens closed rows. Missing
structured linkage is not proof that no issue exists. Read the evidence, search
the tracker and file/reuse actual work; do not manufacture duplicate tasks.
The offline option avoids public network reads and reports those receipts as
unconfirmed. It cannot certify routing completion from cached assertions.

Reads are fresh per row, not an atomic snapshot of a concurrent batch. Re-read
after uncertain responses. Read-only audit errors return 1 (or 0 with `--pause`)
and never claim complete. JSON omits measurement contents and fetched task bodies;
review it before publishing any checkpoint because task identifiers can be private.
