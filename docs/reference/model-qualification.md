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

Every case carries `reference_provenance {label_source: "human", reviewer,
rubric_version}`. **The owner labels and approves.** A session may draft case
inputs blind, without showing any machine grade, but a row counts only once the
owner has given it a human label; drafts stay outside the corpus directory.

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
qualification requires one so a request's cost is bounded, and the report counts
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

A request cap bounds the count: `cases x 3 repetitions x (1 + 1 resend)`, counted
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

Answers are looked up by the prompt rendered **now**. A crash or a rescore never
pays twice; an edited case or a rubric change renders a new prompt, so its old
answer no longer matches and the case is reported `unanswered` until `run` pays
for it. A torn final line is discarded on open: its write never finished, so a
torn dispatch was never sent and a torn answer is resent, within the cap.

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
