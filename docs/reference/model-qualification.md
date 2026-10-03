# Isolated model qualification

`python -m genesis.eval.qualification` is a standalone evidence runner. It does
not change routing, benchmark arms, production databases or model allowlists.
The CLI's `prepare`, `dry-run`, `report` and `compare` commands are offline and need no
credentials. Only `execute` submits completions. `reconcile` uses authenticated
GET requests for already observed OpenRouter generation IDs; it never resends a
completion. MiMo Pro is the default candidate; DeepSeek V4.1 Flash requires an
explicit candidate alias. No command changes production routing or runs live Fusion.

The CLI pins LiteLLM's bundled model map before importing the SDK, avoiding its
default import-time public HTTP lookup. Verified pricing and observed provider
billing govern campaign accounting. Direct Python library imports retain their
caller's SDK import/environment policy; use the CLI for this offline guarantee.

The public tests exercise synthetic references and mocked HTTP. They are not
human approval or evidence that either model is qualified. Real accuracy and whether the
complete protocol fits the owner's $5 target remain unmeasured. The full request
schedule must be estimated before spending; the manifest freezes an explicit
campaign ceiling, defaulting to $5, even when a higher ceiling is authorized.

## Prepare and inspect

Use the installed Genesis Python environment. Keep the spec, campaign directory
and reports outside the public checkout. The campaign directory must be owned by
the operator with mode `0700`; evidence files have mode `0600`. Pass a disk-backed
temporary directory explicitly for disposable SQLite. For example:

Create the campaign's parent directory first. The runner creates only the
campaign directory and fsyncs its parent entry before recording evidence.

```bash
python -m genesis.eval.qualification prepare ~/private/mimo-campaign \
  --spec ~/private/mimo-spec.json --temp-root ~/tmp/mimo-sqlite
python -m genesis.eval.qualification dry-run ~/private/mimo-campaign
python -m genesis.eval.qualification report ~/private/mimo-campaign
```

Commands emit JSON and exit `0` for pass, `1` for fail, `2` for incomplete.
Preparation normally reports incomplete until every prerequisite is present and
all scheduled repetitions have been scored. `preflight_issues` identifies missing
prerequisites. A small smoke corpus cannot qualify or authorize a paid run.

`dry-run` rechecks current source, contract, provider and library identities and
pricing expiry without network access. `report` evaluates frozen historical
evidence, so later upgrades or expired prices do not erase a completed result.

The JSON spec contains:

| Key | Contract |
| --- | --- |
| `provider` | Defaults to `openrouter-mimo` (`xiaomi/mimo-v2.6-pro`); explicit `openrouter-deepseek-flash` selects `deepseek/deepseek-v4.1-flash`. Both require the exact shipped OpenRouter model identity. Other aliases and model substitutions are rejected before rendering or execution. |
| `ceiling_usd` | Positive decimal string or integer; defaults to `"5"`. Freeze an authorized cap after estimating the complete schedule. |
| `cases` | A nonempty list with unique `id` and `contract` values from the registry, `j9_relevance`, or `procedure_novelty`. |
| `parameters` | Intended production parameters for `judge`, `relevance`, and `novelty`. These are frozen for subsequent promotion. |
| `reference_approval` | A separately supplied independent approval artifact, bound to `corpus_hash`. |
| `pricing` | Verified maximum-charge evidence bound to model, endpoint, parameters and corpus, with an expiry. |

For rubric cases, use the existing strict human-reference schema from
[golden-reference-integrity.md](golden-reference-integrity.md): `actual`,
`expected`, boolean `user_passed`, `scorer_config` naming the rubric and its
required context, and `reference_provenance` with a human reviewer and current
rubric version. Historical machine results may remain additional fields; do not
copy a machine score into a human label. Labels and independent approval must
come from their real reviewers.

J9 cases use `query`, `memory_content`, boolean `user_passed` (relevance at least
0.5, matching production J9 aggregation), and human provenance naming the current
J9 prompt version. The runner uses J9's actual truncation, rendering and parser. Duplicate rendered questions are
rejected. Invalid raw relevance numbers are qualification errors even if the
current J9 parser clamps them to a finite value. All seven judge contracts require
an actual JSON number in [0, 1]; exact decimal checks reject values outside the
range before float rounding, and strings and booleans cannot become valid scores.
Production parse-error sentinels remain qualification errors.

Novelty cases use `new` and an ordered `existing` list of procedures with
`task_type`, `principle`, `steps`, and an explicit deterministic `embedding` vector.
Each existing procedure also has an `id`; `expected_target` must explicitly be
that ID or `null` for DISTINCT. Human provenance uses the version returned by
`contracts.versions()`. Task types and principles must be unique within a case,
so the actual rendered candidate order can be mapped unambiguously to procedure
IDs. Optional `deprecated` and `quarantined` candidate flags exercise exclusion.
Every case must reach one cross-type judgment; a reference target outside the
actual selected candidates makes preparation incomplete.
Preparation also replays deterministic DISTINCT and valid redundant-target controls
through both actual storage paths, in current and hypothetical promoted arms.
These controls validate the fixture's storage prerequisites without assigning a
reference label. A skipped or quality-gated fixture cannot enter the paid schedule.

Each parameter set requires a positive integer `max_tokens`. Rubric calls require
`temperature: 0.0`; relevance also requires `max_tokens: 150`. The provider policy
requires exactly one `provider.only` entry, `provider.allow_fallbacks: false` and
`provider.require_parameters: true`. Supported additional keys are `top_p`, `seed`
and `reasoning`. Unfrozen provider defaults are rejected. LiteLLM's automatic
OpenRouter `usage: {include: true}` field is also frozen. The serialized request
must match this body exactly before it can leave the process.
Numeric parameters reject booleans, strings and nulls before any reservation:
`temperature` accepts finite numbers in [0, 2], `top_p` in [0, 1], and `seed`
requires an integer. A supplied `reasoning` value must be an object; its optional
`max_tokens` requires a nonnegative integer.

The reference approval is an object with `approved: true`, `independent: true`,
`corpus_hash`, a nonblank `reviewer` different from the case labelers, and an
`evidence` reference. This is declared provenance, not cryptographic authentication
of a human decision. An agent must never manufacture it.

Pricing requires `currency: "USD"`, `model_id`, `endpoint`, `parameters_hash`,
`corpus_hash`, `verified: true`, `reviewer`, `evidence`, `bound_basis`, and an aware
ISO timestamp `valid_until`. Decimal USD rates are `input_per_million`,
`output_per_million`, and `request_fee`. `max_input_tokens` must be a verified
upper bound for **every rendered request**, including chat framing and the
provider's billed token semantics. Output prices and caps must include reasoning
tokens and all other billable output; the request fee must bound other charges.
The code calculates the bound from this evidence; it cannot establish that an
asserted price or token bound is true. Have the bound independently checked before
execution. Use `evidence.digest` for canonical JSON hashes; do not hash pretty
printed JSON text. Expired or mismatched pricing blocks execution.

Currency fields must use decimal strings or integers; floating point prices are
rejected. Provider numeric billing is parsed without binary float conversion.
Reservations and totals use exact integer-scaled decimal arithmetic independent
of the process Decimal context. Pricing validity is checked before each
reservation and immediately before each serialized request leaves the client.

The manifest freezes the source commit and file hashes, LiteLLM/httpx versions,
contract versions, corpus and prompt hashes, ordered schedule, selected candidate
IDs, provider configuration, effective parameters, approvals, pricing and ceiling.
Changing any frozen input requires a new campaign. Preparing again in the same
directory only succeeds for an identical manifest. Never replace a spent campaign
with a new manifest to replenish its budget.

## Execute and recover

Only after real independent references and the complete maximum-charge schedule
are approved, set `OPENROUTER_API_KEY` in the environment and deliberately run:

```bash
python -m genesis.eval.qualification execute ~/private/mimo-campaign \
  --temp-root ~/tmp/mimo-sqlite
python -m genesis.eval.qualification reconcile ~/private/mimo-campaign
python -m genesis.eval.qualification execute ~/private/mimo-campaign \
  --temp-root ~/tmp/mimo-sqlite
```

Completion and reconciliation share the production delegate's credential order:
`API_KEY_OPENROUTER`, then `OPENROUTER_API_KEY`, then `OPENROUTER_API_TOKEN`.
The serialized completion authorization must match the selected credential before
egress. Credential values are never included in the manifest or journal.
Reconciliation requires credentials only when an unresolved observed generation
can be queried; an invocation with no eligible generation leaves the report and
journal unchanged. Network failures retain reservations and report incomplete.

One exclusive writer holds the campaign lock. Each reservation is appended and
fsynced before dispatch. Settled charges plus unresolved reservations plus the
next reservation must not exceed the frozen campaign ceiling, across contracts
and process restarts. Reopening a campaign cannot raise that ceiling.
Decimal arithmetic is used throughout. There is exactly one delegate attempt,
zero client retries, no provider/model fallback and no nonzero chain rotation.

Per-call HTTP observers run on the existing delegate transport. The request hook
checks endpoint and serialized parameters before sending; the response hook reads
and journals provider-reported model, generation ID, usage, billing and content
before returning to LiteLLM. Their completion is awaited by httpx, so settlement
cannot race a queued logging callback. Failure and cancellation are journaled
independently of observation. Shared routing and global callback configuration
are not rewritten. Execution refuses active global LiteLLM callbacks; run the
standalone CLI in a fresh process so private references cannot be exported by a
previously configured observer. Retained provider metadata and fully formatted
delegate/SDK logs redact environment credentials. The temporary logging factory
is restored on every exit. Transport references:
[LiteLLM callbacks](https://docs.litellm.ai/docs/observability/custom_callback),
[OpenRouter generation metadata](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation),
[OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).

Missing identity or billing is never zero cost. Interruptions are not retried.
Any unresolved attempt blocks new dispatch; known generation IDs may be
reconciled by GET. A crash after settlement can recover scoring without sending
the completion again, without credentials or unexpired prices. Local scoring still
requires matching source/configuration and valid references; it runs before new
request checks. Missing credentials cannot reserve money for a new request.
Paid dispatch uses the frozen rendered request directly. Production contract and
storage replay happen offline against the retained answer. Malformed model
judgments are permanent qualification errors; local replay or storage failures
leave the paid answer and charge unscored, so recovery can score them without
another completion. Local failures keep the result incomplete.
A torn journal, conflicting manifest, duplicated generation
ID, unexpected charge or charge exceeding a reservation stops the campaign and
reports incomplete. Reservations with no known generation ID remain unresolved;
do not release them on the assumption that the request was free.

Reconciliation retains received billing evidence even when settlement is
rejected. Conflicting known same-generation charges keep the campaign incomplete;
a later smaller bill cannot silently replace a known larger bill. An interrupted
`execute` or `reconcile` emits an incomplete report containing recorded attempts
when durable state remains readable.

## Read the result

The report includes all failed attempts, settled charges, unresolved reservations,
per-contract denominators, per-class agreement, errors and each of three
repetitions. Historical reports use the frozen evidence even after source or
prices change; a new execution still checks live source and pricing prerequisites.
The inventory and reference provenance are read from the frozen manifest;
executing or recovering scoring requires the original source and strict current
reference validation.

The judge gate includes **all six registered rubrics and the separate J9
relevance contract**: at least 50 distinct questions per contract, at least 25 in
each class, at least 80% agreement overall and per class, zero qualifying errors,
and three passing repetitions. Novelty requires at least 300 DISTINCT and 100
redundant questions, zero false or wrong-target suppression, at least 80%
correct-target detection, zero qualifying errors, and three passing repetitions.
Coverage is never silently reduced to fit the budget. A complete bound over the
frozen ceiling reports its shortfall before any completion.

The raw novelty candidate verdict is captured before the production allowlist.
Recorded responses are replayed through `_store_judged_procedure` and
`extract_procedure` against copied full-schema disposable SQLite databases. Each
case reports current and hypothetical promoted behavior separately, preserves the
candidate IDs, and checks the actual replayed prompt. The hypothetical arm changes
only a locally restored allowlist in the offline process. Deterministic embeddings
and extraction/scoping stubs establish storage integration; they do **not** prove
real retrieval or extraction quality.

Judge and novelty route verdicts are separate. Promotion is a later coordinated
routing change for each passing route, using the exact frozen parameters and
rollback tests followed by review and E2E. This tooling performs no promotion.
The model-validation skill should be created after exercising the real approved
qualification/promotion workflow, rather than claiming the synthetic runs prove it.

## Compare MiMo Pro and V4.1 Flash

Prepare two separate campaigns from the same approved corpus and source files,
using the explicit provider alias for each. Freeze independently verified pricing,
upstream policy, effective parameters and an authorized ceiling in each manifest.
Each full campaign still requires at least 750 cases and 2,250 requests across
three repetitions. Estimate the **combined** complete maximum before spending;
the owner's $5 target is not a shared counter across two journals.

Run each campaign deliberately with `execute`. There is no command that executes
both models automatically. To compare existing evidence without credentials:

```bash
python -m genesis.eval.qualification compare \
  ~/private/mimo-campaign ~/private/flash-campaign
```

`compare` requires two existing journals, holds both exclusive locks while reading,
and leaves the evidence unchanged. It rejects missing or aliased inputs. Cases,
labels, approval artifacts, contract versions, source file hashes, libraries and
the ordered request schedule must match. Every task field except its effective
parameters, delegate parameters and maximum charge must match, including ordered
novelty candidate IDs. Different inputs report incomplete instead of comparing a
smaller intersection. Historical source commits may differ if all frozen source
file hashes match; live execution still requires its original source identity.

The report embeds both model reports and their parameters and prices. For each
contract and repetition it counts scheduled and scored pairs, valid prediction
matches, disagreements and correctness against the references. Failed, malformed
or missing answers never become matching predictions. Valid DISTINCT novelty
answers can match as `null`. Route eligibility remains separate for each model.
The overall status is incomplete if either campaign is incomplete or pairing is
confounded, otherwise fail if either fails, otherwise pass. No winner is selected.

Combined financial fields sum known settlements, retained commitments, unresolved
reservations and individual frozen ceilings with exact currency arithmetic. The
maximum is `null` if either verified bound is unavailable. The summed ceilings
disclose the two campaigns' limits; they do not authorize spending. Missing billing
remains an unresolved liability in the embedded model report. Historical MiMo v1
evidence remains readable, but new preparation, execution and comparison require
the exact currently supported candidate identities.

## Current handoff

The offline tooling is implemented in this change. The remaining user-dependent
steps are real independent labels/approval, a defensible complete-protocol charge
bound, an explicitly authorized paid qualification, and route-specific promotion.
No paid result, production qualification or promotion is claimed by this document.
