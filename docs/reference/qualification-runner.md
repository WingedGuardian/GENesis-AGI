# Offline qualification runner and reports

Run this workflow in a dedicated serial process, never inside the serving
process. Production rendering and disposable storage replay temporarily patch
production globals. Use an owned disk-backed scratch directory.

`python -m genesis.eval.qualification check` inspects a subset offline. Supply
`--corpus` (one JSONL file per contract) and `--temp-root`. A subset returns
incomplete coverage and counts; it cannot authorize execution. Complete admission
also needs `--params` (JSON keyed by routing alias), `--reference-policy`,
`--current-reference-policy`, `--reference-approval`, and `--source-files`
(JSON mapping original source paths to SHA-256). The current policy is loaded
independently. The same inputs are required for reports. An optional `--alias`
selects the candidate for a complete check.

`review-references --spec <JSON>` returns aggregate admission counts offline.
It publishes no case IDs, prompts, raw judgments or exception text.

`report --alias <alias> --campaign <private-directory>` recomputes current
complete admission, reads one existing private campaign snapshot under a shared
lock, and rescores verified retained answers. It creates no campaign, changes no
permissions, appends no events, discards no torn bytes, acknowledges no incident,
reads no credential and makes no provider request. A missing lock, active writer,
foreign file link, invalid history or torn tail cannot qualify. Missing answers
remain unscored. Acknowledged receipt-only recovery does not recreate an answer
or permit resending.

The frozen manifest must match all current contract/version/case/repetition/
prompt coordinates, canonical wire bodies, model, explicit parameters,
credential fingerprint, upstream namespace and receipt-provider mapping.
Corpus, reference policy, approval, source bytes and declared production/runner
module fingerprints also bind the report. A changed binding invalidates current
scoring; observed historical counters remain separate. Source bytes and module
fingerprints are checked again after scoring. These checks are scoped integrity
checks, not authentication of declared actors or a full transitive attestation.

Add `--against <second-alias> --against-campaign <second-private-directory>`
for paired reporting. Each model has its own campaign. Pairing requires the same
current shared admission identity and never selects a winner or promotes a route.
Paired financial reporting censuses both exact scored snapshots together;
conflicting generation ownership makes the combined result incomplete and
excludes the conflicting charges from its resolved expense total.
Judge qualification includes all registered rubrics and J9 relevance. Every
repetition needs 80% agreement overall and in each class with the required class
floors. Novelty permits no false or wrong-target suppression, including when
other cases are unscored. Saved valid novelty answers exercise both actual stores
under current and simulated promoted allowlists in disposable SQLite databases.
Deterministic embeddings and synthetic classifiers establish wiring, not model
or retrieval quality.

`run` supports Flash alone (`--alias openrouter-deepseek-flash --campaign`),
or the historical MiMo–Flash pair with both `--against` and `--against-campaign`.
Both modes require `--operating-plan`, `--spending-approval` and all complete
admission inputs above. The parameters file must contain exactly the selected
population. Other singleton aliases and additional candidates are refused.
Missing spending approval still
refuses before credentials or campaign creation. The private version-1 operating
plan freezes one or two complete 2,250-attempt manifests, source admission, requested
parameters, receipt-provider identities, credential fingerprint, price evidence,
per-model and combined allowances, key limit and baseline usage. Currency uses
exact nonnegative decimal strings. The plan names
`financial_promise=operating_budget_with_overrun_risk` and
`stop_semantics=stop_campaign_on_unresolved_evidence_no_resend` for Flash alone,
or `stop_semantics=stop_both_on_unresolved_evidence_no_resend` for the pair.

These are operating allocations, not externally guaranteed maximum charges.
The key envelope must fund every allocation and fit the combined allowance.
The separate private approval binds the entire canonical plan hash, the owner,
`2250_requests` or `4500_requests` scope matching the exact selected population,
and actual decision provenance. Changing the population invalidates old consent.
This is a trusted local
operator assertion, not cryptographic authentication of a human decision. An
agent must never fabricate that record from design approval or a test fixture.

A private kernel comparison lock, shared by credential fingerprint across both
population sizes and all campaign directories, precedes journal writers and credential reads. The final pre-dispatch
key observation must match the approved nonreset limit and baseline plus exact
consistent receipt costs from the selected campaigns; remaining credit must cover all
undispatched allocations. One inference is in flight globally. Settlement and a
fresh key observation precede the next request. Uncertainty or processing failure
stops new acquisition in the entire selected population and retains an existing journal incident
when there is a dispatched attempt. Pre-dispatch refusal invents no reservation,
dispatch or acknowledgement. Resume never resends a dispatched coordinate.

Plan and approval descriptors remain open: metadata/replacement checks run per
request, and full hashes are checked at admission and completion. Equal-size
writes within a filesystem timestamp tick can evade the per-request metadata
check; edited bytes never replace the frozen admitted plan or consent, and
completion hashing refuses changed content. Editing these files is not an
immediate revocation mechanism. These checks do not prevent deliberate
same-user evasion. Reports separately show
consistent observed receipt expenses (including above-allocation charges),
unresolved costs, over-allocation incidents and journal settled/reserved totals.
Late or undisclosed provider charges can exceed the allowance; a stopped or
incomplete run cannot qualify or promote any model. Single-model execution reports
its model and financial aggregates without fabricated paired statistics; detached
single-model reports retain their existing format. Effective provider
parameters, real quality, final paid settings, genuine corpus consent and actual
spending approval remain separate decisions.
The CLI prints aggregate results and fixed failure codes. Exit codes are 0 for
pass, 1 for fail and 2 for incomplete. Private campaign evidence can contain model
answers; retain its operator-controlled 0700 directory and 0600 files. Reports
authorize no spending, deployment or judge/novelty promotion.
