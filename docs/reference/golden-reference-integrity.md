# Human reference integrity

`python -m genesis.eval.reflection_golden_set` writes a private, unapproved
`~/.genesis/output/reflection_quality_draft.jsonl`. Machine decisions are
`proposed_passed`, never `user_passed`. Existing output files are refused before
sampling or judge calls; the final write also uses exclusive creation. The
historical `reflection_quality_golden.jsonl` is not migrated or overwritten.
Zero samples or all failed grades raise without creating an output, so the same
path can be retried. Partial nonempty drafts retain their grading error counts.

Reflection calibration and both MCP consumers (`experiment_run`, `evo_run`)
keep the historical reference path as their default through
`calibration.DEFAULT_REFLECTION_REFERENCE`; generator output is a separate draft.
Missing-reference guidance requires generation followed by independent human
adjudication into a separate reference. Explicit reference paths remain supported.
A historical file existing does not establish human approval.

Generation itself uses inference: do not run it as an offline validation step.

For human-graded references, each row contains a unique, nonblank string `id`,
nonblank `actual`, a boolean `user_passed`, optional string `expected`, and
`scorer_config` naming the selected rubric and providing its required context
strings. `reference_provenance` declares `label_source: "human"`, a nonblank
`reviewer`, and `rubric_version` matching the registered rubric. For example:

```json
{"id":"synthetic-1","actual":"Synthetic observation","user_passed":true,"scorer_config":{"rubric_name":"reflection_quality","session_context":"Synthetic context"},"reference_provenance":{"label_source":"human","reviewer":"example reviewer","rubric_version":"1.0.0"}}
```

Use the current registered version, not a version copied from this example.
After independent human adjudication, opt into whole-file preflight:

```bash
python -m genesis.eval.run_reflection_calibration --golden /private/human-reference.jsonl --strict-references
```

That command performs inference only after validation; it is not a validate-only
command. The library equivalent is `run_calibration(..., strict_references=True)`.
Malformed rows, label coercion, duplicates, missing context, mismatched rubrics or
versions, and model-only provenance are rejected before any scoring call. Strict
mode also rejects repeated grading questions under different IDs: identity uses
the exact rendered judge prompt, using the same pure renderer as scoring.
Different labels, unused fields or irrelevant metadata do not make a question
distinct. Different expected text or context counts as distinct only when it
changes the rendered question. No text normalization is applied.

Without strict mode, existing valid legacy datasets retain their historical
reporting behavior. Generated drafts omit the required `user_passed` field and
are rejected in either mode. Legacy reports do not establish human adjudication.

Provenance here is **declared**, not authenticated: a writable JSON assertion
cannot prove owner approval, independent grading or blindness. Passing strict
validation also does not establish sample size, class balance, qualification
accuracy or candidate model identity. Those require an independently approved,
frozen reference batch and the separate qualification protocol. Historical
machine labels must not be promoted to human labels by adding metadata.
