# Native Codex hook harness

## Contained compatibility qualification

The `Native Codex compatibility qualification` workflow builds the exact PR head
with its recorded base and the reference0.166.0 Linux x64 package. The package is
SHA-512 checked before extraction; the executable and bundled code-mode host
retain their package layout and have separate SHA-256 records. No installed
native binary on the developer's host is required or launched by this workflow.

Execution is inside a read-only, unprivileged container with no network, host
bind mounts, credentials or capabilities. A unique owned Docker volume retains
test evidence after exit; it is copied out before the container and volume are
removed, then transported as `codex-evidence.tar.gz` to preserve hidden files and
symbolic-link shape. Build logs remain available even when image collection
fails. Model responses come from loopback fixtures, not a paid provider. The
fixed CI resource limits and25-minute job ceiling bound this deterministic
Linux qualification; they are not production agent timeouts or install defaults.

The suite reuses both native hook clients and the existing adapter controls,
then adds app-server command approval accept/decline, interruption, late replies,
disconnect, steering with a turn-ID precondition, persistent resume, required MCP
failure, dispatched-marker forwarding and ready-descendant cleanup. Fixture
approvals are synthetic. A real secrets guard runs only against a scratch file
containing synthetic text: its raw `ask` output and the existing adapter's deny
interpretation are measured separately. This does not install a full Genesis
Codex policy or establish an in-session canonical owner approval path.
The six approval fixtures use `read-only` with `on-request` and explicit
escalation, and verify the policy returned by `thread/start`. Full-access
commands in the initial fixture ran immediately without approval, so that
configuration cannot establish approval handling. Early approval requests are
retained while awaiting the turn response; unexpected requests still fail by
default in the shared driver.
Resume uses an append-only action witness, checks its exact bytes before resume,
and checks bytes and provider-request count after shutdown.

The image installs test tools under `.github/test-tools/requirements.txt`, the
same constraints CI uses; changes to those pins trigger fresh qualification.
Changes to the main CI workflow, this reference or the owned-process cleanup helper
also trigger qualification. Ordinary CI skips
the fifteen contained-only compatibility cases; its skip ceiling includes those
measured identities. The contained workflow requires every native case to run.
It first runs the pure driver tests to exercise shared fixture setup and its
Python dependencies without starting a native executable.
Before execution, collection freezes the case census, source inventory (including
the CI configuration and test-tool pins) and
binary hashes into the image. Every executed case must have passed setup, call
and teardown phases and a unique raw receipt. Independent readback after copying
the artifacts verifies the head/base, source snapshot, manifest binding, full
census, phase outcomes, artifact hashes and new sink witnesses. Missing,
skipped, partial, failed or altered evidence refuses a completion verdict.
This qualification requires both clients to select the same package executable
named `codex`; different or renamed overrides are refused. Inherited receipts
must match their declared action/decision or adapter case/instrument and actual
sink bytes. Direct MCP calls unsupported by the native tool metadata remain
transport controls with no sink. Safe canonical guard cases require silence.
An expected incompatibility can be a completed measurement: native command
approval is distinct from canonical guard approval, and `production_ready`
always remains false. Read the copied `readback.json`, raw receipts and workflow
conclusion together; fixture results do not establish native subscription login,
live quality, production operation, arbitrary descendant containment or support
for a different native version. Native outer-hook fail-open behavior remains the
documented limitation described below.

The local driver, collection-binding and readback unit files run without native
execution. Native runs use the contained workflow; the older opt-in invocation
below describes the inherited harness mechanics, not authorization to run it on
a live install.

## Separate MCP elicitation experiment

`codex-elicitation.yml` adds a separate17-case, app-server-only experiment on
the same pinned package and containment. It extends the parent image rather than
installing another native runtime. Its closed census, capture plugin, manifest
and reader are independent of the inherited111-case qualification; neither
reader accepts the other's population. The shared deterministic provider gains
an optional request-index action plan. Append and index selection share one lock,
so concurrent requests receive the action for their own recorded index; existing one-action callers keep their
original behavior and retain the parent qualification workflow.
Ordinary CI discovers and skips the17 opt-in cases; any skip-ceiling adjustment
must use the actual CI JUnit measurement rather than a developer-host count.
Run42566419285 measured615skips including2xfails: exactly the parent population
plus these17native ids, with no removed skips or changed reasons. Its65,730
passing tests had no failures/errors; the old598ceiling alone failed the job.
The615ceiling change still requires fresh CI against the final head and main.

The zero-argument fake `record` tool can append only to its owned scratch sink.
It sends a required boolean form through the typed MCP SDK `send_request` API,
with Codex's proper approval metadata and no persistent-grant option. Only an
explicit accept with exactly `confirmed: true` can append an approval row.
The synthetic frontend binds the observed native request id, thread and turn,
consumes it once, and retires pending requests on interruption or shutdown.
The nonce identifies the invocation and displayed action; it is not an echoed
secret and cannot detect an old boolean deliberately re-enveloped under a new
valid request id. A nullable native `turnId` is recorded but refuses this
during-turn experiment, rather than inventing a turn association.

The census covers true/false/decline/cancel/malformed responses, foreign and
duplicate ids, interruption, EOF, two fresh requests, a stale reply after an
interrupted turn, proper approval under Never, and fixture-local unattended
refusal. Two separate data-form controls exercise the `Context.elicit(bool)`
route under Never and empty-schema automatic acceptance. Data entry is not
approval authority. The unattended control is the fixture's pre-request check
of the explicitly forwarded `GENESIS_CC_SESSION=1`; it does not establish that
native Codex understands Genesis roles. Modern protocol and missing-capability
refusal are local SDK/fake-session controls, outside the native17-case census.

The isolated frontend advertises `openai/standard-form-input` at initialization
and starts an ephemeral thread with `threadSource: "user"`. Codex0.166 enables
the nonempty data-form exception only for a root user thread advertising that
extension; approval metadata still excludes a request from that exception.
See the pinned [thread manager](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/thread_manager.rs#L2632)
and [policy routing](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/codex-mcp/src/elicitation.rs#L590).
These fixture declarations do not change a running user's frontend or policy.
The first native experiment (run42566237032) passed15cases and failed2:
its data control lacked those declarations, and the missing-content reader
expected omission where native returned an empty object. The corrected reader
requires the observed `accept` plus `{}` response, refusal and zero writes for
that negative case. This failed run remains diagnostic evidence; a fresh complete
native run is required to qualify the revised source.

Foreign-ID and stale-after-interruption controls inject the old/foreign acceptance,
then send a correctly bound decline and require natural completion plus the
server's refusal before cleanup. The stale control interrupts only its first
turn; its second turn must remain usable. These are bounded observable routing
controls, not proof of every possible internal interleaving.
Every server invocation must finish before fallback cleanup. The duplicate-reply
control must observe the first physical append and its server record before replay.
The raw stream must place each ask before its matching completion; an EOF turn
has at most one optional interrupted completion. The reader thread records a
separate deep copy and monotonic receipt time before queueing each completion.
Readback requires exact correspondence with raw completions. An interrupted
receipt must follow its matching interrupt input and precede the post-interrupt
observation. Normal receipts follow their own frontend reply (the no-ask controls
use their server entry) and precede cleanup, the next ask, or the duplicate-reply
baseline as applicable. Optional EOF receipts follow EOF input and may arrive
during the stdout drain, before the after-close observation. These measure when
the Python reader observed a completion, not backend emission time or universal
causality across buffered processes.

An interruption must start from a matching pending invocation and retain a
nonempty terminal server sequence after its exact input and before the
post-interrupt observation. EOF additionally rechecks the root and active writer
live immediately before closing stdin, retains their ancestry from that same
census, observes natural root exit0, and freezes
retirement of the exact known child population before fallback cleanup. An empty
EOF server tail is allowed only with that process proof; any retained terminal
must follow EOF input. Final cleanup observations cannot replace natural EOF
retirement. Fresh ancestry must connect each writer to the live root through
known PID/start identities and consistent parent edges; valid reparenting within
that known tree is allowed. Scope is the already-observed owned descendants,
not a complete monitor of escaped processes or authentication against someone
deliberately rewriting mutually consistent artifacts and the verifier.

Readback rejects malformed and RPC-error frames across the complete retained
stream, including shutdown drains. Positive server invocations require the exact request/response/append sequence,
so an extra early cancellation cannot hide alongside a later successful effect.
Synthetic golden replies use an independent
literal scenario map and separate deep copies for frontend and server content;
removing either comparison must fail its own damage control. Earlier passing
runs using weaker lifecycle evidence do not qualify this strengthened contract.

The frozen manifest binds source and both binary hashes, head/base, workflow
run and attempt. Readback requires complete passed phases, raw request/response
provenance, negotiated legacy2025-06-18 capability, actual native policy,
pre-response and post-retirement sink bytes, write ordering and owned-child
retirement. Modified, missing, duplicate, stale or partial witnesses refuse
qualification. Source observations or passing local fake-session tests are not
native compatibility measurements. Read the actual workflow and downloaded raw
artifact readback before claiming that the experiment passed.

Even a complete result reports `production_ready: false` and human frontend
behavior as unmeasured. This builds no real review dispatcher, approval store,
live MCP profile, host agent execution or provider inference. An owner-operated
Codex frontend with a supported approval interface is a separate final check;
a Claude/Genesis terminal cannot certify that UI. Production action binding and
fresh budget rechecks remain separate work.

## Inherited opt-in harness

Run the opt-in integration file on a Unix host with Codex and the Genesis Python dependencies installed:

```bash
GENESIS_CODEX_NATIVE_TESTS=1 GENESIS_PYTEST_LOCK_WAIT=1 \
  python -m pytest tests/test_hooks/test_codex_native.py -q \
  -o tmp_path_retention_policy=all \
  --basetemp "$HOME/tmp/codex-native-harness-$$"
```

`GENESIS_CODEX_CLI_BIN` and `GENESIS_CODEX_APP_SERVER_BIN` can select different absolute binary paths. Both default to the installed `codex`. An opted-in run fails when a required binary is missing; ordinary test runs skip this file.

Each case creates an isolated home and workspace, starts a localhost Responses fixture provider, and requests one shell, patch, or fake MCP action. No production Genesis server, database, MCP configuration, credentials, or paid model is used. The fixture hook records its real native input and permits or denies the action. Tests check the actual file sink, including positive controls for each denial case.

The 20 cases cross both clients with both decisions and five call paths: direct shell, direct patch, nested code-mode shell, nested code-mode patch, and nested code-mode MCP. The model name selects native tool behavior; all responses come from the local fixture provider.

Results apply to the recorded binaries and `gpt-6.1-sol` tool metadata. Analytics and plugins are disabled in the isolated config; the tests also reject evidence of a plugin-clone attempt. This is configuration-based suppression, not an OS network sandbox.

Activate the Genesis Python environment first. Both the unique `--basetemp` and retention override are required to retain passing and failing evidence: the repository otherwise removes successful cases and its automatic per-process temporary directory. Pytest clears an explicit base directory at startup, so use a new path for each run.

Hook trust is scoped to the vetted fixture: `hooks/list` supplies its exact key and current hash, which are persisted only in the isolated configuration. No hook-trust bypass flag is used. This confirms trust of the hook definition, not integrity of the referenced script bytes: a separate isolated Codex 0.161.0 probe kept the same current hash and trusted status after adding a harmless script comment. Production readiness must verify guard contents separately. `native-evidence.json` in each pytest temporary directory records binary versions, hook discovery, fixture requests, native events, and stderr. The fake MCP is marked required so startup must succeed before the initial tool catalog is built.

The app-server path drives initialization, an ephemeral thread, and a turn over stdio. It exercises the native backend; it does not certify desktop folder selection, the desktop trust UI, or production validator policy. Those require separate readiness checks. This baseline file does not cover hook crashes, timeouts, later stdin input, or arbitrary indirect execution.

The separate opt-in `tests/test_hooks/test_codex_validator_adapter_native.py`
provides the qualification harness for the inactive interactive adapter on
Codex 0.166.0. It copies the guard
source and records its hashes, reuses the installed Python interpreter without
changing it, and exercises actual scratch receipts on both clients. It adds
child ask, crash, missing-script, malformed-output and timeout cases, advisory
and successful decision-reason delivery, relative patches and code-mode MCP
admission. Raw direct MCP function
calls rejected as unsupported before any hook are transport controls, not
adapter denial evidence. The outer-timeout pair uses the same delayed denial:
the short deadline must let its harmless receipt execute, while the normal
deadline must refuse it. It checks hook startup and the effective trusted timeout;
CLI JSON does not expose the timeout message. This documents the accepted native
fail-open. These are
child failures caught by the adapter, not evidence that a failed outer native
hook is safe. Keep the unique base directory and retention options above when
running this file too. No production validator profile is activated.

This file refuses a binary reporting a different version. If the default Codex
has moved on, use both binary overrides above to select the reference 0.166.0
runtime; do not relax the assertion to turn version drift into a passing run.
Earlier 0.163.0 and 0.165.0 receipts remain historical evidence; a version change requires
fresh qualification. The launcher-discovery, role, constructor and shared-quote
tests use this same reference. Both selected client versions are checked; the
shared-quote test also binds both binary SHA-256 values explicitly. This is a
reproducibility requirement for qualification, not the launcher's admission
policy: recognizable non-reference versions warn and face the same startup
inspection. Those inspections do not prove actual tool denial on future clients.

On 0.166.0, an owned copy of the reference executable also needs the bundled
`codex-code-mode-host` beside it for `functions.exec` dispatch. Preserve that
layout from the same verified package and record both hashes. A lone executable
can pass version and discovery checks while failing to start its tool host.

The inherited `tests/test_mcp/test_validator_writers_native.py` also binds this
reference version and digest. Its fourteen cells exercise real tool dispatch
against owned SQLite/vector fixtures, including external-profile exclusion,
preview-only close and explicitly enabled fixture recording. No live store runs.

`tests/test_scripts/test_codex_validator_constructor_native.py` adds composed
adapter checks using actual Git in owned directories. Separate-value template
and depth options have denial, explicit external-destination and no-op controls
on both clients. These are exec/app-server fixtures, not terminal trials.

The direct-shell positive case reads a real scratch body file containing prose
that mentions worktree removal. It requires that content to appear in the next
provider request and checks the separate action receipt. The read runs as
background work after the foreground cd/exit newline opener; a separate pwd
receipt must still name the anchor. The legacy AND-list opener has refusal and
no-op controls, with the no-op recording its different foreground directory.
This qualifies actual file-to-tool-output flow and starting-directory behavior
on each pinned client. The unit adapter tests separately
pin the known compound-heredoc refusal, the file-operand alternative and refusal
of a real removal after a data body; their supplied action commands never execute.

## Existing guard compatibility replay

Load `tests.test_hooks.codex_adapter_replay` explicitly as a pytest plugin to run
existing compatible Bash cases through the adapter and copied `genesis-hook`.
For ordinary cases, the original assertions and adapter decision/context checks
use the same child execution, preserving advisory deduplication. Explicit named
mappings retain direct controls for the missing-cwd fallback, the single-step
discard note, compound-heredoc and GraphQL-file limitations. The guard-only
no-spawn invariant runs directly before its separately checked adapter action;
that action must preserve the baseline admission and API-refusal marker.
Lexical-cwd fixtures are
materialized only under the owned test directory, and the self-cwd test derives
its target after entering that directory. The copied public topology fallback
and imported source dependencies are hashed alongside the actual guard copies.
Copied guards execute at their original owned fixture paths through the copied
launcher. Their manifests retain missing/poisoned siblings, script-parent
dependencies and explicit public-source imports; no intact overlay repairs a
fixture. Normal runtime files come from the tracked scripts/hook inventory with
executable modes preserved. The native harness's source copier is unchanged.

The frozen remote-policy test module uses a paired execution: its original
assertions inspect an unanchored baseline from the copied launcher, taken before
any injected fault. The anchored adapter must preserve admission. For force
pushes, a separate source parser census must agree with the baseline's force
reason; the adapter must produce a genuine force refusal, including the shared
guard's whole-command refusal. Other calls must also preserve raw permission
and explanations. Both no-op and generic-denial child controls must catch every
force case. This pairing is limited to that module's empty local remotes; it
does not apply to guards with advisory deduplication or publishing state.

Six source-bound publishing-allowance tests in `test_push_create_override.py`
are an owner-approved direct-only exception: bare/explicit/safe-flag re-push,
record-on-hit, recorded-offline, and record-location assertions. Their original
fixtures and assertions still run, with zero adapter-compatibility credit.
The module and guard source digests are pinned; drift requires requalification.
The foreground opener adds an `exit` neighbour, which the unchanged guard does
not treat as inert, so these ask-enabled fixtures emit an ask instead of the
old publishing allowance. The guard can record its local push cache before
emitting that ask. A same-cache baseline-first replay would change the adapted
arm's initial state, so this family is never added to the generic paired mapping.

Run `tests/test_hooks/test_codex_adapter_republish.py` separately, without the
legacy replay plugin. Its dedicated real-child proof prepares independent
direct/adapted cache roots before either arm runs, crossing fresh-online and
recorded-offline state with bare, explicit and safe-flag push commands. It
requires direct allow, raw adapted ask with the extra-step cause, adapter
refusal preserving that cause, and the expected cache record at each assigned
location. Git config, refs and remote availability remain unchanged. Real empty
and generic-deny child controls must fail the expected ask/cause relation;
they are instrument controls, not unchanged-source qualification. This composed
proof adds no native-client case and does not override ask suppression. The
policy-independent validator push prohibition is enforced by the role guard.

`tests/test_hooks/test_codex_validator_role_native.py` adds role-policy
qualification on the same pinned version. Its source defines 96 opt-in cases:
72 direct role checks across twelve operation families and twenty-four full-adapter
code-mode GitHub, heredoc, hash/escape and assignment-only redirection checks. Each family has an allow, a refusal and a matched no-op
control on both clients. Direct role fixtures supply the independently fixed
scratch anchor as the child cwd; the full-adapter arm uses the actual native
input and adapter translation. Commands use absolute harmless fake executables
and owned redirection/database fixtures, including in every no-op arm. No live
service, SQLite write or public post is attempted. Stable root/file aliases are
owned fixtures; source hashes are captured before those fixture additions.
Interpret these separately
from the original 76 adapter controls, and retain both source hashes and actual
action receipts. Declared cases are not a claim that a particular run passed.

The plugin requires fresh, separate absolute base, receipt and sidecar paths
under the original `HOME/tmp`, outside the checkout. Before collection it gives
the pytest process a fresh shallow owned HOME under `/tmp`, preserving existing
path-depth assertions. Bulk fixtures and the Python temporary-directory cache
stay in the disk-based base/sidecar. All replay state must resolve under those
three owned roots. HOME and the cache restore at unconfigure, including partial
setup failure; rejected newly allocated homes are removed. Parent TMPDIR stays
unchanged. A replay child's inherited ambient temp root is narrowed to owned
temp; explicit fixture-specific state paths must already be owned. Per-case
imported push-guard deadlines reset before fixtures model separate invocations;
explicit fixture/body deadlines still take precedence.
An omitted operation cwd uses the actual process cwd when it is already owned,
otherwise the neutral owned operation directory. Outside the named
repository-independent fixture mappings, explicit non-owned cwd values remain
ineligible; this fallback never replaces them.
The command-only refusal-precedence class uses that owned operation directory
before its helper derives payload/process cwd and the git ceiling. Explicit
Claude dispatched-session push and credential cases retain direct assertions:
Codex must scrub their session stamp, so those cases are not compatible replays.

Run the qualification child with umask `022`. The existing FIFO helper creates
its directory with default permissions and expects to reach the regular-file
check; inherited umask `002` creates a group-writable directory and correctly
triggers the earlier directory-safety refusal instead. This process-local test
premise does not change the host or the guard's accepted permissions.

For example, after activating the Genesis Python environment, this bounded
starter set runs the main guard modules. Complete qualification also inventories
and accounts for compatible cases in other guard consumers, including inline
hooks and `tests/test_scripts/test_hook_input_contract.py`:

```bash
(
umask 022
GENESIS_PYTEST_LOCK_WAIT=1 python -m genesis.hostmetrics run \
  --name codex-guard-replay --ram 1 --cpu 100 -- python -m pytest -q \
  -p tests.test_hooks.codex_adapter_replay \
  --codex-replay-receipt "$HOME/tmp/codex-guard-replay-$$.json" \
  tests/test_hooks/test_capped_read_advisory.py \
  tests/test_hooks/test_credential_surface_hook.py \
  tests/test_hooks/test_destructive_command_guard.py \
  tests/test_hooks/test_full_suite_guard.py \
  tests/test_hooks/test_git_discard_guard.py \
  tests/test_hooks/test_git_push_guard_repo.py \
  tests/test_hooks/test_main_checkout_guard.py \
  tests/test_hooks/test_pipe_status_guard.py \
  tests/test_hooks/test_pr_close_advisory.py \
  tests/test_hooks/test_protected_paths_guard.py \
  tests/test_hooks/test_push_guard_sqlite_write.py \
  tests/test_hooks/test_push_publish_ask_switch.py \
  tests/test_hooks/test_repo_routing_guard.py \
  tests/test_hooks/test_rm_trash_advisory.py \
  tests/test_hooks/test_secrets_env_access_guard.py \
  tests/test_hooks/test_tmux_kill_server_guard.py \
  tests/test_hooks/test_worktree_guard.py \
  -o tmp_path_retention_policy=all \
  --basetemp "$HOME/tmp/codex-guard-replay-$$"
)
```

The private receipt lists every test's source location, direct result, replay
count and `FULL`/`PARTIAL`/`NO` invocation coverage. Failed, skipped and unfinished
test lifecycles have zero qualified replays. Changed instrument, copied/public
source or HEAD invalidates qualification. Intentionally faulted children are
explicit instrument controls with zero replay/qualification credit, including
digest-only fault metadata; mixed calls retain credit only for real-source rows.
Every unreplayed invocation has a reason; tests without a subprocess
payload retain their direct evidence and referenced source symbols. Internal
helper/mocked-main tests, the six source-bound publishing allowances,
Claude-only lifecycle/read/file event shapes, malformed
or empty non-native payloads and the explicitly excluded main-checkout Bash half
are not relabelled as adapter replays. Review the complete exception list rather
than adding direct-suite passes to the replay count. Receipts contain local
fixture paths and belong outside the repository. This is subprocess compatibility
qualification, separate from the pinned native-client harness above.
