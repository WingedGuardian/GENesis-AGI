# Frozen qualification transport (replacement 4 of 6)

`PinnedRouter` adapts rubric, relevance and novelty callers to the production
`LiteLLMDelegate` without the production Router's fallback or cost-store writes.
It supports one frozen model campaign. The campaign funds every scheduled
maximum before execution; this module does not establish provider billing
bounds or grant spending approval. Complete preflight and the isolated CLI
runner are later replacements. No automatic execution or route promotion is
wired by this change.

`binding.transports[alias]` freezes model, requested `params`, gateway `endpoint`,
common `generation_namespace` and SHA256 `credential_fingerprint`. Every attempt
also repeats the credential fingerprint and binds contract/version/case/repetition/prompt hash, the exact serialized
request hash, `receipt_provider` display name and `provider_identity_evidence`.
The display name needs verified mapping to the pinned endpoint variant; a
routing tag is not automatically a generation receipt's provider name.
Other aliases, duplicate coordinates, implicit routing defaults, changed
requests and unfunded schedules refuse before credential access.

Run in a fresh isolated process with serialized judgments. Global LiteLLM
observers refuse execution. The dedicated key must differ from all visible
production OpenRouter credentials and match the frozen fingerprint. Each new
completion reads its finite nonreset limit, remaining credit and usage; explicit
`limit_reset: null` is required. Remaining credit must cover the outstanding
scheduled maxima. A current key snapshot cannot prove exclusive key use,
in-flight overshoot, billable reasoning/framing or fees. `max_price` filters
rates; it is not a per-request dollar cap. Missing verified bounds or concrete
owner spending approval must still prevent the later runner from executing.

Reservation and dispatch are durable before the delegate runs. Every SDK
replacement client shares the same exact-wire and second-request refusal guard.
The serialized HTTP hook enables documented routing metadata and explicitly
disables OpenRouter response caching. Each qualified answer needs one selected
endpoint matching the frozen model and provider display, direct routing, one
gateway attempt, no BYOK and no material pipeline. Unselected candidates and
unrelated additive metadata fields do not imply retries. The legacy top-level
provider is optional; if present, it cannot contradict the selected identity.
This gateway witness does not prove effective backend parameters or internal
provider retries. See [routing metadata](https://openrouter.ai/docs/guides/features/router-metadata)
and [response caching](https://openrouter.ai/docs/guides/features/response-caching).
The response hook defers stream closure until the complete decoded response is
durably recorded, then releases the original stream. Partial reads retain the
funded dispatch without inventing an answer. It appends before auth/SDK/client cleanup can
fail. Failed delegate results are checked explicitly even when no exception
was raised. Answers and failures are retained independently. Cancellation
keeps its meaning; uncertain journal publication poisons further state reads
and egress until a new writer reconstructs the durable prefix.

Financial JSON first uses the existing duplicate-key/nonfinite validator, then
parses the same immutable bytes with Decimal. Only financial fields become
nonnegative fixed-point strings; no float or delegate estimate determines
settlement. Numeric lexemes remain in the redacted raw body. Redaction decodes
JSON strings in diagnostic messages, exception traces and stack text as well as
response evidence, so escaped credential echoes are removed. Expansion above
4096 characters refuses rather than rounding or allocating exponent-sized
strings. This is an evidence-processing limit, not a claimed provider precision.

`reconcile(journal, attempt, ...)` only performs GET generation lookup. It
rejects error-bearing envelopes, checks frozen model/provider identity and appends the exact receipt to the
journal. Costs must agree and remain within the funded maximum. Invalid or
contradictory retained evidence keeps liability unresolved; it is not replaced
by a later receipt. Missing or contradictory observed generation IDs refuse.
Receipt-only recovery requires explicit independently verified attempt/request
association, never an inferred latest generation. A settled lost answer needs
an explicit answer-loss acknowledgement and is never resent.
Body and response-header generation identities are retained separately; a
contradiction stays unresolved. An ID captured from this response's header may
support expense recovery when the body ID is absent, but cannot qualify an
answer lacking the required completion evidence. Only generation and cache
status headers are retained; credentials and unrelated headers are excluded.
Contradictory observed model/provider identities remain lists of actual contrary
facts in the existing observation fields, which cannot match frozen string
identities. They block settlement, including when appended after an earlier
receipt. Missing identity is not silently replaced by requested routing.

Any unresolved campaign incident pauses both new completions and answer replay.
After GET settlement and explicit acknowledgement of a retained local failure,
an eligible retained answer can replay without reading a credential or sending another
completion. An acknowledged undispatched reservation continues the same attempt
without reserving twice. Neither acknowledgement deletes evidence. Final reports
must reconstruct current accounting, including generation collisions that can
invalidate earlier answers; the later runner owns those reports.
Every live acceptance and retained replay rechecks the raw routing evidence.
Expense settlement and failure acknowledgement cannot qualify a cached,
transformed, retried or otherwise invalid response. Recovery remains explicit
GET-only; rejected answers never trigger another completion.
