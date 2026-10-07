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
Judge qualification includes all registered rubrics and J9 relevance. Every
repetition needs 80% agreement overall and in each class with the required class
floors. Novelty permits no false or wrong-target suppression, including when
other cases are unscored. Saved valid novelty answers exercise both actual stores
under current and simulated promoted allowlists in disposable SQLite databases.
Deterministic embeddings and synthetic classifiers establish wiring, not model
or retrieval quality.

`run` currently performs offline admission and then refuses with
`financial_eligibility_unavailable`, before credentials, network or campaign
creation. This is an unfinished paid execution boundary. Declared maxima,
`max_price`, output token settings and key credit balance do not establish a
verified total charge bound. Complete provider costs/effective parameters,
key isolation and concrete per-model and combined spending approval remain
separate prerequisites. Existing corpus approval remains intact; missing actual
historical actor provenance must be reconciled from real evidence.

The CLI prints aggregate results and fixed failure codes. Exit codes are 0 for
pass, 1 for fail and 2 for incomplete. Private campaign evidence can contain model
answers; retain its operator-controlled 0700 directory and 0600 files. Reports
authorize no spending, deployment or judge/novelty promotion.
