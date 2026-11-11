# Inbox evaluation evidence and reuse

The inbox composes `src/genesis/identity/INBOX_EVALUATE.md`, the complete
`evaluate` and `user_evaluate` skills, and its precedence footer. The inbox
template controls output shape; the embedded skills supply the analysis.
Standalone evaluations use their skill's template. Slash commands remain
pointers to those skills.

Evaluate the distinctive mechanism before choosing a disposition. Compare it
with the current Genesis implementation, including quality and failure handling,
rather than treating a shared category as equivalence. Consider direct use,
integration and separable upstream components before rebuilding. A foreign
runtime affects integration cost; legitimate privacy, license, architectural-core
and equivalence constraints still justify non-adoption.

Both evaluator templates request `why_not_adopt` for ADAPT: the reusable option
considered and evidence for choosing adaptation over direct integration, with
uncertainty and search limits preserved. The existing Recommendation parser
retains the full trimmed nonempty string. Missing, blank, null or nonstring
values become `None`; ADAPT then emits a generic warning without rejecting the
recommendation or changing routing, approvals or capability-build ownership.
The optional field is appended after `classification`, preserving the original
positional constructor prefix. Other actions may retain it without requiring it.
This records an explanation; it does not validate the decision or establish a
measured improvement in model judgment. Existing evaluations remain usable.

Each evaluated item includes a concise `### Evidence` section after its
recommendation and before comparison/lenses. It records a source claim and short
quote from acquired text, or an acquisition limitation; the relevant current
Genesis mechanism or specific user evidence; and labelled inference/unknowns.
Quoted source headings use Markdown quote prefixes. Dedicated `**Source:**`
fields and existing Recommendation YAML fields remain required as before.

This is a model-reported record, not mechanically verified source grounding.
A fetch attempt, available text, a matching quotation and a sound interpretation
are different claims. Missing content must remain unknown. No new acquisition
gate, parser, model stage or provider setting is introduced.

Saving an item establishes attention rather than agreement, priority or a user
preference. The evaluator must keep explicit facts, inference and unknowns
distinct. `user_signal` observations require explicit user evidence. When wired
and enabled, the existing asynchronous memory extractor reads the curated output;
it may fail or find nothing durable. The extractor is unchanged by this delivery.
Bounded prompt experiments did not fully preserve uncertainty: an unsupported
claim or comparison could still reach the isolated store. A separate extraction
design is needed before claiming that durable memories retain these distinctions.
Existing confidence handling, dedup, external-untrusted provenance and the kill
switch remain unchanged. Source overlap compares extraction with the evaluation
text, not the original source, and does not verify the truth of a stored claim.

Under a capability-build bracket, `action: BUILD` remains the contract. A
`build` verdict needs a real allowed repository artifact and all five nonempty
`build_spec` lists. An existing CLI plus a necessary skill/integration can meet
that contract. A pure operator install, config/scripts/install-home change, or
empty diff cannot complete through the PR build lane: use `needs_discussion`
and name the adoption/access decision. Do not fabricate an adapter, infer host
installation permission or rebuild the tool to satisfy the lane. Cognitive-core
work remains a `dont_build` scope refusal. Ordinary evaluations retain
ADOPT/ADAPT/WATCH/IGNORE and the existing personal action vocabulary.

Prompt compliance needs behavioral qualification in addition to contract tests.
Frozen OLD/NEW comparisons should keep model, effort and supplied evidence
identical, inspect every output, and treat infrastructure/model mismatches as
inconclusive. Reasoning-only snapshots do not certify live source acquisition.
Any future memory-extraction change needs separate qualification through
`9_fact_extraction`; improving evaluator output alone does not qualify extraction.
Controlled scan/response/consumer/baseline tests establish downstream compatibility;
they do not substitute for either model comparison.

The bounded synthetic comparison demonstrated the install-only BUILD discussion
boundary, while both prompt versions still produced some unsupported traction
scores. That is evidence for a narrow improvement, not general source fidelity
or reliable memory extraction.
