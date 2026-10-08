# Qualification corpus and adapters (replacement 3 of 6)

The corpus loader reads one `<contract>.jsonl` file per registered contract.
Registered rubric versions come from the production rubric registry; J9 uses its
production relevance prompt version, and novelty uses `novelty-v1`. Each line is
one JSON object. Literal newlines split records; valid Unicode separators inside
strings are preserved. Unknown contracts, empty corpora, stray visible non-JSONL
files, duplicate case IDs and unlabelled or malformed references stop loading.
Rubric/J9 question identity uses actual production rendering. Novelty uses a
structural digest of its new/existing population here, which does not prove
uniqueness of the selected rendered prompt: IDs, unused fields, truncation or
selection can differ while the judge sees the same question. Part5 preflight
must hash every actual rendered request and reject duplicates before execution.

Human-only rubric references use the existing strict calibration validator.
Frontier-assisted loading requires a validated policy and reference admission
receipts. Shared actor checks require feedback reviewers to declare their own human/frontier
role, and frontier feedback requires an approved model and identity evidence.
Legacy receipts missing these declarations remain review obligations; loading
does not infer historical actors or transfer approval to modified snapshots.
The loader supplies the canonical `corpus.versions()` mapping to
the reference admission boundary; standalone reference APIs require that
context explicitly. Relevance and novelty also check declared provenance and current
contract versions. Rubric questions use the production renderer; J9 uses its
production truncation/formatting; novelty validates explicit null/member labels,
deterministic finite float32 embeddings and procedure fields. Boolean exclusion
flags remain typed. Ordinary multiline steps are supported, while text that
injects the exact rendered candidate-header shape is rejected.

`coverage(..., complete=True)` checks missing contracts as well as class floors:
25 negative and25 positive cases per rubric/J9,300 distinct and100 redundant
novelty cases. These checks establish declared class counts, not actual rendered-question
uniqueness or a qualification verdict. A loadable subset is not complete
qualification coverage. Candidate
quality, cost and repetitions are measured later by the runner.

Relevance calls the production J9 judge and checks its raw answer for invalid
nonfinite values before using the production >=0.5 decision threshold. Novelty
calls the production cross-type selector with deterministic case embeddings,
observes selected IDs from actual rendering, verifies every numbered candidate
and the exact prompt suffix, then maps the raw answer to that rendered identity.
A malformed model verdict becomes a model error; missing/ambiguous candidate
mapping or an unreachable expected target is an incomplete local preparation.
Selection is not reimplemented from case order or similarity assumptions.

`Sandbox` creates a full-schema template under the caller-selected disk-backed
scratch root, then copies a disposable case database and admits it through the
normal database-integrity connection helper. It populates reference-case rows,
including exclusion flags, and closes each case connection after judgment.
This supports isolated standalone adapter use; it never opens the production DB.
Do not run these globally patched novelty observers concurrently inside a serving
process. Use an isolated qualification process and serialize its case judgments.

The offline `Probe` records rendering while answering not-redundant; that answer
is a preparation placeholder, not measured candidate performance. Full offline
preflight and both disposable production storage paths land in replacement5;
transport and runner orchestration are separate replacements. No CLI or paid
completion is supplied here. Real references and approvals remain private.

For offline acceptance, set `LITELLM_LOCAL_MODEL_COST_MAP=True` before importing
Genesis. LiteLLM otherwise attempts an optional public model-price-map download
at import time and falls back to its bundled map if it fails. Install Python
connection/DNS interception before imports when checking for zero network
attempts; interception installed afterward does not cover SDK initialization.
