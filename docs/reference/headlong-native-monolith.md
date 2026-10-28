# Monolith/native invocation qualification

This developer experiment connects pinned Headlong's actual monolith step to the
[qualified native action port](headlong-native-action-port.md). It is the B1
invocation seam in the offline runtime comparison. All native and upstream code
executes in disposable CI with scripted model responses and synthetic state.

The image keeps stock `/headlong` unchanged and makes a separate `/headlong-b`
copy. Checked patches replace one invocation with an explicitly named native
bridge and guard its prompt-redaction status. The
[reference prerequisite](headlong-reference-redaction.md) adapts the shared
redactor. Original context assembly, work probes, health classification, backoff
and wake code remain unchanged. Before runtime, the runner checks the derived
step against the exact expected replacement. Both source trees, fixtures, patch,
Genesis source, native binary and dependency inventories identify the run.

The bridge accepts model, trajectory, workdir and prompt-file arguments, validating
them against the owned fixture configuration. It does not impersonate shellm's
CLI or silently accept its environment, variable and binary flags. The mapping is:

| Stock responsibility | B1 native mapping |
| Model | Explicit SDK model option; requested and resolved identities recorded |
| Trajectory/run ID | Actual `traj append` header; assigned ID returned to monolith |
| Run-scoped context | Fresh native session and HOME for every wake |
| Workdir | Exact owned identity workdir |
| Identity, memory, goals and skills | Unchanged outer assembly; original generated context retained as reference data |
| Shellm environment/provider/key setup | Preflighted container; loopback backend and dummy key injected by SDK driver |
| Eight legacy binaries | Explicitly unavailable inner tools; only protected `claim_effect` MCP tool exposed |

The native prompt explains that legacy Bash/FINAL examples are reference material.
The SDK's verbatim setting prevents file-mention expansion and slash dispatch.
An owned harmless mention-target fixture checks actual request contents. A large
context case verifies more than128KiB of Unicode-capable file/stdin transport.
These scripted checks establish transport, not useful interpretation by a model.

`MONOLITH_TIERED_MEMORY=0` explicitly disables the recap summary path, which can
invoke `llm`. Tiered-summary integration remains separate. Related memories stay
enabled at3, using the pinned deterministic BM25 prefilter and seeded synthetic
memory. Identity, goal, skill, recent-stream and related-memory markers must reach
the actual native request. Subsequent-wake related-memory rotation is retained.

Only the actual canonical claim can invoke the append-based fixed effect. A true
claim and verified bytes precede an actual attributed observation. Native completion
then records a separate Headlong final. That final begins with the verified fixture
receipt reference: the stock context renderer otherwise drops the latest observation
when its final arrives and filters custom fields. The next wake must actually see
the receipt and full run-details pointer. Verbatim native completion remains in its
own receipt. Final text alone never establishes visible work.

Per-run call receipts let the loopback backend observe real cross-process handler
dispositions before responding. Missing, partial, malformed or failed receipts are
incomplete. Handler exceptions remain recorded out of band because the SDK can
convert them into tool errors followed by successful native text. No error is
normalized into policy refusal. Claims committed before an uncertain effect are
not replayed automatically.

Driver evidence is published through an exclusive pending file and atomic rename
only after the database closes and output streams flush. Open, short-write,
close, rename or output failures cannot leave a published success receipt. A
failed invocation remains incomplete even when its effect/observation/final are
already present, and the same run directory cannot replay its work.

Thirteen cases use fifteen outer wakes and thirteen native sessions. Nine normal
scenarios include effect, no-op, refusal, provider error, handler error, large
context, reactive no-op, two-wake restart/duplicate, and two-wake forced-reference
continuity. Observation-append and final-append fault controls add two sessions.
Timer and reactive reference-conflict controls add two wakes with zero SDK requests.
Both must retain an incomplete driver outcome. After an effect and observation,
the unchanged monolith can reset pacing even if final append fails; independent
checks must still reject completed-work status. Wake delay is checked against file
mtime with disjoint0s/5s intervals, avoiding a broad window that could hide a wrong wake.

The fixture token is not a real dispatcher lease. Actual dispatcher recovery,
native thought emission, generation fencing, full capability/initiative/feedback
parity and real-model behavior remain unqualified. The canonical claim helper is
not the full production dispatcher: higher-level proposal gates, action exclusions,
staleness and execution-brief semantics remain mandatory comparison requirements.
No production runtime, provider, migration, deployment or inference is activated.

The workflow first reruns stock, reference-redaction and native-port controls,
then runs this workload in a fourth container with the same readonly/nonroot/no-network/no-mount resource
boundary. Reports and actual image/container metadata are retained for seven days.
Host tests exercise only owned Python helpers, actual disposable CRUD and passive
evidence. Integrated success requires the exact-head post-review contained run.

The pinned redactor can corrupt UUID references; stock H retains that limitation
as comparison evidence. B registers exact successful controlled writer returns and
IDs from complete, independently qualified previous native runs. The operational
prior run in the continuity case is forced to
`aaaaaaaa-1234-4567-8912-bbbbbbbbbbbb`; the next native request must retain its
actual details pointer. Actual `traj tail --filter run_id=...` must return the
original observation/final records. Favorable random run IDs cannot pass this case.

Both redactor and bridge validate single-use configuration against root, run,
identity, trajectory, actual trigger and fresh parent nonce. Failed or incomplete
prior runs cannot supply continuity provenance. Status65 is reserved for explicit
reference crossing/loss. All failures empty the retained prompt file; a valid
bound bridge writes incomplete evidence before SDK execution and returns2. Timer
conflicts retain level1/5s pacing; reactive conflicts retain the upstream immediate
wake. Compiler/config/runtime errors cannot substitute for the expected conflict.
Provider counters, unchanged approved canonical state, captured empty prompt files,
no effect/final and bound outer errors are all required independently.

A separate disposable correlation control uses actual trajectory writers and
`chat pending`/`chat reply` readers: an original bad-shape inbound ID must join its
pending request, stamp a reply, suppress a duplicate, and resolve the request.
Only synthetic trajectory files are touched; no responder model, delivery process
or transport runs. These checks establish carried references and reader semantics,
not native access to the withheld legacy tools or full conversation agency.
