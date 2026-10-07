# Qualification journal (replacement 1 of 6)

This package supplies the private evidence and accounting boundary for the
qualification rebuild. It performs no network calls or production writes.
Transport, full manifest preparation and the qualification CLI land later.

`Campaign` owns `answers.jsonl` and `campaign.lock` under an operator-selected
private directory. `Campaign.read` reads existing evidence with a shared lock;
it never creates, truncates or repairs a campaign. Complete records survive an
incomplete final write; the torn bytes remain as evidence and prohibit writing.
Legacy dispatch/answer/failure journals remain readable, but cannot execute
through `Journal`. Version-1 manifests remain historical evidence only; there
is no automatic conversion into an executable version-2 campaign.

`Journal` writes and fsyncs its manifest before yielding the writer. Manifest
version 2 freezes the budget as a decimal string, a nonempty `binding` object,
and scheduled attempts. Each attempt carries its unique ID, alias, model,
upstream, endpoint, request hash, `generation_namespace` and verified
`max_charge` decimal string. The exact sum of every scheduled maximum must fit
the budget before the manifest is published. This funds the full schedule:
settlement does not create reusable headroom for additional attempts, and late
conflicting evidence can restore a reservation without exceeding that ceiling.
The later preparation layer populates the complete source/implementation,
contract, reference, approval, rendered-request, parameter and pricing bindings.
The caller must verify maxima against provider evidence; a declared maximum
alone establishes no provider billing bound.

`reserve` also checks current settled charges plus unresolved reservations plus
the next reservation against the frozen budget. `dispatch` durably records one
attempt before its caller may send a completion. `observe` retains raw answer
evidence, and `settle` retains a billing receipt. Invalid or conflicting receipts,
identity contradictions and missing billing keep execution stopped. There is no
stored aggregate counter or retry ledger; state is reconstructed incrementally
from events, including receipt generation ownership. In-memory contributions
are derived caches and are never persisted separately. Appends stage only the
changed attempt and any generation-collision owners; they do not copy the full
campaign. `attempt(id)` provides a detached per-attempt view. The explicit
`state` snapshot copies the campaign and is intended for inspection/reporting.

Generation ownership is keyed by the frozen issuing authority and generation
ID, not alias, model or reported upstream. OpenRouter response generation IDs
use a common OpenRouter namespace across upstreams; its upstream response ID is
separate provenance. Equal IDs issued by independent providers do not collide.
An observation cannot choose a different namespace to evade ownership checks.

Ordinary reopening never resends a dispatched attempt. After verified settlement,
an explicit `acknowledge` can identify a failed operation or lost answer and its
resolution, preserving all original events. The lost answer remains unavailable.
A verified receipt without an observation must explicitly match the frozen
attempt ID and request hash as well as model, upstream and generation identity.
It settles accounting while leaving an answer-loss incident for acknowledgement;
it supplies no answer and never authorizes resending that attempt.
A reservation that never reached durable dispatch can be acknowledged from that
verified no-dispatch evidence and used once; its full liability stays reserved.
Unknown charge or identity cannot be acknowledged away. Acknowledgements are
operator attestations, not authenticated external billing verification.

Directory entries, records and the private files are synced before writer
admission. Short writes, failed fsync and failures publishing in-memory state
after fsync poison that writer. Its derived-state readers refuse service until
a fresh reader reconstructs the disk evidence; a durable event is never resent.
Files must be owned,
private regular files without symlinks or extra hard links. Competing writers are
excluded. Intentional edits or deletion of an active lock by the operator are
outside this accident-prevention boundary; keep its parent under operator control.

The separate private file plane is needed because raw provider evidence and
unknown liability must survive process exit and offline reconciliation without
using the production database or credentials. Retention is operator-controlled:
there is no automatic deletion. Back up campaigns privately before removing
them; never publish real manifests, references or responses. Only synthetic
fixtures belong in the repository.
