# Immutable qualification lineage

`study-check` and `study-report` inspect a frozen continuation plan without
credentials or provider requests. They require the same complete corpus,
reference policy/current policy, actual approval, retained source map, parameter
file and disk scratch arguments as `check`, plus `--study-plan <private.json>`.
Both commands recompute current full admission internally. The check's pass
means only that the inherited inputs and remaining allocation were admitted.
Neither command approves spending or promotes a route.

The closed version-3 plan uses `protocol: "bounded_continuation_v1"`. It binds:

- An absolute `study_root`; `source_commit` and `runner_sources` from
  `study_report.source_identity()`; `financial_promise` equal to
  `operating_budget_with_overrun_risk`.
- `credential_mode: "shared_existing"`, the credential's SHA-256 fingerprint,
  a decimal-string `combined_allowance`, and `price_snapshot` with nonblank
  `observed_at` and `evidence` strings. A price declaration is not an invoice
  guarantee. These readers never read the underlying credential.
- `models`, keyed by the existing supported alias population, with one
  `manifest` per model. Full current campaign validation still applies,
  including every repetition and the explicit incumbent sample when present.
- Ordered `histories` entries, each containing `directory`, `journal_sha256`,
  `manifest_hash` and `closure`. This tranche requires null closure and refuses
  every bounded-study child marker, including a malformed/null marker. The
  dependent controller tranche must add its child reader and complete sibling
  financial census together with the writer and closure lifecycle tests.
- A finite `expense_evidence` list. Each auxiliary GET body binds `path`,
  `sha256`, `journal_sha256`, `attempt`, `request_hash`, `generation_id`,
  `model` and `provider`. A hash-bound raw price annotation cannot settle an
  invalid historical receipt, reconstruct an answer or release a reservation.
- The exact derived `reuse` map: alias → logical manifest attempt ID →
  `{journal_sha256, attempt}`. Select the first independently eligible response
  in declared acquisition order, including incorrect or malformed judgments.
  No eligible answer may be omitted or replaced because of its grade.
- Nonnegative integer `max_total_replacements`, `max_new_attempts` and
  `cooldown_seconds` (at most 600). Already rejected coordinates consume the
  replacement cap. Each remaining coordinate can receive at most one additional
  replacement. The exact maximum new count and worst additional allocation
  are derived and checked, rather than interpreted as an open retry allowance.

Inheritance permits a change to runner implementation fingerprints only. Corpus,
labels, source bytes, rubric definitions, actual rendered messages, candidate
mapping, sampled population and production source fingerprints remain identical.
Public, backend and generation-receipt identities, provider, endpoint, key mode,
fingerprint, effective parameters and serialized request also have to match.
Every selected response retains its original physical spec and event history.

Missing coordinates with dispatched nonreplaceable evidence or interrupted
reservations make execution admission incomplete. Only the precise, complete
upstream-pool HTTP429 envelope recognized by the lineage reader can consume a
bounded replacement slot. It remains a failed physical attempt with unknown
expense. Generic throttling, timeout, cancellation, partial output, observer or
cleanup failure never establishes this exception.

Accounting comes exclusively from original journals. Decimal settled charges
plus unresolved reservations and the full remaining worst allocation must fit
the declared allowance. Raw GET price annotations are disclosed separately and
may overlap reserved liabilities. Duplicate histories, generation ownership
collisions, changed or torn inputs, contradictory prices and charges above their
reservations refuse admission. Readers never modify, truncate or acknowledge a
history. Private file ownership/link rules and existing writer locks apply.

`study-report` replays eligible retained answers through the real rubric/J9 and
novelty contracts, using disposable storage for the latter. Logical projections
are never passed to accounting or published as fabricated campaign events.
`physical_history` counts original events; `lineage` reports selected/missing
logical responses. Model quality, sampled incumbent scope and unknown expenses
remain separate. Synthetic embeddings/extraction establish storage integration
only.

This read-only tranche reports an inherited prefix and always retains an
incomplete overall study status without verified study closure. An existing
`study_root`, even empty, prevents a fresh acquisition admission. Root/child
inspection during bounded acquisition and verified terminal reporting belong to
the dependent controller tranche; these commands cannot resume acquisition.
The existing `check`, `run` and `report` commands keep their original contracts.
Keep plans, approvals, raw responses and journals private under the existing
qualification evidence retention/backup convention.
