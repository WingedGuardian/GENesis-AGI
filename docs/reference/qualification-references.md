# Qualification reference admission (replacement 2 of 6)

`genesis.eval.qualification.references` is an offline library. It examines
already graded references and identifies outstanding review obligations;
it makes no model calls and never invents labels, receipts or human approval.
Corpus/prompt validation, CLI integration and candidate qualification land later.

The caller passes the current contract-version mapping to `validate_policy`
and `review`, or a contract version to `admit`. Frontier-assisted policy uses
`frontier-assisted-v1`, an approved grader list, source-backed guidance and a
feedback list. Approved grader identities normalize NFC, case and supported
slash-delimited gateway prefixes; MiMo and DeepSeek candidate families are
excluded. The confidence threshold is an integer from 90 to 100. Confidence is
self-reported, not measured calibration or proof of correctness.

Human decisions use `user_passed`; frontier decisions use `reference_passed`.
The opposite active field is rejected. Earlier decisions may remain in
`reference_history`, which is historical evidence rather than the active label.
Novelty decisions use `expected_target`, either null or an existing candidate ID.
Frontier provenance declares its reviewer/model, contract version, confidence,
identity evidence, rationale, supporting evidence and an empty uncertainty list.
Missing evidence or uncertainty stops admission even at high confidence.

Every admitted case, including human-labelled cases, needs a feedback review
receipt. It binds the current policy hash and case hash, declares its reviewer,
and accounts for every feedback ID with a reasoned `regraded` or `unaffected`
disposition. The case hash covers active inputs, decisions and provenance;
review receipts and historical judgments are excluded from that hash. Changing
guidance invalidates existing receipts throughout the corpus, including cases
outside the feedback's named contracts: every case needs a fresh applicability
assessment. Never fill receipts automatically to bypass that review.

`review(spec, versions)` returns case obligations keyed by contract and case ID.
Routine missing/stale receipts route to `needs_llm_regrade`; below-threshold
confidence, missing supporting evidence or unresolved uncertainty route to
`human_review`. A clean receipt is `admitted`. This status proves reference
admission only; it is not a candidate qualification result or storage replay.

For an admitted corpus, `approval_issues(corpus, policy, approval)` checks a
human, approved, independent attestation with supporting evidence and exact
corpus/policy hashes. Its reviewer must differ from the declared frontier
labelers after identity normalization. These are declarations, not authenticated
identities or signatures. Keep real approvals and references private; publish
only synthetic test fixtures. Preserve approved snapshots when guidance changes,
and record new evidence rather than overwriting their history.
