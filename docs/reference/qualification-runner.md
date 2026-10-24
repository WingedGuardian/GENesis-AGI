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

Keep the corpus, policies, approval and original source bytes in durable private
storage before admission. The source map records hashes and locations; it does
not back up those files or make a missing source recoverable.

Use `archive-sources --source-files <map.json> --generation <new-name>` to retain
the exact mapped bytes under `~/.genesis/qualification-sources/<new-name>`.
The resolved HOME must already exist as a directory; durable HOME initialization
belongs to the caller/platform. Every invocation synchronizes the store,
`.genesis` and HOME before creating a generation, including after a failed
initialization left directories behind.
This writes a private canonical manifest and copied content-addressed blobs;
it changes no source, reference decision or approval. For SQLite, supply an
already frozen, consistent standalone snapshot: this
command does not create a live database snapshot or collect WAL sidecars.
Existing generations,
including partial generations, are never overwritten or repaired. Failure after
manifest publication reports failure and uncertain durability while preserving
evidence; readable complete bytes do not retroactively establish a successful
durable publication.
The blob directory and each new blob entry are synchronized before copying.
Copied bytes are flushed and synchronized even when a source read aborts, so
later read or validation failures preserve completed blobs and copied prefixes
when those operations succeed. Failed flush, synchronization or close still
reports failure with uncertain durability and retains the normal Python
exception chain; it never establishes successful publication.

Complete admission requires genuine human approval metadata `source_retention`
with `primary_generation`, `restored_backup_generation` and `backup_evidence`.
Both generation paths must contain the exact manifest and complete copied blob
set; missing, corrupt, foreign, symlinked or hardlinked members refuse. The
primary uses the fixed durable source store; the separately restored generation
must be under `~/.genesis/qualification-source-restores/`. Both store directories
are private and cannot have symlinked ancestors. Originals may then disappear without
changing their logical origin identities. This does not recover a source that
was already missing, authenticate its historical origin or transfer approval.

Use the existing encrypted backup extra-directory mechanism to retain this
store, preserving all other configured extra directories. Verify this exact
generation was included and restore into isolation, never over the primary.
Only then record genuine restore evidence and approve the exact new approval
identity. A second local copy or a generic successful backup is not proof of an
independent retained backup. The CLI checks actual restored bytes; operator
assertions are not authenticated. No backup configuration is changed by the
archive command. There is no automatic archive expiry: retire only after
checking dependent approvals/campaigns and a verified recoverable copy, using
the existing recoverable trash workflow.

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
module fingerprints, including financial accounting and durable evidence, also
bind the report. A changed binding invalidates current
scoring; observed historical counters remain separate. Source bytes, the complete
declared module population and MCP registry/cache coherence are checked again
after scoring. These are admission and completion checks: detected drift
invalidates qualification but does not promise to prevent every intervening
request. These checks are scoped integrity
checks, not authentication of declared actors or a full transitive attestation.
Detached reports do not take operating-plan or spending-approval inputs and do
not independently authenticate current external consent for the frozen campaign.

Add `--against <second-alias> --against-campaign <second-private-directory>`
for paired reporting. Each model has its own campaign. Pairing requires the same
current shared admission identity and never selects a winner or promotes a route.
Paired financial reporting censuses both exact scored snapshots together;
conflicting generation ownership makes the combined result incomplete and
excludes the conflicting charges from its resolved expense total.

Unmarked legacy histories retain detailed incomplete reports with
`campaign_incompatible` reasons. Reporting accesses no credentials and preserves
the history files. Present malformed `baseline_sample` markers are refused.

An explicit Pro incumbent sample can reduce comparison requests while Flash
still runs the complete corpus. Put `baseline_sample` in Pro's campaign binding,
with exactly `purpose: "incumbent_baseline"`, the complete `corpus_hash`,
`case_ids` mapping every admitted contract to a nonempty proper subset of unique
case IDs, and nonblank `selection_evidence`. Every selected contract must include
both reference classes. Select and freeze the sample before examining candidate
results. Sampling is permitted only for Pro in a Pro–Flash execution pair; Flash
retains its complete schedule. The operating consent scope names the exact
combined request count, including all three repetitions of each selected case.
Complete preflight still validates the full corpus, approval, sources and class
floors; sampling does not approve a cropped reference corpus.

The Pro report has `status: "baseline_only"`,
`scope: "sampled_incumbent_baseline"`, `qualification_status: "not_assessed"`,
and a separate `baseline_completion`. A verified served malformed model grade
is a completed observed error; missing, local or unverified evidence remains
incomplete. A paired report has scope `full_flash_with_sampled_pro_baseline`.
Its status preserves Flash's full pass/fail result only when the baseline and
full Flash evidence are complete; otherwise the comparison is incomplete.
An exit of zero never means that sampled Pro was fully requalified.

Paired tables use the exact declared common sample. Case-level summaries for
each contract and reference class use
the majority of three correct judgments, counting malformed model grades as
incorrect. Repetitions are not additional independent cases. McNemar p-values
are exploratory because source-family independence is unverified; they do not
select a winner or authorize promotion. Deliberately balanced sample classes
and correlated constructed cases do not support an unweighted whole-corpus or
production accuracy claim. Close results can remain inconclusive, without
automatically running a larger sample.

Judge qualification includes all registered rubrics and J9 relevance. Every
repetition needs 80% agreement overall and in each class with the required class
floors. Novelty permits no false or wrong-target suppression, including when
other cases are unscored. Saved valid novelty answers exercise both actual stores
under current and simulated promoted allowlists in disposable SQLite databases.
Deterministic embeddings and synthetic classifiers establish wiring, not model
or retrieval quality.

`run` supports Flash alone (`--alias openrouter-deepseek-flash --campaign`),
or an explicit Pro–Flash or historical MiMo–Flash pair with both `--against`
and `--against-campaign`. Pro uses `openrouter-deepseek-v4`, preserving the
configured `deepseek/deepseek-v4-pro` identity. Choose and verify each model's
upstream, parameters and receipt identity independently; Flash's upstream is
not evidence that Pro supports that upstream.
Both modes require `--operating-plan`, `--spending-approval` and all complete
admission inputs above. The parameters file must contain exactly the selected
population. Other singleton aliases and additional candidates are refused.
Missing spending approval still
refuses before credentials or campaign creation. The default private version-1 operating
plan freezes the exact selected manifests (normally 2,250 attempts per model), source admission, requested
parameters, receipt-provider identities, credential fingerprint, price evidence,
per-model and combined allowances, key limit and baseline usage. Currency uses
exact nonnegative decimal strings. The plan names
`financial_promise=operating_budget_with_overrun_risk` and
`stop_semantics=stop_campaign_on_unresolved_evidence_no_resend` for Flash alone,
or `stop_semantics=stop_both_on_unresolved_evidence_no_resend` for the pair.

These are operating allocations, not externally guaranteed maximum charges.
The key envelope must fund every allocation and fit the combined allowance.
The separate private approval binds the entire canonical plan hash, the owner,
request-count scope matching the exact selected population (for example,
`2250_requests`, `4500_requests`, or `2850_requests` for a 200-case Pro baseline),
and actual decision provenance. Changing the population invalidates old consent.
This is a trusted local
operator assertion, not cryptographic authentication of a human decision. An
agent must never fabricate that record from design approval or a test fixture.

A private kernel comparison lock, shared by credential fingerprint across both
population sizes and all campaign directories, precedes journal writers and credential reads. The final pre-dispatch
key observation must match the approved nonreset limit and baseline plus exact
consistent receipt costs from the selected campaigns; both key credit and validated
effective/member budget headroom must cover all undispatched allocations across
the entire selected population. One inference is in flight globally. Settlement and a
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
