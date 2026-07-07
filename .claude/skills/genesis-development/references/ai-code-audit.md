# Auditing AI-Generated Code

Distilled from published research on AI-generated codebases (2026-07 knowledge
ingest, domain `engineering.ai-code-audit`; original source in the KB). Use
when auditing or deep-reviewing code that was produced through iterative LLM
sessions — which includes Genesis itself. The idle-time surplus auditor
carries a compact version of the taxonomy in
`src/genesis/identity/CODE_AUDITOR.md`; this reference adds the ordered audit
passes for interactive deep audits.

## Why iteration makes it worse, not better

The counterintuitive core finding: security and structure degrade across
AI refinement cycles. Vague "improve this" prompts degrade fastest —
each cycle tends to strip validation, relax types, and widen function scope.
Consequences for how WE work:

- **Iterate with scoped, explicit prompts.** "Fix the race in
  `mark_completed` by serializing on the queue lock" — never "improve this
  file" or "make this more robust".
- **Be security-explicit when touching validation/auth/boundaries.** State
  what must NOT be weakened, or the refinement will weaken it.
- **Diff every refinement against what it replaced** for removed
  constraints, dropped guards, and widened signatures — that is where the
  regressions live, not in what was added.

## Failure-mode taxonomy (what to hunt)

Structural: excessive narrating comments substituting for clarity; features
bolted on with zero refactoring (monolith growth as context fills);
phantom guards for impossible conditions; near-duplicate functions far apart
(context loss); cosmetic abstractions (single-implementation interfaces that
relocate complexity instead of hiding it); pattern abandonment in
later-generated modules; mid-file naming-convention drift.

Async/state: un-awaited or unhandled promises; catch-log-return-nothing error
handling (caller proceeds on a missing value); orphan state (written
conditionally, read unconditionally; no teardown for listeners/timers;
mutation after cancel); concurrent writes to shared state with no
lock/serialization; polling loops without cancellation.

Error handling: reveals too much (stack traces / paths / schema in
responses) or handles too little (one generic message for every failure);
missing boundary validation — the single most common flaw class in
LLM-generated code.

Tests: high count, low depth — happy path asserted N ways; empty
collections, nulls, and zeros never probed.

## Ordered audit passes (deep audits)

**Pass 0 — inventory.** Map modules: exports, imports, callers. Flag modules
importing from 5+ sources (god module) and modules with 10+ consumers
(highest-priority audit targets). Estimate AI-generation density (commit
history shape, comment style) to calibrate suspicion.

**Pass 1 — structure.** Orphan modules (zero live callers — tested-but-dead
code counts); orphan state declarations; pattern-consistency sweep (which
modules deviate from the established architecture, and are they the
later-added ones?); abstraction audit (would deleting this interface change
any behavior?); dead-path detection (unreachable branches, unconsumed
returns).

**Pass 2 — async/state.** Inventory every await/task/callback: does each
have a failure path? Trace every catch: does it rethrow, return a typed
fallback, or notify — or does it swallow? Map shared-state writers: what
serializes them? Verify every registration has a teardown. Trace
collection-processing code against empty/null/single-item inputs.

Genesis-specific overlays for these passes: `tracked_task()` for background
tasks, the observability rules in `references/observability.md`, and the
enumerate-don't-spot-check protocol in the skill body.
