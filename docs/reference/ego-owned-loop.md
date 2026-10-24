# Offline owned-loop mechanics

`scripts/ci/ego_owned_state.py` is the private C1 reducer for an offline runtime
comparison. It uses the existing `Campaign` exclusive writer and append-only
`answers.jsonl` evidence format. It adds no production store, runtime source,
provider or approval authority. `ego_owned_loop.py` supplies the contained adapter;
actual native qualification requires the contained report, independently of
passing state tests.

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
reconcile effects; its caller must independently collect and validate
that evidence before supplying an observation.

Host-safe state tests cover timers, bursts, ties/preemption, retry order, restart,
torn journals, writer exclusion, failed append and malformed manifests/transitions.
They use synthetic evidence and never invoke upstream or native executables.
Full ego workflow, production dispatch, multiple egos/generations, Codex execution
and useful model behavior remain outside this state component's qualification.

`ego_owned_loop.py` is an explicit finite offline CLI, runnable only inside the
same preflighted image as the native bridge. `--root` selects a fresh owned
manifest under `/work`; `--steps` is an attempt budget, not a declaration of
success. Exhausted response programs stop without waiting for another timer.
It validates the image source pin and five component hashes before opening the
journal. No production daemon, source activation or automatic failover is added.

For each selected input/timer it records prepared intent, obtains the actual
pinned trajectory writer's trigger UUID, publishes a fresh config/nonce, renders
and redacts context, then records the bound digests before launching the existing
native bridge. The context wrapper calls only pinned data helpers: identity,
goals, the fixture skill, current trigger, recent stream and deterministic related
memory prefilter. It never evaluates model text or loads `.env` defaults. Tiered
recap and the legacy model shell tools remain unavailable in this profile.

The child uses its own process group. Completion, stop or error reaps that child
and checks group closure before a cleanup receipt is accepted. Late output/receipt
failure leaves an unpaired attempt; reopening cannot automatically invoke it.
Stop interrupts backoff. This is fixture process ownership, not production
generation fencing or reconciliation after an abruptly killed parent.

Before committing an observation, the adapter independently reads canonical
proposal state and effect bytes, then checks the child receipt, actual run/header/
observation/final correlation, trigger/nonce/config/prompt hashes, typed native
terminal and the closed MCP tool profile. It records requested and resolved model
identities separately. Required captured artifacts must be present; later
continuity verifies each retained relative path, size and SHA256 before carrying
accepted earlier references forward. A provider error is retryable only with
complete typed no-effect evidence; partial/failed handlers remain uncertain.

`ego_owned_controls.py` adds an early four-case smoke: two actual timer wakes,
effect plus a separate-process reopen/refused second claim, reactive input and a
reference conflict with zero SDK requests. The expected population is six
attempts and five native sessions, scored separately from B's established matrix.
The controller retains synthetic manifests, journals, prompts, receipts, trajectory
snapshots, database and effect artifacts in its JSON report before scratch cleanup.
SDK HOME caches are excluded. The workflow retains this report and Docker settings
alongside the stock/reference/native/B foundation reports. Source/binary/container,
physical-effect and cleanup verification of the actual run is still required.

The shared native population in `ego_owned_matrix.py` replays all thirteen
mechanics cases from `ego_runtime_cases.py` using C's own timer/cursor/pacing
semantics. Native invocation counts are derived from actual receipts and checked
against the predeclared applicability. The forced operational UUID, exact history
lookup, large context, safe provider-error retry, partial effects and reference
conflicts retain separate expected outcomes. A typed native final alone cannot
qualify a completed effect. Expected failure cases must also match the declared
collector and protected-call error; unrelated artifact or binding failures cannot
qualify merely by producing uncertainty. C halts uncertain partial effects, which differs
from the monolith's pacing after its visible-work probe.

The same report keeps a distinct pure reducer population:36event/outcome/clock
cells,17burst/delivery cells,6restart/manifest/writer guards and one ladder/cap/
reopen cell. One impossible empty-feed altered-duplicate combination is declared
excluded. Synthetic reducer observations establish no native work or policy
authority. All60cell IDs must match the inventory exactly; counts alone cannot
hide a missing cell. A144mutant calibration checks the scoring instrument.
Typed counter/time checks reject Python boolean/integer aliases, and retries
must retain the bound logical event and wake. Actual synthetic journals are
retained for detached replay inspection.

The new container/report are separate from the early smoke and existing
foundation arms. Qualification requires actual post-review contained results,
source/binary/container identities and independent artifact/state/effect readback.
Real-process interruption, timing/recovery cells and the final B/C comparison
follow separately. Neither native mechanics nor synthetic state scoring establish
full workflow, useful model behavior, production or Codex parity.
