# Model Benchmark Results

Last updated: 2026-04-16

Benchmark methodology: 3 datasets (classification, extraction, structured_output)
with 37 total cases. Scores are `passed/attempted` — skipped cases (rate limits,
API errors) excluded from the denominator. All runs use the eval harness in
`src/genesis/eval/` with binary pass/fail scorers (no LLM-as-judge).

## Current Results

Best run per provider. Providers marked (free) cost $0; others are paid.

| Provider | Classification | Extraction | Structured Output | AVG | Notes |
|---|---|---|---|---|---|
| groq-free | 12/15 (80%) | 8/12 (67%) | 10/10 (100%) | **82%** | Llama 4 Scout |
| cerebras-qwen | 9/13 (69%) +2sk | 8/11 (73%) +1sk | 9/9 (100%) +1sk | **81%** | Qwen3 235B, 10 RPM |
| gemini-free | 6/7 (86%) +8sk | 4/7 (57%) +5sk | 3/3 (100%) +7sk | **81%** | Daily quota (20 req/day) |
| kimi-k2.5 | 10/15 (67%) | 9/12 (75%) | 10/10 (100%) | **81%** | Paid, $0.60/$2/MTok |
| openrouter-qwen36plus | 10/15 (67%) | 8/12 (67%) | 10/10 (100%) | **78%** | Paid, $0.325/MTok |
| mistral-large-free | 8/14 (57%) +1sk | 9/12 (75%) | 9/9 (100%) +1sk | **77%** | 4 RPM, slow |
| openrouter-gemma4 | 3/4 (75%) +11sk | 1/2 (50%) +10sk | 4/4 (100%) +6sk | **75%** | Heavily rate-limited |
| openrouter-free | 8/15 (53%) | 7/12 (58%) | 10/10 (100%) | **71%** | Llama 4 Maverick |
| mistral-small-free | 7/15 (47%) | 7/12 (58%) | 10/10 (100%) | **68%** | Fast, low quality |
| openrouter-trinity-free | 7/15 (47%) | 6/12 (50%) | 10/10 (100%) | **66%** | 400B MoE, free until 2026-04-22 |

### Not scoreable

| Provider | Reason | Status |
|---|---|---|
| openrouter-deepseek-r1 | Free endpoint removed from OpenRouter | Disabled |
| openrouter-qwen3coder | Returns HTTP 401 "No cookie auth credentials found" on all requests — the Qwen3-Coder free endpoint appears to require browser/cookie auth, not API key (retested 2026-04-16 off-peak) | Disable or investigate |

## Key Findings

1. **Structured output is easy.** Every functional provider scores 100%.
   The dataset needs harder cases to differentiate.

2. **Classification is the differentiator.** Scores range from 47% to 86%.
   Gemini-free leads (86% on 7 attempts), Groq-free close behind (80% on 15).

3. **Free tier rate limits distort results.** Gemma4 (heavy throttling) and
   Mistral Large (4 RPM) have incomplete data. Gemini (20 req/day cap)
   completed ~50% of cases — enough for directional signal but still partial.

4. **Groq is the best free provider** for surplus workloads: fast, 80%
   classification accuracy, no skips on 30 RPM.

5. **Top tier clusters at 81-82%.** Groq-free (82%), Gemini-free (81%),
   Cerebras-Qwen (81%), and Kimi-K2.5 (81%) all perform similarly. Gemini's
   86% classification score is the highest but on fewer attempts (7 vs 15).

## Scoring Methodology

- **Fair denominator**: `passed / (passed + failed)`. Skipped cases (API errors,
  rate limits, 404s) are excluded. A provider answering 6/6 correctly but
  skipping 9 scores 100%, not 40%.
- **Binary scoring**: Pass or fail, no partial credit.
- **Scorer types**: exact_match, json_field_match, set_overlap, json_validity,
  slop_detection.
- **Rate-aware**: Each provider is throttled to its `rpm_limit` from
  `model_routing.yaml` to avoid quota exhaustion.
- **Retry**: 2 retries with exponential backoff (5s, 15s) on transient errors.

## Run History

Stored in `eval_runs` and `eval_results` tables in `genesis.db`.
Query with `genesis eval results` or `genesis eval compare`.

## Updating This File

After running `genesis eval benchmark`, update with `genesis eval export -o docs/eval/benchmark-results.md`
or manually edit the table above.
