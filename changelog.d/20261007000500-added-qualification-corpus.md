- Added qualification corpus validation and offline production-contract adapters
  for relevance and novelty, preserving source-labelled references and actual
  rendered candidate identity. Full execution and storage replay land later.

Corpus JSONL decoding uses UTF-8 with structured invalid-byte refusal. Adapter observations are scoped to each invocation, raw relevance must be a numeric value in [0, 1], and embeddings that store as an all-zero float32 vector are rejected.
