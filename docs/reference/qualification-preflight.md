# Offline qualification preflight

`genesis.eval.qualification.preflight.check` is the complete offline entry
point. Run it in a dedicated, serial qualification process with an owned scratch
directory. Rendering and storage replay temporarily patch production globals;
do not call them inside the serving process or concurrent qualification tasks.

The caller supplies the complete corpus, reference policy, independently read
current policy, human reference approval, explicit candidate parameters, and
frozen reference source files (`path -> SHA-256`). Admission uses the existing
corpus and reference validators: all registered contracts and class floors,
current versions, complete feedback actors, exact approval hashes, and candidate
exclusion from reference graders. Every source hash declared in case `source`
metadata must match a supplied file's actual bytes; explicit source paths must
also match. Composite references check all nested arms. Historical Git baselines
bind exported bytes by their declared hash; their repository-relative path is
part of approved provenance, not the export's filesystem location. Hashing uses
a bounded buffer, including large database snapshots. Source bytes and contract
versions are checked again after rendering.
Historical missing actor declarations still require real provenance; this API
does not infer them or replace historical approval.

Production rubric, relevance and novelty functions render the questions.
Deduplication compares actual messages within each contract, including production
truncation and selection. Novelty mappings retain selected database identities;
ambiguous rendered candidates and unreachable reference targets refuse. Explicit
parameters must agree with every actual call-site argument. Any implicit alias
parameters refuse. `prepare` renders a validated subset for focused probes;
it is deliberately insufficient for complete execution admission.

Each saved novelty verdict is replayed through the actual judge and extractor
stores under current and simulated promoted allowlists. Replay first regenerates
the production question and mapping. Full-schema disposable databases contain
the original candidate population, including deprecated/quarantined rows.
All original procedure columns must remain unchanged. A returned store ID must
identify the one new row with all expected semantic columns, including tools,
context, counters, version and validation audit fields. Unknown new columns
refuse until their semantics are covered. Generated UUID/time are checked for
their production shape, not fixed values or chronology. Existing/upserted IDs cannot masquerade as
new insertion. Scoping and validation decisions are reported separately from
the raw novelty target; a failed mode returns no complete four-mode result.

Extraction and scoping use deterministic synthetic responses, distinguished by
their actual prompts despite a shared call-site ID. These responses isolate
novelty wiring; they do not measure classifier quality. The simulated allowlist,
gate wrappers and fail-open cooldown are restored after each mode. Database
handles close on success, error and cancellation.

Returned evidence binds corpus, policy, approval, current versions, source bytes,
candidate models, explicit parameters and the actual rendered plan. It supplies
no spending consent, provider charge bound, candidate qualification or promotion.
The later runner must recompute complete preflight from current files before
reading credentials and bind it to the fully funded three-repetition manifest.
A cached or caller-supplied prepared dictionary is never execution authority.
