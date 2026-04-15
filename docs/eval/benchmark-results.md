# Model Benchmark Results

Last updated: 2026-04-15

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
| kimi-k2.5 | 10/15 (67%) | 9/12 (75%) | 10/10 (100%) | **81%** | Paid, $0.60/$2/MTok |
| openrouter-qwen36plus | 10/15 (67%) | 8/12 (67%) | 10/10 (100%) | **78%** | Paid, $0.325/MTok |
| mistral-large-free | 8/14 (57%) +1sk | 9/12 (75%) | 9/9 (100%) +1sk | **77%** | 4 RPM, slow |
| openrouter-gemma4 | 3/4 (75%) +11sk | 1/2 (50%) +10sk | 4/4 (100%) +6sk | **75%** | Heavily rate-limited |
| openrouter-free | 8/15 (53%) | 7/12 (58%) | 10/10 (100%) | **71%** | Llama 4 Maverick |
| mistral-small-free | 7/15 (47%) | 7/12 (58%) | 10/10 (100%) | **68%** | Fast, low quality |
| openrouter-trinity-free | 7/15 (47%) | 6/12 (50%) | 10/10 (100%) | **66%** | 400B MoE, free until 2026-04-22 |
| gemini-free | 1/1 (100%) +14sk | 1/1 (100%) +11sk | 1/1 (100%) +9sk | **100%*** | Daily quota, needs re-run |

\* Gemini hit daily quota after 1 case per dataset. Score is not reliable — re-run needed.

### Not scoreable

| Provider | Reason | Status |
|---|---|---|
| openrouter-deepseek-r1 | Free endpoint removed from OpenRouter | Disabled |
| openrouter-qwen3coder | Venice backend congestion, all requests timeout | Retry off-peak |

## Key Findings

1. **Structured output is easy.** Every functional provider scores 100%.
   The dataset needs harder cases to differentiate.

2. **Classification is the differentiator.** Scores range from 47% to 80%.
   Groq (Llama 4 Scout) leads by a wide margin.

3. **Free tier rate limits distort results.** Gemini (15 RPM but daily cap),
   Gemma4 (heavy throttling), and Mistral Large (4 RPM) all have incomplete
   data. Rate-limited providers should be evaluated during off-peak hours or
   with extended timeouts.

4. **Groq is the best free provider** for surplus workloads: fast, 80%
   classification accuracy, no skips on 30 RPM.

5. **Paid providers cluster at 78-81%.** Kimi-K2.5, Qwen 3.6+, and Cerebras
   all perform similarly. Kimi has a slight edge on extraction.

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

After running `genesis eval benchmark`, update this file with the latest results.
A future automation (`genesis eval export`) should generate this automatically.
