# Advisory inbox attempt identity

Inbox execution can expose available CLI identity through the existing trace
path. This is diagnostic metadata, not an acquisition or completion gate.

`CCError.cc_session_id` is optional. Classification populates it from a supported
structured CLI result, preserving an explicit result argument's precedence over
raw stdout. Prose, blank/nonstring values and unsupported envelopes remain
unknown. Existing exception constructors, args, types, raw evidence and causes
retain their semantics. The replay wrapper and the classified-result stream-drop
wrapper preserve a known same-attempt identity. Other early or manually created
errors may have no value; existing `raw_event` evidence remains independently
available. No arbitrary cause-chain, manager-ID or filename inference is made.

The buffered and streaming invoker spans add these attributes for ordinary
execution:

| Attribute | Meaning |
|---|---|
| `cc_session_id` | Optional reported CLI identity |
| `cc_identity_source` | `cc_output`, `classified_result`, or `unknown` |
| `cc_outcome` | Whether this wrapper `returned` an output or was `not_returned` |

The span's existing `session_id` column keeps its Genesis ContextVar meaning.
Private execution adds none of these new attributes. Attaching these identity
attributes cannot replace the original result/error; an absent attribute is
unqualified evidence.
An output ID observed before wrapper postprocessing fails can remain available,
with outcome `not_returned`.

The inbox wraps only its retry await in an `inbox.evaluate` operation span. It
records `inbox_batch_id`, `inbox_item_id` and `evaluation_outcome` (`returned` or
`not_returned`). Observed invoker attempts have separate child span IDs, even
when they report the same CLI ID. There is no fabricated ordinal or pooled
source evidence. The batch span nests under any existing trace context and uses
`start_span(include_exception_text=False)` to
record a categorical failure without formatting exception prose. Other span
callers keep the existing default formatter.

`returned` means that the wrapper/await produced an output. It does not mean a
response was delivered, the item was baselined, a source body was acquired, the
CLI exited cleanly, or a conclusion was correct. Later inbox handling and
existing output flags still decide their original outcomes. No retry, replay,
approval, baseline, prompt, extraction or model policy changes.

The existing trace reader and trace API expose attributes and parent links.
Tracing remains optional and best effort, using the existing writer, table and
retention. Disabled/unwired capture, pending/failed flush, process interruption,
expired retention and missing parent/child spans leave incomplete observations.
No rows never means zero attempts or complete URL coverage. Hook session columns
can use another namespace and do not replace the explicitly named CLI attribute.

Real CLI-to-file linkage, exit/flush timing, collector lag, native peers, exact
tool-result attribution and semantic support need separate qualification. A CLI
ID alone does not establish any of them. No collector is enabled and no new
store, schema, observer framework, runtime gate or endpoint is introduced.

Verification uses isolated scan/approval fixtures, actual invoker/retry and
trace persistence/readers, with simulated CLI transport. It covers success,
safe retries, errors, replay holds, cancellation, disabled capture and concurrent
batch namespaces. These are controlled functionality checks, not production
reliability rates or live-provider/flush qualification.
