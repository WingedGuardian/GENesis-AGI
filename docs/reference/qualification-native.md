# Native DeepSeek qualification

The explicit `deepseek_native_v1` profile qualifies the shipped `deepseek-flash`
alias through the existing delegate and bounded version-3 study controller.
Only `study-run` can acquire native answers, after complete admission and a
separate exact-plan spending decision. Legacy `run` rejects this profile.
Offline admission and retained reporting read no key and submit no completions.
This adds no production routing selection, promotion or deployment.

## Identity and request

The binding and every scheduled attempt require
`transport_kind: deepseek_native_v1`, `cost_basis: native_tariff_v1`,
`credential_mode: shared_existing`, the exact credential fingerprint and the
same full native tariff. The binding endpoint is `https://api.deepseek.com`;
scheduled completions use `/chat/completions` and generation namespace
`deepseek`. Missing markers retain the legacy OpenRouter path; a native alias
without its marker refuses. Unknown or contradictory identities refuse.

The request explicitly disables thinking and fixes temperature0, top_p1 and
output caps1000 for judge/novelty or150 for J9 relevance. There is no seed or
OpenRouter provider/usage control in the native body. The adapter passes the
root API base explicitly because the installed SDK otherwise defaults to beta.
The final HTTP hook checks the exact body, URL and authorization, refuses a
second POST, disables redirects and keeps zero client retries. Nonzero
`chain_offset` and undeclared caller controls refuse locally.

Raw response model, generation ID, usage and optional `system_fingerprint`
are retained. Origin attribution comes from the verified first-party HTTPS
endpoint; it is not an invented provider-reported field. The raw model must
be `deepseek-flash`. Fingerprints are reported as observations, including
changes, rather than an immutable weights claim or an automatic first-value
pin. Freeze dated public model/release evidence in the plan; a provider alias
and fingerprint cannot prove an advertised fixed revision.

Reported thinking, extra features or output beyond the frozen cap stop the
attempt and retain its reservation. An explicit refusal makes the answer
ineligible while valid native token usage can still support calculated expense.

## Calculated expense

DeepSeek native usage supplies prompt, completion, total, prompt-cache-hit and
prompt-cache-miss counts. All five must be strict nonnegative integers, with
prompt = hit + miss and total = prompt + completion. The calculation reparses
the retained raw body and checks the normalized usage and identity against it.
Contradictory identities, incomplete usage or unsupported tool expenses keep
the reservation held.

`native_tariff_v1` freezes USD prices per million tokens for both published
tiers, with pricing URL, source hash and observation date. The journal retains
an exact lower/upper calculation bound to the observation and tariff hashes.
It accounts the upper against the request reservation without rounding the
measured calculation. Native amounts appear separately from
`observed_receipt_cost`; invoice cost remains unobserved. An undocumented raw
server cost field remains diagnostic and cannot replace the declared authority.
See [journal evidence](qualification-journal.md).

These are calculations under frozen token tariffs, not authenticated provider
charges or an invoice ceiling. Initial request allocations still need independent
arithmetic over rendered inputs, tokenizer evidence, output caps and explicit
framing/margin assumptions. A valid calculation can survive an unusable answer;
answer eligibility, failures and controller closure remain separate gates.
There is no native generation-receipt or balance GET. Missing response usage
cannot be recovered by inventing zero expense.

## Approval, interruption and credentials

Use `GENESIS_QUALIFICATION_DEEPSEEK_KEY` only for the separately approved existing
key. Obtain narrow local identification/fingerprint permission before reading it;
then bind the exact future plan and obtain its paid-use decision. Approval of
this offline profile supplies neither key-read authority nor spending consent.
The adapter checks global callback/payload-print isolation before reading the
key, lends it through the fixed native resolver aliases and restores those
aliases afterwards. Explicit redaction covers setup, SDK threads, observation,
cleanup, cancellation and calculation finalization.

Native studies require zero replacements. A429, timeout, cancellation, malformed
grade or unresolved evidence stops acquisition; the OpenRouter upstream-pool
exception is not a native retry rule. Keep every original attempt and reservation.
An existing root never authorizes relaunch. See
[continuation](qualification-continuation.md) and
[supervision](qualification-supervision.md).

For a fresh route, retain older studies in the existing plan-bound
`session_financial_envelope`, independently verify their complete journals and
owner closures, and withhold their liabilities from the new allowance. Do not
feed cross-provider expense-only histories through answer reuse or relax their
original receipt checks. Report the fresh native study and old incomplete
histories separately, with the combined session liability disclosed.

Mock HTTP and deterministic storage fixtures prove integration only. Real
model quality, qualification, promotion and activation require their own evidence
and decisions.
