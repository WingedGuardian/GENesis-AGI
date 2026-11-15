# Verification review source packets

Prepare complete inputs for an independent post-merge evidence review:

```bash
.venv/bin/python scripts/verification_review.py prepare --repo owner/repository --pr 123 --output /path/to/new-private-directory
```

The output directory must not exist; its parent must exist. The command creates
it with mode 0700 and atomically publishes one `packet.json`. A leftover directory
without that file is not a ready packet. Use a new directory for a retry; existing
output is never overwritten. Treat the packet as private: it contains the PR body,
raw diff and changed-file identities, not just hashes.

The packet binds the full merge/head/base commit identities, exact body and diff,
all file destinations and rename sources, and SHA-256 digests of body, diff and
canonical file metadata. Metadata reads before and after acquisition must agree.
The file census is paginated and reconciled with GitHub's reported denominator;
Git's patch parser checks every destination and textual addition/deletion count.
Binary entries retain their blob identity; binary content still needs independent
inspection or a stated evidence gap.

Acquisition refuses incomplete, changing, unavailable, invalid UTF-8 or oversized
sources. It has a 120-second overall deadline, an 8 MiB raw diff bound, a 4 MiB
metadata bound and a maximum of 3,000 changed files (GitHub's file API ceiling).
A refusal does not publish a ready packet. It must not be interpreted as an empty
PR or successful completeness review. An authenticated `gh` account for
`github.com` and a local `git` executable are required. The command obtains the
credential through `gh auth token --hostname github.com` and keeps it in memory.
It streams source bytes directly from fixed `https://api.github.com` using the
existing HTTP client, with TLS verification enabled and redirects disabled. `GH_HOST`
does not select the source host. Responses must use identity content encoding;
unexpected compression is refused before reading the body. Credential lookup
shares the acquisition deadline and has a 16 KiB output bound.

This direct transport matters: some `gh api` versions sanitize control characters
inside JSON strings, changing body text before it can be hashed. The packet
must retain the source characters, including escaped controls and literal caret
text, distinctly. Errors omit HTTP diagnostics and credential output.

This command acquires sources only. It does not run a model, assess evidence,
close a verification obligation, change validator settings or release a closing
HOLD. The CLI/MCP evidence and routing rules remain authoritative. A complete
packet makes independent review possible; it does not itself establish that a
validator listed or tested every claim.

Sources: [GitHub pull-request APIs](https://docs.github.com/en/rest/pulls/pulls),
[Git apply](https://git-scm.com/docs/git-apply). Patch parsing uses
`git apply --numstat -z --allow-empty` without applying any changes. Its rename
output names the destination; the file API supplies the previous filename.

## Retained independent review

The optional typed evidence field `completeness` records a two-stage review.
Stage A inventories the complete packet before the independent reviewer sees
the candidate. Stage B binds that retained inventory, evidence, claim/gap
coverage and per-claim control judgments to the source digests. Full workflow,
field forms and authority limits:
[independent completeness review](../../.claude/skills/validating-merges/references/completeness-review.md).

Prepare can also accept `--evidence /path/to/candidate.json` to print its
canonical digest without placing candidate reasoning in the source packet.
The candidate is validated before any acquisition/publication.

```bash
.venv/bin/python scripts/verification_review.py check --packet /path/to/packet-directory --inventory /path/to/stage-a.json
.venv/bin/python scripts/verification_review.py check --packet /path/to/packet-directory --inventory /path/to/stage-a.json --evidence /path/to/candidate-with-review.json
```

Without evidence, this checks inventory structure/source binding and prints its
canonical digest. With evidence, it requires a completed bound report and the
same retained inventory. Both forms are read-only and offline: live GitHub
source freshness and actual reviewer independence are not established by a
successful check. Refusals exit `2` and do not alter any input.

Historical evidence without a report still parses and serializes without the
new field. Malformed optional reports are schema errors. Failed/missing/stale
reports cannot pass `check`; retained execution establishes whether an actual
fresh-context reviewer ran. The stored evidence cap remains 256 KiB including
the report. Both close writers retain their earlier behavior in this P2
intermediate release; enforcement and installed qualification are separate work,
and HOLD remains in force.
