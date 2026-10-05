# Model qualification by routing alias

`python -m genesis.eval.qualification` measures whether an OpenRouter routing
alias (for example `openrouter-mimo` or `openrouter-deepseek-flash`) agrees with
owner-labelled references well enough to serve the `judge` route (six rubrics plus
J9 relevance) or the procedure-novelty route. It changes no routing, no production
database and no model allowlist. Promotion is a separate, deliberate routing edit.

| Command | Network | Purpose |
| --- | --- | --- |
| `check --corpus DIR --temp-root T` | none | Validate the corpus; print per-contract and per-class counts and coverage issues, including any contract with no file yet. |
| `run --alias A ...` | OpenRouter | Pay only for answers the campaign does not hold yet, then report. |
| `report --alias A [--against B] ...` | none | Rescore paid answers; `--against` pairs two aliases case by case. |
| `review-references --spec FILE` | none | Check declared frontier reference admission and show the feedback review queue; this does not assign labels or establish qualification. |

`run` and `report` also take `--corpus`, `--temp-root`, `--campaign` and `--params`.
Every command prints one JSON object and exits `0` pass, `1` fail, `2` incomplete.

## Corpus

One calibration-format JSONL file per contract: `<corpus>/<contract>.jsonl`. Any
other non-hidden file in the directory is refused, so a misnamed file is never
silently skipped. Keep the corpus, params and campaign outside the public
checkout; tests use synthetic fixtures only.

- **Rubrics** (`bench_task_success`, `memory_recall_grounding`, ...): the golden-set
  format of [golden-reference-integrity.md](golden-reference-integrity.md), validated
  by the same strict check `run_calibration` applies.
- **`j9_relevance`**: `id`, `query`, `memory_content`, boolean `user_passed`
  (relevant means a score of at least 0.5, as production J9 aggregation decides).
- **`procedure_novelty`**: `id`, `new` and `existing` procedures (`task_type`,
  `principle`, `steps`, a deterministic `embedding`; each existing one has an `id`,
  optionally `deprecated`/`quarantined`), and `expected_target`: an existing id, or
  `null` for a distinct procedure.

By default every case carries `reference_provenance {label_source: "human",
reviewer, rubric_version}` and a real human label. Drafts stay outside the corpus
directory. Shared calibration's strict human validator remains unchanged.

An explicit `--reference-policy FILE` opts qualification into
`frontier-assisted-v1`: a frontier model grades the bulk against the actual
registered contracts, and genuinely uncertain cases go to human adjudication.
This policy does not convert machine grades into human judgments. Coverage and
the three passing repetitions remain the same. High self-reported confidence is
an admission threshold, not evidence of measured calibration.

The policy is a JSON object with `version: "frontier-assisted-v1"`,
`confidence_threshold` (integer 90–100, default 90), `approved_models` (explicit
model IDs), nonblank `guidance`, and `feedback` (a list). Each feedback item has
`id`, `text`, `evidence`, and `contracts` naming the contracts it affects. MiMo
and DeepSeek graders are excluded; the actual qualification candidate cannot
grade its own references either. Declared model identity evidence must be honest
about any unavailable provider-reported backend identity.

Frontier rubric and J9 cases use boolean **`reference_passed`**, never
`user_passed`. Novelty continues to use an explicit `expected_target` (ID or null).
Frontier provenance requires `label_source: "frontier_llm"`, `reviewer`,
`rubric_version`, `model_id`, `model_identity_evidence`, integer
`confidence_percent`, `rationale`, `evidence`, `evidence_complete: true`, and
`uncertainties: []`. Missing grounding or uncertainty requires review even at
100% confidence. Mixed rubric cases run through the same production
`LLMJudgeScorer`; human-only cases retain `run_calibration`.

**Apply every human correction throughout the corpus**, including earlier
accepted cases. Every human or frontier case under this policy needs a
`reference_review` with `reviewer`, `evidence_complete: true`, `uncertainties: []`,
`policy_hash`, `case_hash`, and `feedback_applicability`. The last field maps
every feedback ID to `{disposition: "regraded" | "unaffected", reason}`. Review
the actual case before writing that disposition; never blanket-mark unaffected.
Use `evidence.digest(policy)` and `references.case_hash(contract, case)` for the
hashes. The case hash binds current label, provenance, sources and inputs; it
excludes `reference_review` and `reference_history`. Preserve prior judgments in
private append-only grading artifacts and optional `reference_history`; do not
overwrite historical human input. A new feedback item invalidates all old
reviews until the whole set is checked, including human anchors. Interpret
feedback through each contract: topical relevance is not overall usefulness,
and procedural supersession preference is not automatically redundancy.

`review-references --spec FILE` accepts `{reference_policy, cases}`; every draft
case in this review spec also carries `contract`. It prints admitted/human_review
counts and blockers. Ungraded or stale cases enter `needs_llm_regrade`; only
below-threshold confidence, missing evidence or unresolved uncertainty enter
`human_review`. Its pass describes reference admission only: the separate
`check` validates real prompts, duplicate questions, embeddings and novelty
replay prerequisites. This CLI validates declarations; it cannot verify that a
reviewer actually read a cited source or that a human approved it.

Before a frontier-assisted paid `run`, provide `--reference-approval FILE` and
`--current-reference-policy FILE`. Approval requires `label_source: "human"`,
`approved: true`, `independent: true`, `reviewer`, `evidence`, `policy_hash` and
`corpus_hash` (`digest` of the loaded dictionary keyed by contract). The human
approver must differ from all frontier labelers; they may have supplied human
anchors themselves. Never generate approval on their behalf. Missing or changed
current policy, stale reviews, uncertainty or missing approval stops before any
router or paid request. The current file must include all human input received
since earlier grading; an unchanged stale file cannot reveal newer chat input.
`report` rescores saved answers against the supplied corpus and policy, reports
source denominators and flags missing approval. Paired aliases share those exact
references. It does not claim historical grades used today's guidance.

Coverage floors: at least 25 cases in each class for every rubric and for J9
relevance (50 per contract), and 300 distinct plus 100 redundant novelty cases.
`run` refuses to start while any contract in the directory is below its floor.
Routes can be staged: a directory holding only the judge contracts qualifies the
judge route and reports the novelty route as incomplete.

## Params

`--params` is a JSON object keyed by alias:

```json
{"openrouter-mimo": {
  "upstream": "<OpenRouter provider name>",
  "max_price": {"prompt": 0.5, "completion": 1.0},
  "judge": {"max_tokens": 400, "temperature": 0.0},
  "relevance": {"max_tokens": 150, "temperature": 0.0},
  "novelty": {"max_tokens": 300}}}
```

Every request is pinned: `provider.only: [upstream]`, `allow_fallbacks: false`,
`require_parameters: true`, and `max_price` (USD per million tokens), which
OpenRouter enforces by refusing to route above it. Each route needs an explicit
`max_tokens`; `temperature`, `top_p`, `seed` and `reasoning` are optional. A
parameter the production call site passes itself (the judge's `temperature: 0.0`,
J9's `max_tokens: 150`) must not be contradicted, or the request is refused before
it is sent. Production sends no `max_tokens` on the judge and novelty routes;
qualification requires one to constrain generated output, and the report counts
answers cut off at it (`requests.truncated`). The upstream decides both identity
and price, so choose it from the model's OpenRouter endpoint list.

## Spend

Set `GENESIS_QUALIFICATION_OPENROUTER_KEY` to a **dedicated** OpenRouter key that
has a credit limit. Before its first request `run` reads `GET /api/v1/key` and
refuses a key whose `limit` is null or exhausted, and any key equal to the
production OpenRouter key in the environment or `secrets.env` (that key's limit,
if any, would not bound this run); an unreadable `secrets.env` is a refusal too.
The credit limit is the money bound. The report shows `limit_reset`: a limit that
resets bounds each period rather than the run.

Two caveats. OpenRouter documents that a request is charged when it finishes, so
concurrent requests could commit more than a balance covers; whether a key limit
counts in-flight requests is not documented. Requests here are sequential, so the
residual is about one request. And the dedicated-key check can only compare
against production keys this process can see.

A request cap bounds the count: `cases x 3 repetitions`, counted
from the campaign's own `dispatch` lines across restarts. It is per campaign
directory and params file, so it bounds loops, not money. Each request is sent
exactly once: LiteLLM's client retries a dropped connection by itself, and that
second POST is refused before it leaves the process.

## Evidence

The campaign directory (mode `0700`) holds `answers.jsonl` (mode `0600`) and an
exclusive `flock`, so two sessions cannot run one campaign at once. Each request
appends a `dispatch` line, fsynced before the request leaves the process, then an
`answer` or `failure` line. An answer records the alias, model, params hash,
contract and rubric version, case id, repetition, **prompt hash**, the served
model and upstream, `finish_reason`, generation id, usage and the content, with environment
credentials redacted. OpenRouter keeps no completions to fetch later, so this file
is the only copy of what was paid for.

Answers are looked up by the prompt rendered **now**. Failed or unresolved dispatches and wrong identities stop new requests across restarts; saved answers remain available for offline rescoring. No request is automatically resent. Changing a recorded alias's model or parameters also stops dispatch. Recovery requires an operator to account for the original attempt; this CLI has no override that clears uncertain requests. an edited case or a rubric change renders a new prompt, so its old
answer no longer matches and the case is reported `unanswered` until `run` pays
for it. A torn final line is discarded on open: its write never finished, so a
torn dispatch was never sent; a torn answer leaves its intact dispatch unresolved and stops new requests.

## Scoring and the gate

Rubrics run through `calibration.run_calibration` with a pinned stand-in router;
J9 relevance and novelty drive the production `_judge_relevance` and
`_principle_is_novel` paths in a disposable full-schema SQLite copy. Novelty is
graded on the raw `redundant_with` target mapped through the candidate order the
production prompt actually rendered, before the production allowlist can turn it
into "store".

- A **local failure** (no answer, an error object even under HTTP 200, request
  cap, key refusal, an evidence-write error, a served model or upstream other than
  the pinned one) leaves the case unscored: the repetition is `incomplete` and the
  run stops sending further requests. Only an answer the router actually served is
  ever graded. The rubric pass reads a snapshot of the cases validated at start,
  so editing the corpus mid-run cannot change what is graded.
- A **model error** (an answer production cannot parse) counts against the model.
- Per contract and repetition: every case scored, both class floors met, zero
  model errors, at least 80% agreement in each class and overall. Novelty also
  fails on any suppression that is not the labelled target, even while incomplete.
- A contract passes when all three repetitions pass; a route passes when all its
  contracts pass. Within a route a failed repetition is decided, so `fail`
  outranks `incomplete`. Routes are judged independently: the top-level status
  is `incomplete` while any route still has work, and `routes` holds each verdict. `clamped_raw_scores` counts answers whose raw score fell outside
  [0, 1] and was clamped by production.

`report --against B` pairs the two aliases per contract, repetition and case:
matching predictions, both correct, only one correct, neither, and pairs with an
error or unscored side. Pairing selects no winner; each alias is graded against
the owner's labels.

## Not established here

Synthetic tests prove the mechanics, not that any model qualifies. Real labels,
a paid run and any route promotion remain owner steps.

## Rework and preflight

This implementation replaces sent-back PRs #2819 and #2832 in a new PR based on main. The former PRs' review history is retained; this is the owner-directed rebuild after the terminal review decision.

`check` and `run` share offline production rendering. Before a paid campaign opens, the CLI validates actual selected novelty targets, rendered question uniqueness, call-site parameters and supported alias transformations. Alias fallback model arrays and sampling parameters dropped by an alias are refused. J9 non-finite judgments are model errors, including numeric strings. JSONL uses LF record delimiters; procedure steps cannot introduce extra candidate lines, and exclusion flags must be booleans.

Saved novelty answers are replayed through both `judge._store_judged_procedure` and `extractor.extract_procedure` in disposable SQLite. Reports count current and hypothetical promoted storage separately. Embedding and extraction stubs establish storage integration only.
