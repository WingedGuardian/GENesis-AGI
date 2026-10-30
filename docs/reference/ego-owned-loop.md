# Offline owned-loop state

`scripts/ci/ego_owned_state.py` is the private C1 reducer for an offline runtime
comparison. It uses the existing `Campaign` exclusive writer and append-only
`answers.jsonl` evidence format. It adds no production store, runtime source,
provider or approval authority. The runnable adapter and actual contained native
qualification are separate work; passing state tests does not qualify them.

The immutable manifest binds the synthetic external feed, response program,
executor/model, source identities, trajectory references and pacing. External
events have unique IDs, contiguous sequence numbers and nondecreasing due times.
They never supply C's timers. This profile accepts only `claude-code` and
`genesis_ego_cycle`; source hashes describe the intended state, loop, context,
bridge and case files. They are metadata here, not proof those files ran.

Each complete record carries `schema: ego-owned-journal/v1` and one transition:

| Kind | Meaning |
|---|---|
| manifest | Exact workload/configuration identity |
| acknowledgement, phase=intake | Copied external event; repeat intake is idempotent |
| dispatch, stage=prepared | Fresh attempt, selected logical event, starting state |
| dispatch, stage=bound | Actual trigger UUID, nonce, config/prompt digests and status |
| observation | Outcome, supplied evidence and recomputed next state |

Replay rejects torn tails, changed manifests, invalid schemas/transitions and
next-state mismatches. Canonical JSON comparison distinguishes booleans from
integer sequence/cursor values. Transitions validate on a copy before appending;
failed writes never publish the candidate in-memory state. Campaign retains its
existing crash, synchronization, lock and private-directory semantics.

The external cursor advances only after an accepted observation for the next
FIFO event. Generated timers reserve and complete separate ordinals. Empty feeds
still produce timers; base/factor/cap/hold are explicit manifest values, with
hold fixed to one in C1. No-op/refusal increases backoff, capped at the configured
limit; completed work or accepted reactive engagement resets it. Ready external
input wins a timer tie. Related-memory pointer selection persists in observations.

A fully observed safe provider error retains the logical event and a retry time;
new arrivals remain admitted but cannot bypass it. `blocked` and `uncertain`
outcomes halt automatic scheduling. An unpaired prepared/bound attempt also
prevents replay on reopening. This reducer does not infer child completion or
reconcile effects; its future caller must independently collect and validate
that evidence before supplying an observation.

Host-safe state tests cover timers, bursts, ties/preemption, retry order, restart,
torn journals, writer exclusion, failed append and malformed manifests/transitions.
They use synthetic evidence and never invoke upstream or native executables.
Full ego workflow, production dispatch, multiple egos/generations, Codex execution
and useful model behavior remain outside this state component's qualification.
