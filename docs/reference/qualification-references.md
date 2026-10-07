# Qualification reference admission (replacement 2 of 6)

`genesis.eval.qualification.references` is an offline library. It examines
already graded references and identifies outstanding review obligations;
it makes no model calls and never invents labels, receipts or human approval.
Corpus/prompt validation, CLI integration and candidate qualification land later.

The caller passes the full current contract-version mapping to `validate_policy`
and `review`, and as `contracts=versions` to `admit`, `blockers` and
`approval_issues`. Direct admission also takes its contract version, which must
match that mapping. Missing context refuses admission; no prior validation call
is trusted. The caller owns the mapping's completeness and currentness; the
library does not authenticate a canonical contract registry. Each boundary
validates the complete policy against a detached
snapshot. `approval_issues` checks admission of the corpus as well as the
attestation. Invalid policy, unknown contracts or empty/malformed case lists
cannot produce an empty approval-issues result. Frontier-assisted policy uses
`frontier-assisted-v1`, an approved grader list, source-backed guidance and a
feedback list. Approved grader identities normalize NFC, case and supported
slash-delimited gateway prefixes; MiMo and DeepSeek candidate families are
excluded, including configured bare model IDs, context-window suffixes and
routing aliases. The confidence threshold is an integer from 90 to 100. Confidence is
self-reported, not measured calibration or proof of correctness.

Human decisions use `user_passed`; frontier decisions use `reference_passed`.
The opposite active field is rejected. Earlier decisions may remain in
`reference_history`, which is historical evidence rather than the active label.
Novelty decisions use `expected_target`, either null or an existing candidate ID.
Frontier provenance declares its reviewer/model, contract version, confidence,
identity evidence, rationale, supporting evidence and an empty uncertainty list.
Missing evidence or uncertainty stops admission even at high confidence.

Every admitted case, including human-labelled cases, needs a feedback review
receipt. It binds the current policy hash and case hash, declares its reviewer
and `label_source` (`human` or `frontier_llm`),
and accounts for every feedback ID with a reasoned `regraded` or `unaffected`
disposition. The case hash covers active inputs, decisions and provenance;
review receipts and historical judgments are excluded from that hash. Changing
guidance invalidates existing receipts throughout the corpus, including cases
outside the feedback's named contracts: every case needs a fresh applicability
assessment. Never fill receipts automatically to bypass that review.

Frontier feedback actors also declare an approved, noncandidate `model_id` and
nonblank `model_identity_evidence`. Human actors, including labelers and
approvers, cannot carry these machine identity fields. Reviewer names are opaque;
they need not match a model ID. Missing legacy feedback actor declarations remain
review obligations; preserve historical snapshots and obtain grounded metadata
before making a new snapshot. Reassessment can retain an unchanged judgment.
Changed active judgments must carry corresponding active provenance.

`review(spec, versions)` returns case obligations keyed by contract and case ID.
Routine missing/stale receipts route to `needs_llm_regrade`; below-threshold
confidence, missing supporting evidence or unresolved uncertainty route to
`human_review`. A clean receipt is `admitted`. This status proves reference
admission only; it is not a candidate qualification result or storage replay.

`approval_issues(corpus, policy, approval, contracts=versions)` first checks
reference admission and then checks a
human, approved, independent attestation with supporting evidence and exact
corpus/policy hashes. Its reviewer must differ from the declared frontier
labelers and frontier feedback reviewers after identity normalization. A valid
receipt actor change can retain case admission but invalidates the old full-corpus
approval. `summary` reports initial `sources` and separate `feedback_sources`;
these counts confer no admission. These are declarations, not authenticated
identities or signatures. Keep real approvals and references private; publish
only synthetic test fixtures. Preserve approved snapshots when guidance changes,
and record new evidence rather than overwriting their history.
