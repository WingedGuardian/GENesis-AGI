# Qualification journal (replacement 1 of 6)

This package supplies the private evidence and accounting boundary for the
qualification rebuild. It performs no network calls or production writes.
Transport, full manifest preparation and the qualification CLI land later.

`Campaign` owns `answers.jsonl` and `campaign.lock` under an operator-selected
private directory. `Campaign.read` reads existing evidence with a shared lock;
it never creates, truncates or repairs a campaign. Complete records survive an
incomplete final write; the torn bytes remain as evidence and prohibit writing.
Legacy dispatch/answer/failure journals remain readable, but cannot execute
through `Journal` without the new manifest.

`Journal` writes and fsyncs its manifest before yielding the writer. Manifest
version 1 freezes the budget as a decimal string, a nonempty `binding` object,
and scheduled attempts. Each attempt carries its unique ID, alias, model,
upstream, endpoint, request hash and verified `max_charge` decimal string.
The later preparation layer populates the complete source/implementation,
contract, reference, approval, rendered-request, parameter and pricing bindings.
The caller must verify maxima against provider evidence; a declared maximum
alone establishes no provider billing bound.

`reserve` enforces settled charges plus unresolved reservations plus the next
reservation at or below the frozen budget. `dispatch` durably records one
attempt before its caller may send a completion. `observe` retains raw answer
evidence, and `settle` retains a billing receipt. Invalid or conflicting receipts,
identity contradictions and missing billing keep execution stopped. There is no
stored aggregate counter or retry ledger; state is reconstructed from events.

Ordinary reopening never resends a dispatched attempt. After verified settlement,
an explicit `acknowledge` can identify a failed operation or lost answer and its
resolution, preserving all original events. The lost answer remains unavailable.
A reservation that never reached durable dispatch can be acknowledged from that
verified no-dispatch evidence and used once; its full liability stays reserved.
Unknown charge or identity cannot be acknowledged away. Acknowledgements are
operator attestations, not authenticated external billing verification.

Directory entries, records and the private files are synced before writer
admission. Short writes and failed fsync poison that writer. Files must be owned,
private regular files without symlinks or extra hard links. Competing writers are
excluded. Intentional edits or deletion of an active lock by the operator are
outside this accident-prevention boundary; keep its parent under operator control.

The separate private file plane is needed because raw provider evidence and
unknown liability must survive process exit and offline reconciliation without
using the production database or credentials. Retention is operator-controlled:
there is no automatic deletion. Back up campaigns privately before removing
them; never publish real manifests, references or responses. Only synthetic
fixtures belong in the repository.
