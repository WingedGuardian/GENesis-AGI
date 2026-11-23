# Bounded qualification continuation

`study-run` is an explicit paid entry point. Building or installing it conveys
no spending approval. It computes full current reference/source admission and
requires the version-3 plan described in `qualification-lineage.md`, plus a
separate private consent file supplied with `--spending-approval`.

The consent has exactly these fields: `version: 2`,
`protocol: "bounded_continuation_v1"`, `plan_hash` (the canonical plan digest),
`max_new_attempts` (the exact integer from that plan), `author: "owner"`, and
nonblank `decision_evidence` identifying the actual decision. This is a trusted
local operator assertion, not human authentication. Historical consent, design
approval and synthetic fixtures cannot provide the new paid decision.

The command takes the same corpus, parameters, reference-policy/current-policy,
reference-approval, source-file map and disk scratch arguments as `study-check`.
It additionally takes `--study-plan` and `--spending-approval`. There is no live
output-directory override: creation uses the exact absolute `study_root` in the
approved plan. An existing root, even an empty or partial one, refuses relaunch.
Read-only inspection never repairs acquisition by sending another completion.

Input bytes are hashed at admission and final boundaries. Between those
boundaries, some source and input checks use filesystem metadata. Rapid
same-size in-place writes can leave that metadata unchanged; these checks are
not a complete write history. Keep source and inputs stable throughout a study.

## Physical attempts and replacement policy

One invocation serially drives the actual contracts. Each new physical request
has its own existing version-2 accounting journal under `attempts/<id>`. The ID
binds the plan hash, model alias, logical attempt and ordinal (zero or one).
Children retain the actual transport and spec; the complete logical admission
is stored once in the owner's root manifest. Scoring projections never become
accounting events or replace the original physical row.

The delegate has no client retry or provider/model fallback. Only a complete,
durably closed upstream-pool HTTP429 with the precise source-bound identity and
failure ordering recognized by `lineage.rejected_row` permits a replacement.
Each coordinate can receive at most one such replacement, and inherited
rejected coordinates already consume the plan's total replacement cap. The
plan's exact maximum new count includes its finite possible extra attempts.

The controller closes the rejected child before waiting the exact declared
cooldown. It checks inputs, source, the physical census and funding again before
the next dispatch. Timeout, cancellation during transport, partial output,
unknown failure, conflicting identity/billing, an over-allocation charge or a
second rejection at the same coordinate stops acquisition. A malformed grade
is a measured model error: stop subsequent acquisition and retain that answer
for detached grading. Neither an unfavorable grade nor malformed output is a
replacement opportunity.

Decimal settled charges plus unresolved reservations from all histories and
new children plus the next reservation must fit the operating allowance.
Initial admission also funds the complete remaining schedule and the largest
distinct eligible replacement allocations. Missing billing never becomes zero;
a successful replacement does not release the rejected request's reservation.
The allowance is not a guaranteed final provider invoice ceiling.

## Owner, terminal and readers

The root uses the existing private Campaign writer and fingerprint-scoped
comparison lock. It records the immutable plan and input hashes. Every closed
child remains an immutable checked input until finality. The controller closes
its writers before exclusively publishing `terminal.json`; a crash never
implicitly resumes acquisition. Preserve partial and failed generations.

The terminal binds the root journal and complete child hashes, actual writer
closure, final admission disposition and each child's physical incident status.
A verified answer and an incomplete overall study are compatible: model quality,
operational failures and unresolved expense are separate report dimensions.

Both `study-check` and `study-report` inspect current-root children. Active
writers refuse concurrent reading. A missing or invalid terminal cannot certify
reuse, but readable child costs remain visible with an incomplete disposition.
An unreadable root or child journal leaves the affected financial census
unknown; it never reports that unknown amount as zero. A changed previously
bound input refuses the frozen view. No reader appends, truncates or sends GET
or completion requests.

When a future plan references a child, its `closure` binds the owner's physical
`directory`, `journal_sha256` and `terminal_sha256` (null only when absent).
The complete sibling census, flattened ancestors and auxiliary expense inputs
must also be supplied. Unsafe siblings remain in financial accounting. A closed
incomplete owner may contribute independently verified unaffected children;
partial or invalid terminal evidence permits no automatic answer reuse.

Read-only commands accept `--study-root <exact-copy>` to inspect a relocated
owner while retaining the original plan and hashes. Child locations resolve
under that explicitly selected physical copy. External ancestor/source paths
remain bound as declared; this option does not recursively rewrite them or
authorize execution in a new location. `study-run` accepts no such option.

Reports use the existing rubric/J9 scoring, novelty candidate mapping and four
disposable storage replay modes. Embedding/extraction stubs establish integration
only. The financial `new_allocation` is the frozen initial maximum allocation,
not the unspent account balance. Financial uncertainty can keep overall status
incomplete even when every logical answer has been measured. These results
never select a production route or approve promotion.

Keep plan, actual consent, journals, terminal, reports and original failed
generations private under the qualification evidence archive/backup convention.
The model-validation skill describes operator supervision and evidence closure.
